"""T-020 앱 배선·메인 루프 (fake 브로커, 가상 데이터) — 05-plan T-020, 04 V-56·V-58·V-59·V-60·V-124,
스레드 간 경쟁(통보·운영 명령·판정 동시).
"""

from __future__ import annotations

import random
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.gate_fakes import scenario_limits
from tests.support.invariants import check_invariants
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, CrossStrategy
from trading_system.app.engine import Engine
from trading_system.app.wiring import StrategyInput, build_engine
from trading_system.broker.fake.broker import FakeBehavior, FakeBroker
from trading_system.config.errors import ConfigError
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import IntentState, Mode
from trading_system.domain.market import CashView, CostRates, GateSettings, InstrumentInfo, InstrumentKind, Quote
from trading_system.domain.models import OrderEvent
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.storage.repository import Repository

ACCOUNT = "KR-BT-01"
CAL = KrxCalendar({2026: parse_calendar({"market": "KRX", "year": 2026, "source": "가상", "source_version": "t"}, 2026)})
SETTINGS = GateSettings(60, Decimal("0.3"), CostRates(Decimal("0"), Decimal("0")))


def kst(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=KST).astimezone(UTC)


DAY = date(2026, 10, 1)


class ClockOk:
    def __init__(self) -> None:
        self.ok: bool | None = True

    def clock_ok(self) -> bool | None:
        return self.ok


def make_store(clock: FixedClock) -> MarketDataStore:
    store = MarketDataStore(CAL, None)
    for inst in ("TST-A", "TST-B", "TST-C", "TST-D", "TST-E"):
        store.register(InstrumentInfo(inst, InstrumentKind.STOCK, Decimal("1"), Decimal("1")))
        store.update_status(inst, frozenset())
    rows, d = [], DAY - timedelta(days=1)
    while len(rows) < 60:
        if CAL.is_trading_day(d):
            rows.append(RawDaily(d, Decimal("10000"), Decimal("10100"), Decimal("9900"), Decimal("10000"),
                                 Decimal("1000")))
        d -= timedelta(days=1)
    store.update_daily("TST-A", sorted(rows, key=lambda r: r.day))
    return store


def quote(store: MarketDataStore, clock: FixedClock, inst: str = "TST-A") -> None:
    store.update_quote(Quote(inst, Decimal("10000"), clock.now(), upper_limit=Decimal("10000"),
                             lower_limit=Decimal("7000")))


class World:
    def __init__(self, tmp: Path, behavior: FakeBehavior = FakeBehavior.FILL) -> None:
        self.tmp = tmp
        self.db = tmp / "trading.db"
        self.clock = FixedClock(kst(DAY, 8, 59))
        self.broker = FakeBroker(behavior)
        self.broker.cash[ACCOUNT] = CashView(Decimal("10000000"), Decimal("10000000"))
        self.health = ClockOk()
        self.engine = self.build()

    def build(self) -> Engine:
        self.repo = Repository(self.db, Mode.BACKTEST)
        self.store = make_store(self.clock)
        quote(self.store, self.clock)
        e = build_engine(mode=Mode.BACKTEST, repo=self.repo, clock=self.clock, broker=self.broker, store=self.store,
                         account=ACCOUNT, account_limits=scenario_limits(), gate_settings=SETTINGS,
                         caps=CAPS_ALL_SUPPORTED, kill_file=self.tmp / "KILL", clock_health=self.health,
                         strategies=[StrategyInput(CrossStrategy(), dict(PARAMS), (datetime(2000, 1, 1, 9, 5).time(),))],
                         alert_thresholds=(10, 5))
        return e

    def restart(self) -> Engine:
        self.repo.close()
        self.engine = self.build()
        return self.engine

    def command(self, cmd: str, args: dict) -> None:
        with self.repo.tx():
            self.repo.insert_command(f"local-{random.random()}", "LOCAL", "tester", cmd, args, self.clock.now())

    def resume_k8(self) -> None:
        self.command("RESUME", {"account": None, "halt_type": "K8"})
        self.engine.tick()

    def at(self, hh: int, mm: int, ss: int = 0) -> None:
        self.clock._now = kst(DAY, hh, mm, ss)  # noqa: SLF001
        quote(self.store, self.clock)

    def intents(self) -> list:
        return self.repo.select("SELECT intent_id, state, request_type, instrument FROM intent ORDER BY created_seq")

    def close(self) -> None:
        assert check_invariants(self.repo.connection, Mode.BACKTEST.value) == []
        self.repo.close()


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = World(tmp_path)
    yield w
    w.close()


