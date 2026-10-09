"""T-027 지정가 능력 (CP-032 단계 1, A-085: D-173~D-176, V-235~V-240).

오프라인·fake·백테스트만. 가격·종목·달력은 가상이다(호가 표 config/ticks/krx.toml만 실제 KRX 표, krx-market §2.1).
키움 모의 서버·네트워크 사용 없음.
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_t003_gate import op, rejected, run, set_limits, sig
from tests.support.gate_fakes import TEST_POLICY, scenario_limits
from tests.support.invariants import check_invariants
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, CrossStrategy, cross_spec
from trading_system.app.backtest import render_report, run_backtest
from trading_system.app.main import _load_common
from trading_system.app.wiring import StrategyInput, build_engine
from trading_system.broker.fake.broker import FakeBehavior, FakeBroker
from trading_system.broker.fake.fill_model import BacktestBroker, BarFacts
from trading_system.config.capabilities import load_capabilities
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import (
    Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.market import CashView, CostRates, InstrumentInfo, InstrumentKind, Quote
from trading_system.domain.models import GateRequest, OrderRequest, Signal
from trading_system.domain.strategy import EvaluationSnapshot, ParamSpec
from trading_system.execution.resolver import ResolveOutcome, UnknownResolver
from trading_system.gate.gate import Gate
from trading_system.marketdata.errors import MarketDataError
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.marketdata.ticks import load_ticks, parse_ticks
from trading_system.reporting.paper_review import _ticks
from trading_system.storage.repository import Repository
from trading_system.strategy.base import ParamValues, make_signal

REPO = Path(__file__).resolve().parents[2]
KRX = REPO / "config" / "ticks" / "krx.toml"
STOCK, ETF, ETN = InstrumentKind.STOCK, InstrumentKind.ETF, InstrumentKind.ETN


# --- V-235 호가 표 파일 (D-173) ---------------------------------------------------------------------

@pytest.mark.parametrize(("kind", "price", "tick"), [
    (STOCK, "1", "1"), (STOCK, "1999", "1"), (STOCK, "2000", "5"), (STOCK, "4995", "5"), (STOCK, "5000", "10"),
    (STOCK, "19990", "10"), (STOCK, "20000", "50"), (STOCK, "49950", "50"), (STOCK, "50000", "100"),
    (STOCK, "199900", "100"), (STOCK, "200000", "500"), (STOCK, "499500", "500"), (STOCK, "500000", "1000"),
    (STOCK, "3000000", "1000"),
    (ETF, "1999", "1"), (ETF, "2000", "5"), (ETF, "150000", "5"),
    (ETN, "1999", "1"), (ETN, "2000", "5"),
])
def test_v235_krx_table_matches_krx_market_2_1(kind: InstrumentKind, price: str, tick: str) -> None:
    """경계 가격은 상위 구간 (krx-market §2.1 "N원 이상 ~ M원 미만")."""
    t = load_ticks(KRX)
    assert t is not None
    assert t.tick_of(kind, Decimal(price)) == Decimal(tick)


def test_v235_krx_file_strings_source_version_and_scope() -> None:
    raw = tomllib.loads(KRX.read_text(encoding="utf-8"))
    assert "RGL03010100T3.jsp" in raw["source"] and "RGL03020100.jsp" in raw["source"]   # X6·X7
    assert "2023-01-25" in raw["version"] and "2026-10-08" in raw["version"]
    assert set(raw["bands"]) == {"STOCK", "ETF", "ETN"}                                    # ELW 없음 (범위 밖)
    for rows in raw["bands"].values():
        for row in rows:
            assert all(isinstance(v, str) for v in row.values()), row                     # 부동소수·정수 금지
    assert [r["below"] for r in raw["bands"]["STOCK"]] == ["2000", "5000", "20000", "50000", "200000", "500000",
                                                           "none"]
    assert [r["tick"] for r in raw["bands"]["STOCK"]] == ["1", "5", "10", "50", "100", "500", "1000"]
    t = parse_ticks(raw)
    assert t.valid(STOCK, Decimal("277500")) is True and t.valid(STOCK, Decimal("277750")) is False   # 250원 배수 거부
    assert t.valid(InstrumentKind.STOCK, Decimal("0")) is False


@pytest.mark.parametrize("breaker", ["float_tick", "int_below", "no_source", "no_version"])
def test_v235_bad_table_refuses_start(tmp_path: Path, breaker: str) -> None:
    text = KRX.read_text(encoding="utf-8")
    bad = {"float_tick": text.replace('tick = "1000"', "tick = 1000.0"),
           "int_below": text.replace('below = "2000"', "below = 2000.5", 1),
           "no_source": "\n".join(ln for ln in text.splitlines() if not ln.startswith("source")),
           "no_version": "\n".join(ln for ln in text.splitlines() if not ln.startswith("version"))}[breaker]
    assert bad != text
    root = _root_copy(tmp_path)
    (root / "config" / "ticks" / "krx.toml").write_text(bad, encoding="utf-8")
    for mode in (Mode.PAPER, Mode.BACKTEST):
        with pytest.raises(MarketDataError):
            _load_common(root, mode)                      # run·backtest·validate 공통 로더 → 시작 거부


def test_v235_common_loader_reads_table_for_paper_and_backtest() -> None:
    for mode in (Mode.PAPER, Mode.BACKTEST):
        ticks = _load_common(REPO, mode)[3]
        assert ticks is not None and ticks == load_ticks(KRX), mode


def _root_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in ("config/market.paper.toml", "config/market.backtest.toml", "docs/capabilities.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    shutil.copytree(REPO / "config" / "calendar", root / "config" / "calendar")
    (root / "config" / "ticks").mkdir(parents=True)
    return root


# --- V-236 G10 지정가 사전 검사 (D-174) --------------------------------------------------------------

def _unknown_band(env: Env, inst: str, base: str | None) -> None:
    env.facts.market.upper[inst] = None
    env.facts.market.lower[inst] = None
    env.facts.market.base[inst] = Decimal(base) if base else None


def _hold(env: Env, inst: str = "TST-B", qty: str = "10") -> None:
    d = run(env, sig(RequestType.ENTRY, inst, qty, ev=f"h-{inst}"))
    assert d.result is GateResult.APPROVED, d.reason
    assert env.repo.position(ACCOUNT, inst).quantity == Decimal(qty)


def test_v236_off_tick_limit_rejected_and_not_sent(env: Env) -> None:
    _hold(env)
    n = len(env.broker.sent)
    assert rejected(run(env, sig(RequestType.ENTRY, order_type=OrderType.LIMIT, price="9997")), Check.G10, "호가")
    assert rejected(run(env, sig(RequestType.EXIT, "TST-B", "10", ev="x", order_type=OrderType.LIMIT,
                                 price="9998")), Check.G10, "호가")              # 위험 축소도 호가 위반은 거부
    _unknown_band(env, "TST-B", None)
    assert rejected(run(env, sig(RequestType.EXIT, "TST-B", "10", ev="x2", order_type=OrderType.LIMIT,
                                 price="9998")), Check.G10, "호가")              # 기준가 모름 허용보다 호가 검사가 먼저
    assert len(env.broker.sent) == n


def test_v236_known_band_outside_rejected_inside_passes(env: Env) -> None:
    env.facts.market.upper["TST-A"] = Decimal("13000")
    env.facts.market.lower["TST-A"] = Decimal("7000")
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-A", "10", order_type=OrderType.LIMIT, price="13005")),
                    Check.G10, "가격제한폭")
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-A", "10", ev="lo", order_type=OrderType.LIMIT,
                                 price="6995")), Check.G10, "가격제한폭")
    assert run(env, sig(RequestType.ENTRY, "TST-A", "10", ev="ok", order_type=OrderType.LIMIT,
                        price="13000")).result is GateResult.APPROVED


@pytest.mark.parametrize(("upper", "lower"), [(None, None), ("13000", None), (None, "7000")])
def test_v236_unknown_band_rejects_risk_increasing_limit(env: Env, upper: str | None, lower: str | None) -> None:
    env.facts.market.upper["TST-A"] = Decimal(upper) if upper else None
    env.facts.market.lower["TST-A"] = Decimal(lower) if lower else None
    env.facts.market.base["TST-A"] = Decimal("10000")          # 기준가가 있어도 위험 확대는 거부
    n = len(env.broker.sent)
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-A", "10", order_type=OrderType.LIMIT, price="10000")),
                    Check.G10, "상·하한가 모름")
    assert len(env.broker.sent) == n
    assert run(env, sig(RequestType.ENTRY, "TST-A", "10", ev="mkt")).result is GateResult.APPROVED  # 시장가는 무관


def test_v236_unknown_band_rejects_increase_and_buy_modify(env: Env) -> None:
    _hold(env, "TST-A", "10")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, "TST-C", "10", ev="open", order_type=OrderType.LIMIT, price="9500"))
    assert d.result is GateResult.APPROVED, d.reason
    key = env.repo.intent(d.approved.intent_id).broker_order_key
    _unknown_band(env, "TST-A", "10000")
    _unknown_band(env, "TST-C", "10000")
    assert rejected(run(env, sig(RequestType.INCREASE, "TST-A", "1", ev="inc", order_type=OrderType.LIMIT,
                                 price="10000")), Check.G10, "상·하한가 모름")
    mod = GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY, account=ACCOUNT,
                      instrument="TST-C", side=Side.BUY, quantity=Decimal("10"), order_type=OrderType.LIMIT,
                      price=Decimal("9600"), idem_key="m-1", owner="t001-fixed:1", target_order_key=key)
    assert rejected(env.gate.decide(mod), Check.G10, "상·하한가 모름")       # 매수 정정 = 위험 확대 (01 D-015)


@pytest.mark.parametrize(("price", "ok"), [
    ("7005", False), ("7010", True),      # 하한 = 10,010 × 0.7 = 7,007 → 호가 5원 올림 7,010
    ("13010", True), ("13015", False),    # 상한 = 10,010 × 1.3 = 13,013 → 호가 5원 내림 13,010
])
def test_v236_unknown_band_risk_reducing_checks_base_band(env: Env, price: str, ok: bool) -> None:
    _hold(env)
    _unknown_band(env, "TST-B", "10010")
    d = run(env, sig(RequestType.EXIT, "TST-B", "10", ev="x", order_type=OrderType.LIMIT, price=price))
    if ok:
        assert d.result is GateResult.APPROVED, d.reason
        assert d.approved.request.price == Decimal(price)       # 게이트는 가격을 고치지 않는다
    else:
        assert rejected(d, Check.G10, "기준가")


def test_v236_unknown_band_and_base_allows_risk_reducing(env: Env) -> None:
    _hold(env)
    _unknown_band(env, "TST-B", None)
    d = env.gate.decide(op(RequestType.REDUCE, "TST-B", "5", key="op-r", order_type=OrderType.LIMIT, price="100"))
    assert d.result is GateResult.APPROVED, d.reason            # 모르면 위험 축소를 미리 막지 않음 (§8-3)
    assert d.approved.request.price == Decimal("100")
    d2 = run(env, sig(RequestType.EXIT, "TST-B", "5", ev="x", order_type=OrderType.LIMIT, price="20000"))
    assert d2.result is GateResult.REJECTED and d2.failed_check is Check.G4   # 같은 방향 처리 중(운영 요청) — G10 아님


def test_v236_known_single_bound_violation_rejected_for_risk_reducing(env: Env) -> None:
    _hold(env)
    env.facts.market.upper["TST-B"] = None
    env.facts.market.lower["TST-B"] = Decimal("7000")            # 아는 한쪽 위반은 확실한 위반 → 거부
    assert rejected(run(env, sig(RequestType.EXIT, "TST-B", "10", ev="x", order_type=OrderType.LIMIT,
                                 price="6995")), Check.G10, "가격제한폭")
    assert run(env, sig(RequestType.EXIT, "TST-B", "10", ev="x2", order_type=OrderType.LIMIT,
                        price="7000")).result is GateResult.APPROVED


# --- V-237 지정가 예약 (D-175, 03 §6.2) ---------------------------------------------------------------

def _cost_gate(env: Env) -> Gate:
    return Gate(env.repo, env.clock, env.facts.inputs(),
                replace(TEST_POLICY, cost_rates=CostRates(Decimal("0.001"), Decimal("0"))))


def test_v237_reservation_is_order_price_plus_cost_partial_fill_moves_cancel_releases(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = _cost_gate(env).decide(sig(RequestType.ENTRY, "TST-A", "100", order_type=OrderType.LIMIT, price="9500"))
    assert d.result is GateResult.APPROVED, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert (rsv.amount, rsv.cash_amount) == (Decimal("950000"), Decimal("950950"))   # 주문가 × 수량 (+ 비용 0.1%)
    [no] = env.broker.open_order_nos()
    env.broker.fill(no, Decimal("40"))
    env.executor.process_events()
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert rsv.consumed == Decimal("380000") and rsv.remaining == Decimal("570000")     # 체결분 이동
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("40")
    key = env.repo.intent(d.approved.intent_id).broker_order_key
    c = env.gate.decide(GateRequest.cancel(RequestSource.EXEC_POLICY, ACCOUNT, "TST-A", Side.BUY, key,
                                           idem_key="c-1", owner="t001-fixed:1"))
    assert c.result is GateResult.APPROVED, c.reason
    env.executor.submit(c.approved)
    env.executor.process_events()
    assert env.repo.active_reservations(ACCOUNT) == []                                # 남은 예약 해제
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("40")


def test_v237_close_expiry_releases_reservation(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, "TST-A", "100", order_type=OrderType.LIMIT, price="9500"))
    assert env.repo.active_reservations(ACCOUNT)
    env.broker.expire_open()
    env.executor.process_events()
    assert env.repo.active_reservations(ACCOUNT) == []
    assert env.repo.intent(d.approved.intent_id).state is IntentState.EXPIRED


def test_v237_two_limit_buys_over_l3_second_rejected(env: Env) -> None:
    set_limits(env, l3_max_total_ratio=Decimal("0.30"))         # 3,000,000
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    a = env.gate.decide(sig(RequestType.ENTRY, "TST-A", "190", ev="a", order_type=OrderType.LIMIT, price="9500"))
    b = env.gate.decide(sig(RequestType.ENTRY, "TST-B", "190", ev="b", order_type=OrderType.LIMIT, price="9500"))
    assert a.result is GateResult.APPROVED                       # 1,805,000 예약 (아직 전송 전)
    assert rejected(b, Check.G7, "L3")                           # 1,805,000 + 1,805,000 > 3,000,000
    env.executor.submit(a.approved)
    env.executor.process_events()
    assert len(env.repo.active_reservations(ACCOUNT)) == 1


# --- V-238 미체결 처리·CAP 판정 (D-175) --------------------------------------------------------------

def _caps(**rows: str) -> dict[str, dict[str, str]]:
    caps = {k: dict(v) for k, v in CAPS_ALL_SUPPORTED.items()}
    for cap, m1 in rows.items():
        caps[cap.replace("_", "-")] = {"M1": m1, "M2": "미지원", "M3": "미지원"}
    return caps


FUTURE = dict(CAP_ORD_03a="미지원", CAP_ORD_03b="검증 중")      # §3.1 반영 후 형식 (03a·03b 행 분리)
_BASE_CAPS = ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01")


def _spec03(cap: str, policy: str = "미체결 10분 후 취소", **over: object):  # type: ignore[no-untyped-def]
    return cross_spec(capabilities=_BASE_CAPS + ("CAP-ORD-02", cap), unfilled_policy=policy, **over)


def test_v238_ord03_requires_03a_and_is_rejected() -> None:
    for caps in (_caps(**FUTURE), CAPS_ALL_SUPPORTED):          # 새 형식 표 / 기존 표(CAP-ORD-03 한 행, "지원")
        problems = validate_spec(_spec03("CAP-ORD-03"), caps, scenario_limits(), Mode.BACKTEST)
        assert any("CAP-ORD-03a" in p and "정정" in p for p in problems), problems
    problems = validate_spec(_spec03("CAP-ORD-03a"), _caps(**FUTURE), scenario_limits(), Mode.BACKTEST)
    assert any("CAP-ORD-03a" in p for p in problems), problems


def test_v238_ord03b_only_is_registered_by_table_status() -> None:
    assert validate_spec(_spec03("CAP-ORD-03b"), _caps(**FUTURE), scenario_limits(), Mode.BACKTEST) == []
    paper = validate_spec(_spec03("CAP-ORD-03b"), _caps(**FUTURE), scenario_limits(), Mode.PAPER)
    assert any("CAP-ORD-03b" in p and "검증 중" in p for p in paper), paper   # PAPER는 "지원"만 (DP-1)
    # 기존 표 호환(03b 행 없음 → CAP-ORD-03 행 상태 적용)
    old = {k: v for k, v in load_capabilities(REPO / "docs" / "capabilities.md").items()
           if k not in ("CAP-ORD-03a", "CAP-ORD-03b")}
    problems = validate_spec(_spec03("CAP-ORD-03b"), old, scenario_limits(), Mode.BACKTEST)
    assert problems == [f"CAP-ORD-03b: M1에서 {old['CAP-ORD-03']['M1']} — BACKTEST 등록 불가 (D-004, CP로 기능 추가)"]
    # 실제 표 (CP-032 §3.1 반영 2026-10-09, A-085 — 시험 변경 사유): 03b M1 "검증 중" → BACKTEST 허용, PAPER 거부
    real = load_capabilities(REPO / "docs" / "capabilities.md")
    assert real["CAP-ORD-03b"]["M1"] == "검증 중" and real["CAP-ORD-03a"]["M1"] == "미지원"
    assert validate_spec(_spec03("CAP-ORD-03b"), real, scenario_limits(), Mode.BACKTEST) == []
    assert validate_spec(_spec03("CAP-ORD-03b"), real, scenario_limits(), Mode.PAPER) != []
    assert validate_spec(_spec03("CAP-ORD-03"), real, scenario_limits(), Mode.BACKTEST) != []


def test_v238_ord03b_needs_unfilled_policy_and_unknown_cap_rejected() -> None:
    problems = validate_spec(_spec03("CAP-ORD-03b", policy="시스템 기본"), _caps(**FUTURE), scenario_limits(),
                             Mode.BACKTEST)
    assert any("미체결 정책" in p for p in problems), problems
    caps = {k: v for k, v in CAPS_ALL_SUPPORTED.items() if k != "CAP-ORD-03"}   # 03 계열 행이 하나도 없음
    problems = validate_spec(_spec03("CAP-ORD-03b"), caps, scenario_limits(), Mode.BACKTEST)
    assert any("CAP-ORD-03b: 정의되지 않은 능력" in p for p in problems), problems


# 엔진 수준: 실제 조립(build_engine) + 실제 호가 표 + fake 브로커

CAL_DAY = date(2026, 10, 1)
_UNFILLED = ParamSpec("unfilled_max_minutes", "int", "분", "미체결 취소 시간", Decimal("1"), Decimal("600"))


def _kst(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(CAL_DAY.year, CAL_DAY.month, CAL_DAY.day, hh, mm, ss, tzinfo=KST)


class LimitCross(CrossStrategy):
    """진입을 지정가(그날 판정 시점 종가 = 호가 표 배수)로 내는 시험용 전략. 청산은 시장가."""

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out = []
        for s in super().evaluate(snapshot, params):
            if s.kind is RequestType.ENTRY:
                s = make_signal(self.spec, snapshot, s.instrument, s.rule_id, s.kind, s.quantity, OrderType.LIMIT,
                                s.price, timedelta(minutes=30))
            out.append(s)
        return out


class _World:
    def __init__(self, tmp: Path, cap: str | None) -> None:
        from tests.scenario.test_t020_app import CAL, SETTINGS, ClockOk
        self.clock = FixedClock(_kst(8, 59))
        self.broker = FakeBroker(FakeBehavior.ACCEPT_ONLY)
        self.broker.cash[ACCOUNT] = CashView(Decimal("10000000"), Decimal("10000000"))
        self.repo = Repository(tmp / "trading.db", Mode.BACKTEST)
        self.store = MarketDataStore(CAL, load_ticks(KRX))
        for inst in ("TST-A", "TST-B", "TST-C", "TST-D", "TST-E"):
            self.store.register(InstrumentInfo(inst, InstrumentKind.STOCK, Decimal("1"), Decimal("1")))
            self.store.update_status(inst, frozenset())
        rows, d = [], CAL_DAY - timedelta(days=1)
        while len(rows) < 60:
            if CAL.is_trading_day(d):
                rows.append(RawDaily(d, Decimal("10000"), Decimal("10100"), Decimal("9900"), Decimal("10000"),
                                     Decimal("1000")))
            d -= timedelta(days=1)
        self.store.update_daily("TST-A", sorted(rows, key=lambda r: r.day))
        self.quote()
        caps = _BASE_CAPS + ("CAP-ORD-02",) + ((cap,) if cap else ())
        spec = cross_spec(version=3, capabilities=caps, params=cross_spec().params + (_UNFILLED,),
                          unfilled_policy="미체결 10분 후 취소" if cap else "시스템 기본")
        params = {**PARAMS, "unfilled_max_minutes": 10}
        self.engine = build_engine(mode=Mode.BACKTEST, repo=self.repo, clock=self.clock, broker=self.broker,
                                   store=self.store, account=ACCOUNT, account_limits=scenario_limits(),
                                   gate_settings=SETTINGS, caps=_caps(**FUTURE), kill_file=tmp / "KILL",
                                   clock_health=ClockOk(),
                                   strategies=[StrategyInput(LimitCross(spec), params, (time(9, 5),))])

    def quote(self) -> None:
        self.store.update_quote(Quote("TST-A", Decimal("10000"), self.clock.now(), upper_limit=Decimal("13000"),
                                      lower_limit=Decimal("7000")))

    def at(self, hh: int, mm: int, ss: int = 0) -> None:
        self.clock._now = _kst(hh, mm, ss)  # noqa: SLF001
        self.quote()
        self.engine.tick()

    def start(self) -> None:
        assert self.engine.start().ok
        with self.repo.tx():
            self.repo.insert_command("local-k8", "LOCAL", "tester", "RESUME", {"account": None, "halt_type": "K8"},
                                     self.clock.now())
        self.engine.tick()

    def intents(self) -> list[dict]:
        return self.repo.select("SELECT intent_id, state, request_type, order_type, price FROM intent "
                                "ORDER BY created_seq")

    def close(self) -> None:
        try:
            assert check_invariants(self.repo.connection, Mode.BACKTEST.value) == []
        finally:
            self.repo.close()


def test_v238_ord03b_cancels_stale_limit_through_gate(tmp_path: Path) -> None:
    w = _World(tmp_path, "CAP-ORD-03b")
    try:
        assert w.engine.strategies[0].unfilled_minutes == 10            # 03b 선언 + 파라미터 → 배선이 켬
        w.start()
        w.at(9, 5)
        [entry] = w.intents()
        assert (entry["request_type"], entry["order_type"], entry["price"]) == ("ENTRY", "LIMIT", "10000")
        w.at(9, 14)
        assert len(w.intents()) == 1                                    # 10분 전에는 취소 없음
        w.at(9, 15, 1)
        w.engine.tick()
        its = w.intents()
        assert [i["request_type"] for i in its] == ["ENTRY", "CANCEL"]
        dec = w.repo.select("SELECT source, result FROM gate_decision WHERE request_type='CANCEL'")
        assert dec == [{"source": "EXEC_POLICY", "result": "APPROVED"}]   # 취소 = 위험 축소, 게이트 경유
        assert w.repo.intent(entry["intent_id"]).state is IntentState.CANCELLED
        assert w.repo.active_reservations(ACCOUNT) == []
        assert [r.kind for r in w.broker.sent] == ["NEW", "CANCEL"]
    finally:
        w.close()


def test_v238_undeclared_strategy_keeps_order_until_close(tmp_path: Path) -> None:
    w = _World(tmp_path, None)
    try:
        assert w.engine.strategies[0].unfilled_minutes is None          # 파라미터가 있어도 미선언이면 꺼짐
        w.start()
        w.at(9, 5)
        w.at(11, 0)
        w.engine.tick()
        assert [i["request_type"] for i in w.intents()] == ["ENTRY"]    # 시스템 기본: 당일 유지 (자동 취소 없음)
        assert len(w.repo.active_reservations(ACCOUNT)) == 1
    finally:
        w.close()


def test_v238_undeclared_close_reconcile_expires_limit(env: Env) -> None:
    """시스템 기본 = 마감 대조에서 만료 종결 (2-M1.4, 키움 만료 통보 없음 K-U3 가정)."""
    from trading_system.ops.reconcile import ReconcileResolver
    from trading_system.reconciliation.reconciler import ItemKind, Reconciler
    from trading_system.storage.alerts import AuditAlertSink
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, "TST-A", "10", order_type=OrderType.LIMIT, price="9500"))
    env.broker.expire_open()
    env.broker.drop_events()                                   # 만료 통보 없음
    Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock)).run(ACCOUNT, "CLOSE")
    [item] = [i for i in env.repo.open_recon_items(ACCOUNT) if i["kind"] == ItemKind.STATE_MISMATCH]
    assert item["broker_value"] == "EXPIRED"
    ReconcileResolver(env.repo, env.executor, env.clock).resolve(item["item_id"], "CLOSE_ORDER", "u")
    assert env.repo.intent(d.approved.intent_id).state is IntentState.EXPIRED
    assert env.repo.active_reservations(ACCOUNT) == []


def test_v238_cancel_response_lost_unknown_no_resend_then_k3(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, "TST-A", "10", order_type=OrderType.LIMIT, price="9500"))
    key = env.repo.intent(d.approved.intent_id).broker_order_key
    env.broker.behavior = FakeBehavior.UNKNOWN                 # 취소 응답 없음
    c = env.gate.decide(GateRequest.cancel(RequestSource.EXEC_POLICY, ACCOUNT, "TST-A", Side.BUY, key,
                                           idem_key="c-1", owner="t001-fixed:1"))
    assert c.result is GateResult.APPROVED, c.reason
    env.executor.submit(c.approved)
    env.executor.process_events()
    assert env.repo.intent(c.approved.intent_id).state is IntentState.UNKNOWN
    assert len(env.broker.sent) == 2
    r = UnknownResolver(env.repo, env.broker, env.executor, env.clock,
                        lambda s: env.clock.advance(timedelta(seconds=s)))
    assert r.resolve(c.approved.intent_id) is ResolveOutcome.AMBIGUOUS
    assert env.repo.intent(c.approved.intent_id).state is IntentState.UNKNOWN
    assert HaltType.K3.value in env.repo.active_halts(ACCOUNT)
    assert len(env.broker.sent) == 2                           # 재전송 0 (§8-7)
    again = env.gate.decide(GateRequest.cancel(RequestSource.EXEC_POLICY, ACCOUNT, "TST-A", Side.BUY, key,
                                               idem_key="c-2", owner="t001-fixed:1"))
    assert again.approved is None                              # 결과 불명 취소가 있는 동안 같은 주문 취소 재요청 거부


# --- V-239 백테스트 지정가 (04 §2.2, P-5) ----------------------------------------------------------

BAR = BarFacts(Decimal("10000"), Decimal("10200"), Decimal("9900"), Decimal("1000"))


@pytest.mark.parametrize(("side", "limit", "bar", "fill"), [
    ("BUY", "9950", BAR, "9950"),                    # 관통 → 지정가
    ("BUY", "9900", BAR, None),                      # 저가 = 지정가 (접촉만) → 미체결
    ("BUY", "10100", BarFacts(Decimal("9800"), Decimal("9900"), Decimal("9700"), Decimal("1000")), "10100"),  # 갭 하락
    ("SELL", "10150", BAR, "10150"),
    ("SELL", "10200", BAR, None),
    ("SELL", "9500", BarFacts(Decimal("9800"), Decimal("9900"), Decimal("9700"), Decimal("1000")), "9500"),   # 갭: 시가 유리
])
def test_v239_limit_fill_rule(side: str, limit: str, bar: BarFacts, fill: str | None) -> None:
    b = BacktestBroker(lambda i, p: Decimal("10"))
    b.place_order(OrderRequest("NEW", "A", "TST-A", side, Decimal("10"), OrderType.LIMIT, Decimal(limit)))
    b.match_bar("TST-A", bar, datetime(2026, 1, 5, 0, 0, tzinfo=KST))
    ev = b.poll_events()
    assert [e.price for e in ev] == ([Decimal(fill)] if fill else [])


def _limit_cfg(tmp_path: Path, run_id: str, **over: object):  # type: ignore[no-untyped-def]
    from tests.scenario.test_t009_backtest import config
    spec = cross_spec(capabilities=_BASE_CAPS + ("CAP-ORD-02",))
    return config(tmp_path, run_id, strategy=LimitCross(spec), ticks=load_ticks(KRX), **over)


def test_v239_backtest_limit_entries_pass_g10_and_fill_at_limit(tmp_path: Path) -> None:
    r = run_backtest(_limit_cfg(tmp_path, "lim"))
    assert "G10" not in r.rejects, r.rejects                     # 백테스트 시세에 상·하한가·기준가가 있다
    repo = Repository(r.db_path, Mode.BACKTEST)
    try:
        rows = repo.select("SELECT i.price AS lim, f.price AS px FROM intent i JOIN broker_order o "
                           "ON o.intent_id=i.intent_id JOIN fill f ON f.order_key=o.order_key "
                           "WHERE i.order_type='LIMIT'")
    finally:
        repo.close()
    assert rows and all(x["lim"] == x["px"] for x in rows), rows   # 체결가 = 지정가


@pytest.mark.parametrize("times", [(time(9, 5),), (time(9, 30), time(14, 30))])
def test_v239_report_states_daily_limit_order_limitation(tmp_path: Path, times: tuple[time, ...]) -> None:
    cfg = _limit_cfg(tmp_path, f"rep{len(times)}", eval_times=times)
    r = run_backtest(cfg)
    text = render_report(cfg, r, settings_hash="h", reproducible=True, lookahead_bad=[])
    limits = text[text.index("## 한계"):]
    assert "일봉 지정가 한계(CP-032 P-5)" in limits and "하루 고가·저가" in limits
    assert limits.count("하루 고가·저가") == 1


# --- V-240 호가 표 도입 회귀 (D-173) ----------------------------------------------------------------

V231_TICK_DIGEST = "c5345ebbbe00d98a5c7f5dccb2f98ee994054a9482d8c2a40ce51d09cbe77c00"


def _scaled(k: str) -> dict[str, list[RawDaily]]:
    from tests.scenario.test_t009_backtest import DATA
    f = Decimal(k)
    return {i: [RawDaily(r.day, r.open * f, r.high * f, r.low * f, r.close * f, r.volume, r.corporate_action)
                for r in rows] for i, rows in DATA.items()}


def _x30(tmp_path: Path, run_id: str, **over: object):  # type: ignore[no-untyped-def]
    """가상 데이터 × 30 (20만 원대, KRX 호가 500원·100원 구간). 기준선도 × 30, 자금 1억 원(L2·L7 안)."""
    from tests.scenario.test_t009_backtest import config
    from trading_system.config.strategy_params import validate_params
    strat = CrossStrategy()
    params = validate_params(strat.spec, {"entry_level": "270000", "exit_level": "240000", "qty": 10})
    return config(tmp_path, run_id, data=_scaled("30"), params=params, initial_cash=Decimal("100000000"), **over)


def test_v240_tick_mode_digest_with_krx_table(tmp_path: Path) -> None:
    """가상 데이터(5,000~20,000원대)는 KRX 호가 10원 = 기존 slippage_tick 10원이라 지문 불변.
    가격대가 다르면(×30: 20만 원대 500원 단위) 틱 방식 결과가 바뀐다 → 새 지문을 고정 기록(CP-032 D-173 변경 사유)."""
    from tests.scenario.test_t009_backtest import config
    krx = load_ticks(KRX)
    assert run_backtest(config(tmp_path, "none")).digest == V231_TICK_DIGEST
    assert run_backtest(config(tmp_path, "krx", ticks=krx)).digest == V231_TICK_DIGEST
    hi_none = run_backtest(_x30(tmp_path, "hi-none"))
    hi_krx = run_backtest(_x30(tmp_path, "hi-krx", ticks=krx))
    assert hi_none.fills and hi_none.trades
    assert hi_none.digest != hi_krx.digest
    assert hi_krx.digest == KRX_X30_DIGEST, hi_krx.digest
    assert hi_none.signals == hi_krx.signals                    # 신호(판정)는 같고 체결가만 다름
    assert [f[:5] for f in hi_none.fills] == [f[:5] for f in hi_krx.fills]


KRX_X30_DIGEST = "5679323e6ad3849d8d93c74eb79d21cae2f1932ea171852900d7b6fdd98dc4e8"   # 2026-10-09 T-027 기록


def test_v240_pct_mode_only_unit_alignment_changes(tmp_path: Path) -> None:
    """CP-030 % 방식: 호가 표가 생겨도 판정·체결 수량·날짜는 그대로, 가격은 호가 맞춤만 달라진다."""
    from tests.scenario.test_t009_backtest import config
    krx = load_ticks(KRX)
    a = run_backtest(_x30(tmp_path, "p-none", slippage_pct=Decimal("0.001")))
    b = run_backtest(_x30(tmp_path, "p-krx", ticks=krx, slippage_pct=Decimal("0.001")))
    assert a.fills and a.signals == b.signals
    assert [f[:5] for f in a.fills] == [f[:5] for f in b.fills]
    for fa, fb in zip(a.fills, b.fills, strict=True):
        assert abs(Decimal(fa[5]) - Decimal(fb[5])) < krx.tick_of(STOCK, Decimal(fb[5])), (fa, fb)


@pytest.mark.e2e
def test_v240_paper0_verify_unchanged_by_tick_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PAPER 조립(실제 설정·paper0-verify v1)에서 호가 표 유무와 무관하게 판정·의도·전송이 같다 (시장가 경로)."""
    from tests.scenario import test_e2e_paper0_days as days

    def one_day(sub: str) -> tuple:
        w = days.PaperWorld(tmp_path / sub)
        try:
            days.start_and_resume(w)
            days.run_trading_day(w, days.DAYS[0])
            its = [(i["request_type"], i["state"], i["side"], i["quantity"]) for i in w.intents()]
            decs = [(x["request_type"], x["result"], x["failed_check"]) for x in w.decisions()]
            sent = [(r.kind, r.side, r.order_type, r.price, r.quantity) for r in w.broker.sent]
            return its, decs, sent, w.store.ticks is not None
        finally:
            w.close()

    with_table = one_day("krx")
    monkeypatch.setattr(days, "load_ticks", lambda p: None)
    without = one_day("none")
    assert with_table[3] is True and without[3] is False
    assert with_table[:3] == without[:3]
    assert all(s[2] is OrderType.MARKET for s in with_table[2])


