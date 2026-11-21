import json
from pathlib import Path
from app.reconcile.matching import reconcile_receipts

folder = Path(__file__).resolve().parent
bundle = json.loads((folder / "DL-A131_B1_실제판독.json").read_text(encoding="utf-8"))
context = bundle["matching_context"]
delivery, invoice, receipt = (next(row for row in bundle["documents"] if row["doc_type"] == kind)
    for kind in ("DELIVERY", "INVOICE", "RECEIPT"))
keys = ("store_name", "supplier_name_read", "delivery_no", "delivery_date", "item", "unit")
assert all(len({row[key] for row in (delivery, invoice, receipt)}) == 1 for key in keys)
assert (invoice["period_start_read"], invoice["period_end_read"]) == (
    receipt["period_start_read"], receipt["period_end_read"]) == (context["period_start"], context["period_end"])
scope = {"store_id": "b1-local-store", "supplier_id": "b1-local-supplier", "period_id": "b1-local-period"}
def evidence(row):
    return {key: row[key] for key in ("document_id", "line_id", "source_location")} | {"kind": row["doc_type"]}
line = {**scope, **{key: delivery[key] for key in ("delivery_no", "delivery_date", "item", "spec", "unit")},
    "line_id": "b1-local-A02-1", "declared_qty": delivery["qty_read"], "billed_qty": invoice["qty_read"],
    "unit_price": invoice["unit_price_read"], "billed_amount": invoice["amount_read"],
    "evidence_refs": [evidence(delivery), evidence(invoice)]}
candidate = {**scope, **{key: receipt[key] for key in ("delivery_no", "delivery_date", "item", "spec", "unit")},
    "receipt_id": bundle["receipt"]["receipt_id"], "source_line_id": receipt["line_id"],
    "confirmed_by": bundle["receipt"]["confirmed_by"], "confirmed_at": bundle["receipt"]["confirmed_at"],
    "qty_read": receipt["qty_read"], "received_qty": bundle["receipt"]["received_qty"], "evidence_refs": [evidence(receipt)]}
inputs = {"lines": [line], "receipts": [candidate], **scope}
input_path = folder / "DL-A131_B1_대조입력.json"
encoded = json.dumps(inputs, ensure_ascii=False, indent=2) + "\n"
input_path.write_text(encoded, encoding="utf-8")
result = reconcile_receipts(**json.loads(input_path.read_text(encoding="utf-8")))
link = result["receipt_links"][0]
assert (link["status"], link["reason"], link["candidates"], link["target_delivery_line_id"]) == ("UNCONFIRMED","RECEIPT_NOT_CONFIRMED",[{"line_id": line["line_id"], **{k: line[k] for k in ("delivery_no","delivery_date","item","spec","unit","evidence_refs")}}],None)
assert [e["kind"] for e in result["items"][0]["evidence_refs"]] == ["DELIVERY","INVOICE"]
assert result["items"][0]["receipt_ids"] == []
for row in (result["items"][0], result["summary"]):
    assert (row["declared_qty"], row["billed_qty"], row["billed_amount"]) == ("40","40",800000)
    assert all(row[key] is None for key in ("received_qty","delta_qty","check_amount"))
assert (result["confirmation_scope"], result["match_result"], result["can_approve"]) == ("DOCUMENTS_ONLY","MISSING_DATA",False)
assert "MISSING_CONFIRMED_RECEIPT" in result["blockers"]
defensive = reconcile_receipts([line], [{**candidate, "received_qty": candidate["qty_read"]}], **scope)
assert defensive["items"][0]["received_qty"] is None and defensive["summary"]["received_qty"] is None
print(json.dumps({"candidate_fields_match": True, "ocr_candidate_kg": link["receipt"]["qty_read"],
    "status": link["status"], "reason": link["reason"], "returned_candidates": link["candidates"],
    "target_delivery_line_id": link["target_delivery_line_id"], "summary": result["summary"],
    "confirmation_scope": result["confirmation_scope"], "match_result": result["match_result"],
    "blockers": result["blockers"], "can_approve": result["can_approve"], "defensive_check_passed": True},
    ensure_ascii=False, indent=2))
