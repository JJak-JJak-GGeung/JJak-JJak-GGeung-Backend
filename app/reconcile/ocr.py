"""B1 adapter for app.photo.extract_document output with server-assigned IDs."""

from collections import Counter
from copy import deepcopy

from .matching import delivery_candidates, reconcile_receipts


def reconcile_ocr_documents(documents, confirmations, *, store_id, supplier_id, period_id):
    """Connect OCR facts to B1; confirmations must come from the trusted server.

    Each document needs server-verified scope and globally unique row line_id.
    OCR fields never constitute confirmation. No IDs, units or quantities are
    guessed. This adapter handles initial invoices; corrected invoices remain
    pending for the settlement replacement/quantity-agreement workflow.
    """
    scope = {"store_id": store_id, "supplier_id": supplier_id, "period_id": period_id}
    if not all(isinstance(value, str) and value.strip() for value in scope.values()):
        raise ValueError("Scope IDs are required")
    documents = deepcopy(documents)
    confirmations = deepcopy(confirmations)
    in_scope = lambda value: all(value.get(key) == field for key, field in scope.items())
    unique = {}
    excluded = []
    for document in documents:
        if not in_scope(document):
            excluded.append(document.get("document_id"))
            continue
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("Server-assigned document_id required")
        if document_id in unique and unique[document_id] != document:
            raise ValueError("Conflicting document_id")
        unique[document_id] = document
    rows = []
    source_blockers = set()
    row_ids = set()
    for document in unique.values():
        if document.get("doc_type") not in ("DELIVERY", "INVOICE", "RECEIPT", "CORRECTED_INVOICE"):
            raise ValueError("Unsupported OCR document type")
        if not document.get("lines"):
            source_blockers.add("EMPTY_OCR_DOCUMENT")
        for raw in document.get("lines") or []:
            line_id = raw.get("line_id")
            if not isinstance(line_id, str) or not line_id.strip() or line_id in row_ids:
                raise ValueError("Server-assigned globally unique line_id required")
            row_ids.add(line_id)
            fields = {key: raw.get(key) for key in ("delivery_no", "delivery_date", "item", "spec", "unit")}
            # OCR already compacts item whitespace; normalize consistently at this boundary.
            if fields["item"]:
                fields["item"] = "".join(fields["item"].split())
            rows.append({**scope, **fields, "line_id": line_id,
                         "transaction_id": raw.get("transaction_id"),
                         "ocr_line": raw, "document_id": document["document_id"],
                         "doc_type": document["doc_type"],
                         "evidence_refs": [{"document_id": document["document_id"],
                                            "line_id": line_id, "kind": document["doc_type"],
                                            "source_location": raw.get("source_location")} ]})
    deliveries = [{**row, "declared_qty": row["ocr_line"].get("qty_read"),
                   "billed_qty": None, "unit_price": None, "billed_amount": None}
                  for row in rows if row["doc_type"] == "DELIVERY"]
    bills = [row for row in rows if row["doc_type"] in ("INVOICE", "CORRECTED_INVOICE")]
    numbers = Counter(document.get("doc_no") for document in unique.values()
                      if document["doc_type"] == "INVOICE" and document.get("doc_no"))
    links, assignments, blockers = [], {}, set(source_blockers)
    for bill in bills:
        document = unique[bill["document_id"]]
        pool = delivery_candidates(deliveries, bill)
        link = {"source_line_id": bill["line_id"], "document_id": bill["document_id"],
                "candidates": [row["line_id"] for row in pool],
                "target_delivery_line_id": None, "status": "HELD", "reason": None}
        links.append(link)
        if bill["doc_type"] == "CORRECTED_INVOICE":
            link["reason"] = "CORRECTION_REQUIRES_SETTLEMENT"
        elif not document.get("doc_no") or numbers[document["doc_no"]] > 1:
            link["reason"] = "AMBIGUOUS_INVOICE_NUMBER"
        elif not bill.get("delivery_no") and not all(bill.get(k) for k in ("delivery_date", "item", "spec", "unit")):
            link["reason"] = "INCOMPLETE_MATCH_FIELDS"
        elif len(pool) != 1:
            link["reason"] = "AMBIGUOUS_MATCH" if pool else "NO_MATCH"
        elif not bill.get("unit") or bill["unit"] != pool[0].get("unit"):
            link["reason"] = "UNCONFIRMED_INVOICE_UNIT"
        else:
            target = pool[0]["line_id"]
            assignments.setdefault(target, []).append((bill, link))
    for delivery in deliveries:
        matched = assignments.get(delivery["line_id"], [])
        if len(matched) != 1:
            blockers.add("AMBIGUOUS_BILLING_LINE" if matched else "MISSING_BILLING_LINE")
            for _, link in matched:
                link["reason"] = "AMBIGUOUS_BILLING_LINE"
            continue
        bill, link = matched[0]
        delivery.update(billed_qty=bill["ocr_line"].get("qty_read"),
                        unit_price=bill["ocr_line"].get("unit_price_read"),
                        billed_amount=bill["ocr_line"].get("amount_read"),
                        invoice_ocr_line=bill["ocr_line"])
        delivery["evidence_refs"] += bill["evidence_refs"]
        link.update(status="LINKED", reason=None, target_delivery_line_id=delivery["line_id"])
    events = []
    used = set()
    for row in rows:
        if row["doc_type"] != "RECEIPT":
            continue
        # Never forward confirmation fields from raw OCR, even if supplied.
        candidate = {**{key: row[key] for key in scope},
                     **{key: row.get(key) for key in ("delivery_no", "delivery_date", "item", "spec", "unit")},
                     "source_line_id": row["line_id"], "received_qty": row["ocr_line"].get("qty_read"),
                     "receipt_id": None, "confirmed_by": None, "confirmed_at": None,
                     "ocr_line": row["ocr_line"], "evidence_refs": row["evidence_refs"]}
        matches = [(index, event) for index, event in enumerate(confirmations)
                   if in_scope(event) and event.get("source_line_id") == row["line_id"]]
        if not matches:
            events.append(candidate)
        for index, event in matches:
            used.add(index)
            combined = {**candidate, **event}
            combined["evidence_refs"] = candidate["evidence_refs"] + list(event.get("evidence_refs") or [])
            events.append(combined)
    if any(index not in used and in_scope(event) for index, event in enumerate(confirmations)):
        blockers.add("UNKNOWN_CONFIRMATION_SOURCE")
    blockers.update(link["reason"] for link in links if link["status"] == "HELD")
    result = reconcile_receipts(deliveries, events, **scope)
    blockers.update(result["blockers"])
    if blockers - {"QTY_MISMATCH"}:
        for field in ("received_qty", "delta_qty", "check_amount"):
            result["summary"][field] = None
        result["match_result"] = "MISSING_DATA"
    result.update(document_links=links, blockers=sorted(blockers), excluded_document_ids=excluded)
    return result
