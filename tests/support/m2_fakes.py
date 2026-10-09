"""M2(크립토 현물) 시나리오용 가상 환경 (CP-024 T-M2-01). 모든 값은 가상이다.

- 가상 심볼 TSTA-USDT·TSTB-USDT·TSTC-USDT (실제 심볼 아님), 가상 계좌 BN-BT-01, 가상 기준 금액 10,000 USDT.
- 거래 규칙(tickSize·stepSize·minNotional)은 가상값이다. 바이낸스 실제 규칙값은 미확인(BN B-U4)이라 쓰지 않는다.
  게이트 G10은 SymbolRules(PRICE_FILTER·LOT_SIZE·MARKET_LOT_SIZE·NOTIONAL) 사전 검사를 한다(T-M2-03, D-106).
- 24/7 세션: 매일 UTC 00:00~24:00 한 구간(session_247). 게이트는 M2_TIME(UTC)으로 L8 창·L5 일일 집계를 판정한다
  (T-M2-02, D-108). 시험 시각 값은 A-064와 같은 UTC 00:10~23:50 창, 일일 기준 UTC 00:00.
- IOC: build_m2_env(ioc=True)이면 fake 브로커가 지정가를 IOC로 처리한다(D-104). 기본 False(T-M2-01 시험은 미체결 예약 합산에 ACCEPT_ONLY를 씀).
- 한도는 시나리오 가상값이다. M2 PAPER 값(A-064)은 설정 파일(config/m2/, T-M2-02·T-M2-08)로 들어간다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from tests.support.gate_fakes import FakeAccount, FakeClockHealth, FakeLimits, FakeStrategies
from trading_system.broker.fake.broker import FakeBroker, FakeSpotFees
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import (
    CashView, CostRates, DailyRisk, InstrumentInfo, InstrumentKind, Quote, SessionInfo, TradingStatus,
)
from trading_system.domain.market_time import M2_TIME, session_247
from trading_system.domain.trading_rules import SpotOrderPolicy, SymbolRules
from trading_system.domain.trading_rules import ioc_buy_price as _ioc_buy_price
from trading_system.domain.models import Account
from trading_system.execution.dust import DustHandler
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs, GatePolicy
from trading_system.halt.manager import HaltManager
from trading_system.monitoring.spot_cash import SpotAccountFacts
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import Repository

ACCOUNT = "BN-BT-01"
QUOTE_ASSET = "USDT"
BASE_AMOUNT = Decimal("10000")          # 가상 기준 금액 (USDT)
FEE_RATE = Decimal("0.001")             # 가상 수수료율 0.1% (실제 테스트넷 요율 미확인 B-U10)
BUY_BUFFER = Decimal("0.005")           # 매수 가격 완충 0.5% (A-064, D-104)
T0 = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)  # UTC 01:00 = KST 10:00
QUOTE_QUANTUM = Decimal("0.00000001")   # 가상 USDT 금액 자릿수 (D-118 비용 추정 올림 단위)
CASH_MAX_AGE = timedelta(minutes=3)     # 가상 거래소 잔고 조회 신선도 (T-M2-07 시험값)


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
    # 비용 추정 = quoteAssetPrecision 자릿수 올림(D-118). 자릿수 8은 가상값(실제 USDT quoteAssetPrecision은 exchangeInfo 적재, T-M2-11)
    cost_rates=CostRates(commission_rate=FEE_RATE, sell_tax_rate=Decimal("0"), cost_quantum=QUOTE_QUANTUM),
    market_time=M2_TIME,                   # L8·L5·달력 = UTC (D-108)
)
# 매수 완충 0.5%(A-064), 지연 매도 완충 0.5%(A-065 Q4), 시세 신선도 60초(A-064)
M2_ORDER_POLICY = SpotOrderPolicy(buy_buffer=BUY_BUFFER, sell_buffer=Decimal("0.005"),
                                  quote_max_age=timedelta(seconds=60))


def asset_pair(instrument: str) -> tuple[str, str] | None:
    return PAIRS.get(instrument)


def symbol_rules(instrument: str) -> SymbolRules:
    """가상 SpotRule → SymbolRules. 최소 수량 = stepSize, 시장가 수량 규칙 = 지정가와 같음, maxNotional 없음."""
    r = RULES[instrument]
    return SymbolRules(instrument=instrument, tick_size=r.tick_size, min_price=r.tick_size,
                       max_price=Decimal("1000000"), lot_min_qty=r.step_size, lot_max_qty=Decimal("9000000"),
                       lot_step=r.step_size, market_min_qty=r.step_size, market_max_qty=Decimal("9000000"),
                       market_step=r.step_size, min_notional=r.min_notional, max_notional=None,
                       apply_min_to_market=True, apply_max_to_market=False)


def ioc_buy_price(last: Decimal, tick: Decimal, buffer: Decimal = BUY_BUFFER) -> Decimal:
    """IOC 지정가 매수 가격 (D-104) — 제품 규칙(domain.trading_rules.ioc_buy_price)에 위임. tickSize로 내림."""
    return _ioc_buy_price(last, replace(symbol_rules("TSTA-USDT"), tick_size=tick), buffer)


def m2_limits(**over: object) -> AccountLimits:
    """가상 한도: L1 20%, L2 20%, L3 50%, L4 2%, L5 10건, L6 5개, L7 5,000 USDT (기준 10,000 USDT)."""
    values: dict[str, object] = dict(
        l1_max_loss_ratio=Decimal("0.20"), l2_max_symbol_ratio=Decimal("0.20"), l3_max_total_ratio=Decimal("0.50"),
        l4_daily_loss_ratio=Decimal("0.02"), l5_daily_new_orders=10, l6_max_positions=5,
        l7_max_order_amount=Decimal("5000"), l10_leverage_total_ratio=None, warn_usage_ratio=Decimal("0.80"),
        # UTC (A-064 값과 같은 형태): 주문 창 00:10~23:50, 일일 기준 00:00, 보고 00:10
        order_window_start=time(0, 10), order_window_end=time(23, 50),
        daily_reference_time=time(0, 0),
        report_time=time(0, 10),
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
        return session_247(trade_date)  # 24/7: UTC 날짜 하루 전체 한 구간 (D-108)

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        rule = RULES.get(instrument)
        return None if rule is None else price % rule.tick_size == 0


@dataclass
class SpotRulesFake:
    """가상 거래 규칙 공급자 (gate/ports.SymbolRulesSource). table에서 빼면 "규칙 미적재"(V-134)."""

    market: SpotMarket
    table: dict[str, SymbolRules] = field(default_factory=lambda: {s: symbol_rules(s) for s in RULES})
    # 최근가 외 참조가(avgPrice·referencePrice 가상값, T-M2-04 dust 경계 시험)
    extra_prices: dict[str, tuple[Decimal, ...]] = field(default_factory=dict)

    def rules(self, instrument: str) -> SymbolRules | None:
        return self.table.get(instrument)

    def market_prices(self, instrument: str) -> tuple[Decimal, ...]:
        q = self.market.quote(instrument)
        return (() if q is None else (q.price,)) + self.extra_prices.get(instrument, ())


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
    account: FakeAccount | SpotAccountFacts
    rules: SpotRulesFake
    dust: DustHandler


def build_m2_env(tmp_path: Path, broker_env: str = "fake", *, limits: AccountLimits | None = None,
                 ioc: bool = False, spot_rules: bool = True, spot_cash: bool = False) -> M2Env:
    """spot_cash=True이면 게이트 계좌 사실 = SpotAccountFacts(M2 현금 정책·UTC 일일 리스크, T-M2-07).
    기본 False는 고정값 FakeAccount(T-M2-01~03 시험)."""
    repo = Repository(tmp_path / "trading.db", Mode.BACKTEST)
    repo.register_account(Account(alias=ACCOUNT, mode=Mode.BACKTEST, broker_env=broker_env))
    clock = FixedClock(T0)
    broker = FakeBroker()
    broker.spot_fees = FakeSpotFees(rate=FEE_RATE, pairs=PAIRS)
    broker.ioc_limit = ioc
    market = SpotMarket(now=clock.now)
    rules = SpotRulesFake(market)
    lim = limits or m2_limits()
    account: FakeAccount | SpotAccountFacts
    if spot_cash:
        def price_of(instrument: str) -> Decimal | None:
            q = market.quote(instrument)
            return None if q is None or clock.now() - q.at > M2_POLICY.quote_max_age else q.price

        account = SpotAccountFacts(repo, clock, quote_asset=QUOTE_ASSET, asset_pair=asset_pair, price_of=price_of,
                                   cost_rates=M2_POLICY.cost_rates, reference_time=lim.daily_reference_time,
                                   max_age=CASH_MAX_AGE, market_time=M2_TIME)
    else:
        account = FakeAccount(
            daily={ACCOUNT: DailyRisk(BASE_AMOUNT, Decimal("0"), Decimal("0"), Decimal("0"))},
            cashes={ACCOUNT: CashView(BASE_AMOUNT, BASE_AMOUNT)},
        )
    limits = FakeLimits({ACCOUNT: lim})
    inputs = GateInputs(limits, market, account, FakeStrategies(), FakeClockHealth(),
                        spot_rules=rules if spot_rules else None)
    gate = Gate(repo, clock, inputs, M2_POLICY)
    kill_file = tmp_path / "KILL"
    executor = Executor(repo, broker, clock, kill_file, asset_pair=asset_pair)
    halts = HaltManager(repo, gate, executor, clock,
                        reference_time_of=lambda a: (lim.daily_reference_time
                                                     if (lim := limits.by_account.get(a)) else None),
                        market_time=M2_TIME)
    # dust 처리 (D-102, T-M2-04): 청산 전 판정·-1013 거부 후 확정. 규칙·참조가·시세는 위 가상 공급자
    dust = DustHandler(repo, clock, AuditAlertSink(repo, clock, actor="dust"), rules_of=rules.rules,
                       market_prices_of=rules.market_prices, quote_of=market.quote, policy=M2_ORDER_POLICY)
    executor.reject_listeners.append(dust.after_reject)
    return M2Env(repo, clock, broker, gate, executor, halts, kill_file, market, account, rules, dust)
