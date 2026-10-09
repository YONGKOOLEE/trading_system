"""게이트가 판정에 쓰는 시장·계좌 사실의 값 객체 (01 §2.2~§2.4·§2.8, 02 ENT-02·03·04·22, 03 §6.2).

값을 어디서 가져오는지(시세 공급자·달력 파일·브로커 조회)는 다른 패키지가 정한다 (T-008·T-010·T-012).
여기서는 형식과 계산 규칙만 정한다. 금액·가격·수량은 Decimal, 시각은 시간대 있는 값이다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from .clock import require_aware


def _dec(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name}은 Decimal이어야 한다 (float 금지)")
    if not value.is_finite():
        raise ValueError(f"{name}이 유한한 값이 아니다")
    return value


class InstrumentKind(StrEnum):
    STOCK = "STOCK"
    ETF = "ETF"
    ETN = "ETN"


@dataclass(frozen=True)
class InstrumentInfo:
    """종목 속성 (02 ENT-02). 레버리지 배수는 부호 포함 (인버스 2배 = -2)."""

    instrument: str
    kind: InstrumentKind
    leverage: Decimal
    lot_size: Decimal

    def __post_init__(self) -> None:
        _dec("leverage", self.leverage)
        _dec("lot_size", self.lot_size)
        if self.leverage == 0:
            raise ValueError("레버리지 배수는 0일 수 없다")
        if self.lot_size <= 0:
            raise ValueError("수량 단위는 0보다 커야 한다")

    @property
    def exposure_multiplier(self) -> Decimal:
        """노출 = 평가액 × |배수| (01 L9)."""
        return abs(self.leverage)

    @property
    def is_leveraged(self) -> bool:
        """L10 대상: 레버리지·인버스 상품 (배수 ≠ 1)."""
        return self.leverage != 1


@dataclass(frozen=True)
class Quote:
    """최근 시세. 상·하한가·기준가(가격제한폭 기준 = 전일 종가)는 모르면 None.

    base_price는 상·하한가를 모를 때 위험 축소 지정가의 범위 검사에만 쓴다 (CP-032 D-174)."""

    instrument: str
    price: Decimal
    at: datetime
    upper_limit: Decimal | None = None
    lower_limit: Decimal | None = None
    base_price: Decimal | None = None

    def __post_init__(self) -> None:
        _dec("price", self.price)
        require_aware(self.at)
        for n in ("upper_limit", "lower_limit", "base_price"):
            v = getattr(self, n)
            if v is not None:
                _dec(n, v)


class TradingStatus(StrEnum):
    """01 §2.8 종목 거래 상태 (02 ENT-03)."""

    HALTED = "HALTED"                  # 거래정지
    VI = "VI"                          # 변동성 완화장치 발동
    UPPER_LIMIT = "UPPER_LIMIT"        # 상한가
    LOWER_LIMIT = "LOWER_LIMIT"        # 하한가
    CAUTION = "CAUTION"                # 관리종목·투자경고·투자위험
    DELISTING = "DELISTING"            # 상장폐지 예정·정리매매
    ETN_REDEMPTION = "ETN_REDEMPTION"  # ETN 조기상환·상장폐지 예정


# 감액·청산을 막는 상태 (01 §2.8: 거래정지는 시장이 받지 않음, VI는 해제까지 거부)
BLOCKS_RISK_REDUCTION = frozenset({TradingStatus.HALTED, TradingStatus.VI})


@dataclass(frozen=True)
class SessionInfo:
    """한 거래일의 정규장 (02 ENT-04). 휴장이면 open/close가 None."""

    is_open: bool
    regular_open: datetime | None = None
    regular_close: datetime | None = None

    def __post_init__(self) -> None:
        if self.is_open:
            if self.regular_open is None or self.regular_close is None:
                raise ValueError("개장일에는 정규장 시작·종료가 있어야 한다")
            require_aware(self.regular_open)
            require_aware(self.regular_close)
            if not self.regular_open < self.regular_close:
                raise ValueError("정규장 시작이 종료보다 빨라야 한다")

    def in_regular(self, at: datetime) -> bool:
        if not self.is_open or self.regular_open is None or self.regular_close is None:
            return False
        return self.regular_open <= at < self.regular_close


@dataclass(frozen=True)
class CostRates:
    """비용 추정 요율 (03 D-065). 값은 설정에서 온다. 모의·실전 실제 요율은 미확인(K-U9)."""

    commission_rate: Decimal  # 매수·매도 공통 수수료율
    sell_tax_rate: Decimal    # 매도 시 세금 요율

    def __post_init__(self) -> None:
        for n in ("commission_rate", "sell_tax_rate"):
            if _dec(n, getattr(self, n)) < 0:
                raise ValueError(f"{n}은 0 이상이어야 한다")


def estimate_cost(amount: Decimal, is_sell: bool, rates: CostRates) -> Decimal:
    """주문 금액의 추정 비용. 원 미만 올림 (03 D-065, 02 §2.2)."""
    _dec("amount", amount)
    rate = rates.commission_rate + (rates.sell_tax_rate if is_sell else Decimal("0"))
    return (amount * rate).to_integral_value(rounding=ROUND_CEILING)


def price_limits(ref: Decimal, ratio: Decimal, tick: Callable[[Decimal], Decimal]) -> tuple[Decimal, Decimal]:
    """상한가 = 기준가×(1+비율)을 호가 단위로 내림, 하한가 = 기준가×(1−비율)을 올림 (계산값 그대로 비교하지 않음).

    호가 단위는 계산값이 속한 가격대 기준(tick(계산값)). 가격대 경계에서 내림 결과가 아래 가격대로 넘어가는 경우의
    세부 규칙은 공식 근거 미확인(X-U1). 백테스트 상·하한가(04 §2.2)와 게이트의 기준가 범위 검사(CP-032 D-174)가 같이 쓴다."""
    raw_u, raw_l = ref * (1 + ratio), ref * (1 - ratio)
    tu, tl = tick(raw_u), tick(raw_l)
    upper = (raw_u / tu).to_integral_value(rounding=ROUND_FLOOR) * tu
    lower = (raw_l / tl).to_integral_value(rounding=ROUND_CEILING) * tl
    return upper, lower


@dataclass(frozen=True)
class DailyRisk:
    """일일 리스크 상태 (02 ENT-22, 01 §2.4).

    - base_amount: 당일 시작 순자산 (일일 기준 시각의 예수금 + 보유 평가액). 장중 고정.
    - realized: 당일 실현 손익, unrealized_change: 미실현 손익의 당일 변화, costs: 당일 비용(추정+확정).
    - evaluable: 평가 가격이 신선하지 않으면 False → 위험 확대 거부 (01 §2.4).
    """

    base_amount: Decimal
    realized: Decimal
    unrealized_change: Decimal
    costs: Decimal
    evaluable: bool = True

    def __post_init__(self) -> None:
        for n in ("base_amount", "realized", "unrealized_change", "costs"):
            _dec(n, getattr(self, n))
        if self.base_amount <= 0:
            raise ValueError("기준 금액은 0보다 커야 한다")
        if self.costs < 0:
            raise ValueError("비용은 0 이상이어야 한다")

    @property
    def pnl(self) -> Decimal:
        """당일 손익 = 실현 + 미실현 변화 − 비용."""
        return self.realized + self.unrealized_change - self.costs

    @property
    def loss_ratio(self) -> Decimal:
        """당일 손실률 (손실이면 양수, 이익이면 0). L4와 비교한다."""
        return max(Decimal("0"), -self.pnl) / self.base_amount


@dataclass(frozen=True)
class CashView:
    """주문 가능 현금의 두 출처 (03 §6.2)."""

    deposit: Decimal           # 내부 기준 예수금
    broker_orderable: Decimal  # 브로커가 알려 준 주문가능현금 (키움 kt00001 ord_alow_amt)
    # 관찰 전용 (필드명, 값 문자열): 브로커 응답의 현금 필드 원값. 게이트·현금 계산에 쓰지 않는다 (DP-31 재결정 입력)
    observed: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _dec("deposit", self.deposit)
        _dec("broker_orderable", self.broker_orderable)


@dataclass(frozen=True)
class GateSettings:
    """게이트 동작 값의 설정 원본 (config/market.<mode>.toml, 03 D-095·D-065). 앱이 GatePolicy로 바꾼다."""

    quote_max_age_seconds: int
    price_limit_ratio: Decimal
    cost_rates: CostRates

    def __post_init__(self) -> None:
        if isinstance(self.quote_max_age_seconds, bool) or not isinstance(self.quote_max_age_seconds, int)                 or self.quote_max_age_seconds <= 0:
            raise ValueError("quote_max_age_seconds는 0보다 큰 정수")
        if _dec("price_limit_ratio", self.price_limit_ratio) <= 0:
            raise ValueError("price_limit_ratio는 0보다 커야 한다")
