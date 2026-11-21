"""Run: python -m unittest app.reconcile.test_reconcile -v"""

import unittest

from app.reconcile import reconcile_lines, reconcile_single_line


def row(billed="40", received="36", **changes):
    return {
        "billed_qty": billed, "received_qty": received, "unit_price": "20000",
        "unit": "kg", "units_comparable": True, **changes,
    }


class ReconcileTests(unittest.TestCase):
    def test_a1_missing_and_confirmed_zero_differ(self):
        missing = reconcile_single_line("40", None, "20000")
        self.assertEqual(missing["match_result"], "MISSING_DATA")
        for key in ("received_qty", "delta_qty", "check_amount"):
            self.assertIsNone(missing[key])
        zero = reconcile_single_line("40", "0", "20000")
        self.assertEqual((zero["received_qty"], zero["delta_qty"], zero["check_amount"]),
                         ("0", "40", 800000))

    def test_a2_difference_and_approval_fields(self):
        result = reconcile_lines([row()])
        self.assertEqual(result["summary"]["delta_qty"], "4")
        self.assertEqual(result["summary"]["check_amount"], 80000)
        self.assertEqual(result["match_result"], "MISMATCH")
        self.assertFalse(result["can_approve"])
        self.assertIsNone(result["approvable_amount"])
        self.assertIsNone(result["approved_amount"])

    def test_decimal_negative_difference_and_fractional_won(self):
        result = reconcile_single_line("1.25", "1.5", "20000")
        self.assertEqual((result["delta_qty"], result["check_amount"]), ("-0.25", -5000))
        self.assertIsNone(reconcile_single_line("1.001", "1", "1")["check_amount"])

    def test_missing_price_preserves_quantity_difference(self):
        result = reconcile_single_line("40", "36", None)
        self.assertEqual(result["delta_qty"], "4")
        self.assertIsNone(result["check_amount"])

    def test_partial_receipts_keep_confirmed_row_and_clear_summary(self):
        evidence = [{"document_id": "local-example", "source_location": {"row": 2}}]
        result = reconcile_lines([row(evidence_refs=evidence), row("60", None)])
        self.assertEqual(result["items"][0]["check_amount"], 80000)
        self.assertEqual(result["items"][0]["evidence_refs"], evidence)
        self.assertEqual(result["summary"]["billed_qty"], "100")
        for key in ("received_qty", "delta_qty", "check_amount"):
            self.assertIsNone(result["summary"][key])
        self.assertEqual(result["match_result"], "MISSING_DATA")
        self.assertEqual(result["confirmation_scope"], "RECEIPTS_INCLUDED")
        self.assertFalse(result["can_approve"])

    def test_unknown_conversion_does_not_erase_other_rows(self):
        result = reconcile_lines([row(), row("2", "1", unit="box", units_comparable=False)])
        self.assertEqual(result["items"][0]["delta_qty"], "4")
        self.assertEqual(result["items"][1]["received_qty"], "1")
        self.assertEqual(result["items"][1]["unit"], "box")
        self.assertIsNone(result["items"][1]["delta_qty"])
        self.assertIsNone(result["items"][1]["check_amount"])
        self.assertIsNone(result["summary"]["check_amount"])
        self.assertIsNone(result["summary"]["received_qty"])

    def test_batch_requires_explicit_unit_confirmation(self):
        line = row()
        del line["units_comparable"]
        self.assertIsNone(reconcile_lines([line])["items"][0]["delta_qty"])
        self.assertIsNone(reconcile_lines([row(unit=None)])["items"][0]["delta_qty"])

    def test_complete_rows_and_offsetting_differences(self):
        result = reconcile_lines([row("60", "60", billed_amount=1200000),
                                  row(billed_amount=800000)])
        self.assertEqual(result["summary"]["received_qty"], "96")
        self.assertEqual(result["summary"]["check_amount"], 80000)
        self.assertEqual(result["summary"]["billed_amount"], 2000000)
        offset = reconcile_lines([row("1", "2"), row("2", "1")])
        self.assertEqual(offset["summary"]["delta_qty"], "0")
        self.assertEqual(offset["match_result"], "MISMATCH")
        self.assertFalse(offset["can_approve"])

    def test_empty_and_matched_are_not_approval(self):
        self.assertEqual(reconcile_lines([])["match_result"], "MISSING_DATA")
        result = reconcile_lines([row("40", "40")])
        self.assertEqual(result["match_result"], "MATCHED")
        self.assertFalse(result["can_approve"])

    def test_invalid_numeric_values_are_rejected(self):
        for value in ("", "NaN", "Infinity", "-1", 1.25):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reconcile_single_line(value, "0", "20000")
        with self.assertRaises(ValueError):
            reconcile_lines([row(billed_amount=1.5)])


if __name__ == "__main__":
    unittest.main()
