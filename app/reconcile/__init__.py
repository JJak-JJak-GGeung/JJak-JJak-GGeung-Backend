"""Quantity and amount comparison for a single billing line."""

from decimal import Decimal
from typing import Any


def _quantity(value: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Quantity and unit price must be decimal strings")
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise ValueError("Quantity and unit price must be finite and nonnegative")
    return number


def reconcile_single_line(
    billed_qty: str | None, received_qty: str | None, unit_price: str | None,
    *, units_comparable: bool = True,
) -> dict[str, str | int | None]:
    """Inputs must already share a confirmed unit (including price per unit).

    Pass units_comparable=False if that precondition cannot be established.
    Approval requires settlement evidence beyond this single-line calculation.
    """
    billed = None if billed_qty is None else _quantity(billed_qty)
    received = None if received_qty is None else _quantity(received_qty)
    price = None if unit_price is None else _quantity(unit_price)
    if billed is None or received is None or not units_comparable:
        return {
            "received_qty": received_qty,
            "delta_qty": None,
            "check_amount": None,
            "match_result": "MISSING_DATA",
            "can_approve": False,
        }

    delta = billed - received
    amount = None if price is None else delta * price
    return {
        "received_qty": received_qty,
        "delta_qty": format(delta, "f"),
        "check_amount": (
            int(amount) if amount is not None and amount == amount.to_integral_value()
            else None
        ),
        "match_result": "MATCHED" if delta == 0 else "MISMATCH",
        "can_approve": False,
    }


def reconcile_lines(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """A2 calculation for already linked rows in one store/supplier/period.

    Each row supplies billed_qty, received_qty, unit_price, unit and explicit
    units_comparable. The caller normalizes quantities and price to that unit
    only with confirmed evidence. IDs, raw units and evidence_refs pass through.
    This is a calculation fragment, not the full settlement/approval response.
    """
    items = []
    for line in lines:
        result = reconcile_single_line(
            line["billed_qty"], line["received_qty"], line["unit_price"],
            units_comparable=(line.get("units_comparable") is True
                              and bool(line.get("unit"))),
        )
        items.append({**line, **result})

    common_unit = (items[0].get("unit") if items and all(
        item.get("unit") == items[0].get("unit") for item in items
    ) else None)
    complete = bool(common_unit) and all(
        item["delta_qty"] is not None for item in items
    )

    def total(field: str, *, money: bool = False) -> str | int | None:
        values = [item.get(field) for item in items]
        if not items or any(value is None for value in values):
            return None
        if money and any(type(value) is not int for value in values):
            raise ValueError("Won amounts must be integers")
        amount = sum((Decimal(str(value)) for value in values), Decimal(0))
        return int(amount) if money else format(amount, "f")

    return {
        "items": items,
        "summary": {
            "declared_qty": total("declared_qty") if common_unit else None,
            "billed_qty": total("billed_qty") if common_unit else None,
            "received_qty": total("received_qty") if complete else None,
            "unit": common_unit,
            "delta_qty": total("delta_qty") if complete else None,
            "check_amount": total("check_amount", money=True) if complete else None,
            "billed_amount": total("billed_amount", money=True),
        },
        "confirmation_scope": (
            "RECEIPTS_INCLUDED" if any(
                item["received_qty"] is not None for item in items
            ) else "DOCUMENTS_ONLY"
        ),
        "match_result": (
            "MISSING_DATA" if not complete else
            "MISMATCH" if any(item["match_result"] == "MISMATCH" for item in items)
            else "MATCHED"
        ),
        # shortcut: A2 has no settlement evidence validation; add it in C2/D1.
        "can_approve": False,
        "approvable_amount": None,
        "approved_amount": None,
    }