# --- 시작 → K8 → 사용자 재개 → 예정 판정 → 주문 → 체결 → 재시작 -----------------------------------------

def test_start_k8_resume_evaluate_order_fill_and_restart_no_duplicate(world: World) -> None:
    rep = world.engine.start()
    assert rep.ok and "K8" in world.repo.active_halts(ACCOUNT)
    world.at(9, 5)
    world.engine.tick()
    assert world.intents() == []                                       # K8 중에는 판정하지 않음
    skipped = [e for e in world.repo.audit_events() if e["kind"] == "EVALUATION_SKIPPED"]
    assert skipped and "전역 정지" in skipped[-1]["summary"]
    world.engine.evaluated.clear()                                     # 같은 예정 시각을 재개 후 다시 (시험용)
    world.resume_k8()
    assert "K8" not in world.repo.active_halts(ACCOUNT)
    world.engine.tick()
    world.engine.tick()
    [it] = world.intents()
    assert (it["request_type"], it["instrument"], it["state"]) == ("ENTRY", "TST-A", "FILLED")
    assert world.repo.position(ACCOUNT, "TST-A").quantity == Decimal("10")
    # 재시작: 같은 판정(같은 예정 시각) 재실행 → 같은 판정 ID·신호 ID → 의도 추가 없음 (V-56)
    world.at(9, 6)
    e2 = world.restart()
    assert e2.start().ok and "K8" in world.repo.active_halts(ACCOUNT)
    world.resume_k8()
    world.engine.tick()
    assert len(world.intents()) == 1 and len(world.broker.sent) == 1


def test_v59_v58_restart_settles_pending_and_unknown(tmp_path: Path) -> None:
    w = World(tmp_path, FakeBehavior.LOST_ACCEPT)          # 브로커 접수, 응답 유실 → 결과 불명
    try:
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        w.engine.tick()
        [it] = w.intents()
        assert it["state"] == "UNKNOWN"
        w.restart()                                         # 해소 중 재시작 (V-58)
        rep = w.engine.start()
        assert not rep.ok and "K3" in w.repo.active_halts(ACCOUNT)   # 결과 불명 미해소 → 복구 실패
        for s in (2, 4, 8, 16, 32):
            w.clock.advance(timedelta(seconds=s))
            w.engine.tick()
        assert w.repo.intent(it["intent_id"]).state is IntentState.ACCEPTED   # 조회로 찾음, 재전송 없음
        assert len(w.broker.sent) == 1
    finally:
        w.close()


def test_v124_stepwise_resolution_does_not_block_other_work(tmp_path: Path) -> None:
    w = World(tmp_path, FakeBehavior.UNKNOWN)              # 브로커도 받지 않음 → 5회 모두 없음 → K3 (A-084/CP-031)
    try:
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        t0 = time.monotonic()
        w.engine.tick()
        w.engine.tick()                                    # 다음 주기에 해소 진행 시작(begin)
        assert time.monotonic() - t0 < 2                   # 해소를 기다리며 멈추지 않음
        [it] = w.intents()
        assert it["state"] == "UNKNOWN" and it["intent_id"] in w.engine.progress
        w.command("KILL", {"account": None, "reason": "해소 중 킬"})
        w.engine.tick()
        assert "K1" in w.repo.active_halts(ACCOUNT)       # 해소 대기 중에도 운영 명령 처리
        rounds = []
        for s in (2, 4, 8, 16, 32):
            w.clock.advance(timedelta(seconds=s - 1))
            w.engine.tick()
            rounds.append(w.engine.progress.get(it["intent_id"]).round if it["intent_id"] in w.engine.progress
                          else "done")
            w.clock.advance(timedelta(seconds=1))
            w.engine.tick()
        assert rounds == [0, 1, 2, 3, 4]                   # 간격 2·4·8·16·32초 전에는 조회하지 않음
        # 요구사항 변경 A-084/CP-031: 자동 미접수 종결 대신 결과 불명 유지 + K3
        assert w.repo.intent(it["intent_id"]).state is IntentState.UNKNOWN
        assert "K3" in w.repo.active_halts(ACCOUNT) and it["intent_id"] not in w.engine.progress
    finally:
        w.close()


