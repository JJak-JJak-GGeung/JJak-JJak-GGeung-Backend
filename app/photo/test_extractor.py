"""Provider responses are mocked; these tests do not claim image reading accuracy."""
from copy import deepcopy
from io import BytesIO
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from .extractor import ExtractionError, INSTRUCTIONS, extract_document, validate_fields
from .review import prepare_review, review_warnings, sign, verify
from app.reconcile.documents import reconcile_documents


def image_bytes():
    stream = BytesIO()
    Image.new("RGB", (60, 40), "white").save(stream, "PNG")
    return stream.getvalue()


def facts():
    return {"store_name_read": None, "trade_date": None, "issued_on": None, "quoted_on": None, "source_date_text": None,
            "doc_type": "DELIVERY", "supplier_name_read": "달마루 수산", "doc_no": "DL-A131",
            "doc_date": "2026-11-13", "period_start": None, "period_end": None,
            "payment_due_on": None, "replaces_doc_no": None, "agreement_text": None,
            "lines": [{"delivery_no": "DL-A131", "delivery_date": "2026-11-13", "item": "손질 장어",
                       "spec": None, "unit": "kg", "qty_read": "40", "unit_price_read": "20000",
                       "amount_read": 800000, "tax_amount_read": None, "line_date_text": None}]}


def provider(value=None, **changes):
    response = SimpleNamespace(status="completed", output=[], output_text=json.dumps(value or facts()),
                               id="response-test", usage=SimpleNamespace(input_tokens=20, output_tokens=10))
    for key, val in changes.items():
        setattr(response, key, val)
    return SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))