def test_v240_limit_strategy_still_cannot_register_for_paper() -> None:
    """A-085: Q-3 미승인 → 지정가 PAPER 사용 금지. krx.toml이 생겨도 PAPER 등록은 실제 능력 표(capabilities.md, 이 작업에서
    수정하지 않음)가 막는다: CAP-ORD-02는 "지원"이 아니고, PAPER-0 검증 예외는 시장가 능력만 허용한다."""
    real = load_capabilities(REPO / "docs" / "capabilities.md")
    assert real["CAP-ORD-02"]["M1"] != "지원"
    spec = cross_spec(capabilities=_BASE_CAPS + ("CAP-ORD-02",))
    assert any(p.startswith("CAP-ORD-02: M1에서") for p in validate_spec(spec, real, scenario_limits(), Mode.PAPER))
    p0 = validate_spec(replace(spec, sizing="S-4", universe=("005930",)), real, scenario_limits(), Mode.PAPER,
                       verification_approval="A-037")
    assert any("허용되지 않는 능력 CAP-ORD-02" in p for p in p0), p0


def test_v240_store_tick_column_and_half_tick_slippage_is_normal() -> None:
    """krx.toml이 생기면 시세 기록의 호가 단위 칸이 채워진다. 중간가 체결(±0.5틱)은 정상 값으로 표시한다."""
    from tests.scenario.test_t020_app import CAL
    s = MarketDataStore(CAL, load_ticks(KRX))
    s.register(InstrumentInfo("005930", STOCK, Decimal("1"), Decimal("1")))
    assert s.tick_of("005930", Decimal("277750")) == Decimal("500")
    assert s.tick_valid("005930", Decimal("277750")) is False   # 중간가 체결가는 호가 격자 밖 (krx-market §2.2)
    assert _ticks(Decimal("250"), Decimal("500")) == "0.50"
    assert _ticks(Decimal("-250"), Decimal("500")) == "-0.50"
    assert _ticks(Decimal("500"), Decimal("500")) == "1"
    assert _ticks(Decimal("250"), None) == "-"


# --- 운영 요청 지정가 차단 (리드 검토 추가, A-085: Q-3 승인 전 PAPER 지정가 사용 금지) ---------------------

def test_ops_limit_request_refused_until_q3_market_still_goes(env: Env) -> None:
    from tests.scenario.test_t005_halts import _command, _ops
    _hold(env, "TST-A", "50")
    _command(env, "c1", "EXIT", {"account": ACCOUNT, "instrument": "TST-A", "quantity": "50",
                                 "order_type": "LIMIT", "price": "10000"})
    _command(env, "c2", "EXIT", {"account": ACCOUNT, "instrument": "TST-A", "quantity": "50"})
    o1, o2 = _ops(env).process_pending()
    assert o1.status == "REJECTED" and "Q-3" in o1.result
    assert o2.status == "DONE"
    assert not any(a.order_type is OrderType.LIMIT for a in env.broker.sent)
