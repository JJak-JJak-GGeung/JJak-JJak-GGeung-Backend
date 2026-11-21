"""B1 linking of normalized billing/delivery rows and confirmed receipt events."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from . import _quantity, reconcile_lines


def delivery_candidates(lines, source):
    """Find candidates only; this does not confirm or aggregate a source row."""
    target = source.get("target_delivery_line_id")
    number = source.get("delivery_no")
    pool = list(lines)
    if target:
        pool = [line for line in pool if line["line_id"] == target]
    if number:
        pool = [line for line in pool if line.get("delivery_no") == number]
    for key in ("delivery_date", "item", "spec"):
        if source.get(key):
            pool = [line for line in pool if line.get(key) == source[key]]
    if source.get("unit") and (not target and not number or len(pool) > 1):
        pool = [line for line in pool if line.get("unit") == source["unit"]]
    return pool


def reconcile_receipts(
    lines: list[dict[str, Any]], receipts: list[dict[str, Any]],
    *, store_id: str, supplier_id: str, period_id: str,
) -> dict[str, Any]:
    """Return A2 calculations plus receipt_links and blockers.

    The server supplies the full set of current billed delivery rows and
    authenticated confirmation events (including correction predecessors).
    This function neither authenticates users nor extracts invoice documents.
    """
    scope = {"store_id": store_id, "supplier_id": supplier_id, "period_id": period_id}
    if not all(isinstance(value, str) and value.strip() for value in scope.values()):
        raise ValueError("A store, supplier and period are required")
    by_line = {}
    for line in lines:
        line_id = line.get("line_id")
        if not isinstance(line_id, str) or not line_id.strip() or line_id in by_line:
            raise ValueError("Delivery line IDs must be present and unique")
        if any(line.get(key) != value for key, value in scope.items()):
            raise ValueError("Billing rows must belong to the requested scope")
        by_line[line_id] = line

    links = []
    by_receipt = {}
    id_conflicts = set()
    for receipt in receipts:
        event = {"receipt": dict(receipt), "receipt_id": receipt.get("receipt_id"),
                 "target_delivery_line_id": None, "candidates": [],
                 "status": "HELD", "reason": None}
        links.append(event)
        if any(receipt.get(key) != value for key, value in scope.items()):
            event.update(status="OUT_OF_SCOPE", reason="SCOPE_MISMATCH")
            continue
        pool = delivery_candidates(by_line.values(), receipt)
        event["candidates"] = [
            {key: line.get(key) for key in (
                "line_id", "delivery_no", "delivery_date", "item", "spec", "unit",
                "evidence_refs",
            )} for line in pool
        ]
        # Candidate display does not confirm a link or contribute to quantities.
        if not receipt.get("confirmed_by") or not receipt.get("confirmed_at"):
            event.update(status="UNCONFIRMED", reason="RECEIPT_NOT_CONFIRMED")
            continue
        receipt_id = receipt.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            event["reason"] = "MISSING_RECEIPT_ID"
            continue
        if receipt_id in by_receipt:
            previous = by_receipt[receipt_id]
            if receipt == previous["receipt"]:
                event.update(status="DUPLICATE", reason="REPEATED_RECEIPT_ID")
            else:
                id_conflicts.add(receipt_id)
                event["reason"] = "RECEIPT_ID_CONFLICT"
            continue
        by_receipt[receipt_id] = event
        if not receipt.get("target_delivery_line_id") and not receipt.get("delivery_no"):
            if not all(receipt.get(key) for key in ("delivery_date", "item", "spec", "unit")):
                event["reason"] = "INCOMPLETE_MATCH_FIELDS"
                continue
        if len(pool) != 1:
            event["reason"] = "AMBIGUOUS_MATCH" if pool else "NO_MATCH"
            continue
        event["target_delivery_line_id"] = pool[0]["line_id"]
        try:
            _quantity(receipt["received_qty"])
        except (KeyError, ValueError, InvalidOperation):
            event["reason"] = "INVALID_RECEIVED_QTY"
            continue
        event.update(status="LINKED", reason=None)

    invalid_lines = set()
    for event in links:
        if isinstance(event["receipt_id"], str) and event["receipt_id"] in id_conflicts:
            event.update(status="HELD", reason="RECEIPT_ID_CONFLICT")
            invalid_lines.update(candidate["line_id"] for candidate in event["candidates"])

    # A correction replaces its predecessor; branching/cyclic/unlinked chains hold.
    successors = defaultdict(list)
    for receipt_id, event in by_receipt.items():
        previous_id = event["receipt"].get("supersedes_receipt_id")
        if previous_id:
            successors[previous_id].append(receipt_id)
    for receipt_id, event in by_receipt.items():
        previous_id = event["receipt"].get("supersedes_receipt_id")
        if not previous_id:
            continue
        chain = []
        current = receipt_id
        valid = True
        while current:
            if current in chain or current not in by_receipt:
                valid = False
                break
            chain.append(current)
            member = by_receipt[current]
            if (member["status"] != "LINKED"
                    or member["target_delivery_line_id"] != event["target_delivery_line_id"]
                    or len(successors[current]) > 1):
                valid = False
            current = member["receipt"].get("supersedes_receipt_id")
        if not valid:
            for member_id in chain:
                member = by_receipt[member_id]
                member.update(status="HELD", reason="INVALID_RECEIPT_CORRECTION")
                invalid_lines.update(c["line_id"] for c in member["candidates"])
    for previous_id in successors:
        if successors[previous_id] and previous_id in by_receipt and by_receipt[previous_id]["status"] == "LINKED":
            by_receipt[previous_id].update(status="SUPERSEDED", reason="REPLACED_RECEIPT")

    # Source rows are another identity boundary, even if different receipt IDs arrive.
    by_source = defaultdict(list)
    for event in by_receipt.values():
        source = event["receipt"].get("source_line_id")
        if event["status"] == "LINKED" and source:
            by_source[source].append(event)
    for events in by_source.values():
        signatures = {(
            event["target_delivery_line_id"],
            _quantity(event["receipt"]["received_qty"]), event["receipt"].get("unit"),
        ) for event in events}
        if len(signatures) > 1:
            for event in events:
                event.update(status="HELD", reason="SOURCE_LINE_CONFLICT")
                invalid_lines.add(event["target_delivery_line_id"])
        else:
            for event in events[1:]:
                event.update(status="DUPLICATE", reason="REPEATED_SOURCE_LINE")

    grouped = defaultdict(list)
    for event in by_receipt.values():
        target = event["target_delivery_line_id"]
        if event["status"] == "LINKED":
            if not event["receipt"].get("unit") or event["receipt"]["unit"] != by_line[target].get("unit"):
                event.update(status="HELD", reason="UNCONFIRMED_UNIT_CONVERSION")
                invalid_lines.add(target)
            else:
                grouped[target].append(event)
        elif event["status"] == "HELD" and target:
            invalid_lines.add(target)

    calculation_rows = []
    for line_id, line in by_line.items():
        events = grouped[line_id]
        received = None
        if events and line_id not in invalid_lines:
            received = format(sum((_quantity(e["receipt"]["received_qty"]) for e in events), Decimal(0)), "f")
        evidence = list(line.get("evidence_refs") or [])
        for event in events:
            for ref in event["receipt"].get("evidence_refs") or []:
                if ref not in evidence:
                    evidence.append(ref)
        calculation_rows.append({**line, "received_qty": received,
                                 "units_comparable": received is not None,
                                 "receipt_ids": [e["receipt_id"] for e in events],
                                 "evidence_refs": evidence})
    result = reconcile_lines(calculation_rows)
    if any(event["status"] not in ("OUT_OF_SCOPE", "UNCONFIRMED") for event in links):
        result["confirmation_scope"] = "RECEIPTS_INCLUDED"
    blockers = sorted({event["reason"] for event in links if event["status"] == "HELD"})
    if any(item["received_qty"] is None for item in result["items"]):
        blockers.append("MISSING_CONFIRMED_RECEIPT")
    if blockers:
        for field in ("received_qty", "delta_qty", "check_amount"):
            result["summary"][field] = None
        result["match_result"] = "MISSING_DATA"
    if any(item["match_result"] == "MISMATCH" for item in result["items"]):
        blockers.append("QTY_MISMATCH")
    result.update(**scope, receipt_links=links, blockers=sorted(set(blockers)))
    return result
