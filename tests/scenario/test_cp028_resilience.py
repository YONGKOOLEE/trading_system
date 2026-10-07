"""CP-028 운영 복원력 P1~P4 (승인 A-074, 03 D-146~D-150, 04 V-193~V-200).

P1 알림 전송 처리 예외 연속 3회·알림 전송 스레드 종료 → K11 (명령 수신 스레드는 알림만, Q-2).
P2 저장 단위 안의 알림은 저장 단위가 끝난 뒤(커밋·롤백 모두) 기록. 롤백분은 "(처리 취소됨)" 표시로 기록·전송.
   반복 억제 창은 커밋된 기록일 때만 연다.
P3 장 마감 후 그날 첫 대조(요청 대조 포함)를 CLOSE로. 만료 종결 조건은 그대로.
P4 장 밖 K5가 대조만 남았을 때(모든 연결 경로 정상) 대조를 5분 간격 최대 12회 재시도.

가상 데이터·fake 브로커·가짜 시계만 쓴다. 네트워크 연결 없음.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT as ENV_ACCOUNT
from tests.scenario.conftest import Env, env  # noqa: F401 — 픽스처
from tests.scenario.test_rv_loop import Mono, alerts_of, make_sup
from tests.scenario.test_t020_app import ACCOUNT, World
from trading_system.app.supervisor import QueuedHalts
from trading_system.app.telegram_run import NotifyFaults, _guarded
from trading_system.domain.clock import KST
from trading_system.domain.enums import HaltType
from trading_system.domain.models import OrderEvent
from trading_system.halt.manager import K5_LINK_REALTIME
from trading_system.ops.telegram import Notifier
from trading_system.reconciliation.reconciler import CLOSE_TRIGGER
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import StorageFailure


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = World(tmp_path)
    yield w
    w.close()


def _dead(name: str) -> threading.Thread:
    t = threading.Thread(target=lambda: None, name=name)
    t.start()
    t.join()
    return t


class _Api:
    """가짜 텔레그램 API (메모리). 네트워크 없음."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append(text)


def _alert_rows(repo) -> list[dict]:  # type: ignore[no-untyped-def]
    return [json.loads(e["summary"]) for e in repo.audit_events() if e["kind"] == "ALERT"]


# --- P1 V-193: 알림 처리 예외 연속 → K11 ------------------------------------------------------------

def _failing_enqueue(env: Env, monkeypatch: pytest.MonkeyPatch, exc: Exception, times: int) -> None:
    real = env.repo.enqueue_new_alerts
    left = {"n": times}

    def enqueue(*a, **kw):  # type: ignore[no-untyped-def]
        if left["n"] > 0:
            left["n"] -= 1
            raise exc
        return real(*a, **kw)
    monkeypatch.setattr(env.repo, "enqueue_new_alerts", enqueue)


