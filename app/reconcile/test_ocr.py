"""OCR schema examples, not outputs from executing the OCR engine."""
from copy import deepcopy
import unittest

from .ocr import reconcile_ocr_documents


SCOPE = {"store_id": "local-store", "supplier_id": "local-supplier", "period_id": "local-period"}


def ocr_documents():
    documents = []
    for kind, qty, price, amount, number in (
        ("DELIVERY", "40", "20000", 800000, "local-delivery-number"),
        ("INVOICE", "40", "20000", 800000, "local-invoice-number"),
        ("RECEIPT", "36", None, None, None),
    ):
        documents.append({
            **SCOPE, "document_id": f"local:{kind}", "doc_type": kind,
            "doc_no": number, "supplier_name_read": "local supplier",
            "doc_date": "2026-11-13", "period_start": "2026-11-09", "period_end": "2026-11-15",
            "lines": [{"line_id": f"local:{kind}:1", "transaction_id": "local:transaction",
                       "delivery_no": "local-delivery-number", "delivery_date": "2026-11-13",
                       "item": "손질 장어" if kind == "DELIVERY" else "손질장어", "spec": None,
                       "unit": "kg", "qty_read": qty, "unit_price_read": price, "amount_read": amount,
                       "source_location": {"page": 1, "row": 1, "region": [1, 2, 3, 4]}}],
        })
    return documents


def confirmation(**changes):
    return {**SCOPE, "source_line_id": "local:RECEIPT:1", "receipt_id": "local-confirmation",
            "confirmed_by": "local-owner", "confirmed_at": "2026-11-21T11:00:00+09:00", **changes}


def calculate(documents, confirmations=None):
    return reconcile_ocr_documents(documents, confirmations or [], **SCOPE)


