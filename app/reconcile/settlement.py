"""C1-D2 pure settlement rules. Server owns authentication, persistence and locks."""

from collections import Counter
from copy import deepcopy
from datetime import date
from typing import Any

from . import _quantity
from .matching import reconcile_receipts


def _confirmed(record):
    return bool(record.get("confirmed_by") and record.get("confirmed_at"))


def _evidence(refs):
    return bool(refs) and all(
        ref.get("document_id") and "line_id" in ref and ref.get("source_location")
        for ref in refs
    )


def reconcile_period(
    deliveries: list[dict[str, Any]], invoices: list[dict[str, Any]],
    receipts: list[dict[str, Any]], memos: list[dict[str, Any]], *,
    store_id: str, supplier_id: str, period_id: str, period_start: str, period_end: str,
    input_revision: int, approvals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calculate a period from server-validated normalized source records.

    Invoice rows target delivery line IDs; replacement confirmation is stored
    separately from bilateral quantity agreement. Unknown targets remain pending.
    Source document dates are retained but never select the accounting period.
    """
    if type(input_revision) is not int or input_revision < 0:
        raise ValueError("input_revision must be a nonnegative integer")
    start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
    if start > end:
        raise ValueError("Invalid period dates")
    scope = {"store_id": store_id, "supplier_id": supplier_id, "period_id": period_id}
    in_scope = lambda record: all(record.get(key) == value for key, value in scope.items())
    blockers, pending, excluded = set(), [], []
    by_delivery = {row["line_id"]: row for row in deliveries}
    if len(by_delivery) != len(deliveries):
        raise ValueError("Duplicate delivery line IDs")
    for row in deliveries:
        if not in_scope(row):
            raise ValueError("Delivery outside requested scope")
        if not row.get("delivery_date") or not start <= date.fromisoformat(row["delivery_date"]) <= end:
            blockers.add("DELIVERY_OUTSIDE_PERIOD")

    documents = {}
    for invoice in invoices:
        if not in_scope(invoice):
            excluded.append(deepcopy(invoice))
            continue
        document_id = invoice.get("document_id")
        if not document_id:
            blockers.add("MISSING_INVOICE_ID")
            pending.append(deepcopy(invoice))
            continue
        if document_id in documents and invoice != documents[document_id]:
            blockers.add("INVOICE_ID_CONFLICT")
        else:
            documents[document_id] = invoice
    numbers = Counter(doc.get("doc_no") for doc in documents.values() if doc.get("doc_no"))
    eligible = {}
    for document_id, invoice in documents.items():
        reason = None
        if (invoice.get("period_start"), invoice.get("period_end")) != (period_start, period_end):
            reason = "INVOICE_PERIOD_UNCONFIRMED"
        elif not invoice.get("doc_no") or numbers[invoice["doc_no"]] > 1:
            reason = "AMBIGUOUS_INVOICE_NUMBER"
        elif invoice.get("doc_type") not in ("INVOICE", "CORRECTED_INVOICE"):
            reason = "INVALID_INVOICE_TYPE"
        elif not invoice.get("lines") or not _evidence(invoice.get("evidence_refs")):
            reason = "MISSING_INVOICE_EVIDENCE"
        if reason:
            blockers.add(reason)
            pending.append(deepcopy(invoice))
        else:
            eligible[document_id] = invoice
    active = {key: value for key, value in eligible.items() if value["doc_type"] == "INVOICE"}
    corrections = {key: value for key, value in eligible.items() if value["doc_type"] == "CORRECTED_INVOICE"}
    history = []
    agreed_lines = set()
    # Resolve explicit replacement chains in dependency order, independent of upload order.
    while corrections:
        proposals = {}
        for key, invoice in corrections.items():
            link = invoice.get("replacement") or {}
            targets = [target for target, original in active.items() if
                       (link.get("target_document_id") == target if link.get("target_document_id")
                        else bool(invoice.get("replaces_doc_no")) and invoice["replaces_doc_no"] == original["doc_no"])]
            if len(targets) == 1 and _confirmed(link) and _evidence(link.get("evidence_refs")):
                target = targets[0]
                if invoice.get("replaces_doc_no") and invoice["replaces_doc_no"] != active[target]["doc_no"]:
                    continue
                proposals.setdefault(target, []).append(key)
        if not proposals:
            blockers.add("UNCONFIRMED_REPLACEMENT")
            pending.extend(deepcopy(list(corrections.values())))
            break
        for target, keys in proposals.items():
            if len(keys) != 1:
                blockers.add("AMBIGUOUS_REPLACEMENT")
                for key in keys:
                    pending.append(deepcopy(corrections.pop(key)))
                continue
            key = keys[0]
            invoice = corrections.pop(key)
            old = active.pop(target)
            old_result = reconcile_receipts(
                _billing_rows(deliveries, [old], set()), receipts, **scope,
            )
            history.append({"document": deepcopy(old), "replaced_by": key,
                            "summary": old_result["summary"], "items": old_result["items"]})
            active[key] = invoice
            agreement = invoice.get("agreement") or {}
            if not (_confirmed(agreement) and agreement.get("supplier_confirmed") is True
                    and agreement.get("owner_confirmed") is True
                    and _evidence(agreement.get("evidence_refs"))):
                blockers.add("UNCONFIRMED_QUANTITY_AGREEMENT")
            else:
                agreed_lines.update(row.get("target_delivery_line_id") for row in invoice["lines"])

    rows = _billing_rows(deliveries, list(active.values()), blockers)
    for invoice in active.values():
        blockers.update(invoice.get("blockers") or [])
    result = reconcile_receipts(rows, receipts, **scope)
    blockers.update(result["blockers"])
    if not active:
        blockers.add("MISSING_EFFECTIVE_INVOICE")
    confirmed_memos = []
    seen_memos = {}
    conflicts = set()
    for memo in memos:
        if not in_scope(memo) or not _confirmed(memo):
            continue
        memo_id = memo.get("memo_id")
        if not memo_id or not memo.get("confirmed_text"):
            blockers.add("INVALID_CONFIRMED_MEMO")
            continue
        if memo_id in seen_memos:
            if seen_memos[memo_id] != memo:
                blockers.add("MEMO_ID_CONFLICT")
            continue
        seen_memos[memo_id] = memo
        confirmed_memos.append(deepcopy(memo))
        targets = [item for item in result["items"] if item.get("transaction_id") == memo.get("transaction_id")]
        if len(targets) != 1:
            blockers.add("UNLINKED_CONFIRMED_MEMO")
            continue
        item = targets[0]
        item.setdefault("memos", []).append(deepcopy(memo))
        # No NLP adjudication: an opposing claim is an explicit adapter field.
        claim = memo.get("supplier_claim_qty")
        opposed = (claim is not None and item["received_qty"] is not None
                   and memo.get("unit") == item.get("unit")
                   and _quantity(claim) != _quantity(item["received_qty"]))
        if (opposed or memo.get("conflicting") is True) and item["line_id"] not in agreed_lines:
            conflicts.add(item["line_id"])
    if conflicts:
        blockers.add("CONFLICTING_CLAIM")
    for item in result["items"]:
        item.setdefault("memos", [])
        item["fact_status"] = ("CONFLICTING" if item["line_id"] in conflicts else
                               "OWNER_CONFIRMED" if item["received_qty"] is not None else "UNCONFIRMED")
        refs = item.get("evidence_refs") or []
        if not all(_evidence([ref for ref in refs if ref.get("kind") == kind])
                   for kind in ("DELIVERY", "INVOICE", "RECEIPT")):
            blockers.add("MISSING_SOURCE_EVIDENCE")
        if (item.get("declared_qty") is None or item.get("billed_qty") is None):
            blockers.add("MISSING_DOCUMENT_QUANTITY")
        elif (_quantity(item["declared_qty"]) != _quantity(item["billed_qty"])
              and item["line_id"] not in agreed_lines):
            blockers.add("UNRESOLVED_DECLARED_QUANTITY")
        blockers.update(item.get("blockers") or [])
        if item["check_amount"] is None:
            blockers.add("UNCONFIRMED_AMOUNT")
    amount = result["summary"]["billed_amount"]
    if amount is None:
        blockers.add("MISSING_BILLED_AMOUNT")
    ready = bool(result["items"]) and result["match_result"] == "MATCHED" and not blockers
    result.update(
        input_revision=input_revision, period_start=period_start, period_end=period_end,
        fact_status=("CONFLICTING" if conflicts else "OWNER_CONFIRMED"
                     if result["summary"]["received_qty"] is not None else "UNCONFIRMED"),
        work_status=("OPEN" if ready else "NEEDS_INFO" if result["match_result"] == "MISSING_DATA" else "WAITING_REPLY"),
        blockers=sorted(blockers), can_approve=ready, approvable_amount=amount if ready else None,
        approved_amount=None, payment_status="UNCONFIRMED", memos=confirmed_memos,
        invoice_history=history, active_invoice_ids=list(active), pending_invoices=pending,
        excluded_invoices=excluded, approval_history=deepcopy(approvals or []),
    )
    current_approvals = [approval for approval in approvals or [] if in_scope(approval)
                         and approval.get("input_revision") == input_revision]
    if current_approvals:
        if (len(current_approvals) == 1 and ready and current_approvals[0].get("approval_id")
                and type(current_approvals[0].get("approved_amount")) is int
                and current_approvals[0]["approved_amount"] == amount):
            result.update(work_status="APPROVED", can_approve=False,
                          approved_amount=amount, approval_id=current_approvals[0]["approval_id"])
        else:
            result["blockers"] = sorted(set(result["blockers"]) | {"APPROVAL_SNAPSHOT_CONFLICT"})
            result.update(can_approve=False, approvable_amount=None)
    return result


def _billing_rows(deliveries, invoices, blockers):
    rows = {row["line_id"]: {**deepcopy(row), "billed_qty": None, "unit_price": None,
                             "billed_amount": None} for row in deliveries}
    assigned = set()
    for invoice in invoices:
        for bill in invoice["lines"]:
            target = bill.get("target_delivery_line_id")
            if target not in rows or target in assigned:
                blockers.add("AMBIGUOUS_BILLING_LINE")
                continue
            assigned.add(target)
            row = rows[target]
            if type(bill.get("billed_amount")) is not int or bill["billed_amount"] < 0:
                blockers.add("INVALID_BILLED_AMOUNT")
            if bill.get("unit") != row.get("unit"):
                blockers.add("UNCONFIRMED_INVOICE_UNIT")
                continue
            row.update(billed_qty=bill.get("billed_qty"), unit_price=bill.get("unit_price"),
                       billed_amount=bill.get("billed_amount"))
            row["evidence_refs"] = list(row.get("evidence_refs") or []) + deepcopy(invoice["evidence_refs"])
    if len(assigned) != len(rows):
        blockers.add("MISSING_BILLING_LINE")
    return list(rows.values())


class ApprovalBlocked(ValueError):
    def __init__(self, code, status_code, result=None):
        super().__init__(code)
        self.code, self.status_code, self.result = code, status_code, result


def validate_approval(*, expected_revision: int, **period_input) -> dict[str, Any]:
    """Call under the server's period lock using one freshly loaded DB revision.

    Returns eligibility only; never saves an approval or sends a payment.
    The server must atomically store its snapshot/key and handle successful retries.
    """
    if type(expected_revision) is not int or expected_revision != period_input.get("input_revision"):
        raise ApprovalBlocked("STALE_REVISION", 409)
    result = reconcile_period(**period_input)
    if not result["can_approve"]:
        raise ApprovalBlocked("BLOCKED", 422, result)
    return result