class ExtractorTests(unittest.TestCase):
    def test_image_only_structured_call_and_legacy_contract(self):
        client = provider()
        result = extract_document(image_bytes(), "image/png", "server-document", client=client)
        self.assertEqual(result["document_id"], "server-document")
        self.assertEqual(result["lines"][0]["qty_read"], "40")
        self.assertEqual(result["lines"][0]["source_location"], {"page": 1, "row": 1, "region": None})
        call = client.responses.create.call_args.kwargs
        self.assertFalse(call["store"])
        self.assertTrue(call["text"]["format"]["strict"])
        self.assertEqual(call["instructions"], INSTRUCTIONS)
        self.assertTrue(call["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("hints", call)
        self.assertEqual(result["analysis"]["input_tokens"], 20)

    def test_configured_model(self):
        with patch.dict(os.environ, {"OPENAI_DOCUMENT_MODEL": "configured-vision-model"}):
            client = provider()
            extract_document(image_bytes(), "image/png", None, client=client)
            self.assertEqual(client.responses.create.call_args.kwargs["model"], "configured-vision-model")

    def test_invalid_images_do_not_call_provider(self):
        client = provider()
        for data, mime in [(b"", "image/png"), (b"not an image", "image/png"),
                           (image_bytes(), "image/jpeg"), (image_bytes(), "text/plain")]:
            with self.subTest(mime=mime), self.assertRaises(ValueError):
                extract_document(data, mime, None, client=client)
        client.responses.create.assert_not_called()

    def test_refusal_incomplete_and_bad_output_fail_closed(self):
        for changes in [{"status": "incomplete"}, {"output_text": "not json"},
                        {"output": [SimpleNamespace(content=[SimpleNamespace(type="refusal")])]},
                        {"output_text": json.dumps({**facts(), "confirmed_by": "injected"})}]:
            with self.subTest(changes=changes), self.assertRaises(ExtractionError):
                extract_document(image_bytes(), "image/png", None, client=provider(**changes))

    def test_transport_error_does_not_leak_provider_message(self):
        client = provider()
        client.responses.create.side_effect = RuntimeError("secret provider body")
        with self.assertRaisesRegex(ExtractionError, "^DOCUMENT_READING_FAILED$"):
            extract_document(image_bytes(), "image/png", None, client=client)

    def test_preserves_missing_zero_decimals_and_written_amount(self):
        value = facts()
        value["lines"][0].update(qty_read="0", unit_price_read="20000.125", amount_read=7)
        result = extract_document(image_bytes(), "image/png", None, client=provider(value))
        self.assertEqual(result["lines"][0]["amount_read"], 7)
        self.assertEqual(result["lines"][0]["qty_read"], "0")
        self.assertEqual(result["lines"][0]["unit_price_read"], "20000.125")
        self.assertIsNone(result["lines"][0]["spec"])

    def test_odd_model_cells_become_reviewable_instead_of_failing_the_photo(self):
        value = facts()
        value.update(doc_no=" ", trade_date="2026년 11월 21일", doc_date="26.11.21",
                     period_start="2026-11-15", period_end="2026-11-09")
        value["lines"][0].update(qty_read="1,200", unit_price_read="₩12,000", spec="",
                                 delivery_date="11/21", line_date_text=None)
        base = value["lines"][0]
        value["lines"] += [
            {**base, "qty_read": "10kg", "unit_price_read": "12,000원", "delivery_date": "2026.11.20", "unit": "  "},
            {**base, "qty_read": "1,2", "unit_price_read": "12.000원", "delivery_date": "11/22",
             "line_date_text": "11월 22일분"},
            {**base, "qty_read": "0,5", "unit_price_read": "12,000.50", "delivery_date": None},
            {**base, "qty_read": "-", "unit_price_read": "₩", "delivery_date": None},
        ]
        result = extract_document(image_bytes(), "image/png", None, client=provider(value))
        first, second, third, fourth, fifth = result["lines"]
        self.assertEqual((first["qty_read"], first["unit_price_read"]), ("1200", "12000"))
        self.assertIsNone(first["spec"])
        self.assertIsNone(first["delivery_date"])
        self.assertEqual(first["line_date_text"], "11/21")
        self.assertIsNone(second["qty_read"])  # "10kg" is left for the reviewer, not guessed
        self.assertEqual(second["unit_price_read"], "12000")
        self.assertEqual(second["delivery_date"], "2026-11-20")
        self.assertIsNone(second["unit"])
        # Ambiguous separators are never collapsed into a bigger or smaller number.
        self.assertIsNone(third["qty_read"])         # "1,2" is not 12
        self.assertIsNone(third["unit_price_read"])  # "12.000원" is not 12 won
        self.assertEqual(third["line_date_text"], "11월 22일분 / 11/22")
        self.assertIsNone(fourth["qty_read"])        # "0,5" is not 05
        self.assertEqual(fourth["unit_price_read"], "12000.50")
        self.assertIsNone(fifth["qty_read"])
        self.assertIsNone(fifth["unit_price_read"])
        self.assertIsNone(result["doc_no"])
        self.assertEqual(result["trade_date"], "2026-11-21")
        self.assertIsNone(result["doc_date"])
        self.assertIsNone(result["period_start"])
        self.assertIsNone(result["period_end"])
        # Unreadable header dates keep their printed text for the reviewer.
        self.assertEqual(result["source_date_text"], "26.11.21 / 2026-11-15 ~ 2026-11-09")
        # Owner edits are still validated strictly.
        edited = facts()
        edited["lines"][0]["qty_read"] = "1,200"
        with self.assertRaises(ValueError):
            validate_fields(edited)

    def test_non_text_period_still_fails_as_invalid_text(self):
        value = facts()
        value.update(period_start=5, period_end="2026-11-09")
        with self.assertRaisesRegex(ExtractionError, "^INVALID_DOCUMENT_TEXT$"):
            extract_document(image_bytes(), "image/png", None, client=provider(value))

    def test_bad_numbers_dates_and_extra_fields_rejected(self):
        for field, invalid in [("qty_read", "NaN"), ("qty_read", "1,000"),
                               ("amount_read", True), ("amount_read", 1.5),
                               ("amount_read", 10**20), ("delivery_date", "2026-02-30")]:
            value = facts()
            value["lines"][0][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                validate_fields(value)

    def test_agreement_is_page_evidence_not_confirmation(self):
        value = facts()
        value.update(doc_type="CORRECTED_INVOICE", agreement_text="양측 확인 후 36kg으로 수정")
        result = extract_document(image_bytes(), "image/png", None, client=provider(value))
        self.assertEqual(result["agreement_source_location"]["page"], 1)
        self.assertNotIn("confirmed_by", result)

    def test_unknown_stays_blocked_in_adapter(self):
        scope = {"store_id": "s", "supplier_id": "v", "period_id": "p"}
        result = reconcile_documents([{**scope, "document_id": "unknown", "doc_type": "UNKNOWN", "lines": []}], [], **scope)
        self.assertIn("UNCLASSIFIED_DOCUMENT", result["blockers"])
        self.assertFalse(result["can_approve"])

    def test_gpt_facts_to_calculation_require_separate_receipt_confirmation(self):
        scope = {"store_id": "s", "supplier_id": "v", "period_id": "p"}
        documents = []
        for kind, qty in [("DELIVERY", "40"), ("INVOICE", "40"), ("RECEIPT", "36")]:
            value = facts()
            value.update(doc_type=kind, doc_no="INV-1" if kind == "INVOICE" else None)
            value["lines"][0]["qty_read"] = qty
            draft = extract_document(image_bytes(), "image/png", kind, client=provider(value))
            draft.update(scope)
            draft["lines"][0]["line_id"] = kind + ":1"
            documents.append(draft)
        result = reconcile_documents(documents, [], **scope)
        self.assertIsNone(result["summary"]["received_qty"])
        self.assertFalse(result["can_approve"])
        event = {**scope, "receipt_id": "confirmed-once", "source_line_id": "RECEIPT:1",
                 "confirmed_by": "owner", "confirmed_at": "2026-11-21T12:00:00+09:00"}
        confirmed = reconcile_documents(documents, [event, deepcopy(event)], **scope)
        self.assertEqual(confirmed["summary"]["received_qty"], "36")
        self.assertEqual(confirmed["summary"]["delta_qty"], "4")
        self.assertEqual(confirmed["summary"]["check_amount"], 80000)
        self.assertFalse(confirmed["can_approve"])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PHOTO_REVIEW_SIGNING_KEY": "unit-test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.draft = facts()
        self.review = deepcopy(self.draft)
        self.review["lines"][0]["source_row"] = 1

    def test_original_and_draft_bound_and_expiring(self):
        with patch("app.photo.review.time.time", return_value=10000):
            digest = __import__("hashlib").sha256(b"original").hexdigest()
            signature = sign(digest, self.draft, 10000)
            self.assertEqual(verify(b"original", self.draft, 10000, signature), digest)
            for raw, draft, issued, token in [(b"changed", self.draft, 10000, signature),
                    (b"original", {**self.draft, "doc_no": "edited"}, 10000, signature),
                    (b"original", self.draft, 10001, signature), (b"original", self.draft, 1, signature),
                    (b"original", self.draft, 10000, "한글")]:
                with self.subTest(issued=issued), self.assertRaises(ExtractionError):
                    verify(raw, draft, issued, token)

    def test_amount_warning_never_rewrites_the_original(self):
        self.draft["lines"][0]["amount_read"] = 799999
        self.assertTrue(review_warnings(self.draft))
        self.assertEqual(self.draft["lines"][0]["amount_read"], 799999)

    def test_edit_add_delete_and_null_are_preserved(self):
        added = {**self.review["lines"][0], "source_row": None, "qty_read": "0", "amount_read": None}
        self.review["lines"] = [added]
        before = deepcopy(self.review)
        headers, edits = prepare_review(self.draft, self.review)
        self.assertEqual(edits[0]["qty_read"], "0")
        self.assertIsNone(edits[0]["amount_read"])
        self.assertIsNone(edits[0]["source_row"])
        self.assertEqual(self.review, before)
        self.assertNotIn("confirmed_by", headers)

    def test_duplicate_or_fake_source_rejected(self):
        for lines in [[self.review["lines"][0]] * 2,
                      [{**self.review["lines"][0], "source_row": 2}],
                      [{**self.review["lines"][0], "source_row": True}]]:
            with self.subTest(lines=lines), self.assertRaises(ExtractionError):
                prepare_review(self.draft, {**self.review, "lines": lines})

    def test_unclassified_blank_and_injected_confirmations_rejected(self):
        for review in [{**self.review, "doc_type": "UNKNOWN"}, {**self.review, "lines": []},
                       {**self.review, "confirmed_by": "injected"},
                       {**self.review, "lines": [{**self.review["lines"][0], "item": " "}]}]:
            with self.subTest(review=review), self.assertRaises(ExtractionError):
                prepare_review(self.draft, review)


if __name__ == "__main__":
    unittest.main()
