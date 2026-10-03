"""PAPER-0 전 검토 수정 (docs/changes/review-2026-10-03-pre-paper0.md) — 감시 스레드 분리(RV-22, 03 D-038),
루프 감시 측정(RV-05), 예외 내성(RV-06), 일봉 갱신(RV-04), K5 연결 조건(RV-02), 단일 실행 잠금(RV-15),
시세 스레드 생존·가격 신선도(RV-18), 일일 보고 시각(RV-21), 대조 주기(RV-25).

가상 데이터·fake 브로커·가짜 단조 시계만 쓴다. 네트워크 연결 없음, 실시간 대기 없음.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t020_app import ACCOUNT, CAL, DAY, World, kst
from trading_system.app.supervisor import STALL_ALERT_SECONDS, QueuedHalts, Supervisor
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import HaltType, Mode
from trading_system.domain.market import InstrumentInfo, InstrumentKind, Quote
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.monitoring import liveness
from trading_system.monitoring.liveness import LoopMonitor, write_heartbeat
from trading_system.storage.repository import StorageFailure


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Alerts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def alert(self, level: str, message: str, ref: str | None) -> None:
        self.items.append((level, message))


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = World(tmp_path)
    yield w
    w.repo.close()


def alerts_of(w: World) -> list[str]:
    return [json.loads(r["summary"])["message"]
            for r in w.repo.select("SELECT summary FROM audit_event WHERE kind='ALERT' ORDER BY rowid")]


def make_sup(w: World, mono: Mono, **kw) -> Supervisor:  # type: ignore[no-untyped-def]
    w.engine.mono = mono
    w.engine.last_progress = mono()
    return Supervisor(w.engine, heartbeat=w.tmp / "heartbeat", fallback=w.tmp / "logs" / "fallback.log",
                      mono=mono, **kw)


# --- RV-05 루프 감시 측정 --------------------------------------------------------------------------

def test_rv05_decision_thread_work_does_not_trigger_k8(world: World) -> None:
    """판정·실행 스레드가 20초 걸려도(주문 응답 대기 등) 감시 스레드가 제때 돌면 K8 오탐 없음."""
    mono = Mono()
    sup = make_sup(world, mono)
    sup.step()
    for _ in range(20):                    # 감시 스레드는 1초마다 정상 실행, 판정 스레드는 멈춰 있음
        mono.t += 1.0
        world.clock.advance(timedelta(seconds=1))
        sup.step(waited=1.0)
    world.engine._drain_calls()  # noqa: SLF001
    assert "K8" not in world.repo.active_halts(ACCOUNT)
    assert sup.loop.events == []


def test_rv05_scheduling_lateness_and_clock_jump_trigger_k8(world: World) -> None:
    mono = Mono()
    sup = make_sup(world, mono)
    sup.step()
    mono.t += 7.0                          # 감시 스레드 자신이 7초 늦게 깨어남 (절전·일시 정지 의심)
    world.clock.advance(timedelta(seconds=7))
    sup.step(waited=7.0)
    world.engine._drain_calls()  # noqa: SLF001
    assert "K8" in world.repo.active_halts(ACCOUNT)
    assert "루프 지연" in sup.loop.events[0]
    mono.t += 1.0                          # 벽시계만 4초 이동 = 시계 점프
    world.clock.advance(timedelta(seconds=4))
    sup.step(waited=1.0)
    assert "시계 점프" in sup.loop.events[1]


def test_rv05_loop_monitor_waited_overrides_work_duration() -> None:
    m = LoopMonitor(None, None)
    t0 = kst(DAY, 9, 0)
    m.tick(100.0, t0)
    # 이전 표본 뒤 9초가 지났지만 실제 대기는 1초(나머지는 감시 자신의 작업) → 지연 아님
    assert m.tick(109.0, t0 + timedelta(seconds=9), waited=1.0) is None
    assert m.tick(115.5, t0 + timedelta(seconds=15.5), waited=6.5) is not None


def test_rv05_decision_stall_alerts_without_k8_and_stops_heartbeat(world: World) -> None:
    mono = Mono()
    sup = make_sup(world, mono)
    sup.step()
    assert (world.tmp / "heartbeat").exists()
    (world.tmp / "heartbeat").unlink()
    mono.t += STALL_ALERT_SECONDS + 1
    world.clock.advance(timedelta(seconds=STALL_ALERT_SECONDS + 1))
    sup.step(waited=1.0)
    world.engine._drain_calls()  # noqa: SLF001
    assert any("판정·실행 스레드" in a for a in alerts_of(world))
    assert "K8" not in world.repo.active_halts(ACCOUNT)
    assert not (world.tmp / "heartbeat").exists()      # watchdog도 멈춤을 본다


# --- RV-22 감시 스레드 분리 ------------------------------------------------------------------------

def test_rv22_kill_recorded_while_decision_thread_blocked(world: World) -> None:
    mono = Mono()
    sup = make_sup(world, mono)
    started, release = threading.Event(), threading.Event()
    world.engine.call_soon(lambda: (started.set(), release.wait(10)))
    t = threading.Thread(target=world.engine.tick)
    t.start()
    try:
        assert started.wait(5)
        world.command("KILL", {"account": None, "reason": "테스트"})
        sent_before = len(world.broker.sent)
        sup.step()                                          # 판정 스레드가 막혀 있어도 즉시 K1 기록
        assert "K1" in world.repo.active_halts(ACCOUNT)
        assert (world.tmp / "heartbeat").exists()
        assert len(world.broker.sent) == sent_before        # 감시 스레드는 주문하지 않는다
    finally:
        release.set()
        t.join(10)
    world.engine.tick()                                     # 명령 처리(취소 등)는 판정·실행 스레드
    rows = [r for r in world.repo.halt_rows(None) if r["halt_type"] == "K1"]
    assert len(rows) == 1
    cmds = world.repo.select("SELECT status FROM ops_command WHERE command='KILL'")
    assert [c["status"] for c in cmds] == ["DONE"]


def test_rv22_queued_halts_run_on_decision_thread(world: World) -> None:
    QueuedHalts(world.engine).activate(HaltType.K6, "monitor", "테스트", None)
    assert "K6" not in world.repo.active_halts(ACCOUNT)
    world.engine.tick()
    assert "K6" in world.repo.active_halts(ACCOUNT)


# --- RV-06 예외 내성 --------------------------------------------------------------------------------

def test_rv06_hook_exception_does_not_escape_tick(world: World) -> None:
    def bad(now: datetime) -> None:
        raise PermissionError("heartbeat 교체 실패")
    world.engine.on_tick.append(bad)
    world.engine.tick()                                     # 예외가 루프 밖으로 나가지 않는다
    assert any("PermissionError" in e for e in world.engine.errors)


def test_rv06_k8_record_failure_in_step_error_goes_to_fallback(world: World, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    world.engine.fallback_file = world.tmp / "logs" / "engine-fallback.log"

    def boom() -> None:
        raise RuntimeError("단계 실패")

    def k8_fail(*a, **k):  # type: ignore[no-untyped-def]
        raise StorageFailure("잠김")

    monkeypatch.setattr(world.engine, "_ops", boom)
    monkeypatch.setattr(world.engine.halts, "activate", k8_fail)
    world.engine.tick()
    text = world.engine.fallback_file.read_text(encoding="utf-8")
    assert "K8" in text and "StorageFailure" in text


def test_rv06_heartbeat_retries_permission_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}
    real = Path.replace

    def flaky(self, target):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("다른 프로세스가 읽는 중")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    write_heartbeat(tmp_path / "heartbeat", kst(DAY, 9, 0), sleep=lambda s: None)
    assert (tmp_path / "heartbeat").exists() and calls["n"] == 3


def test_rv06_supervisor_survives_clock_check_failure(world: World) -> None:
    class BadClock:
        def check(self) -> None:
            raise StorageFailure("시계 점검 기록 실패")
    mono = Mono()
    sup = make_sup(world, mono, clock_monitor=BadClock(), clock_every=10.0)
    mono.t += 11
    sup.step(waited=1.0)                                    # 예외가 감시 스레드를 죽이지 않는다
    assert any("시계 점검" in e for e in sup.errors)
    assert (world.tmp / "heartbeat").exists()


def test_rv06_record_fatal_writes_traceback(tmp_path: Path) -> None:
    from trading_system.app.main import record_fatal
    try:
        raise ValueError("치명")
    except ValueError as exc:
        record_fatal(tmp_path / "logs" / "fatal.log", exc)
    text = (tmp_path / "logs" / "fatal.log").read_text(encoding="utf-8")
    assert "ValueError" in text and "Traceback" in text


# --- RV-25 대조 주기 -------------------------------------------------------------------------------

def test_rv25_periodic_reconcile_only_in_session_plus_once_after_close(world: World) -> None:
    runs: list[datetime] = []
    world.engine.reconciler.run = lambda acct, trig: runs.append(world.clock.now())  # type: ignore[method-assign]
    world.engine._last_reconcile = None  # noqa: SLF001
    world.at(8, 59)
    world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert runs == []                                       # 장 전 주기 대조 없음
    for hh, mm, ss in ((9, 10, 0), (9, 10, 30), (9, 11, 0)):
        world.at(hh, mm, ss)
        world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert len(runs) == 2
    for hh, mm in ((15, 40), (15, 45), (20, 0)):
        world.at(hh, mm)
        world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert len(runs) == 3                                   # 장 마감 후 1회
    world.engine.request_reconcile()                        # 재접속·체결 통보 후 요청은 언제든
    world.at(22, 0)
    world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert len(runs) == 4


# --- RV-21 일일 보고 시각 --------------------------------------------------------------------------

def test_rv21_daily_report_generated_at_report_time(world: World) -> None:
    def reports() -> list[str]:
        return [str(r["trade_day"]) for r in world.repo.daily_reports(ACCOUNT)]

    world.at(9, 0)
    world.engine._daily(world.clock.now())  # noqa: SLF001
    world.at(15, 59)
    world.engine._daily(world.clock.now())  # noqa: SLF001
    assert reports() == []
    world.at(16, 0, 30)                                     # report_time 16:00 (scenario_limits)
    world.engine._daily(world.clock.now())  # noqa: SLF001
    world.at(16, 5)
    world.engine._daily(world.clock.now())  # noqa: SLF001
    assert reports() == [DAY.isoformat()]                   # 그날 보고서 1회

# --- RV-04 일봉 갱신 -------------------------------------------------------------------------------

def _rows_until(last: date, n: int = 60) -> list[RawDaily]:
    rows, d = [], last
    while len(rows) < n:
        if CAL.is_trading_day(d):
            rows.append(RawDaily(d, Decimal("100"), Decimal("110"), Decimal("90"), Decimal("100"), Decimal("1")))
        d -= timedelta(days=1)
    return sorted(rows, key=lambda r: r.day)


class FakeFeed:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.store = MarketDataStore(CAL, None)
        self.infos = {"TST-A": InstrumentInfo("TST-A", InstrumentKind.STOCK, Decimal("1"), Decimal("1"))}
        self.store.register(self.infos["TST-A"])
        self.calls: list[datetime] = []
        self.fail = False
        self.lag_days = 0

    def refresh_daily(self, code: str):  # type: ignore[no-untyped-def]
        self.calls.append(self.clock.now())
        if self.fail:
            return None
        from trading_system.marketdata.store import latest_complete_day
        last = latest_complete_day(self.clock.now(), CAL) - timedelta(days=self.lag_days)
        return self.store.update_daily(code, _rows_until(last))

    def refresh_master(self) -> bool:
        return True

    def refresh_vi(self) -> bool:
        return True

    def refresh_quote(self, code: str):  # type: ignore[no-untyped-def]
        return None

    def tick(self) -> None:
        pass


def test_rv04_daily_bars_refreshed_when_trading_date_changes() -> None:
    from trading_system.marketdata.kiwoom_feed import MarketPoller
    clock = FixedClock(kst(DAY, 10, 0))
    f = FakeFeed(clock)
    p = MarketPoller(f, alerts=Alerts())  # type: ignore[arg-type]
    p.poll_once()
    p.poll_once()
    assert len(f.calls) == 1                                 # 같은 확정일이면 다시 받지 않음
    clock._now = kst(DAY, 19, 59)  # noqa: SLF001
    p.poll_once()
    assert len(f.calls) == 1                                 # 확정 시각(20:00, DP-5) 전
    clock._now = kst(DAY, 20, 1)  # noqa: SLF001
    p.poll_once()
    assert len(f.calls) == 2                                 # 다음 판정 전에 새 확정 봉


def test_rv04_daily_refresh_bounded_retry_then_alert() -> None:
    from trading_system.marketdata.kiwoom_feed import DAILY_MAX_ATTEMPTS, DAILY_RETRY, MarketPoller
    clock = FixedClock(kst(DAY, 10, 0))
    f = FakeFeed(clock)
    f.fail = True
    alerts = Alerts()
    p = MarketPoller(f, alerts=alerts)  # type: ignore[arg-type]
    p.poll_once()
    p.poll_once()
    assert len(f.calls) == 1                                 # 재시도 간격 전
    for _ in range(DAILY_MAX_ATTEMPTS + 3):
        clock.advance(DAILY_RETRY)
        p.poll_once()
    assert len(f.calls) == DAILY_MAX_ATTEMPTS                # 상한
    assert sum("일봉" in m for _, m in alerts.items) == 1
    f.fail, f.lag_days = False, 0
    clock._now = kst(DAY, 20, 1)  # noqa: SLF001             # 다음 확정일엔 새로 시도
    p.poll_once()
    assert len(f.calls) == DAILY_MAX_ATTEMPTS + 1


def test_rv04_stale_response_counts_as_failure() -> None:
    from trading_system.marketdata.kiwoom_feed import DAILY_RETRY, MarketPoller
    clock = FixedClock(kst(DAY, 10, 0))
    f = FakeFeed(clock)
    f.lag_days = 7                                           # 응답은 왔지만 마지막 봉이 오래됨
    p = MarketPoller(f, alerts=Alerts())  # type: ignore[arg-type]
    p.poll_once()
    clock.advance(DAILY_RETRY)
    p.poll_once()
    assert len(f.calls) == 2


# --- RV-18 시세 스레드 내성·생존 감시·가격 신선도 --------------------------------------------------

def test_rv18_poller_run_survives_exceptions() -> None:
    from trading_system.marketdata.kiwoom_feed import MarketPoller
    clock = FixedClock(kst(DAY, 10, 0))
    alerts = Alerts()
    stop = threading.Event()
    n = {"i": 0}

    class P(MarketPoller):
        def poll_once(self) -> None:
            n["i"] += 1
            if n["i"] < 3:
                raise RuntimeError("파싱 버그")
            stop.set()

    P(FakeFeed(clock), alerts=alerts).run(stop, 0)  # type: ignore[arg-type]
    assert n["i"] == 3 and any("시세 조회" in m for _, m in alerts.items)


def test_rv18_dead_io_thread_alerts_and_k5(world: World) -> None:
    dead = threading.Thread(target=lambda: None, name="market-poller")
    dead.start()
    dead.join()
    mono = Mono()
    sup = make_sup(world, mono, watched=[(dead, "시세 조회")])
    sup.step()
    sup.step(waited=1.0)
    world.engine._drain_calls()  # noqa: SLF001
    assert "K5" in world.repo.active_halts(ACCOUNT)
    assert sum(a.startswith("시세 조회 스레드 종료") for a in alerts_of(world)) == 1   # 한 번만


def test_rv18_fresh_price_none_when_stale() -> None:
    store = MarketDataStore(CAL, None)
    store.register(InstrumentInfo("TST-A", InstrumentKind.STOCK, Decimal("1"), Decimal("1")))
    now = kst(DAY, 10, 0)
    assert store.fresh_price("TST-A", now, timedelta(seconds=60)) is None
    store.update_quote(Quote("TST-A", Decimal("100"), now - timedelta(seconds=61)))
    assert store.fresh_price("TST-A", now, timedelta(seconds=60)) is None
    store.update_quote(Quote("TST-A", Decimal("101"), now - timedelta(seconds=10)))
    assert store.fresh_price("TST-A", now, timedelta(seconds=60)) == Decimal("101")


# --- RV-02 실시간 연결 상태 → K5 조건 -------------------------------------------------------------

def test_rv02_ws_online_reports_broker_connected_and_requests_reconcile(world: World) -> None:
    """실시간 상태는 실시간 경로(link:realtime)만 보고한다 — 경로 구분 없는 broker_connected 금지 (RV-02)."""
    from trading_system.app.main import ws_state_handler
    from trading_system.broker.kiwoom.realtime import WsState
    from trading_system.halt.manager import K5_LINK_REALTIME
    on_state = ws_state_handler(world.engine, ACCOUNT)
    on_state(WsState.OFFLINE, "끊김")
    world.engine._drain_calls()  # noqa: SLF001
    k5 = [r for r in world.repo.halt_rows(None) if r["halt_type"] == "K5"]
    assert len(k5) == 1 and json.loads(k5[0]["conditions"])[K5_LINK_REALTIME]["ok"] is False
    world.engine._reconcile_requested.clear()  # noqa: SLF001
    on_state(WsState.ONLINE, "연결")
    assert world.engine._reconcile_requested.is_set()  # noqa: SLF001
    world.engine._drain_calls()  # noqa: SLF001
    conds = json.loads(world.repo.halt(k5[0]["halt_id"])["conditions"])
    assert conds[K5_LINK_REALTIME]["ok"] is True


def test_rv02_ws_online_does_not_release_rest_link(world: World) -> None:
    """조회 경로로 걸린 K5(actor broker)는 실시간 ONLINE으로 풀리지 않는다 (RV-02)."""
    from trading_system.app.main import QueuedHalts, ws_state_handler
    from trading_system.broker.kiwoom.realtime import WsState
    from trading_system.domain.enums import HaltType
    from trading_system.halt.manager import K5_LINK_REST
    QueuedHalts(world.engine).activate(HaltType.K5, "broker", "토큰 실패", ACCOUNT)
    world.engine._drain_calls()  # noqa: SLF001
    ws_state_handler(world.engine, ACCOUNT)(WsState.ONLINE, "연결")
    world.engine._drain_calls()  # noqa: SLF001
    k5 = [r for r in world.repo.halt_rows(None) if r["halt_type"] == "K5"]
    assert len(k5) == 1
    assert json.loads(world.repo.halt(k5[0]["halt_id"])["conditions"])[K5_LINK_REST]["ok"] is False
    assert HaltType.K5 in world.repo.active_halts(ACCOUNT)


# --- RV-15 단일 실행 잠금 --------------------------------------------------------------------------

def test_rv15_second_instance_refused(tmp_path: Path) -> None:
    from trading_system.app.instance_lock import InstanceLocked, acquire_instance_lock, instance_running
    first = acquire_instance_lock(tmp_path)
    try:
        assert instance_running(tmp_path)
        with pytest.raises(InstanceLocked):
            acquire_instance_lock(tmp_path)
    finally:
        first.release()
    assert not instance_running(tmp_path)
    acquire_instance_lock(tmp_path).release()


def test_rv15_run_main_refuses_when_locked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_t020_main import make_root
    from trading_system.app.instance_lock import acquire_instance_lock
    from trading_system.app.main import run_main
    root = make_root(tmp_path, Mode.BACKTEST)
    lock = acquire_instance_lock(root / "var" / "backtest")
    try:
        assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1) == 2
        assert "다른 실행" in capsys.readouterr().out
    finally:
        lock.release()


def test_liveness_module_constants_unchanged() -> None:
    assert liveness.LOOP_DELAY_LIMIT == 5.0 and liveness.CLOCK_JUMP_LIMIT == 2.0


# --- 운영 가시성 배선 (RV-16 연결) -----------------------------------------------------------------

def test_vis_step_error_logged_to_oplog_and_flush_called(world: World, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    world.engine.log_dir = world.tmp / "logs"
    flushed = {"n": 0}
    monkeypatch.setattr(world.engine.alerts, "flush", lambda: flushed.__setitem__("n", flushed["n"] + 1),
                        raising=False)

    def boom() -> None:
        raise RuntimeError("단계 실패")

    monkeypatch.setattr(world.engine, "_ops", boom)
    world.engine.tick()
    text = "".join(p.read_text(encoding="utf-8") for p in (world.tmp / "logs").rglob("*") if p.is_file())
    assert "engine.step:ops" in text and "Traceback" in text
    assert flushed["n"] >= 1


def test_vis_start_summary_shows_failures_and_unknown(world: World) -> None:
    from trading_system.app.main import start_summary
    rep = world.engine.start()
    out = start_summary(world.engine, rep)
    assert "결과 불명 의도: 0건" in out and "K8" in out and "전략" in out