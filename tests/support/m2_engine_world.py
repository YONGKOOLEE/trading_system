"""M2 엔진 시나리오 세계 (CP-036 T-045 시험용). 실운영과 같은 조립 함수 build_engine(market="M2")을 쓴다(D-202).

fake 현물 브로커(FakeSpotBroker, IOC·수수료 = 받는 자산)·가상 심볼 TSTA-USDT·가상 규칙(tests/support/m2_fakes)·가상 1d 봉·
가상 계좌 BN-BT-01. 네트워크 없음. 시각은 모두 UTC(D-108).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import FEE_RATE, M2_ORDER_POLICY, QUOTE_ASSET, QUOTE_QUANTUM, m2_limits, symbol_rules
from trading_system.app.engine import Engine
from trading_system.app.wiring import SpotInputs, StrategyInput, build_engine
from trading_system.broker.fake.broker import FakeBehavior, FakeSpotFees
from trading_system.broker.fake.spot import FakeSpotBroker
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import CostRates, GateSettings, Quote
from trading_system.domain.models import Signal
from trading_system.domain.trading_rules import SymbolRules
from trading_system.domain.strategy import (
    Bar, EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec,
)
from trading_system.marketdata.spot_facts import SpotFactsStore
from trading_system.storage.repository import OrderIdentity, Repository
from trading_system.strategy.base import ParamValues, make_signal

ACCOUNT = "BN-BT-01"
SYM = "TSTA-USDT"
PAIRS = {SYM: ("TSTA", QUOTE_ASSET)}
DAY = date(2026, 10, 10)   # 토요일 — 24/7 시장은 주말에도 판정한다
BASELINE = Decimal("10000")
SETTINGS = GateSettings(60, Decimal("0.05"), CostRates(FEE_RATE, Decimal("0"), QUOTE_QUANTUM))
# 가상 능력 표: M2 열이 "지원"인 경우를 흉내(실제 docs/capabilities.md의 M2 열은 CP-036 T-049 전까지 미지원)
CAPS_M2 = {cap: {"M1": "미지원", "M2": "지원", "M3": "미지원"}
           for cap in ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-X1")}


def utc(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=UTC)


def m2_bars(first: date, last: date, close: str = "10000") -> list[Bar]:
    """가상 1d 봉 (봉 시작 UTC 날짜). 판정에는 확정 봉(봉 시작 + 1일 ≤ 판정 시각)만 들어간다."""
    out, d, c = [], first, Decimal(close)
    while d <= last:
        out.append(Bar(d, c, c + 100, c - 100, c, Decimal("1000")))
        d += timedelta(days=1)
    return out


def fixed_spec(**over: object) -> StrategySpec:
    spec = StrategySpec(
        strategy_id="t-m2fixed", name="시험 전용 고정 전략(M2)", description="가상 규칙·가상 심볼. 시험 전용",
        version=1, markets=("M2",), universe_method="U-A", universe=(SYM,),
        capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-X1"),
        data_requirements="1d 봉 (UTC, 확정 봉만)", trigger="CAP-TRG-01 배포 설정 판정 시각 (UTC)",
        rules=(RuleSpec("EXIT-1", "EXIT", ("상태 입력",), "CAP-TRG-01", "내 소유 보유", "청산 전량", "데이터 품질 실패", 1),
               RuleSpec("ENTRY-1", "ENTRY", ("상태 입력",), "CAP-TRG-01", "보유 없음", "진입 S-4 qty", "데이터 품질 실패",
                        1)),
        params=(ParamSpec("qty", "decimal", "기초 자산", "진입 수량(소수)", Decimal("0.0001"), Decimal("100")),),
        signal_format="00 §2.5", validity_rule="판정 기준 시각 + 30분", validity_max_minutes=30,
        unfilled_policy="시스템 기본", sizing="S-4", holding="판정 1회", limits=StrategyLimits())
    return replace(spec, **over)  # type: ignore[arg-type]


class FixedM2Strategy:
    """보유(내 소유)가 없으면 진입 qty, 있으면 청산 전량. 판정 시각은 배포 설정(eval_times). 시장가 신호 —
    M2 주문 유형·가격(매수 IOC 지정가, 매도 시장가)은 의도를 만들 때 엔진이 정한다(D-104, CP-036 D-201 (5))."""

    def __init__(self, spec: StrategySpec | None = None) -> None:
        self.spec = spec or fixed_spec()

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        for v in snapshot.instruments:
            if not v.data_ok or v.last_price is None:
                continue
            pos = v.position
            if pos is not None and pos.owned_by_me and pos.quantity > 0:
                out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT, pos.quantity,
                                       OrderType.MARKET, v.last_price, timedelta(minutes=30)))
            elif pos is None:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                       Decimal(str(params["qty"])), OrderType.MARKET, v.last_price,
                                       timedelta(minutes=30)))
        return out


def rules_checked_spec() -> StrategySpec:
    return fixed_spec(strategy_id="t-m2rules", universe=("TSTAUSDT",), params=(
        ParamSpec("qty", "decimal", "기초 자산", "진입 수량(소수)", Decimal("0.00001"), Decimal("100")),
        ParamSpec("qty_step", "decimal", "기초 자산", "수량 단위 (거래소 LOT_SIZE stepSize와 같아야 함)",
                  Decimal("0.00000001"), Decimal("1")),
        ParamSpec("notional", "decimal", "USDT", "1회 매수 금액 (2 × minNotional 이상)", Decimal("1"),
                  Decimal("100000"))))


class RulesCheckedStrategy(FixedM2Strategy):
    """시작 규칙 검사(D-209 B-8) 시험용 — m2-verify 초안의 validate_rules 형식(파라미터, LOT step, minNotional)."""

    def __init__(self) -> None:
        super().__init__(rules_checked_spec())

    @staticmethod
    def validate_rules(params: ParamValues, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        problems = []
        if Decimal(str(params["qty_step"])) != lot_step:
            problems.append(f"qty_step {params['qty_step']} ≠ 거래소 LOT_SIZE stepSize {lot_step}")
        if Decimal(str(params["notional"])) < 2 * min_notional:
            problems.append(f"notional {params['notional']} < 2 × minNotional {min_notional}")
        return problems


class ClockOk:
    def clock_ok(self) -> bool | None:
        return True


class M2World:
    """한 계좌·한 심볼. at()으로 시계를 옮기고 정상 시세(수신 시각 = 지금)를 다시 넣는다."""

    def __init__(self, tmp: Path, *, eval_times: tuple[time, ...] = (time(1, 0), time(4, 0)), qty: str = "0.0100",
                 day: date = DAY, limits: AccountLimits | None = None, rules: bool = True,
                 behavior: FakeBehavior = FakeBehavior.FILL,
                 strategies: Sequence[StrategyInput] | None = None, symbol_rule: SymbolRules | None = None,
                 price: str = "10000.00", sym: str = SYM, base_asset: str = "TSTA",
                 caps: dict | None = None) -> None:
        """sym·base_asset·caps: 기본은 가상 심볼 TSTA-USDT·가상 능력 표. 실제 심볼 이름(예: BTCUSDT, T-M2-15 시험)과
        실제 docs/capabilities.md 표로도 같은 조립을 쓸 수 있게 둔다(가격·잔고는 여전히 가상값)."""
        self.tmp = tmp
        self.db = tmp / "trading.db"
        self.day = day
        self.sym = sym
        self.pairs = PAIRS if (sym, base_asset) == (SYM, "TSTA") else {sym: (base_asset, QUOTE_ASSET)}
        self.caps = caps if caps is not None else CAPS_M2
        self.clock = FixedClock(utc(day, 0, 30))
        self.broker = FakeSpotBroker(now=self.clock.now, behavior=behavior)
        self.broker.grant = {QUOTE_ASSET: BASELINE}
        self.broker.spot_fees = FakeSpotFees(rate=FEE_RATE, pairs=self.pairs)
        self.broker.ioc_limit = True
        self.facts = SpotFactsStore(now=self.clock.now, universe=(sym,))
        if rules:
            self.facts.load_rules({sym: symbol_rule or symbol_rules(sym)})
        self.facts.set_daily_bars(sym, m2_bars(day - timedelta(days=60), day + timedelta(days=10)))
        self.price = Decimal(price)
        self.limits = limits or m2_limits()
        self.strategies = list(strategies) if strategies is not None else [
            StrategyInput(FixedM2Strategy(), {"qty": qty}, eval_times)]
        self.quote()
        self.engine = self.build()

    def build(self) -> Engine:
        self.repo = Repository(self.db, Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
        return build_engine(mode=Mode.BACKTEST, repo=self.repo, clock=self.clock, broker=self.broker, store=None,
                            account=ACCOUNT, account_limits=self.limits, gate_settings=SETTINGS, caps=self.caps,
                            strategies=self.strategies, kill_file=self.tmp / "KILL", clock_health=ClockOk(),
                            alert_thresholds=(10, 5), market="M2",
                            spot=SpotInputs(self.facts, self.pairs, QUOTE_ASSET, M2_ORDER_POLICY,
                                            cash_max_age=timedelta(minutes=3)))

    def set_baseline(self) -> None:
        """기준선 확정(D-103 — 실운영은 사람이 T-049 도구로). 거래소 USDT와 같은 값."""
        self.engine.account_state.set_baseline(ACCOUNT, BASELINE, self.clock.now())  # type: ignore[attr-defined]

    def quote(self, age_seconds: int = 0) -> None:
        self.facts.update_quote(Quote(self.sym, self.price, self.clock.now() - timedelta(seconds=age_seconds)))

    def at(self, hh: int, mm: int, ss: int = 0, *, day: date | None = None, quote: bool = True) -> None:
        self.clock._now = utc(day or self.day, hh, mm, ss)  # noqa: SLF001
        if quote:
            self.quote()

    def tick_at(self, hh: int, mm: int, ss: int = 0, *, day: date | None = None) -> None:
        self.at(hh, mm, ss, day=day)
        self.engine.tick()

    def command(self, cmd: str, args: dict, channel: str = "LOCAL") -> str:
        cid = f"{channel.lower()}-{random.random()}"
        with self.repo.tx():
            self.repo.insert_command(cid, channel, "tester", cmd, args, self.clock.now())
        return cid

    def start_and_resume(self) -> None:
        self.engine.start()
        self.command("RESUME", {"account": None, "halt_type": "K8"})
        self.engine.tick()
        assert "K8" not in self.repo.active_halts(ACCOUNT), self.repo.halt_rows(ACCOUNT)

    def restart(self) -> None:
        """프로세스 재시작 흉내: 같은 DB·브로커·시세 공급으로 새 엔진(메모리 상태 없음)."""
        self.repo.close()
        self.engine = self.build()

    def sent(self, kind: str = "NEW") -> list:
        return [r for r in self.broker.sent if r.kind == kind]

    def audits(self, kind: str) -> list[dict]:
        return [e for e in self.repo.audit_events() if e["kind"] == kind]

    def intents(self) -> list[dict]:
        return self.repo.select("SELECT intent_id, state, request_type, side, instrument, quantity, idem_key, owner "
                                "FROM intent ORDER BY created_seq")

    def close(self) -> None:
        assert check_invariants(self.repo.connection, Mode.BACKTEST.value) == []
        self.repo.close()
