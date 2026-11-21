"""C1-C2/D1-D2 local contract tests; API/database integration is not exercised."""

from copy import deepcopy
import unittest

from .settlement import ApprovalBlocked, reconcile_period, validate_approval
from .test_weekly import SCOPE, public_a_inputs


def period_input(corrected=False):
    lines, receipts = public_a_inputs()
    deliveries = [{**row, "evidence_refs": [row["evidence_refs"][0]]} for row in lines]
    refs = [{"document_id": "local:A04", "line_id": None, "kind": "INVOICE",
             "source_location": {"page": 1, "text": "invoice header"}}]
    original = {
        **SCOPE, "document_id": "local:A04", "doc_no": "INV-A01", "doc_type": "INVOICE",
        "doc_date": "2026-11-16", "period_start": "2026-11-09", "period_end": "2026-11-15",
        "evidence_refs": refs,
        "lines": [{"target_delivery_line_id": row["line_id"], "billed_qty": row["billed_qty"],
                   "unit_price": row["unit_price"], "unit": row["unit"],
                   "billed_amount": row["billed_amount"]} for row in lines],
    }
    inputs = {**SCOPE, "period_start": "2026-11-09", "period_end": "2026-11-15",
              "input_revision": 8, "deliveries": deliveries, "receipts": receipts,
              "invoices": [original], "memos": []}
    if corrected:
        correction = deepcopy(original)
        correction.update(document_id="local:A05", doc_no="INV-A01-R1", doc_type="CORRECTED_INVOICE",
                          doc_date="2026-11-17", replaces_doc_no="INV-A01")
        correction["evidence_refs"][0]["document_id"] = "local:A05"
        confirmation = {"confirmed_by": "local-owner", "confirmed_at": "2026-11-21T11:00:00+09:00",
                        "evidence_refs": deepcopy(correction["evidence_refs"])}
        correction["replacement"] = {**confirmation, "target_document_id": "local:A04"}
        correction["agreement"] = {**confirmation, "supplier_confirmed": True, "owner_confirmed": True}
        correction["lines"][1].update(billed_qty="36", billed_amount=720000)
        inputs["invoices"].append(correction)
    return inputs


def memo(**changes):
    return {**SCOPE, "memo_id": "local-memo", "transaction_id": "local-transaction-2",
            "confirmed_text": "거래처는 40kg을 보냈다고 합니다. 점포 기록은 36kg입니다.",
            "source_mode": "TYPED_TEXT", "supplier_claim_qty": "40", "unit": "kg",
            "confirmed_by": "local-owner", "confirmed_at": "2026-11-21T10:30:00+09:00", **changes}


