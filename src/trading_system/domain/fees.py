"""체결 수수료의 포지션 반영 규칙 (A-062③, CP-024 D-101). 순수 함수, Decimal만.

- 수수료 정보가 없으면(M1 키움: 수수료는 현금에서 차감, 통보에 수수료 자산 없음) 포지션 변화 = ±체결 수량. M1 동작 그대로.
- 매수 수수료 자산 = 기초 자산 → 포지션 += 체결 수량 − 수수료 (순 수령).
- 매수 수수료 자산 = 호가 자산(USDT) → 포지션 += 체결 수량 (수수료는 현금 흐름, T-M2-06·07).
- 매도 수수료 자산 = 호가 자산 → 포지션 −= 체결 수량.
- 그 밖(기초·호가 외 자산, 매도의 기초 자산 수수료, 음수·체결 수량 이상의 수수료, 종목의 자산 쌍 모름) → None.
  호출자는 None을 "반영할 수 없는 체결"로 보고 원장을 바꾸지 않은 채 K4(사람 확인)로 보낸다 (D-101, 불확실하면 주문하지 않음).
  매도의 기초 자산 수수료는 승인 설계(D-101)에 정의가 없어 보수적으로 None이다.

수수료 모델 (CP-040 D-267, T-065) — FeeModel:
- maker_rate·taker_rate(0 이상 1 미만 Decimal 비율)와 rate_source("placeholder" | "account-commission" | "user").
- 비용 추정 요율 = max(maker, taker) (G8 추정 수수료·L4 확정 전 비용·L1 — 보수).
- 체결 요율 = taker: 시스템 주문은 IOC 지정가 매수·시장가 매도뿐이라 체결분은 테이커 [가정 — PAPER isMaker로 확인, V-338].
  maker_rate는 기록만(메이커가 될 수 있는 대기 주문 유형이 없음). BNB 할인은 가정하지 않는다(B-U10).
- 요율 값 자체는 반올림하지 않는다(설정 문자열 그대로). 수수료 금액의 반올림은 사용처 규칙을 따른다(체결 모델 = 반올림 없음,
  비용 추정 = cost_quantum 올림 D-118).
"""

from __future__ import annotations

from dataclasses import dataclass
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


RATE_SOURCES = ("placeholder", "account-commission", "user")


@dataclass(frozen=True)
class FeeModel:
    """메이커·테이커 수수료 요율과 출처 (CP-040 D-267). 실제 계정 요율은 LIVE 준비 때 account/commission(CP-037 D-233)."""

    maker_rate: Decimal
    taker_rate: Decimal
    rate_source: str

    def __post_init__(self) -> None:
        for name in ("maker_rate", "taker_rate"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite() or not _ZERO <= v < 1:
                raise ValueError(f"{name}: 0 이상 1 미만의 Decimal이어야 한다")
        if self.rate_source not in RATE_SOURCES:
            raise ValueError(f"rate_source: {' | '.join(RATE_SOURCES)} 중 하나")

    @property
    def estimate_rate(self) -> Decimal:
        """비용 추정 요율 = max(maker, taker) (D-267 (2))."""
        return max(self.maker_rate, self.taker_rate)

    @property
    def fill_rate(self) -> Decimal:
        """백테스트·검증 체결 요율 = taker (D-267 (3))."""
        return self.taker_rate

    @property
    def is_placeholder(self) -> bool:
        return self.rate_source == "placeholder"