@pytest.mark.parametrize("exc", [StorageFailure("잠금 대기 초과"), KeyError("level")])
def test_v193_three_consecutive_processing_errors_raise_k11_then_auto_release(
        env: Env, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:  # noqa: F811
    api = _Api()
    notifier = Notifier(env.repo, api, "1", env.clock, env.halts, sleep=lambda s: None)  # type: ignore[arg-type]
    faults = NotifyFaults(env.halts)
    _failing_enqueue(env, monkeypatch, exc, 3)
    for _ in range(2):
        assert _guarded(notifier.pump, "telegram.notify", None, faults) is False
    assert "K11" not in [h["halt_type"] for h in env.repo.halt_rows(None)]
    assert _guarded(notifier.pump, "telegram.notify", None, faults) is False
    [k11] = [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K11"]
    assert k11["reason"] == f"알림 전송 처리 예외 연속 3회: {type(exc).__name__}"   # 예외 유형만 (값 없음)
    # 복구: 다음 회차가 예외 없이 끝나고 전송이 성공하면 기존 notify_ok 경로로 자동 해제 (V-30과 같은 경로)
    assert _guarded(notifier.pump, "telegram.notify", None, faults) is True
    assert faults.count == 0
    assert any("정지 K11 발동" in t for t in api.sent)
    assert "K11" not in [h["halt_type"] for h in env.repo.halt_rows(None)]


def test_v193_two_errors_then_success_resets_count(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    notifier = Notifier(env.repo, _Api(), "1", env.clock, env.halts, sleep=lambda s: None)  # type: ignore[arg-type]
    faults = NotifyFaults(env.halts)
    _failing_enqueue(env, monkeypatch, StorageFailure("x"), 2)
    _guarded(notifier.pump, "telegram.notify", None, faults)
    _guarded(notifier.pump, "telegram.notify", None, faults)
    assert faults.count == 2
    assert _guarded(notifier.pump, "telegram.notify", None, faults) is True
    assert faults.count == 0
    _failing_enqueue(env, monkeypatch, StorageFailure("x"), 2)
    _guarded(notifier.pump, "telegram.notify", None, faults)
    _guarded(notifier.pump, "telegram.notify", None, faults)
    assert "K11" not in [h["halt_type"] for h in env.repo.halt_rows(None)]   # 연속 2회 + 2회 ≠ 3회 연속


def test_v193_k11_goes_through_queued_halts_on_decision_thread(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """다른 스레드(알림 전송)의 정지 발동은 QueuedHalts로 판정·실행 스레드에 넘긴다 (D-038)."""
    faults = NotifyFaults(QueuedHalts(world.engine))

    def boom() -> None:
        raise StorageFailure("x")
    for _ in range(3):
        _guarded(boom, "telegram.notify", None, faults)
    assert "K11" not in [h["halt_type"] for h in world.repo.halt_rows(None)]   # 아직 큐에만
    world.engine._drain_calls()  # noqa: SLF001
    assert "K11" in [h["halt_type"] for h in world.repo.halt_rows(None)]


def test_v193_poll_thread_errors_do_not_count(world: World) -> None:
    """명령 수신 스레드는 계수기를 쓰지 않는다 (Q-2 알림만) — faults 없이 부르면 기존 동작."""
    def boom() -> None:
        raise StorageFailure("x")
    for _ in range(5):
        assert _guarded(boom, "telegram.poll", None) is False
    world.engine._drain_calls()  # noqa: SLF001
    assert world.repo.halt_rows(None) == []


# --- P1 V-194: 텔레그램 스레드 종료 --------------------------------------------------------------------

def test_v194_dead_notify_thread_alerts_once_and_k11(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    sup = make_sup(world, Mono(), watched_k11=[(_dead("telegram-notify"), "텔레그램 알림 전송")])
    sup.step()
    monkeypatch.setattr(world.engine.alerts, "_window", None)   # 억제가 아니라 감시가 1회만 내는지
    sup.step(waited=1.0)
    assert "K11" not in [h["halt_type"] for h in world.repo.halt_rows(None)]   # 감시 스레드는 직접 발동하지 않음
    world.engine._drain_calls()  # noqa: SLF001
    msgs = [m for m in alerts_of(world) if m.startswith("텔레그램 알림 전송 스레드 종료")]
    assert len(msgs) == 1 and "K11" in msgs[0]
    [k11] = [h for h in world.repo.halt_rows(None) if h["halt_type"] == "K11"]
    assert k11["account"] is None
    assert "K5" not in world.repo.active_halts(ACCOUNT)          # K5가 아니라 K11 (위험 축소는 허용 — 01 K11 행)


def test_v194_unstarted_notify_thread_ignored(world: World) -> None:
    t = threading.Thread(target=lambda: None, name="telegram-notify")
    sup = make_sup(world, Mono(), watched_k11=[(t, "텔레그램 알림 전송")])
    sup.step()
    world.engine._drain_calls()  # noqa: SLF001
    assert world.repo.halt_rows(None) == []


def test_v194_watched_threads_split_notify_k11_poll_alert_only() -> None:
    from trading_system.app.main import watched_threads
    io = [threading.Thread(name="market-poller"), threading.Thread(name="telegram-notify"),
          threading.Thread(name="telegram-poll"), threading.Thread(name="other")]
    halting, k11, alert_only = watched_threads(io, None)
    assert [label for _, label in halting] == ["시세 조회"]
    assert [label for _, label in k11] == ["텔레그램 알림 전송"]
    assert [label for _, label in alert_only] == ["텔레그램 명령 수신"]


# --- P2 V-195: 롤백된 저장 단위 안의 알림 -------------------------------------------------------------

def test_v195_alert_inside_rolled_back_tx_recorded_after_with_mark(env: Env) -> None:  # noqa: F811
    sink = AuditAlertSink(env.repo, env.clock, console=False)
    with pytest.raises(RuntimeError):
        with env.repo.tx():
            sink.alert("EMERGENCY", "종결 후 체결 통보 — 대조 필요 (o-1)", "i-1")
            assert _alert_rows(env.repo) == []          # 저장 단위 안에서는 아직 기록하지 않는다
            raise RuntimeError("주입 실패")
    [row] = _alert_rows(env.repo)
    assert row["message"] == "(처리 취소됨) 종결 후 체결 통보 — 대조 필요 (o-1)"
    assert row["rolled_back"] is True and row["level"] == "EMERGENCY"
    with env.repo.tx():
        env.repo.enqueue_new_alerts()
    [n] = env.repo.pending_notifications()
    assert n["message"].startswith("(처리 취소됨) ")    # 텔레그램 대기열 진입


def test_v195_committed_tx_alert_unchanged(env: Env) -> None:  # noqa: F811
    sink = AuditAlertSink(env.repo, env.clock, console=False)
    with env.repo.tx():
        env.repo.audit(env.clock.now(), "X", "t", None, {})
        sink.alert("WARNING", "알림", None)
    [row] = _alert_rows(env.repo)
    assert row == {"level": "WARNING", "message": "알림"}


def test_v195_nested_tx_records_once_after_outermost(env: Env) -> None:  # noqa: F811
    sink = AuditAlertSink(env.repo, env.clock, console=False)
    with env.repo.tx():
        with env.repo.tx():
            with env.repo.tx():
                sink.alert("WARNING", "깊은 알림", None)
            assert _alert_rows(env.repo) == []
        assert _alert_rows(env.repo) == []
    assert [r["message"] for r in _alert_rows(env.repo)] == ["깊은 알림"]


def test_v195_outer_commit_failure_alert_goes_to_fallback(tmp_path: Path, env: Env,  # noqa: F811
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """바깥 커밋이 실패하고 기록용 저장 단위도 실패하면 fallback 파일에 남는다 (종전 구멍)."""
    fb = tmp_path / "logs" / "alerts-fallback.log"
    sink = AuditAlertSink(env.repo, env.clock, fallback=fb, console=False)
    with pytest.raises(StorageFailure):
        with env.repo.tx():
            sink.alert("EMERGENCY", "정지 K4 발동: x", "h-1")
            env.repo.fail_next_transaction = True        # 종료 뒤 기록 저장 단위가 실패하도록
            raise StorageFailure("커밋 실패 주입")
    text = fb.read_text(encoding="utf-8")
    assert "(처리 취소됨) 정지 K4 발동: x" in text


# --- P2 V-196: 롤백 뒤 재처리 알림이 억제되지 않음 ------------------------------------------------------

def test_v196_reprocessed_alert_after_rollback_not_suppressed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    sink = AuditAlertSink(env.repo, env.clock, console=False)       # 반복 억제 60초 (기본)
    env.executor._alerts = sink  # noqa: SLF001
    ev = OrderEvent(kind="FILL", order_no="X-1", account=ENV_ACCOUNT)
    given = [ev]
    monkeypatch.setattr(env.broker, "poll_events", lambda: [given.pop()] if given else [])
    calls = {"n": 0}

    def apply_event(e: OrderEvent) -> None:
        calls["n"] += 1
        env.executor.alert("EMERGENCY", "종결 후 체결 통보 — 대조 필요 (o-9)", "i-9")
        if calls["n"] == 1:
            raise StorageFailure("저장 실패 주입")
    monkeypatch.setattr(env.executor, "apply_event", apply_event)
    with pytest.raises(StorageFailure):
        env.executor.process_events()                 # 1회차: 롤백 (통보는 보관)
    env.clock._now += timedelta(seconds=5)  # noqa: SLF001 — 60초 안
    env.executor.process_events()                     # 2회차: 성공
    msgs = [r["message"] for r in _alert_rows(env.repo)]
    assert msgs == ["(처리 취소됨) 종결 후 체결 통보 — 대조 필요 (o-9)", "종결 후 체결 통보 — 대조 필요 (o-9)"]
    with env.repo.tx():
        env.repo.enqueue_new_alerts()
    assert len(env.repo.pending_notifications()) == 2
    # 커밋된 기록은 억제 창을 연다 — 같은 알림 반복은 종전대로 억제
    sink.alert("EMERGENCY", "종결 후 체결 통보 — 대조 필요 (o-9)", "i-9")
    assert len(_alert_rows(env.repo)) == 2


# --- P3 V-197~V-199: 장 마감 후 첫 대조를 CLOSE로 ------------------------------------------------------

class _Res:
    def __init__(self, result: str) -> None:
        self.result = result


def _record_triggers(w: World, results: list[str] | None = None) -> list[tuple[str, str]]:
    """대조 실행을 기록한다. results가 있으면 차례로 그 결과(가짜), 없으면 실제 대조기 실행."""
    runs: list[tuple[str, str]] = []
    real = w.engine.reconciler.run

    def run(acct: str, trig: str):  # type: ignore[no-untyped-def]
        runs.append((w.clock.now().astimezone(KST).strftime("%H:%M:%S"), trig))
        if results:
            return _Res(results.pop(0))
        return real(acct, trig)
    w.engine.reconciler.run = run  # type: ignore[method-assign]
    return runs


def test_v197_requested_reconcile_right_after_close_is_close(world: World) -> None:
    runs = _record_triggers(world)
    world.at(15, 30)
    world.engine._last_reconcile = world.clock.now()  # noqa: SLF001 — 마감 시각 정기 대조 직후
    world.at(15, 30, 5)
    world.engine.request_reconcile()                  # 마감 직후 재접속 요청
    world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert runs == [("15:30:05", CLOSE_TRIGGER)]
    assert world.engine._closed_reconciled == world.clock.now().date()  # noqa: SLF001
    assert "K4" not in world.repo.active_halts(ACCOUNT)


def test_v198_requests_every_30s_after_close_close_once_not_delayed(world: World) -> None:
    runs = _record_triggers(world, ["MATCH"] * 50)
    world.at(15, 30)
    world.engine._last_reconcile = world.clock.now()  # noqa: SLF001
    for i in range(1, 21):                            # 10분간 30초 간격 요청
        world.clock._now += timedelta(seconds=30)  # noqa: SLF001
        world.engine.request_reconcile()
        world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert runs[0] == ("15:30:30", CLOSE_TRIGGER)     # 첫 대조가 CLOSE — 밀리지 않음
    assert [t for _, t in runs[1:]] == ["PERIODIC"] * 19


def test_v199_close_query_failed_next_request_or_interval_is_close_again(world: World) -> None:
    runs = _record_triggers(world, ["QUERY_FAILED", "MATCH", "QUERY_FAILED", "MATCH", "MATCH"])
    world.at(15, 30)
    world.engine._last_reconcile = world.clock.now()  # noqa: SLF001
    world.at(15, 30, 10)
    world.engine.request_reconcile()
    world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001 — CLOSE 조회 실패
    world.at(15, 30, 20)
    world.engine.request_reconcile()
    world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001 — 다음 요청 대조가 다시 CLOSE (완료)
    assert runs == [("15:30:10", CLOSE_TRIGGER), ("15:30:20", CLOSE_TRIGGER)]


def test_v199_close_query_failed_no_request_retries_after_interval(world: World) -> None:
    runs = _record_triggers(world, ["QUERY_FAILED", "MATCH"])
    world.at(15, 30)
    world.engine._last_reconcile = world.clock.now()  # noqa: SLF001
    for hh, mm, ss in ((15, 30, 30), (15, 31, 0), (15, 31, 30), (15, 32, 0), (15, 40, 0)):
        world.at(hh, mm, ss)
        world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert runs == [("15:31:00", CLOSE_TRIGGER), ("15:32:00", CLOSE_TRIGGER)]


def test_v199_no_close_before_close_time(world: World) -> None:
    runs = _record_triggers(world, ["MATCH"] * 3)
    world.at(15, 0)
    world.engine._last_reconcile = world.clock.now()  # noqa: SLF001
    world.at(15, 0, 10)
    world.engine.request_reconcile()
    world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert runs == [("15:00:10", "PERIODIC")]           # 마감 전 요청은 종전대로 PERIODIC


def test_v199_no_close_on_holiday(world: World) -> None:
    from tests.scenario.test_t020_app import CAL, DAY, kst
    d = DAY + timedelta(days=1)
    while CAL.is_trading_day(d):
        d += timedelta(days=1)
    runs = _record_triggers(world, ["MATCH"] * 3)
    world.clock._now = kst(d, 16, 0)  # noqa: SLF001
    world.engine._last_reconcile = None  # noqa: SLF001
    world.engine.request_reconcile()
    world.engine._reconcile_if_due(world.clock.now())  # noqa: SLF001
    assert runs == [("16:00:00", "PERIODIC")]


# --- P4 V-200: 장 밖 K5 대조 재시도 ------------------------------------------------------------------

def _k5_links_ok(w: World) -> None:
    """07:53 실시간 끊김 → K5, 재접속으로 실시간 경로 정상 (대조만 남음)."""
    q = QueuedHalts(w.engine)
    q.activate(HaltType.K5, "realtime", "실시간 끊김: Bye", ACCOUNT)
    q.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REALTIME, False, "실시간 끊김")
    q.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REALTIME, True, "실시간 RECONNECTED")
    w.engine._drain_calls()  # noqa: SLF001


def _step(w: World, hh: int, mm: int, ss: int = 0) -> None:
    w.at(hh, mm, ss)
    w.engine._reconcile_if_due(w.clock.now())  # noqa: SLF001


def test_v200_retry_every_5min_then_success_releases_k5(world: World) -> None:
    world.at(7, 53)
    _k5_links_ok(world)
    runs = _record_triggers(world, ["QUERY_FAILED", "QUERY_FAILED"])   # 요청 대조 실패, 1차 재시도 실패, 이후 실제 대조
    world.engine.request_reconcile()
    _step(world, 7, 53)
    _step(world, 7, 57, 59)                      # 5분 전: 재시도 없음
    _step(world, 7, 58)                          # 1차 재시도 (실패)
    _step(world, 8, 3)                           # 2차 재시도 (실제 대조 — 일치)
    assert [t for t, _ in runs] == ["07:53:00", "07:58:00", "08:03:00"]
    assert "K5" not in world.repo.active_halts(ACCOUNT)
    _step(world, 8, 8)
    _step(world, 8, 30)
    assert len(runs) == 3                        # K5 해제 뒤 재시도 중단


def test_v200_twelve_failures_then_stop_and_warn_once(world: World) -> None:
    world.at(7, 0)
    _k5_links_ok(world)
    runs = _record_triggers(world, ["QUERY_FAILED"] * 30)
    world.engine.request_reconcile()
    _step(world, 7, 0)
    t = world.clock.now()
    for _ in range(14 * 5):                     # 70분 동안 1분마다
        t += timedelta(minutes=1)
        world.clock._now = t  # noqa: SLF001
        world.engine._reconcile_if_due(t)  # noqa: SLF001
    assert len(runs) == 1 + 12                   # 요청 1 + 재시도 최대 12
    assert runs[1][0] == "07:05:00" and runs[-1][0] == "08:00:00"
    warns = [m for m in alerts_of(world) if "장 밖 K5 대조 재시도" in m]
    assert len(warns) == 1
    assert "K5" in world.repo.active_halts(ACCOUNT)   # 정지 해제 조건은 바뀌지 않음


def test_v200_regular_session_takes_over(world: World) -> None:
    world.at(8, 50)
    _k5_links_ok(world)
    runs = _record_triggers(world, ["QUERY_FAILED"] * 10)
    world.engine.request_reconcile()
    _step(world, 8, 50)
    _step(world, 8, 55)                          # 재시도 1
    _step(world, 9, 0)                           # 정규장: 정기 대조(PERIODIC)가 넘겨받음
    _step(world, 9, 0, 30)                       # 정기 주기(1분) 전 — 5분 재시도 규칙 아님
    _step(world, 9, 1)
    assert [t for t, _ in runs] == ["08:50:00", "08:55:00", "09:00:00", "09:01:00"]


def test_v200_no_retry_without_k5_or_with_link_down(world: World) -> None:
    runs = _record_triggers(world, ["QUERY_FAILED"] * 10)
    world.at(7, 53)
    world.engine.request_reconcile()
    _step(world, 7, 53)
    _step(world, 7, 58)
    _step(world, 8, 3)
    assert len(runs) == 1                        # K5 없음 → 재시도 없음
    q = QueuedHalts(world.engine)
    q.activate(HaltType.K5, "realtime", "실시간 끊김", ACCOUNT)   # 실시간 경로 끊긴 채
    world.engine._drain_calls()  # noqa: SLF001
    _step(world, 8, 10)
    _step(world, 8, 20)
    assert len(runs) == 1                        # 연결 경로가 정상이 아니면 재시도 없음 (대조로 풀 수 없음)


# --- P4 보완 A-076: 조회 경로(link:rest) 끊김은 대조 성공으로 풀리는 조건 ---------------------------------

def test_v200_a076_rest_link_down_after_three_failures_still_retried_then_released(world: World) -> None:
    """1일차·2일차 유형: 실시간 재접속(정상) → 대조 조회 3회 연속 실패(대조기가 link:rest 끊김 기록) →
    뒤의 재시도에서 조회 성공 → 장 전에 K5 자동 해제."""
    from trading_system.halt.manager import K5_LINK_REST
    world.at(7, 53)
    _k5_links_ok(world)
    world.broker.disconnected = True                 # 키움 모의 조회 실패(코드 20) 재현
    world.engine.request_reconcile()
    _step(world, 7, 53)                              # 실패 1 (요청 대조)
    _step(world, 7, 58)                              # 실패 2 (재시도 1)
    _step(world, 8, 3)                               # 실패 3 (재시도 2) → 대조기가 K5에 link:rest 끊김 기록
    [k5] = [r for r in world.repo.halt_rows(ACCOUNT) if r["halt_type"] == "K5"]
    assert json.loads(k5["conditions"])[K5_LINK_REST]["ok"] is False
    world.broker.disconnected = False
    _step(world, 8, 8)                               # 재시도 3: 조회 성공 → link:rest 정상 + 대조 일치
    assert "K5" not in world.repo.active_halts(ACCOUNT)


def test_v200_a076_realtime_link_down_stops_retries_even_with_rest_down(world: World) -> None:
    world.at(7, 53)
    _k5_links_ok(world)
    runs = _record_triggers(world, ["QUERY_FAILED"] * 10)
    world.engine.request_reconcile()
    _step(world, 7, 53)
    q = QueuedHalts(world.engine)
    q.activate(HaltType.K5, "reconcile", "대조 조회 연속 3회 실패", ACCOUNT)       # link:rest 끊김
    q.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REALTIME, False, "실시간 다시 끊김")
    world.engine._drain_calls()  # noqa: SLF001
    _step(world, 7, 58)
    _step(world, 8, 3)
    assert len(runs) == 1                            # 실시간 경로가 끊긴 동안은 재시도 없음 (대조로 못 풂)