def test_v60_recovery_failures_raise_matching_halts(world: World) -> None:
    world.health.ok = None
    world.broker.query_failures = 3
    rep = world.engine.start()
    active = set(world.repo.active_halts(ACCOUNT))
    assert not rep.ok and {"K6", "K5", "K8"} <= active
    world.command("RESUME", {"account": None, "halt_type": "K8"})
    world.engine.tick()
    assert "K8" in world.repo.active_halts(ACCOUNT)          # 복구 조건 미충족 → 재개 안 됨


def test_late_evaluation_skipped_and_non_trading_day(world: World) -> None:
    world.engine.start()
    world.resume_k8()
    world.at(9, 11)                                        # 09:05 + 5분 유예 초과
    world.engine.tick()
    assert world.intents() == []
    assert any("유예" in e["summary"] for e in world.repo.audit_events() if e["kind"] == "EVALUATION_SKIPPED")


def test_exec_policy_cancels_stale_unfilled(tmp_path: Path) -> None:
    from dataclasses import replace
    from tests.support.sample_strategies import cross_spec
    w = World(tmp_path, FakeBehavior.ACCEPT_ONLY)
    try:
        caps = w.engine.strategies[0].strategy.spec.capabilities
        spec = cross_spec(version=2, capabilities=tuple(caps) + ("CAP-ORD-03",), unfilled_policy="미체결 10분 후 취소")

        class S(CrossStrategy):
            pass
        s = S(spec)
        w.repo.close()
        w.repo = Repository(w.db, Mode.BACKTEST)
        w.store = make_store(w.clock)
        quote(w.store, w.clock)
        params = dict(PARAMS)
        w.engine = build_engine(mode=Mode.BACKTEST, repo=w.repo, clock=w.clock, broker=w.broker, store=w.store,
                                account=ACCOUNT, account_limits=scenario_limits(), gate_settings=SETTINGS,
                                caps=CAPS_ALL_SUPPORTED, kill_file=tmp_path / "KILL", clock_health=w.health,
                                strategies=[StrategyInput(s, params, (datetime(2000, 1, 1, 9, 5).time(),))])
        w.engine.strategies[0] = replace(w.engine.strategies[0], unfilled_minutes=10)
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        w.engine.tick()
        w.at(9, 14)
        w.engine.tick()
        assert [r["request_type"] for r in w.intents()] == ["ENTRY"]
        w.at(9, 15, 1)
        w.engine.tick()
        w.engine.tick()
        types = [(r["request_type"], r["state"]) for r in w.intents()]
        assert types[1][0] == "CANCEL" and w.repo.intent(w.intents()[0]["intent_id"]).state is IntentState.CANCELLED
    finally:
        w.close()


def test_wrong_mode_account_and_rejected_strategy_refuse_start(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "x.db", Mode.BACKTEST)
    clock = FixedClock(kst(DAY, 8, 0))
    common = dict(repo=repo, clock=clock, broker=FakeBroker(), store=make_store(clock), account=ACCOUNT,
                  account_limits=scenario_limits(), gate_settings=SETTINGS, kill_file=tmp_path / "K",
                  clock_health=ClockOk())
    with pytest.raises(ConfigError, match="실행할 수 없다"):
        build_engine(mode=Mode.LIVE, caps=CAPS_ALL_SUPPORTED, strategies=[], **common)  # type: ignore[arg-type]
    from trading_system.domain.models import Account
    other = Repository(tmp_path / "y.db", Mode.BACKTEST)
    other.register_account(Account(ACCOUNT, Mode.BACKTEST, "mock"))        # 저장소의 브로커 환경이 다름
    with pytest.raises(ConfigError, match="R-01"):
        build_engine(mode=Mode.BACKTEST, caps=CAPS_ALL_SUPPORTED, strategies=[],
                     **{**common, "repo": other})  # type: ignore[arg-type]
    other.close()
    caps = {k: {"M1": "미지원", "M2": "미지원", "M3": "미지원"} for k in CAPS_ALL_SUPPORTED}
    with pytest.raises(ConfigError, match="등록 거부"):
        build_engine(mode=Mode.BACKTEST, caps=caps, strategies=[StrategyInput(CrossStrategy(), dict(PARAMS), ())],
                     **common)  # type: ignore[arg-type]
    repo.close()


# --- 스레드 간 경쟁: 판정·실행 스레드 + 운영 명령 스레드 + 통보 주입 스레드 ----------------------------------

