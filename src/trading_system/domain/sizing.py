"""포지션 크기 산정 S-1~S-4 (00 §2.8). 수량은 수량 단위로 내림, 0이면 신호 없음. Decimal만."""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def _floor_to_lot(quantity: Decimal, lot: Decimal) -> Decimal:
    if lot <= 0:
        raise ValueError("수량 단위는 0보다 커야 한다")
    return (quantity / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def _check(name: str, v: Decimal) -> None:
    if not isinstance(v, Decimal) or not v.is_finite():
        raise TypeError(f"{name}은 유한한 Decimal이어야 한다")


def size_fixed_amount(amount: Decimal, price: Decimal, lot: Decimal = Decimal("1")) -> Decimal:
    """S-1 고정 금액 ÷ 기준 가격."""
    for n, v in (("amount", amount), ("price", price)):
        _check(n, v)
    if price <= 0 or amount <= 0:
        return Decimal("0")
    return _floor_to_lot(amount / price, lot)


def size_ratio(base_amount: Decimal, ratio: Decimal, price: Decimal, lot: Decimal = Decimal("1")) -> Decimal:
    """S-2 기준 금액의 비율 ÷ 기준 가격."""
    for n, v in (("base_amount", base_amount), ("ratio", ratio)):
        _check(n, v)
    return size_fixed_amount(base_amount * ratio, price, lot)


def size_risk(risk_amount: Decimal, entry_price: Decimal, stop_price: Decimal,
              lot: Decimal = Decimal("1")) -> Decimal:
    """S-3 위험 기반: 1회 허용 손실액 ÷ (진입가 − 손절가). 손절가가 진입가 이상이면 0."""
    for n, v in (("risk_amount", risk_amount), ("entry_price", entry_price), ("stop_price", stop_price)):
        _check(n, v)
    per_unit = entry_price - stop_price
    if per_unit <= 0 or risk_amount <= 0:
        return Decimal("0")
    return _floor_to_lot(risk_amount / per_unit, lot)


def size_fixed_quantity(quantity: Decimal, lot: Decimal = Decimal("1")) -> Decimal:
    """S-4 고정 수량."""
    _check("quantity", quantity)
    return _floor_to_lot(quantity, lot) if quantity > 0 else Decimal("0")
