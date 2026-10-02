"""게이트 입력(gate/ports.py)의 테스트용 구현. 모든 값은 가상이다.

실제 구현(시세·달력 T-010, 현금·대조 T-008·T-014, 시계 T-012, 전략 등록 T-007)이 생기기 전까지
게이트 판정 규칙만 검증하는 데 쓴다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from trading_system.domain.clock import KST
from trading_system.domain.enums import RequestType
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import (
    CashView, CostRates, DailyRisk, InstrumentInfo, InstrumentKind, Quote, SessionInfo, TradingStatus,
)
from trading_system.gate.ports import GateInputs, GatePolicy

TEST_POLICY = GatePolicy(
    quote_max_age=timedelta(seconds=60),
    price_limit_ratio=Decimal("0.30"),
    cost_rates=CostRates(commission_rate=Decimal("0"), sell_tax_rate=Decimal("0")),
)


def scenario_limits(**over: object) -> AccountLimits:
    """시나리오용 가상 한도: L1 20%, L2 20%, L3 50%, L4 2%, L5 10건, L6 5개, L7 5,000,000원."""
    values: dict[str, object] = dict(
        l1_max_loss_ratio=Decimal("0.20"), l2_max_symbol_ratio=Decimal("0.20"), l3_max_total_ratio=Decimal("0.50"),
        l4_daily_loss_ratio=Decimal("0.02"), l5_daily_new_orders=10, l6_max_positions=5,
        l7_max_order_amount=Decimal("5000000"), l10_leverage_total_ratio=None, warn_usage_ratio=Decimal("0.80"),
        order_window_start=time(9, 5), order_window_end=time(15, 15),
        daily_reference_time=time(8, 0), report_time=time(16, 0),
    )
    values.update(over)
    return AccountLimits(**values)  # type: ignore[arg-type]


@dataclass
class FakeLimits:
    by_account: dict[str, AccountLimits] = field(default_factory=dict)

    def effective_limits(self, account: str, at: datetime | None = None) -> AccountLimits | None:
        return self.by_account.get(account)


def regular_session(day: date) -> SessionInfo:
    return SessionInfo(True, datetime.combine(day, time(9, 0), tzinfo=KST).astimezone(UTC),
                       datetime.combine(day, time(15, 30), tzinfo=KST).astimezone(UTC))


@dataclass
class FakeMarket:
    """시세·종목·거래 상태·달력·호가 단위. 시세 시각은 `now`를 따라간다 (fresh=True인 종목)."""

    now: Callable[[], datetime]
    prices: dict[str, Decimal] = field(default_factory=dict)
    upper: dict[str, Decimal | None] = field(default_factory=dict)
    lower: dict[str, Decimal | None] = field(default_factory=dict)
    quote_age: dict[str, timedelta] = field(default_factory=dict)
    infos: dict[str, InstrumentInfo] = field(default_factory=dict)
    statuses: dict[str, frozenset[TradingStatus] | None] = field(default_factory=dict)
    sessions: dict[date, SessionInfo | None] = field(default_factory=dict)
    tick: int | None = 5  # 지정가 호가 단위 (가상). None이면 규칙 없음

    def add(self, instrument: str, price: str, leverage: str = "1", kind: InstrumentKind = InstrumentKind.STOCK,
            upper: str | None = None) -> None:
        self.prices[instrument] = Decimal(price)
        self.upper[instrument] = Decimal(upper) if upper else None
        self.infos[instrument] = InstrumentInfo(instrument, kind, Decimal(leverage), Decimal("1"))
        self.statuses[instrument] = frozenset()

    def quote(self, instrument: str) -> Quote | None:
        if instrument not in self.prices:
            return None
        at = self.now() - self.quote_age.get(instrument, timedelta(0))
        return Quote(instrument, self.prices[instrument], at, self.upper.get(instrument), self.lower.get(instrument))

    def instrument(self, instrument: str) -> InstrumentInfo | None:
        return self.infos.get(instrument)

    def trading_status(self, instrument: str) -> frozenset[TradingStatus] | None:
        return self.statuses.get(instrument)

    def session(self, trade_date: date) -> SessionInfo | None:
        if trade_date in self.sessions:
            return self.sessions[trade_date]
        return regular_session(trade_date)

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        return None if self.tick is None else price % self.tick == 0


@dataclass
class FakeAccount:
    daily: dict[str, DailyRisk | None] = field(default_factory=dict)
    cashes: dict[str, CashView | None] = field(default_factory=dict)

    def daily_risk(self, account: str) -> DailyRisk | None:
        return self.daily.get(account)

    def cash(self, account: str) -> CashView | None:
        return self.cashes.get(account)


@dataclass
class FakeStrategies:
    blocked: dict[str, str] = field(default_factory=dict)  # owner → 사유

    def signal_rejection(self, owner: str, request_type: RequestType) -> str | None:
        return self.blocked.get(owner)


@dataclass
class FakeClockHealth:
    ok: bool | None = True

    def clock_ok(self) -> bool | None:
        return self.ok


@dataclass
class Facts:
    limits: FakeLimits
    market: FakeMarket
    account: FakeAccount
    strategies: FakeStrategies
    clock_health: FakeClockHealth

    def inputs(self) -> GateInputs:
        return GateInputs(self.limits, self.market, self.account, self.strategies, self.clock_health)


def make_facts(account: str, now: Callable[[], datetime], base: str = "10000000") -> Facts:
    return Facts(
        limits=FakeLimits({account: scenario_limits()}),
        market=FakeMarket(now=now),
        account=FakeAccount(
            daily={account: DailyRisk(Decimal(base), Decimal("0"), Decimal("0"), Decimal("0"))},
            cashes={account: CashView(Decimal(base), Decimal(base))},
        ),
        strategies=FakeStrategies(),
        clock_health=FakeClockHealth(),
    )
