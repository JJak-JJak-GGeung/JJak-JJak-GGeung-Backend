"""Quantity and amount comparison for a single billing line."""

from decimal import Decimal


def reconcile_single_line(
    billed_qty: str, received_qty: str | None, unit_price: str | None
) -> dict[str, str | int | None]:
    """Compare one line in a common unit; approval requires settlement evidence."""
    if received_qty is None:
        return {
            "received_qty": None,
            "delta_qty": None,
            "check_amount": None,
            "match_result": "MISSING_DATA",
            "can_approve": False,
        }

    delta = Decimal(billed_qty) - Decimal(received_qty)
    amount = None if unit_price is None else delta * Decimal(unit_price)
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
