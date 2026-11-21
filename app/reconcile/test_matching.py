"""Synthetic B1 inputs only; no photo adapter or server integration exercised."""

from copy import deepcopy
import unittest

from .matching import reconcile_receipts


SCOPE = {"store_id": "example-store", "supplier_id": "example-supplier", "period_id": "example-period"}


def line(line_id="delivery-1", **changes):
    return {**SCOPE, "line_id": line_id, "delivery_no": "example-number",
            "delivery_date": "2026-11-13", "item": "eel", "spec": "cleaned",
            "unit": "kg", "declared_qty": "40", "billed_qty": "40",
            "unit_price": "20000", "billed_amount": 800000,
            "evidence_refs": [{"document_id": "example-delivery", "line_id": line_id,
                               "kind": "DELIVERY", "source_location": {"page": 1, "row": 1}}],
            **changes}


def receipt(receipt_id="receipt-1", **changes):
    return {**SCOPE, "receipt_id": receipt_id, "source_line_id": "source-1",
            "delivery_no": "example-number", "received_qty": "36", "unit": "kg",
            "confirmed_by": "example-owner", "confirmed_at": "2026-11-21T10:00:00+09:00",
            "evidence_refs": [{"document_id": "example-receipt", "line_id": "source-1",
                               "kind": "RECEIPT", "source_location": {"page": 1, "row": 2}}],
            **changes}


def run(lines, receipts):
    return reconcile_receipts(lines, receipts, **SCOPE)


