"""체결 수수료의 포지션 반영 규칙 (A-062③, CP-024 D-101). 순수 함수, Decimal만.

- 수수료 정보가 없으면(M1 키움: 수수료는 현금에서 차감, 통보에 수수료 자산 없음) 포지션 변화 = ±체결 수량. M1 동작 그대로.
- 매수 수수료 자산 = 기초 자산 → 포지션 += 체결 수량 − 수수료 (순 수령).
- 매수 수수료 자산 = 호가 자산(USDT) → 포지션 += 체결 수량 (수수료는 현금 흐름, T-M2-06·07).
- 매도 수수료 자산 = 호가 자산 → 포지션 −= 체결 수량.
- 그 밖(기초·호가 외 자산, 매도의 기초 자산 수수료, 음수·체결 수량 이상의 수수료, 종목의 자산 쌍 모름) → None.
  호출자는 None을 "반영할 수 없는 체결"로 보고 원장을 바꾸지 않은 채 K4(사람 확인)로 보낸다 (D-101, 불확실하면 주문하지 않음).
  매도의 기초 자산 수수료는 승인 설계(D-101)에 정의가 없어 보수적으로 None이다.
"""

from __future__ import annotations

from decimal import Decimal

_ZERO = Decimal("0")


def position_delta(side: str, quantity: Decimal, commission: Decimal | None, commission_asset: str | None,
                   pair: tuple[str, str] | None) -> Decimal | None:
    """체결 1건이 포지션(기초 자산 수량)을 바꾸는 양(부호 포함). 반영할 수 없으면 None.

    side: "BUY" / "SELL". pair: (기초 자산, 호가 자산). commission·commission_asset은 함께 있거나 함께 없어야 한다.
    """
    sign = Decimal("1") if side == "BUY" else Decimal("-1")
    if commission is None and commission_asset is None:
        return sign * quantity
    if commission is None or commission_asset is None or pair is None:
        return None
    if not commission.is_finite() or commission < 0:
        return None
    base, quote = pair
    if commission_asset == quote:
        return sign * quantity
    if commission_asset == base and side == "BUY" and commission < quantity:
        return quantity - commission
    return None
