"""3차 검토 정지 결함 (A-045): 자동 해제 후 막힌 취소 재시도 · RESUME K1 처리 중 들어온 KILL.

1. (높음) 다른 정지(K5 등)에 막힌 K1·K2·K9 매수 미체결 취소는 그 정지가 **자동 해제**될 때도 다시 시도한다
   (CP-018 R-10). 재시도는 이미 활성인 취소를 다시 보내지 않는다(INV-17), 해제 사건마다 한 번뿐이다.
2. (낮음) RESUME K1을 적용하는 순간(저장 단위 안)에 그 RESUME보다 뒤에 들어온 대기 KILL이 있으면 RESUME을 거부한다
   (사용자의 마지막 의사 = KILL, 재검토2 #4의 같은 묶음 규칙을 처리 중 도착분까지 확장).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_rv_halts import _cancels
from tests.scenario.test_t005_halts import _limit_buy
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType
from trading_system.halt.manager import K5_LINK_REST


def _k5_auto_release(env: Env) -> list[str]:
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REST, True, "조회 성공")
    env.clock.advance(timedelta(seconds=1))
    return env.halts.report_condition(HaltType.K5, ACCOUNT, "reconciled", True, "일치")


def _blocked_k1_cancel(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.halts.activate(HaltType.K5, "broker", "토큰 실패", ACCOUNT)
    env.halts.activate(HaltType.K1, "LOCAL:u", "킬", None)
    assert _cancels(env) == 0                       # K5에 막혀 취소가 나가지 못함 (전제)


# --- 1. 자동 해제 후 막힌 취소 재시도 ---------------------------------------------------------------

def test_k1_cancel_blocked_by_k5_is_retried_when_k5_auto_released(env: Env) -> None:
    _blocked_k1_cancel(env)
    released = _k5_auto_release(env)
    assert released                                  # K5 자동 해제
    assert "K5" not in env.repo.active_halts(ACCOUNT)
    assert "K1" in env.repo.active_halts(ACCOUNT)
    assert _cancels(env) == 1                        # 남은 K1의 매수 미체결 취소를 다시 시도


def test_retry_after_auto_release_does_not_resend_active_cancel(env: Env) -> None:
    _blocked_k1_cancel(env)
    _k5_auto_release(env)
    assert _cancels(env) == 1
    # 다른 자동 해제 유형이 또 풀려도 이미 접수된 취소(활성)는 다시 보내지 않는다 (INV-17, §8-7)
    env.halts.activate(HaltType.K6, "clock", "시계 오차", None)
    env.halts.report_condition(HaltType.K6, None, "clock_ok", True, "오차 0초")
    assert "K6" not in env.repo.active_halts(ACCOUNT)
    assert _cancels(env) == 1
    # 해제 없는 조건 보고(같은 조건 재보고 포함)는 재시도하지 않는다
    env.halts.report_condition(HaltType.K5, ACCOUNT, "reconciled", True, "일치")
    env.halts.report_condition(HaltType.K1, None, "anything", True, "")
    assert _cancels(env) == 1


def test_no_retry_while_blocking_halt_not_released(env: Env) -> None:
    _blocked_k1_cancel(env)
    env.clock.advance(timedelta(seconds=1))
    assert env.halts.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REST, True, "조회 성공") == []
    assert _cancels(env) == 0


def test_auto_release_inside_outer_tx_does_not_send(env: Env) -> None:
    """바깥 저장 단위 안에서 해제되면 전송하지 않는다(INV-03). 다음 해제·재개 때 다시 시도된다."""
    _blocked_k1_cancel(env)
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REST, True, "조회 성공")
    env.clock.advance(timedelta(seconds=1))
    with env.repo.tx():
        released = env.halts.report_condition(HaltType.K5, ACCOUNT, "reconciled", True, "일치")
    assert released
    assert _cancels(env) == 0


# --- 2. RESUME K1 적용 중 도착한 KILL ----------------------------------------------------------------

def test_resume_k1_rejected_when_kill_arrives_after_pending_read(tmp_path: Path) -> None:
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    w = World(tmp_path)
    try:
        e = w.engine
        e.start()
        w.resume_k8()
        w.command("KILL", {"account": None, "reason": "k1"})
        e.tick()
        assert "K1" in w.repo.active_halts(APP_ACCOUNT)
        w.at(9, 5)
        w.command("RESUME", {"account": None, "halt_type": "K1"})
        orig = e.ops._halts.resume   # noqa: SLF001

        def resume(*a, **k):  # type: ignore[no-untyped-def]
            # 대기 명령을 읽은 뒤, RESUME 적용 전에 사용자가 다시 KILL — 감시 스레드 fast_kill은 기존 K1을 보고 기록 생략
            w.command("KILL", {"account": None, "reason": "재킬"})
            assert e.fast_kill() == 0
            return orig(*a, **k)
        e.ops._halts.resume = resume  # noqa: SLF001
        e.tick()
        e.ops._halts.resume = orig    # noqa: SLF001
        assert "K1" in w.repo.active_halts(APP_ACCOUNT)                    # 마지막 의사(KILL) 유지
        assert not [i for i in w.intents() if i["request_type"] == "ENTRY"]  # 같은 tick 신규 진입 없음
        rows = w.repo.select("SELECT command, args, status, result FROM ops_command ORDER BY created_seq")
        resume_row = [r for r in rows if r["command"] == "RESUME" and '"K1"' in r["args"]][-1]
        assert resume_row["status"] == "REJECTED"
        assert "뒤이은 KILL" in resume_row["result"]
        e.tick()                                                              # 뒤이은 KILL은 다음 tick에 처리
        assert "K1" in w.repo.active_halts(APP_ACCOUNT)
        assert not [i for i in w.intents() if i["request_type"] == "ENTRY"]
        assert all(r["status"] != "PENDING" for r in w.repo.select("SELECT status FROM ops_command"))
    finally:
        w.close()


def test_resume_k1_without_later_kill_still_works(tmp_path: Path) -> None:
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    w = World(tmp_path)
    try:
        e = w.engine
        e.start()
        w.resume_k8()
        w.command("KILL", {"account": None, "reason": "k1"})
        e.tick()
        w.command("RESUME", {"account": None, "halt_type": "K1"})
        e.tick()
        assert "K1" not in w.repo.active_halts(APP_ACCOUNT)
    finally:
        w.close()