class OcrAdapterTests(unittest.TestCase):
    def test_raw_ocr_to_unconfirmed_candidate_preserves_source(self):
        documents = ocr_documents()
        before = deepcopy(documents)
        result = calculate(documents)
        self.assertEqual(documents, before)
        item = result["items"][0]
        self.assertEqual((item["declared_qty"], item["billed_qty"], item["billed_amount"]), ("40", "40", 800000))
        self.assertEqual(item["transaction_id"], "local:transaction")
        self.assertEqual(len(item["evidence_refs"]), 2)
        link = result["receipt_links"][0]
        self.assertEqual(link["status"], "UNCONFIRMED")
        self.assertEqual(link["candidates"][0]["line_id"], "local:DELIVERY:1")
        self.assertEqual(link["receipt"]["ocr_line"]["qty_read"], "36")
        self.assertEqual(link["receipt"]["evidence_refs"][0]["source_location"]["region"], [1, 2, 3, 4])
        for row in (item, result["summary"]):
            for field in ("received_qty", "delta_qty", "check_amount"):
                self.assertIsNone(row[field])
        self.assertIsNone(link["target_delivery_line_id"])
        self.assertFalse(result["can_approve"])

    def test_separate_confirmation_enables_one_time_receipt_calculation(self):
        event = confirmation()
        result = calculate(ocr_documents(), [event, deepcopy(event)])
        item = result["items"][0]
        self.assertEqual((item["received_qty"], item["delta_qty"], item["check_amount"]), ("36", "4", 80000))
        self.assertEqual(len(item["evidence_refs"]), 3)
        self.assertEqual([link["status"] for link in result["receipt_links"]], ["LINKED", "DUPLICATE"])
        self.assertFalse(result["can_approve"])

    def test_ocr_cannot_self_confirm_or_override_received_quantity(self):
        documents = ocr_documents()
        documents[2].update(confirmed_by="injected", confirmed_at="injected")
        documents[2]["lines"][0].update(confirmed_by="injected", confirmed_at="injected",
                                       received_qty="36", target_delivery_line_id="local:DELIVERY:1")
        result = calculate(documents)
        self.assertEqual(result["receipt_links"][0]["status"], "UNCONFIRMED")
        self.assertIsNone(result["items"][0]["received_qty"])

    def test_scope_exclusion_does_not_expose_candidates(self):
        documents = ocr_documents()
        documents[2]["supplier_id"] = "other"
        result = calculate(documents, [confirmation(supplier_id="other")])
        self.assertEqual(result["excluded_document_ids"], ["local:RECEIPT"])
        self.assertEqual(result["receipt_links"], [])
        self.assertIsNone(result["summary"]["received_qty"])

    def test_unknown_confirmation_source_is_not_aggregated(self):
        result = calculate(ocr_documents(), [confirmation(source_line_id="not-in-documents")])
        self.assertIn("UNKNOWN_CONFIRMATION_SOURCE", result["blockers"])
        self.assertIsNone(result["items"][0]["received_qty"])

    def test_server_document_and_line_ids_are_not_fabricated(self):
        for field in ("document_id", "line_id"):
            documents = ocr_documents()
            if field == "document_id":
                documents[0].pop(field)
            else:
                documents[0]["lines"][0].pop(field)
            with self.assertRaises(ValueError):
                calculate(documents)

    def test_duplicate_documents_are_idempotent_and_conflicts_rejected(self):
        documents = ocr_documents()
        documents.append(deepcopy(documents[0]))
        self.assertEqual(len(calculate(documents)["items"]), 1)
        documents[-1]["lines"][0]["qty_read"] = "999"
        with self.assertRaises(ValueError):
            calculate(documents)

    def test_ambiguous_invoice_link_does_not_choose_or_sum(self):
        documents = ocr_documents()
        extra = deepcopy(documents[0]["lines"][0])
        extra["line_id"] = "local:DELIVERY:2"
        documents[0]["lines"].append(extra)
        result = calculate(documents)
        self.assertEqual(len(result["document_links"][0]["candidates"]), 2)
        self.assertEqual(result["document_links"][0]["status"], "HELD")
        self.assertTrue(all(row["billed_qty"] is None for row in result["items"]))

    def test_missing_values_and_decimal_values_are_preserved(self):
        documents = ocr_documents()
        documents[1]["lines"][0].update(qty_read="40.25", amount_read=None, unit_price_read=None)
        documents[2]["lines"][0]["qty_read"] = "36.125"
        result = calculate(documents, [confirmation()])
        self.assertEqual(result["items"][0]["delta_qty"], "4.125")
        self.assertIsNone(result["items"][0]["check_amount"])
        self.assertIsNone(result["summary"]["billed_amount"])

    def test_missing_number_requires_complete_fallback_and_retains_candidates(self):
        documents = ocr_documents()
        for doc in documents:
            doc["lines"][0]["delivery_no"] = None
        result = calculate(documents)
        self.assertEqual(result["document_links"][0]["reason"], "INCOMPLETE_MATCH_FIELDS")
        for doc in documents:
            doc["lines"][0]["spec"] = "trimmed"
        result = calculate(documents, [confirmation()])
        self.assertEqual(result["items"][0]["check_amount"], 80000)

    def test_uncertain_units_and_corrected_invoices_are_not_automatically_applied(self):
        documents = ocr_documents()
        documents[1]["lines"][0]["unit"] = "box"
        self.assertIn("UNCONFIRMED_INVOICE_UNIT", calculate(documents)["blockers"])
        documents = ocr_documents()
        correction = deepcopy(documents[1])
        correction.update(document_id="local:correction", doc_type="CORRECTED_INVOICE", doc_no="new-number")
        correction["lines"][0]["line_id"] = "local:correction:1"
        correction["lines"][0]["qty_read"] = "36"
        documents.append(correction)
        result = calculate(documents, [confirmation()])
        self.assertEqual(result["items"][0]["billed_qty"], "40")
        self.assertIn("CORRECTION_REQUIRES_SETTLEMENT", result["blockers"])

    def test_empty_source_is_not_silently_ignored_in_weekly_summary(self):
        documents = ocr_documents()
        documents.append({**SCOPE, "document_id": "empty-delivery", "doc_type": "DELIVERY", "lines": []})
        result = calculate(documents, [confirmation()])
        self.assertIn("EMPTY_OCR_DOCUMENT", result["blockers"])
        self.assertEqual(result["items"][0]["received_qty"], "36")
        self.assertIsNone(result["summary"]["received_qty"])

    def test_two_files_with_same_invoice_number_are_not_summed(self):
        documents = ocr_documents()
        duplicate = deepcopy(documents[1])
        duplicate["document_id"] = "another-invoice-file"
        duplicate["lines"][0]["line_id"] = "another-invoice-line"
        documents.append(duplicate)
        result = calculate(documents, [confirmation()])
        self.assertIn("AMBIGUOUS_INVOICE_NUMBER", result["blockers"])
        self.assertIsNone(result["items"][0]["billed_amount"])


if __name__ == "__main__":
    unittest.main()
