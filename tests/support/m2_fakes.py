"""M2(크립토 현물) 시나리오용 가상 환경 (CP-024 T-M2-01). 모든 값은 가상이다.

- 가상 심볼 TSTA-USDT·TSTB-USDT·TSTC-USDT (실제 심볼 아님), 가상 계좌 BN-BT-01, 가상 기준 금액 10,000 USDT.
- 거래 규칙(tickSize·stepSize·minNotional)은 가상값이다. 바이낸스 실제 규칙값은 미확인(BN B-U4)이라 쓰지 않는다.
  게이트는 아직 stepSize(수량 단위)·tickSize(호가 단위)만 검사한다. minNotional 등 필터 사전 검사는 T-M2-03(D-106).
- 24/7 세션: 매일 한 구간. 주문 허용 창(L8)·일일 기준 시각은 아직 KST 기준 검사(T-M2-02에서 시장별 분리)라
  여기서는 시험 시각을 덮는 자리표시 값을 쓴다. 일일 기준 시각 09:00 KST = UTC 00:00 (A-055).
- 한도는 시나리오 가상값이다. M2 PAPER 값(A-064)은 설정 파일(config/m2/, T-M2-02·T-M2-08)로 들어간다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from tests.support.gate_fakes import FakeAccount, FakeClockHealth, FakeLimits, FakeStrategies
from trading_system.broker.fake.broker import FakeBroker, FakeSpotFees
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import (
    CashView, CostRates, DailyRisk, InstrumentInfo, InstrumentKind, Quote, SessionInfo, TradingStatus,
)
from trading_system.domain.models import Account
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs, GatePolicy
from trading_system.halt.manager import HaltManager
from trading_system.storage.repository import Repository

ACCOUNT = "BN-BT-01"
QUOTE_ASSET = "USDT"
BASE_AMOUNT = Decimal("10000")          # 가상 기준 금액 (USDT)
FEE_RATE = Decimal("0.001")             # 가상 수수료율 0.1% (실제 테스트넷 요율 미확인 B-U10)
BUY_BUFFER = Decimal("0.005")           # 매수 가격 완충 0.5% (A-064, D-104)
T0 = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)  # UTC 01:00 = KST 10:00


@dataclass(frozen=True)
class SpotRule:
    """가상 심볼 규칙 (BN §6 필터 이름만 빌림, 값은 가상)."""

    base: str
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    last: Decimal


RULES: dict[str, SpotRule] = {
    "TSTA-USDT": SpotRule("TSTA", Decimal("0.01"), Decimal("0.0001"), Decimal("5"), Decimal("10000.00")),
    "TSTB-USDT": SpotRule("TSTB", Decimal("0.0001"), Decimal("0.1"), Decimal("5"), Decimal("2.5000")),
    "TSTC-USDT": SpotRule("TSTC", Decimal("0.0001"), Decimal("0.1"), Decimal("5"), Decimal("2.5000")),
}
PAIRS: dict[str, tuple[str, str]] = {sym: (r.base, QUOTE_ASSET) for sym, r in RULES.items()}

M2_POLICY = GatePolicy(
    quote_max_age=timedelta(seconds=60),   # 시세 신선도 60초 (A-064)
    price_limit_ratio=Decimal("0.05"),     # M2에는 가격제한폭·시장가 매수가 없다(D-104). 시장가 매도는 G7 대상 아님 → 쓰이지 않는 자리 값
    cost_rates=CostRates(commission_rate=FEE_RATE, sell_tax_rate=Decimal("0")),
)


def asset_pair(instrument: str) -> tuple[str, str] | None:
    return PAIRS.get(instrument)


def ioc_buy_price(last: Decimal, tick: Decimal, buffer: Decimal = BUY_BUFFER) -> Decimal:
    """IOC 지정가 매수 가격 = 최근가 × (1 + 완충), tickSize로 내림 (D-104). 시험 전용 — 제품 코드는 T-M2-03."""
    return (last * (1 + buffer) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def m2_limits(**over: object) -> AccountLimits:
    """가상 한도: L1 20%, L2 20%, L3 50%, L4 2%, L5 10건, L6 5개, L7 5,000 USDT (기준 10,000 USDT)."""
    values: dict[str, object] = dict(
        l1_max_loss_ratio=Decimal("0.20"), l2_max_symbol_ratio=Decimal("0.20"), l3_max_total_ratio=Decimal("0.50"),
        l4_daily_loss_ratio=Decimal("0.02"), l5_daily_new_orders=10, l6_max_positions=5,
        l7_max_order_amount=Decimal("5000"), l10_leverage_total_ratio=None, warn_usage_ratio=Decimal("0.80"),
        # 자리표시: 게이트 L8 검사가 아직 KST 시각 비교(T-M2-02 전) — 시험 시각(KST 10:00 전후)을 덮는다
        order_window_start=time(0, 0), order_window_end=time(23, 59),
        daily_reference_time=time(9, 0),   # KST 09:00 = UTC 00:00 (A-055)
        report_time=time(9, 10),
    )
    values.update(over)
    return AccountLimits(**values)  # type: ignore[arg-type]


@dataclass
class SpotMarket:
    """가상 현물 시세·규칙·24/7 세션 (gate/ports.MarketFacts 구현)."""

    now: Callable[[], datetime]
    prices: dict[str, Decimal] = field(default_factory=lambda: {s: r.last for s, r in RULES.items()})
    quote_age: dict[str, timedelta] = field(default_factory=dict)

    def quote(self, instrument: str) -> Quote | None:
        if instrument not in self.prices:
            return None
        return Quote(instrument, self.prices[instrument], self.now() - self.quote_age.get(instrument, timedelta(0)))

    def instrument(self, instrument: str) -> InstrumentInfo | None:
        rule = RULES.get(instrument)
        if rule is None:
            return None
        # 임시: 크립토 현물 상품 유형은 T-M2-03(02 ENT-02)에서 추가한다. InstrumentKind 열거는 M1의 ticks.py·app_file.py가
        # 전체 순회하므로 여기서 늘리지 않는다. 수량 단위(lot_size) = stepSize(가상)
        return InstrumentInfo(instrument, InstrumentKind.STOCK, Decimal("1"), rule.step_size)

    def trading_status(self, instrument: str) -> frozenset[TradingStatus] | None:
        return frozenset() if instrument in RULES else None

    def session(self, trade_date: date) -> SessionInfo:
        # 24/7: 하루 전체 한 구간 (게이트가 KST 날짜로 묻는다 — 시장별 달력은 T-M2-02)
        start = datetime.combine(trade_date, time(0, 0), tzinfo=KST)
        return SessionInfo(True, start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC))

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        rule = RULES.get(instrument)
        return None if rule is None else price % rule.tick_size == 0


@dataclass
class M2Env:
    repo: Repository
    clock: FixedClock
    broker: FakeBroker
    gate: Gate
    executor: Executor
    halts: HaltManager
    kill_file: Path
    market: SpotMarket
    account: FakeAccount


def build_m2_env(tmp_path: Path, broker_env: str = "fake") -> M2Env:
    repo = Repository(tmp_path / "trading.db", Mode.BACKTEST)
    repo.register_account(Account(alias=ACCOUNT, mode=Mode.BACKTEST, broker_env=broker_env))
    clock = FixedClock(T0)
    broker = FakeBroker()
    broker.spot_fees = FakeSpotFees(rate=FEE_RATE, pairs=PAIRS)
    market = SpotMarket(now=clock.now)
    account = FakeAccount(
        daily={ACCOUNT: DailyRisk(BASE_AMOUNT, Decimal("0"), Decimal("0"), Decimal("0"))},
        cashes={ACCOUNT: CashView(BASE_AMOUNT, BASE_AMOUNT)},
    )
    limits = FakeLimits({ACCOUNT: m2_limits()})
    inputs = GateInputs(limits, market, account, FakeStrategies(), FakeClockHealth())
    gate = Gate(repo, clock, inputs, M2_POLICY)
    kill_file = tmp_path / "KILL"
    executor = Executor(repo, broker, clock, kill_file, asset_pair=asset_pair)
    halts = HaltManager(repo, gate, executor, clock,
                        reference_time_of=lambda a: (lim.daily_reference_time
                                                     if (lim := limits.by_account.get(a)) else None))
    return M2Env(repo, clock, broker, gate, executor, halts, kill_file, market, account)
