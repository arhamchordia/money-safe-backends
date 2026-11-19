"""Part 1: money is not a float.

All amounts inside MoneySafe are integers in paise (1 rupee = 100 paise).
Rupee strings from users are parsed with Decimal, never float.

    >>> 0.1 + 0.2 == 0.3
    False
    >>> to_paise("0.10") + to_paise("0.20") == to_paise("0.30")
    True
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

PAISE_PER_RUPEE = 100


class InvalidAmount(ValueError):
    pass


def to_paise(rupees: str | int | Decimal) -> int:
    """Convert a rupee amount to integer paise, rejecting anything ambiguous.

    Floats are refused on purpose: by the time a value is a float, precision
    may already be lost. Pass a string such as "12.50" instead.
    """
    if isinstance(rupees, bool) or isinstance(rupees, float):
        raise InvalidAmount("pass rupees as a string or Decimal, never a float")
    try:
        value = Decimal(str(rupees).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise InvalidAmount(f"not a number: {rupees!r}") from exc
    if not value.is_finite():
        raise InvalidAmount(f"not a finite amount: {rupees!r}")
    paise = value * PAISE_PER_RUPEE
    if paise != paise.to_integral_value():
        raise InvalidAmount(f"more than 2 decimal places: {rupees!r}")
    return int(paise)


def format_inr(paise: int) -> str:
    """Format paise as rupees with Indian digit grouping: 123456789 -> 'Rs 12,34,567.89'."""
    sign = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(paise), PAISE_PER_RUPEE)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}Rs {digits}.{rem:02d}"


def split_fee(amount_paise: int, fee_bps: int) -> tuple[int, int]:
    """Split an amount into (fee, remainder) using basis points (1 bps = 0.01%).

    The fee is rounded half-up to whole paise, and fee + remainder always equals
    the original amount, so no paisa is ever created or lost.
    """
    if amount_paise < 0 or fee_bps < 0:
        raise InvalidAmount("amount and fee must be non-negative")
    fee = (amount_paise * fee_bps + 5_000) // 10_000
    return fee, amount_paise - fee
