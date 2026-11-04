"""Money rounding, shared by the card processor export and the bank ledger export."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")


def round_money(amount: Decimal) -> Decimal:
    """Round to whole cents."""
    return amount.quantize(CENT, rounding=ROUND_HALF_EVEN)


def line_total(quantity: int, unit_price: Decimal) -> Decimal:
    return round_money(Decimal(quantity) * unit_price)


def order_total(lines: list[tuple[int, Decimal]]) -> Decimal:
    return sum((line_total(q, p) for q, p in lines), Decimal("0"))