@pytest.mark.parametrize("seed", range(5))
def test_concurrency_engine_ops_and_events(tmp_path: Path, seed: int) -> None:
    w = World(tmp_path, FakeBehavior.ACCEPT_ONLY)
    rng = random.Random(seed)
    try:
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        stop = threading.Event()
        runner = threading.Thread(target=w.engine.run, args=(stop, 0.005), daemon=True)
        runner.start()

        def ops() -> None:
            r = Repository(w.db, Mode.BACKTEST)       # 스레드마다 자기 연결
            try:
                for i in range(15):
                    cmd = rng.choice([("EXIT", {"account": ACCOUNT, "instrument": "TST-A", "quantity": "5"}),
                                      ("RESUME", {"account": None, "halt_type": "K1"}),
                                      ("KILL", {"account": None, "reason": "경쟁"})])
                    with r.tx():
                        r.insert_command(f"c{seed}-{i}", "LOCAL", "t", cmd[0], cmd[1], w.clock.now())
                    time.sleep(0.003)
            finally:
                r.close()

        def events() -> None:
            for _ in range(30):
                for no in w.broker.open_order_nos():
                    try:
                        w.broker.fill(no, Decimal("1"))
                    except ValueError:
                        pass
                w.broker.inject(OrderEvent(kind="FILL", order_no="9999999", fill_id="X", quantity=Decimal("1"),
                                           price=Decimal("1")))           # 짝 없는 통보도 섞음
                time.sleep(0.002)

        ts = [threading.Thread(target=ops), threading.Thread(target=events)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        time.sleep(0.05)
        stop.set()
        runner.join(5)
        w.engine.tick()
        assert w.engine.errors == []
        pos = w.repo.position(ACCOUNT, "TST-A").quantity
        filled = sum(Decimal(r["quantity"]) * (1 if r["side"] == "BUY" else -1) for r in w.repo.select(
            "SELECT f.quantity, i.side FROM fill f JOIN broker_order b ON f.order_key=b.order_key "
            "JOIN intent i ON b.intent_id=i.intent_id"))
        assert pos == filled                                   # 원장 = 체결 누적 (경쟁 중에도)
    finally:
        w.close()


def test_no_evaluation_on_non_trading_day(world: World) -> None:
    world.engine.start()
    world.resume_k8()
    world.clock._now = kst(date(2026, 10, 3), 9, 5)  # noqa: SLF001 — 토요일
    quote(world.store, world.clock)
    world.engine.tick()
    assert world.intents() == []
    assert not [e for e in world.repo.audit_events() if e["kind"] in ("EVALUATION", "EVALUATION_SKIPPED")]


def test_same_schedule_evaluated_once(tmp_path: Path) -> None:
    w = World(tmp_path, FakeBehavior.ACCEPT_ONLY)
    try:
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        w.engine.tick()
        w.at(9, 5, 30)
        w.engine.tick()
        assert len(w.repo.select("SELECT decision_id FROM gate_decision")) == 1
        assert len([e for e in w.repo.audit_events() if e["kind"] == "EVALUATION"]) == 1
    finally:
        w.close()


def test_v59_pending_send_at_restart_is_closed_not_sent(world: World) -> None:
    from trading_system.domain.models import GateRequest
    from tests.scenario.conftest import make_signal
    world.engine.start()
    world.resume_k8()
    s = make_signal(__import__("trading_system.domain.enums", fromlist=["RequestType"]).RequestType.ENTRY,
                    instrument="TST-A", quantity="1", strategy="t-cross")
    from dataclasses import replace
    world.at(9, 6)                                                    # 주문 허용 시간 안
    s = replace(s, as_of=world.clock.now() - timedelta(minutes=1), valid_until=world.clock.now() + timedelta(minutes=30))
    d = world.engine.gate.decide(GateRequest.from_signal(s))          # 승인 커밋 직후 종료(전송 전)
    assert d.approved is not None
    world.restart()
    world.engine.start()
    assert world.repo.intent(d.approved.intent_id).state is IntentState.NOT_SENT
    assert world.broker.sent == []


def test_failing_step_raises_k8_and_loop_continues(world: World) -> None:
    world.engine.start()
    world.resume_k8()

    def boom(account: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("가상 장애")

    world.broker.query_cash = boom  # type: ignore[method-assign]
    world.engine.request_reconcile()
    world.command("KILL", {"account": None, "reason": "같은 주기의 운영 명령"})
    world.engine.tick()
    assert world.engine.errors and "reconcile" in world.engine.errors[0]
    assert {"K8", "K1"} <= set(world.repo.active_halts(ACCOUNT))
