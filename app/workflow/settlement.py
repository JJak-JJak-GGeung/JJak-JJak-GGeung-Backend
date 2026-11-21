from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid5

from fastapi import HTTPException

from app.api.database import rpc
from app.reconcile.settlement import reconcile_period


def quantity(row, name):
    value = row.get(name.replace("_read", "_corrected"))
    return str(row[name] if value is None else value) if (
        value is not None or row.get(name) is not None) else None


def money(row):
    value = quantity(row, "amount_read")
    if value is None:
        return None
    number = Decimal(value)
    return int(number) if number.is_finite() and number == number.to_integral_value() else None


def evidence(doc, line=None, kind=None):
    return {"document_id": doc["id"], "line_id": line["id"] if line else None,
            "kind": kind or doc["doc_type"],
            "source_location": line.get("source_location") if line else doc.get("agreement_source_location")}


def calculate(state):
    period = state["period"]
    scope = {name: period[name] for name in ("store_id", "supplier_id")}
    scope["period_id"] = period["id"]
    documents = {d["id"]: d for d in state["documents"]}
    lines = state["lines"]
    by_line = {line["id"]: line for line in lines}
    deliveries = []
    warnings = set()
    for doc in documents.values():
        if doc["source_mode"] != "FILE":
            continue
        if doc["extraction_status"] not in ("COMPLETED", "SUCCESS"):
            warnings.add("EXTRACTION_PENDING")
        elif doc["supplier_id"] != period["supplier_id"] or doc["link_status"] in ("UNCLASSIFIED", "CANDIDATE"):
            warnings.add("UNCLASSIFIED_DOCUMENT")
        if doc.get("metadata", {}).get("doc_type_read") != doc["doc_type"]:
            warnings.add("DOCUMENT_TYPE_UNCONFIRMED")
    for line in lines:
        doc = documents[line["document_id"]]
        if doc["doc_type"] != "DELIVERY" or doc["supplier_id"] != period["supplier_id"]:
            continue
        deliveries.append({
            **scope, "line_id": line["id"],
            "transaction_id": str(uuid5(UUID(period["id"]), line["id"])),
            "delivery_no": line["delivery_no"], "delivery_date": line["delivery_date"],
            "item": line["raw_name"], "spec": line["raw_spec"], "unit": line["raw_unit"],
            "declared_qty": quantity(line, "qty_read"),
            "evidence_refs": [evidence(doc, line)],
        })

    def target(line):
        candidates = [row for row in deliveries
                      if row["delivery_no"] == line["delivery_no"] and row["delivery_no"]
                      and row["item"] == line["raw_name"]
                      and row["unit"] == line["raw_unit"]]
        return candidates[0]["line_id"] if len(candidates) == 1 else None

    invoices = []
    for doc in documents.values():
        if doc["doc_type"] not in ("INVOICE", "CORRECTED_INVOICE") or doc["supplier_id"] != period["supplier_id"]:
            continue
        doc_lines = [line for line in lines if line["document_id"] == doc["id"]]
        invoice = {
            **scope, "document_id": doc["id"], "doc_type": doc["doc_type"],
            "doc_no": doc["doc_no"], "period_start": doc["period_start"],
            "period_end": doc["period_end"], "replaces_doc_no": doc["replaces_doc_no"],
            "evidence_refs": [evidence(doc, line, "INVOICE") for line in doc_lines],
            "lines": [{"target_delivery_line_id": target(line), "billed_qty": quantity(line, "qty_read"),
                       "unit_price": quantity(line, "unit_price_read"), "unit": line["raw_unit"],
                       "billed_amount": money(line)} for line in doc_lines],
        }
        confirmed = [c for c in state["confirmations"]
                     if c["kind"] == "replacement" and c["document_id"] == doc["id"]]
        if confirmed:
            confirmation = confirmed[-1]
            proof = {"confirmed_by": confirmation["confirmed_by"] or confirmation["actor_label"],
                     "confirmed_at": confirmation["confirmed_at"],
                     "evidence_refs": confirmation["evidence_refs"]}
            invoice["replacement"] = {**proof, "target_document_id": confirmation["target_id"]}
            if doc["agreement_text"] and doc["agreement_source_location"]:
                invoice["agreement"] = {
                    **proof, "supplier_confirmed": True, "owner_confirmed": True,
                    "evidence_refs": [evidence(doc)],
                }
        invoices.append(invoice)

    receipts = []
    for receipt in state["receipts"]:
        source = by_line.get(receipt["source_line_id"])
        delivery = by_line.get(receipt["doc_line_id"])
        evidence_text = receipt.get("evidence_text")
        if not delivery or (not source and not evidence_text):
            warnings.add("MISSING_RECEIPT_SOURCE")
            continue
        receipt_evidence = [evidence(documents[source["document_id"]], source, "RECEIPT")] if source else []
        if evidence_text:
            receipt_evidence.append({
                "kind": "RECEIPT", "document_id": None, "line_id": None,
                "receipt_id": receipt["id"], "source_location": None, "evidence_text": evidence_text,
            })
        receipts.append({
            **scope, "receipt_id": receipt["id"], "source_line_id": source["id"] if source else None,
            "target_delivery_line_id": receipt["doc_line_id"],
            "delivery_no": delivery["delivery_no"], "received_qty": (
                None if receipt["received_qty"] is None else str(receipt["received_qty"])),
            "unit": receipt["unit"], "confirmed_by": receipt["confirmed_by"] or receipt["actor_label"],
            "confirmed_at": receipt["confirmed_at"], "supersedes_receipt_id": receipt["supersedes_id"],
            "evidence_refs": receipt_evidence,
        })
    latest_receipts = [r for r in receipts if not any(n["supersedes_receipt_id"] == r["receipt_id"] for n in receipts)]

    def has_unresolved_receipt_difference(memo):
        delivery = next((d for d in deliveries if d["transaction_id"] == memo["transaction_id"]), None)
        if not delivery or delivery["declared_qty"] is None:
            return False
        values = [r for r in latest_receipts if r["target_delivery_line_id"] == delivery["line_id"]]
        return bool(values) and all(r["received_qty"] is not None for r in values) and (
            sum(Decimal(r["received_qty"]) for r in values) != Decimal(delivery["declared_qty"]))

    memos = [{**memo, **scope, "memo_id": memo["id"],
              "confirmed_by": memo["confirmed_by"] or memo["actor_label"],
              "conflicting": has_unresolved_receipt_difference(memo)} for memo in state["memos"]]
    approvals = [{**scope, "approval_id": a["id"], "input_revision": a["input_revision"],
                  "approved_amount": int(a["confirmed_total"])} for a in state["approvals"]]
    result = reconcile_period(
        deliveries, invoices, receipts, memos, **scope,
        period_start=period["period_start"], period_end=period["period_end"],
        input_revision=period["input_revision"], approvals=approvals,
    )
    result.update(**scope, is_example=False)
    result["blockers"] = sorted(set(result["blockers"]) | warnings)
    if warnings:
        result.update(can_approve=False, approvable_amount=None)
    invoice_ids = set(result["active_invoice_ids"])
    links = []
    for item in result["items"]:
        relevant = [evidence(documents[item_line["document_id"]], item_line, "INVOICE")
                    for item_line in lines
                    if item_line["document_id"] in invoice_ids and target(item_line) == item["line_id"]]
        item["evidence_refs"] = [ref for ref in item["evidence_refs"] if ref["kind"] != "INVOICE"] + relevant
        item["work_status"] = result["work_status"] if item.get("fact_status") == "CONFLICTING" else (
            "OPEN" if item["match_result"] == "MATCHED" else "NEEDS_INFO" if item["match_result"] == "MISSING_DATA" else "WAITING_REPLY")
        item["blockers"] = result["blockers"]
        for ref in item["evidence_refs"]:
            if ref.get("line_id") and ref["kind"] != "RECEIPT":
                links.append({"transaction_id": item["transaction_id"], "doc_line_id": ref["line_id"],
                              "kind": ref["kind"]})
        active_receipts = [r for r in receipts if r["target_delivery_line_id"] == item["line_id"]
                           and not any(other["supersedes_receipt_id"] == r["receipt_id"] for other in receipts)]
        item["receipts"] = active_receipts
        for receipt in active_receipts:
            links.append({"transaction_id": item["transaction_id"], "receipt_id": receipt["receipt_id"], "kind": "RECEIPT"})
    return result, links


def get_settlement(period_id, refresh=False):
    for _ in range(3):
        state = rpc("read_period_state", {"p_period": str(period_id)})
        if not state:
            raise HTTPException(404, "Period not found")
        period = state["period"]
        if state.get("result") and not refresh:
            result = deepcopy(state["result"])
        else:
            result, links = calculate(state)
            try:
                result = rpc("publish_reconciliation", {
                    "p_period": period["id"], "p_expected": period["input_revision"],
                    "p_items": result["items"], "p_links": links, "p_result": result})
            except HTTPException as error:
                if error.status_code == 409:
                    continue
                raise
        current = [a for a in state["approvals"] if a["input_revision"] == period["input_revision"]]
        if current:
            approval = current[0]
            result.update(can_approve=False, approved_amount=int(approval["confirmed_total"]),
                          approval_id=approval["id"], work_status="APPROVED")
        return result
    raise HTTPException(409, {"code": "STALE_REVISION"})
