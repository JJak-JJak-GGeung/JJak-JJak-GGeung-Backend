import unittest
from app.photo.extractor import _column, _parse_rows


def row(*cells):
    return [(x, 0, x + 70, 20, text) for x, text in cells]


class LayoutTests(unittest.TestCase):
    def test_trade_statement_table(self):
        result = _parse_rows([
            row((0, "거 래 명 세 표")),
            row((0, "거래일"), (150, "2025-01-31(금)")),
            row((500, "상 호"), (600, "검증 공급사")),
            row((0, "No."), (200, "품 목 · 규 격"), (400, "단 위"),
                (500, "수 량"), (600, "단 가"), (750, "공 급 가 액"), (950, "세 액")),
            row((0, "1"), (200, "검증 품목 가"), (400, "kg"),
                (500, "140.0"), (600, "38,000"), (750, "5,320,000")),
            row((0, "2"), (200, "검증 품목 나"), (400, "kg"),
                (500, "10.0"), (600, "39,000"), (750, "390,000")),
            row((400, "합계"), (500, "150.0"), (750, "5,710,000")),
        ], "document")
        self.assertEqual(result["doc_type"], "DELIVERY")
        self.assertEqual(result["doc_date"], "2025-01-31")
        self.assertEqual(result["supplier_name_read"], "검증 공급사")
        self.assertEqual(len(result["lines"]), 2)
        self.assertEqual([r["qty_read"] for r in result["lines"]], ["140.0", "10.0"])
        self.assertEqual([r["amount_read"] for r in result["lines"]], [5320000, 390000])
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

    def test_combined_item_header_variants(self):
        for title in ("품목·규격", "품목 - 규격", "품명 및 규격"):
            self.assertEqual(_column(title), "item")
        self.assertEqual(_column("공급가액"), "amount_read")


if __name__ == "__main__":
    unittest.main()
