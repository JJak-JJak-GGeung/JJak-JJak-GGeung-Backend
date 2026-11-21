"""Read structured fields from a single photographed Korean trade document."""

import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from threading import Lock

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
PERIOD = re.compile(r"(\d{4}-\d{2}-\d{2})[~～](\d{4}-\d{2}-\d{2}|\d{2}-\d{2})")
NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
OCR_LOCK = Lock()


@lru_cache(maxsize=1)
def _engine():
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    return RapidOCR(params={
        "Rec.lang_type": LangRec.KOREAN,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Global.use_cls": False,
        "Global.text_score": 0.4,
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
    })


def _rows(boxes, texts):
    tokens = []
    for box, value in zip(boxes, texts):
        left, top = box.min(axis=0)
        right, bottom = box.max(axis=0)
        tokens.append((float(left), float(top), float(right), float(bottom), value.strip()))
    rows = []
    for token in sorted(tokens, key=lambda t: (t[1] + t[3]) / 2):
        center = (token[1] + token[3]) / 2
        if rows:
            previous = rows[-1][0]
            distance = abs(center - (previous[1] + previous[3]) / 2)
            if distance < min(token[3] - token[1], previous[3] - previous[1]) * 0.7:
                rows[-1].append(token)
                continue
        rows.append([token])
    return [sorted(row, key=lambda t: t[0]) for row in rows]


def _compact(text):
    return re.sub(r"\s", "", text)


def _number(text):
    if text is not None and NUMBER.fullmatch(_compact(text)):
        return _compact(text).replace(",", "")
    return None


def _id(text):
    normalized = _compact(text or "").upper()
    return normalized if re.fullmatch(r"[A-Z]+(?:-[A-Z0-9]+)+", normalized) else None


def _date(text):
    match = DATE.search(_compact(text or ""))
    if match:
        try:
            return date.fromisoformat(match.group()).isoformat()
        except ValueError:
            pass
    return None


def _location(row, row_number=None):
    return {
        "page": 1,
        "row": row_number,
        "region": [
            round(min(t[0] for t in row)), round(min(t[1] for t in row)),
            round(max(t[2] for t in row)), round(max(t[3] for t in row)),
        ],
    }


def _header_key(text):
    return re.sub(r"[\s·ㆍ.()（）:_/\-]", "", text).lower()


def _headers(rows):
    labels = ("공급처", "받는점포", "점포", "기록기준", "납품일", "명세번호", "청구번호", "발행일", "정산기간", "합의지급일", "대체청구", "거래일")
    aliases = {"상호": "공급처", "상호법인명": "공급처", "법인명": "공급처",
               "공급업체": "공급처", "공급자상호": "공급처",
               "거래일자": "거래일", "거래명세번호": "명세번호", "작성일": "발행일"}
    values = {}
    for row in rows:
        active = None
        for *_, token in row:
            key = aliases.get(_header_key(token), _header_key(token))
            if key in labels:
                active = key
            elif active:
                values.setdefault(active, token)
                active = None
    return values


def _column(label):
    key = _header_key(label)
    if '수량' in key or '실수령' in key:
        return 'qty_read'
    if '단가' in key:
        return 'unit_price_read'
    if '금액' in key or key in ('공급가액', '공급액'):
        return 'amount_read'
    return {
        '납품일': 'delivery_date', '수령일': 'delivery_date', '명세번호': 'delivery_no',
        '대상명세': 'delivery_no', '품목': 'item', '품명': 'item',
        '품목규격': 'item', '품명규격': 'item', '품명및규격': 'item',
        '규격': 'spec', '단위': 'unit',
        '메모': 'memo',
    }.get(key)


