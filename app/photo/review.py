"""Bind an editable reading to original bytes; accounting still requires server confirmation."""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import hmac
import json
import os
import time

from .extractor import ExtractionError, LINE_FIELDS, TEXT_FIELDS, validate_fields


def review_warnings(draft):
    warnings = []
    if draft["doc_type"] == "UNKNOWN":
        warnings.append("문서 종류를 확인하세요.")
    for index, row in enumerate(draft["lines"], 1):
        if row["qty_read"] is None or row["unit"] is None:
            warnings.append(f"{index}행: 수량 또는 단위가 비어 있습니다.")
        if all(row[key] is not None for key in ("qty_read", "unit_price_read", "amount_read")):
            with localcontext() as context:
                context.prec = 64
                if Decimal(row["qty_read"]) * Decimal(row["unit_price_read"]) != row["amount_read"]:
                    warnings.append(f"{index}행: 수량×단가와 기재 금액이 다릅니다. 원본을 확인하세요.")
    return warnings


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sign(digest, draft, issued_at):
    key = os.getenv("PHOTO_REVIEW_SIGNING_KEY") or os.environ["SUPABASE_SECRET_KEY"]
    body = f"photo-review-v2:{digest}:{issued_at}:{fingerprint(draft)}"
    return hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


def verify(raw, draft, issued_at, signature):
    now = int(time.time())
    if type(issued_at) is not int or not 0 <= now - issued_at <= 3600:
        raise ExtractionError("PREVIEW_EXPIRED")
    digest = hashlib.sha256(raw).hexdigest()
    if not isinstance(signature, str) or not signature.isascii() or not hmac.compare_digest(
            sign(digest, draft, issued_at), signature):
        raise ExtractionError("PREVIEW_CHANGED")
    return digest


def prepare_review(draft, review):
    """Keep source lineage for edited rows, and allow explicit insertions and deletions."""
    if not isinstance(review, dict) or set(review) != {"doc_type", *TEXT_FIELDS, "lines"}:
        raise ExtractionError("INVALID_REVIEW")
    if not isinstance(review["lines"], list) or not 1 <= len(review["lines"]) <= 300:
        raise ExtractionError("INVALID_REVIEW_LINES")
    facts = deepcopy(review)
    used = set()
    edits = []
    for row in facts["lines"]:
        if not isinstance(row, dict) or set(row) != {*LINE_FIELDS, "source_row"}:
            raise ExtractionError("INVALID_REVIEW_LINE")
        source = row.pop("source_row")
        if source is not None:
            if type(source) is not int or not 1 <= source <= len(draft["lines"]) or source in used:
                raise ExtractionError("INVALID_SOURCE_ROW")
            used.add(source)
        if not isinstance(row["item"], str) or not row["item"].strip():
            raise ExtractionError("ITEM_REQUIRED")
        edits.append({**row, "source_row": source})
    validate_fields(facts)
    if facts["doc_type"] == "UNKNOWN":
        raise ExtractionError("DOCUMENT_TYPE_REQUIRED")
    return {key: facts[key] for key in ("doc_type", *TEXT_FIELDS)}, edits
