"""B2: public synthetic A values, transcribed manually; not real confirmations."""

from copy import deepcopy
import unittest

from .matching import reconcile_receipts


SCOPE = {"store_id": "local-store", "supplier_id": "local-supplier", "period_id": "local-period"}


def public_a_inputs():
    """A01/02 delivery quantities, A03 receipts, A04 written invoice amounts.

    All IDs/confirmation metadata below are local test fixtures, not server IDs.
    """
    lines, receipts = [], []
    for index, number, date, billed, received, amount in (
        (1, "DL-A091", "2026-11-09", "60", "60", 1200000),
        (2, "DL-A131", "2026-11-13", "40", "36", 800000),
    ):
        source = f"A0{index}"
        lines.append({
            **SCOPE, "line_id": f"local-delivery-{index}",
            "transaction_id": f"local-transaction-{index}",
            "delivery_no": number, "delivery_date": date, "item": "손질 장어", "spec": None,
            "declared_qty": billed, "billed_qty": billed, "unit": "kg",
            "unit_price": "20000", "billed_amount": amount,
            "evidence_refs": [
                {"document_id": f"local:{source}", "line_id": f"local:{source}:1",
                 "kind": "DELIVERY", "source_location": {"page": 1, "row": 1}},
                {"document_id": "local:A04", "line_id": f"local:A04:{index}",
                 "kind": "INVOICE", "source_location": {"page": 1, "row": index}},
            ],
        })
        receipts.append({
            **SCOPE, "receipt_id": f"local-receipt-{index}",
            "source_line_id": f"local:A03:{index}", "delivery_no": number,
            "received_qty": received, "unit": "kg", "confirmed_by": "local-test-owner",
            "confirmed_at": "2026-11-21T10:00:00+09:00",
            "evidence_refs": [{"document_id": "local:A03", "line_id": f"local:A03:{index}",
                               "kind": "RECEIPT", "source_location": {"page": 1, "row": index}}],
        })
    return lines, receipts


def calculate(lines, receipts):
    return reconcile_receipts(lines, receipts, **SCOPE)


class WeeklyTests(unittest.TestCase):
    def test_public_a_weekly_and_individual_results(self):
        result = calculate(*public_a_inputs())
        self.assertEqual(result["summary"], {
            "declared_qty": "100", "billed_qty": "100", "received_qty": "96", "unit": "kg",
            "delta_qty": "4", "check_amount": 80000, "billed_amount": 2000000,
        })
        first, second = result["items"]
        self.assertEqual((first["billed_qty"], first["received_qty"], first["delta_qty"], first["check_amount"]),
                         ("60", "60", "0", 0))
        self.assertEqual((second["billed_qty"], second["received_qty"], second["delta_qty"], second["check_amount"]),
                         ("40", "36", "4", 80000))
        self.assertEqual(len(second["evidence_refs"]), 3)
        self.assertFalse(result["can_approve"])
        self.assertIsNone(result["approvable_amount"])
        self.assertIsNone(result["approved_amount"])

    def test_negative_decimal_difference_preserves_direction(self):
        lines, receipts = public_a_inputs()
        receipts[1]["received_qty"] = "41.125"
        result = calculate(lines, receipts)
        for values in (result["summary"], result["items"][1]):
            self.assertEqual(values["delta_qty"], "-1.125")
            self.assertEqual(values["check_amount"], -22500)
        self.assertEqual(result["summary"]["received_qty"], "101.125")
        self.assertEqual(result["match_result"], "MISMATCH")

    def test_written_invoice_amount_is_not_recomputed(self):
        lines, receipts = public_a_inputs()
        lines[1]["billed_amount"] = 799999
        result = calculate(lines, receipts)
        self.assertEqual(result["items"][1]["billed_amount"], 799999)
        self.assertEqual(result["summary"]["billed_amount"], 1999999)
        self.assertEqual(result["summary"]["check_amount"], 80000)

    def test_missing_written_amount_stays_unknown(self):
        lines, receipts = public_a_inputs()
        lines[1]["billed_amount"] = None
        result = calculate(lines, receipts)
        self.assertIsNone(result["summary"]["billed_amount"])
        self.assertIsNone(result["items"][1]["billed_amount"])
        self.assertEqual(result["summary"]["check_amount"], 80000)

    def test_missing_price_clears_only_amount_not_quantity(self):
        lines, receipts = public_a_inputs()
        lines[1]["unit_price"] = None
        result = calculate(lines, receipts)
        self.assertEqual(result["summary"]["delta_qty"], "4")
        self.assertIsNone(result["summary"]["check_amount"])
        self.assertIsNone(result["items"][1]["check_amount"])
        self.assertEqual(result["items"][0]["check_amount"], 0)
        self.assertEqual(result["summary"]["billed_amount"], 2000000)

    def test_ids_and_file_names_do_not_control_calculation(self):
        lines, receipts = public_a_inputs()
        expected = calculate(lines, receipts)
        for index, (line, receipt) in enumerate(zip(lines, receipts)):
            line["delivery_no"] = receipt["delivery_no"] = f"unrelated-number-{index}"
            line["line_id"] = f"unrelated-line-{index}"
            line["transaction_id"] = f"unrelated-transaction-{index}"
            line["source_file"] = "unrelated-file.png"
            for value in (line, receipt):
                for ref in value["evidence_refs"]:
                    ref["document_id"] = "unrelated-document"
        actual = calculate(lines, receipts)
        self.assertEqual(actual["summary"], expected["summary"])
        self.assertEqual([item["transaction_id"] for item in actual["items"]],
                         ["unrelated-transaction-0", "unrelated-transaction-1"])

    def test_changing_one_receipt_keeps_other_delivery_unchanged(self):
        lines, receipts = public_a_inputs()
        before = calculate(lines, receipts)
        changed = deepcopy(receipts)
        changed[1]["received_qty"] = "37"
        after = calculate(lines, changed)
        self.assertEqual(before["items"][0], after["items"][0])
        self.assertEqual(after["items"][1]["delta_qty"], "3")
        self.assertEqual(after["summary"]["check_amount"], 60000)
        self.assertEqual(len(after["items"]), 2)

    def test_partial_week_does_not_erase_confirmed_delivery(self):
        lines, receipts = public_a_inputs()
        result = calculate(lines, receipts[1:])
        for field in ("received_qty", "delta_qty", "check_amount"):
            self.assertIsNone(result["summary"][field])
        self.assertEqual(result["items"][1]["check_amount"], 80000)
        self.assertEqual(result["summary"]["billed_amount"], 2000000)
        self.assertFalse(result["can_approve"])


if __name__ == "__main__":
    unittest.main()
