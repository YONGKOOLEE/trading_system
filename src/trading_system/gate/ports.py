"""게이트가 판정에 쓰는 외부 사실의 입력 계약 (03 §5.1, 01 §2.2).

각 조회는 모르면 None을 돌려준다. 게이트는 None을 "불확실"로 보고 위험 확대를 거부한다 (CLAUDE.md §8-10).
구현은 시세·달력(T-010), 대조·현금(T-008·T-014), 시계 점검(T-012), 전략 등록(T-007)이 맡는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_system.domain.enums import RequestType
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market_time import M1_TIME, MarketTime
from trading_system.domain.trading_rules import SymbolRules
from trading_system.domain.strategy import StrategyLimits
from trading_system.domain.market import (
    CashView, CostRates, DailyRisk, GateSettings, InstrumentInfo, Quote, SessionInfo, TradingStatus,
)


class MarketFacts(Protocol):
    def quote(self, instrument: str) -> Quote | None: ...

    def instrument(self, instrument: str) -> InstrumentInfo | None: ...

    def trading_status(self, instrument: str) -> frozenset[TradingStatus] | None: ...

    def session(self, trade_date: date) -> SessionInfo | None: ...

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        """호가 단위에 맞는 가격인지. 규칙을 모르면 None (T-010 호가 단위 설정)."""
        ...


class SymbolRulesSource(Protocol):
    """현물 심볼 거래 규칙 (M2, CP-024 D-106). M1은 이 입력이 없다(GateInputs.spot_rules = None)."""

    def rules(self, instrument: str) -> SymbolRules | None:
        """적재된 규칙. 없거나 직전 적재가 실패했으면 None → 위험 확대 거부, 위험 축소 허용."""
        ...

    def market_prices(self, instrument: str) -> tuple[Decimal, ...]:
        """시장가 명목가 판정용 참조가들(최근가·avgPrice·referencePrice 중 아는 것). 모르면 빈 튜플."""
        ...


class AccountFacts(Protocol):
    def daily_risk(self, account: str) -> DailyRisk | None: ...

    def cash(self, account: str) -> CashView | None: ...


class LimitsSource(Protocol):
    def effective_limits(self, account: str, at: datetime | None = None) -> AccountLimits | None: ...


class StrategyFacts(Protocol):
    def signal_rejection(self, owner: str, request_type: RequestType) -> str | None:
        """전략 상태가 이 신호를 막으면 사유, 허용이면 None (01 G6, R-20, V-113)."""
        ...

    def strategy_limits(self, owner: str) -> StrategyLimits | None:
        """전략별 한도 (01 §2.3.1-8). 없으면 None — 계좌 한도만 적용."""
        ...


class ClockHealth(Protocol):
    def clock_ok(self) -> bool | None:
        """시계 오차가 허용치 안이면 True, 넘으면 False, 판정 불가면 None (R-24)."""
        ...


@dataclass(frozen=True)
class GatePolicy:
    """게이트 동작 값. 리스크 수치가 아니라 판정 방식 값이다. 기본값은 두지 않는다.

    - quote_max_age: 시세 신선도 기준 (01 G11). 값은 사용자 결정 항목.
    - price_limit_ratio: 상한가를 모를 때 시장가 예약 가격 = 최근가 × (1 + 이 값) (03 §6.2, KRX ±30% 확인).
    - cost_rates: 비용 추정 요율 (03 D-065).
    - market_time: 시장 시각 기준(CP-024 D-108). L8 주문 창·L5 일일 집계·달력 날짜를 이 시각대로 판정한다.
      기본값 M1(KST)은 기존 M1 호출부를 바꾸지 않기 위한 것이다. M2는 M2_TIME(UTC)을 명시한다.
    """

    quote_max_age: timedelta
    price_limit_ratio: Decimal
    cost_rates: CostRates
    market_time: MarketTime = M1_TIME

    @classmethod
    def from_settings(cls, settings: GateSettings) -> GatePolicy:
        """설정 파일 값(config/market.<mode>.toml)에서 만든다."""
        return cls(timedelta(seconds=settings.quote_max_age_seconds), settings.price_limit_ratio, settings.cost_rates)

    def __post_init__(self) -> None:
        if self.quote_max_age <= timedelta(0):
            raise ValueError("quote_max_age는 0보다 커야 한다")
        if not isinstance(self.price_limit_ratio, Decimal) or not self.price_limit_ratio.is_finite() \
                or self.price_limit_ratio <= 0:
            raise ValueError("price_limit_ratio는 0보다 큰 Decimal이어야 한다")


@dataclass(frozen=True)
class GateInputs:
    limits: LimitsSource
    market: MarketFacts
    account: AccountFacts
    strategies: StrategyFacts
    clock_health: ClockHealth
    spot_rules: SymbolRulesSource | None = None  # M2 현물 거래 규칙 (D-106). None = M1 (기존 G10 검사만)