class SettlementTests(unittest.TestCase):
    def test_confirmed_manual_receipt_supports_approval_and_keeps_evidence(self):
        inputs = period_input(True)
        receipt = inputs["receipts"][1]
        receipt["source_line_id"] = None
        receipt["evidence_refs"] = [{
            "kind": "RECEIPT", "document_id": None, "line_id": None,
            "receipt_id": receipt["receipt_id"], "source_location": None,
            "evidence_text": "점주 재계량 기록",
        }]
        result = reconcile_period(**inputs)
        self.assertEqual(result["summary"]["received_qty"], "96")
        self.assertEqual(result["approvable_amount"], 1920000)
        self.assertTrue(result["can_approve"])
        self.assertIn(receipt["evidence_refs"][0], result["items"][1]["evidence_refs"])
        receipt["evidence_refs"][0]["evidence_text"] = "  "
        result = reconcile_period(**inputs)
        self.assertIn("MISSING_SOURCE_EVIDENCE", result["blockers"])
        self.assertFalse(result["can_approve"])

    def test_c1_claim_preserves_receipt_and_invoice_and_is_idempotent(self):
        inputs = period_input()
        inputs["memos"] = [memo(), memo()]
        before = deepcopy(inputs)
        result = reconcile_period(**inputs)
        self.assertEqual(inputs, before)
        self.assertEqual(result["summary"]["billed_amount"], 2000000)
        self.assertEqual(result["items"][1]["received_qty"], "36")
        self.assertEqual(result["fact_status"], "CONFLICTING")
        self.assertEqual(result["work_status"], "WAITING_REPLY")
        self.assertEqual(len(result["memos"]), 1)
        self.assertEqual(result["items"][1]["memos"][0]["supplier_claim_qty"], "40")
        self.assertFalse(result["can_approve"])

    def test_memo_draft_and_other_scope_do_not_change_result(self):
        inputs = period_input()
        before = reconcile_period(**inputs)
        inputs["memos"] = [memo(confirmed_at=None), memo(store_id="other")]
        self.assertEqual(reconcile_period(**inputs), before)

    def test_c2_valid_replacement_uses_period_not_issue_date_and_preserves_history(self):
        inputs = period_input(True)
        inputs["memos"] = [memo()]
        result = reconcile_period(**inputs)
        self.assertEqual(result["summary"]["declared_qty"], "100")
        self.assertEqual(result["summary"]["billed_qty"], "96")
        self.assertEqual(result["summary"]["received_qty"], "96")
        self.assertEqual(result["summary"]["check_amount"], 0)
        self.assertEqual(result["approvable_amount"], 1920000)
        self.assertTrue(result["can_approve"])
        self.assertIsNone(result["approved_amount"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["invoice_history"][0]["summary"]["check_amount"], 80000)
        self.assertEqual(result["invoice_history"][0]["summary"]["billed_amount"], 2000000)
        self.assertEqual(result["active_invoice_ids"], ["local:A05"])

    def test_replacement_target_or_confirmation_missing_keeps_original(self):
        for changes in ({"replacement": None}, {"replaces_doc_no": "unknown", "replacement": {}},
                        {"replaces_doc_no": "wrong"}):
            inputs = period_input(True)
            inputs["invoices"][1].update(changes)
            result = reconcile_period(**inputs)
            self.assertEqual(result["summary"]["billed_amount"], 2000000)
            self.assertFalse(result["can_approve"])
            self.assertTrue(result["pending_invoices"])

    def test_missing_bilateral_agreement_does_not_approve_matching_numbers(self):
        for key in ("owner_confirmed", "supplier_confirmed", "evidence_refs"):
            inputs = period_input(True)
            inputs["invoices"][1]["agreement"][key] = None
            result = reconcile_period(**inputs)
            self.assertEqual(result["summary"]["delta_qty"], "0")
            self.assertIn("UNCONFIRMED_QUANTITY_AGREEMENT", result["blockers"])
            self.assertFalse(result["can_approve"])

    def test_wrong_scope_or_period_correction_never_adds_to_original(self):
        for changes in ({"supplier_id": "other"}, {"store_id": "other"},
                        {"period_id": "other"}, {"period_start": "2026-11-16"}):
            inputs = period_input(True)
            inputs["invoices"][1].update(changes)
            result = reconcile_period(**inputs)
            self.assertEqual(result["summary"]["billed_amount"], 2000000)
            self.assertFalse(result["can_approve"])

    def test_duplicate_document_retry_and_ambiguous_candidates(self):
        inputs = period_input(True)
        inputs["invoices"].append(deepcopy(inputs["invoices"][1]))
        self.assertEqual(reconcile_period(**inputs)["approvable_amount"], 1920000)
        inputs["invoices"][-1]["document_id"] = "other-file-same-number"
        result = reconcile_period(**inputs)
        self.assertIn("AMBIGUOUS_INVOICE_NUMBER", result["blockers"])
        self.assertEqual(result["summary"]["billed_amount"], 2000000)

    def test_multiple_replacements_hold_and_do_not_sum(self):
        inputs = period_input(True)
        other = deepcopy(inputs["invoices"][1])
        other.update(document_id="other", doc_no="other-number")
        inputs["invoices"].append(other)
        result = reconcile_period(**inputs)
        self.assertIn("AMBIGUOUS_REPLACEMENT", result["blockers"])
        self.assertEqual(result["summary"]["billed_amount"], 2000000)

    def test_d1_recalculates_and_blocks_any_unresolved_line(self):
        inputs = period_input(True)
        result = validate_approval(expected_revision=8, **inputs)
        self.assertEqual(result["approvable_amount"], 1920000)
        inputs["receipts"][0]["received_qty"] = "59"
        with self.assertRaises(ApprovalBlocked) as caught:
            validate_approval(expected_revision=8, **inputs)
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("BLOCKED", 422))

    def test_stale_revision_rejected_before_approval(self):
        with self.assertRaises(ApprovalBlocked) as caught:
            validate_approval(expected_revision=7, **period_input(True))
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("STALE_REVISION", 409))

    def test_missing_receipt_price_evidence_or_issue_blocks_whole_period(self):
        for field in ("receipt", "price", "evidence", "issue"):
            inputs = period_input(True)
            if field == "receipt":
                inputs["receipts"].pop()
            elif field == "price":
                inputs["invoices"][1]["lines"][1]["unit_price"] = None
            elif field == "evidence":
                inputs["receipts"][1]["evidence_refs"] = []
            else:
                inputs["deliveries"][0]["blockers"] = ["UNRESOLVED_QUALITY"]
            result = reconcile_period(**inputs)
            self.assertFalse(result["can_approve"])
            self.assertIsNone(result["approvable_amount"])

    def test_approved_snapshot_payment_and_new_revision(self):
        inputs = period_input(True)
        inputs["approvals"] = [{**SCOPE, "approval_id": "saved-approval", "input_revision": 8,
                                "approved_amount": 1920000}]
        result = reconcile_period(**inputs)
        self.assertEqual(result["work_status"], "APPROVED")
        self.assertEqual(result["approved_amount"], 1920000)
        self.assertEqual(result["payment_status"], "UNCONFIRMED")
        self.assertFalse(result["can_approve"])
        inputs["input_revision"] = 9
        changed = reconcile_period(**inputs)
        self.assertIsNone(changed["approved_amount"])
        self.assertEqual(changed["approval_history"][0]["approved_amount"], 1920000)

    def test_snapshot_conflict_and_memo_id_conflict_cannot_approve(self):
        inputs = period_input(True)
        inputs["approvals"] = [{**SCOPE, "approval_id": "saved", "input_revision": 8, "approved_amount": 1}]
        self.assertFalse(reconcile_period(**inputs)["can_approve"])
        inputs.pop("approvals")
        inputs["memos"] = [memo(), memo(confirmed_text="different")]
        self.assertIn("MEMO_ID_CONFLICT", reconcile_period(**inputs)["blockers"])

    def test_d2_receipt_change_preserves_other_transaction_and_originals(self):
        inputs = period_input(True)
        before = reconcile_period(**inputs)
        inputs["receipts"][1]["received_qty"] = "35"
        after = reconcile_period(**inputs)
        self.assertEqual(before["items"][0], after["items"][0])
        self.assertEqual(after["items"][1]["check_amount"], 20000)
        self.assertEqual(len(after["items"]), 2)
        self.assertEqual(after["items"][1]["transaction_id"], "local-transaction-2")

    def test_missing_invoice_or_unagreed_changed_quantity_never_approves(self):
        inputs = period_input()
        inputs["invoices"] = []
        self.assertFalse(reconcile_period(**inputs)["can_approve"])
        inputs = period_input()
        inputs["invoices"][0]["lines"][1].update(billed_qty="36", billed_amount=720000)
        self.assertIn("UNRESOLVED_DECLARED_QUANTITY", reconcile_period(**inputs)["blockers"])

    def test_different_numbers_with_same_date_and_amount_are_retained(self):
        inputs = period_input()
        inputs["deliveries"][0].update(declared_qty="40", delivery_date="2026-11-13")
        inputs["invoices"][0]["lines"][0].update(billed_qty="40", billed_amount=800000)
        inputs["receipts"][0]["received_qty"] = "40"
        result = reconcile_period(**inputs)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual({item["delivery_no"] for item in result["items"]}, {"DL-A091", "DL-A131"})
        self.assertEqual(result["summary"]["billed_amount"], 1600000)

    def test_negative_invoice_amount_and_document_issue_block_approval(self):
        inputs = period_input(True)
        inputs["invoices"][1]["lines"][1]["billed_amount"] = -720000
        self.assertIn("INVALID_BILLED_AMOUNT", reconcile_period(**inputs)["blockers"])
        inputs = period_input(True)
        inputs["invoices"][1]["blockers"] = ["UNRESOLVED_PRICE_AGREEMENT"]
        self.assertFalse(reconcile_period(**inputs)["can_approve"])


if __name__ == "__main__":
    unittest.main()