def extract_document(raw_bytes: bytes, mime: str, document_id: str, context=None) -> dict:
    """Return OCR facts and evidence without confirming receipts or approving amounts.

    The caller assigns line IDs. Region coordinates are [left, top, right, bottom]
    in source-image pixels. OCR engine failures propagate to the upload handler.
    """
    if not raw_bytes or mime not in {'image/png', 'image/jpeg', 'image/webp'}:
        raise ValueError('A PNG, JPEG or WebP image is required')
    import cv2
    import numpy as np

    grayscale = cv2.imdecode(np.frombuffer(raw_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    if grayscale is None:
        raise ValueError('Image bytes could not be decoded')
    # Keep source pixel coordinates while removing background colors and scan noise.
    _, clean = cv2.threshold(grayscale, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    with OCR_LOCK:
        scan = _engine()(clean)
    rows = _rows(scan.boxes, scan.txts) if scan.boxes is not None and scan.txts else []
    return _parse_rows(rows, document_id)


def _parse_rows(rows, document_id):
    """Parse positioned OCR tokens without relying on one document template."""
    heading = None
    for i, row in enumerate(rows):
        fields = {_column(t[4]) for t in row}
        if 'qty_read' in fields and fields.intersection({'item', 'delivery_no', 'delivery_date'}):
            heading = i
            break

    top = rows[:heading] if heading is not None else rows
    labels = _headers(top)
    title = ' '.join(_compact(t[4]) for row in top[:4] for t in row)
    doc_type = None
    if '수령기록' in title:
        doc_type = 'RECEIPT'
    elif '청구서' in title:
        doc_type = 'CORRECTED_INVOICE' if '수정본' in title or labels.get('대체청구') else 'INVOICE'
    elif any(label in title for label in ('납품명세서', '거래명세표', '거래명세서')):
        doc_type = 'DELIVERY'
    delivery_date = _date(labels.get('납품일'))
    delivery_no = _id(labels.get('명세번호'))
    if doc_type == 'DELIVERY' and delivery_no and delivery_date is None:
        for row in top:
            values = [t[4] for t in row]
            if any(_id(value) == delivery_no for value in values):
                delivery_date = next((_date(value) for value in values if DATE.fullmatch(_compact(value))), None)
                break
    interval = PERIOD.search(_compact(labels.get('정산기간', '')))
    period_start = _date(interval[1]) if interval else None
    period_end = None
    if interval and period_start:
        period_end = _date(interval[2]) or _date(period_start[:5] + interval[2])

    lines = []
    if heading is not None:
        columns = [(t[0], _column(t[4]), t[4]) for t in rows[heading]]
        qty_title = next(t[2] for t in columns if t[1] == 'qty_read')
        unit_match = re.search(r'\(([^)]+)\)', qty_title)
        for row in rows[heading + 1:]:
            if len(row) > 1:
                cells = {}
                for token in row:
                    key = min(columns, key=lambda c: abs(c[0] - token[0]))[1]
                    if key is not None:
                        cells[key] = (cells.get(key, '') + ' ' + token[4]).strip()
                qty = _number(cells.get('qty_read'))
                row_id = _id(cells.get('delivery_no')) or next(
                    (_id(t[4]) for t in row if _id(t[4]) and not _date(t[4])), None,
                )
                if not row_id and not (qty is not None and (cells.get('item') or delivery_no)):
                    continue
                amount = _number(cells.get('amount_read'))
                amount_value = Decimal(amount) if amount is not None else None
                lines.append({
                    'delivery_no': row_id or delivery_no,
                    'delivery_date': _date(cells.get('delivery_date')) or delivery_date,
                    # OCR may insert or omit spaces in the same product name.
                    'item': _compact(cells.get('item', '')) or None, 'spec': cells.get('spec'),
                    'unit': cells.get('unit') or (unit_match[1] if unit_match else None),
                    'qty_read': qty, 'unit_price_read': _number(cells.get('unit_price_read')),
                    'amount_read': int(amount_value) if amount_value is not None and amount_value == int(amount_value) else None,
                    'source_location': _location(row, len(lines) + 1),
                })

    agreement = next((row for row in rows if '양측확인' in _compact(' '.join(t[4] for t in row))), None)
    return {
        'document_id': document_id, 'doc_type': doc_type,
        'supplier_name_read': labels.get('공급처'), 'doc_no': _id(labels.get('청구번호')) or delivery_no,
        'doc_date': _date(labels.get('발행일')) or _date(labels.get('거래일')) or delivery_date,
        'period_start': period_start, 'period_end': period_end,
        'payment_due_on': _date(labels.get('합의지급일')), 'replaces_doc_no': _id(labels.get('대체청구')),
        'agreement_text': ' '.join(t[4] for t in agreement) if agreement else None,
        'agreement_source_location': _location(agreement) if agreement else None,
        'lines': lines,
    }
