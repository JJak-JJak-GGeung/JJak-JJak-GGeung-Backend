import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.photo.extractor import read_ocr
from app.photo.extractor import _column, _parse_rows


def row(*cells):
    return [(x, 0, x + 70, 20, text) for x, text in cells]


class LayoutTests(unittest.TestCase):
    def test_trade_statement_table(self):
        result = _parse_rows([
            row((0, "거 래 명 세 표")),
            row((0, "거래일"), (150, "2024-06-12(금)")),
            row((500, "상 호"), (600, "검증 공급사")),
            row((0, "No."), (200, "품 목 · 규 격"), (400, "단 위"),
                (500, "수 량"), (600, "단 가"), (750, "공 급 가 액"), (950, "세 액")),
            row((0, "1"), (200, "검증 품목 가"), (400, "kg"),
                (500, "14.0"), (600, "2,000"), (750, "28,000")),
            row((0, "2"), (200, "검증 품목 나"), (400, "kg"),
                (500, "3.0"), (600, "3,000"), (750, "9,000")),
            row((400, "합계"), (500, "17.0"), (750, "37,000")),
        ], "document")
        self.assertEqual(result["doc_type"], "DELIVERY")
        self.assertEqual(result["doc_date"], "2024-06-12")
        self.assertEqual(result["supplier_name_read"], "검증 공급사")
        self.assertEqual(len(result["lines"]), 2)
        self.assertEqual([r["qty_read"] for r in result["lines"]], ["14.0", "3.0"])
        self.assertEqual([r["amount_read"] for r in result["lines"]], [28000, 9000])
        self.assertIsNone(result["period_start"])
        self.assertIsNone(result["lines"][0]["delivery_no"])

    def test_original_public_layout(self):
        result = _parse_rows([
            row((0, "납품 명세서")),
            row((0, "명세 번호"), (150, "DL-X01"), (300, "납품일"), (450, "2026-11-13")),
            row((0, "품목"), (200, "수량(kg)"), (400, "단가(원/kg)"), (600, "금액(원)")),
            row((0, "검증 품목"), (200, "40"), (400, "20,000"), (600, "800,000")),
        ], "document")
        self.assertEqual(result["doc_type"], "DELIVERY")
        self.assertEqual(result["lines"][0]["qty_read"], "40")
        self.assertEqual(result["lines"][0]["unit"], "kg")
        self.assertEqual(result["lines"][0]["amount_read"], 800000)

    def test_footer_and_blank_marker_do_not_create_items(self):
        result = _parse_rows([
            row((0, "거래명세표")),
            row((200, "품목규격"), (400, "단위"), (500, "수량"), (600, "단가"), (750, "공급가액")),
            row((200, "검증품목"), (400, "kg"), (500, "2"), (600, "100"), (750, "200")),
            row((200, "안내문"), (400, "합계"), (500, "2"), (750, "200")),
            row((200, "*** 이 하 여 백 ***")),
            row((200, "하단 안내"), (500, "999"), (750, "999")),
        ], "document")
        self.assertEqual(len(result["lines"]), 1)
        self.assertEqual(result["lines"][0]["amount_read"], 200)

    def test_grayscale_fallback_when_threshold_erases_header(self):
        cv = SimpleNamespace(
            IMREAD_GRAYSCALE=0, THRESH_BINARY=1, THRESH_OTSU=2,
            imdecode=lambda *args: "gray", threshold=lambda *args: (0, "threshold"))
        np = SimpleNamespace(uint8=int, frombuffer=lambda *args: "buffer")
        scanned = [
            row((0, "거래명세표")),
            row((0, "품목"), (200, "수량(kg)"), (400, "단가"), (600, "공급가액")),
            row((0, "검증품목"), (200, "2"), (400, "100"), (600, "200")),
        ]
        with patch.dict("sys.modules", {"cv2": cv, "numpy": np}):
            with patch("app.photo.extractor._scan_rows", side_effect=[[], scanned]) as scan:
                result = read_ocr(b"test bytes", "image/png", "document")
        self.assertEqual([call.args[0] for call in scan.call_args_list], ["threshold", "gray"])
        self.assertEqual(len(result["lines"]), 1)

    def test_combined_item_header_variants(self):
        for title in ("품목·규격", "품목 - 규격", "품명 및 규격"):
            self.assertEqual(_column(title), "item")
        self.assertEqual(_column("공급가액"), "amount_read")


if __name__ == "__main__":
    unittest.main()