class MatchingTests(unittest.TestCase):
    def test_number_links_and_calculates_without_mutating_sources(self):
        lines, receipts = [line()], [receipt()]
        before = deepcopy((lines, receipts))
        result = run(lines, receipts)
        self.assertEqual((lines, receipts), before)
        self.assertEqual(result["receipt_links"][0]["status"], "LINKED")
        self.assertEqual(result["items"][0]["received_qty"], "36")
        self.assertEqual(result["summary"]["check_amount"], 80000)
        self.assertEqual(len(result["items"][0]["evidence_refs"]), 2)
        self.assertEqual(result["blockers"], ["QTY_MISMATCH"])
        self.assertFalse(result["can_approve"])

    def test_missing_number_uses_complete_fallback_fields(self):
        value = receipt(delivery_no=None, delivery_date="2026-11-13", item="eel", spec="cleaned")
        result = run([line()], [value])
        self.assertEqual(result["receipt_links"][0]["target_delivery_line_id"], "delivery-1")

    def test_incomplete_fallback_shows_candidates_but_holds(self):
        result = run([line()], [receipt(delivery_no=None, item="eel")])
        link = result["receipt_links"][0]
        self.assertEqual(link["reason"], "INCOMPLETE_MATCH_FIELDS")
        self.assertEqual(link["candidates"][0]["line_id"], "delivery-1")
        self.assertIsNone(result["summary"]["received_qty"])

    def test_ambiguous_number_does_not_pick_first(self):
        result = run([line(), line("delivery-2")], [receipt()])
        self.assertEqual(result["receipt_links"][0]["reason"], "AMBIGUOUS_MATCH")
        self.assertEqual(len(result["receipt_links"][0]["candidates"]), 2)
        self.assertTrue(all(item["received_qty"] is None for item in result["items"]))

    def test_number_and_multiple_items_are_disambiguated(self):
        result = run([line(), line("delivery-2", item="other")], [receipt(item="eel")])
        self.assertEqual(result["items"][0]["received_qty"], "36")
        self.assertIsNone(result["items"][1]["received_qty"])
        self.assertIsNone(result["summary"]["received_qty"])

    def test_wrong_number_never_falls_back(self):
        result = run([line()], [receipt(delivery_no="wrong", delivery_date="2026-11-13",
                                        item="eel", spec="cleaned")])
        self.assertEqual(result["receipt_links"][0]["reason"], "NO_MATCH")

    def test_explicit_target_must_agree_with_provided_number(self):
        result = run([line()], [receipt(target_delivery_line_id="delivery-1", delivery_no="wrong")])
        self.assertEqual(result["receipt_links"][0]["reason"], "NO_MATCH")

    def test_other_scope_and_unconfirmed_receipts_are_not_counted(self):
        values = [receipt(), receipt("other-store", store_id="other"),
                  receipt("other-supplier", supplier_id="other"),
                  receipt("other-period", period_id="other"),
                  receipt("unconfirmed", confirmed_by=None),
                  receipt("no-time", confirmed_at=None)]
        result = run([line()], values)
        self.assertEqual(result["items"][0]["received_qty"], "36")
        self.assertEqual([link["status"] for link in result["receipt_links"]],
                         ["LINKED", "OUT_OF_SCOPE", "OUT_OF_SCOPE", "OUT_OF_SCOPE",
                          "UNCONFIRMED", "UNCONFIRMED"])

    def test_receipt_and_source_retries_count_once(self):
        value = receipt()
        result = run([line()], [value, deepcopy(value), receipt("new-id")])
        self.assertEqual(result["items"][0]["received_qty"], "36")
        self.assertEqual([link["status"] for link in result["receipt_links"]],
                         ["LINKED", "DUPLICATE", "DUPLICATE"])

    def test_unconfirmed_candidates_do_not_link_or_count_received_quantity(self):
        for missing in ("confirmed_by", "confirmed_at"):
            with self.subTest(missing=missing):
                value = receipt(**{missing: None}, target_delivery_line_id="delivery-1")
                result = run([line(received_qty="36")], [value])
                link = result["receipt_links"][0]
                self.assertEqual(link["status"], "UNCONFIRMED")
                self.assertEqual(link["candidates"][0]["line_id"], "delivery-1")
                self.assertIsNone(link["target_delivery_line_id"])
                for record in (result["items"][0], result["summary"]):
                    for field in ("received_qty", "delta_qty", "check_amount"):
                        self.assertIsNone(record[field])
                self.assertFalse(result["can_approve"])

    def test_unconfirmed_ambiguous_candidates_and_scope_boundary(self):
        lines = [line(), line("delivery-2")]
        value = receipt(delivery_no=None, delivery_date="2026-11-13", item="eel",
                        spec="cleaned", confirmed_by=None)
        result = run(lines, [value])
        self.assertEqual(len(result["receipt_links"][0]["candidates"]), 2)
        self.assertEqual(result["receipt_links"][0]["status"], "UNCONFIRMED")
        self.assertIsNone(result["summary"]["received_qty"])
        value["store_id"] = "other-store"
        excluded = run(lines, [value])["receipt_links"][0]
        self.assertEqual(excluded["status"], "OUT_OF_SCOPE")
        self.assertEqual(excluded["candidates"], [])

    def test_duplicate_id_or_source_with_changed_quantity_holds(self):
        for receipt_id in ("receipt-1", "new-id"):
            with self.subTest(receipt_id=receipt_id):
                result = run([line()], [receipt(), receipt(receipt_id, received_qty="37")])
                self.assertIsNone(result["items"][0]["received_qty"])
                self.assertTrue(result["blockers"])

    def test_distinct_receipt_sources_sum_decimals_and_zero(self):
        values = [receipt(received_qty="0"),
                  receipt("r2", source_line_id="s2", received_qty="35.125"),
                  receipt("r3", source_line_id="s3", received_qty="0.875")]
        result = run([line()], values)
        self.assertEqual(result["items"][0]["received_qty"], "36.000")
        self.assertEqual(result["summary"]["check_amount"], 80000)
        zero = run([line()], [receipt(received_qty="0")])
        self.assertEqual(zero["items"][0]["received_qty"], "0")

    def test_confirmed_correction_chain_uses_only_latest(self):
        values = [receipt(), receipt("r2", received_qty="37", supersedes_receipt_id="receipt-1"),
                  receipt("r3", received_qty="38", supersedes_receipt_id="r2")]
        for ordered in (values, list(reversed(values))):
            result = run([line()], ordered)
            self.assertEqual(result["items"][0]["received_qty"], "38")
            self.assertEqual(result["items"][0]["receipt_ids"], ["r3"])

    def test_unconfirmed_correction_does_not_replace_confirmed_receipt(self):
        result = run([line()], [receipt(), receipt("r2", received_qty="40",
                                                  supersedes_receipt_id="receipt-1", confirmed_by=None)])
        self.assertEqual(result["items"][0]["received_qty"], "36")

    def test_invalid_correction_chains_hold(self):
        cases = [
            [receipt(supersedes_receipt_id="missing")],
            [receipt(supersedes_receipt_id="receipt-1")],
            [receipt(), receipt("r2", supersedes_receipt_id="receipt-1"),
             receipt("r3", supersedes_receipt_id="receipt-1")],
            [receipt(supersedes_receipt_id="r2"), receipt("r2", supersedes_receipt_id="receipt-1")],
        ]
        for values in cases:
            with self.subTest(values=values):
                result = run([line()], values)
                self.assertIsNone(result["items"][0]["received_qty"])
                self.assertIn("INVALID_RECEIPT_CORRECTION", result["blockers"])

    def test_unit_conversion_is_not_guessed(self):
        result = run([line()], [receipt(unit="box")])
        self.assertIsNone(result["items"][0]["delta_qty"])
        self.assertEqual(result["receipt_links"][0]["receipt"]["unit"], "box")
        self.assertIn("UNCONFIRMED_UNIT_CONVERSION", result["blockers"])
        self.assertEqual(result["confirmation_scope"], "RECEIPTS_INCLUDED")

    def test_missing_id_or_invalid_quantity_cannot_complete_week(self):
        for changes in ({"receipt_id": None}, {"received_qty": None}, {"received_qty": "NaN"}):
            with self.subTest(changes=changes):
                value = {**receipt("r2", source_line_id="s2"), **changes}
                result = run([line()], [receipt(), value])
                self.assertIsNone(result["summary"]["received_qty"])

    def test_unresolved_receipt_preserves_other_confirmed_rows(self):
        result = run([line()], [receipt(), receipt("r2", delivery_no="unknown", source_line_id="s2")])
        self.assertEqual(result["items"][0]["received_qty"], "36")
        self.assertEqual(result["items"][0]["check_amount"], 80000)
        self.assertIsNone(result["summary"]["received_qty"])

    def test_scope_and_delivery_ids_are_required(self):
        for lines in ([line(store_id="other")], [line(), line()], [line(line_id=None)]):
            with self.assertRaises(ValueError):
                run(lines, [])


if __name__ == "__main__":
    unittest.main()
