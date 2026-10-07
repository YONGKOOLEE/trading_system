"""T-017 후속: PAPER 운영 중 "남은 위험(낮음)" 중 주문 상태 전이·게이트·정지 규칙을 바꾸지 않는 수정 (A-070).

1. status 정지 발동 시각을 KST로 표시한다 (다른 시각 항목과 같게).
2. 텔레그램 스레드(알림 전송·명령 수신)가 죽으면 감시 스레드가 긴급 알림을 낸다. 정지(K5 등)는 걸지 않는다 —
   정지 규칙 추가는 승인 대상이라 제안으로 남김. → CP-028(A-074)로 알림 전송 스레드 종료는 K11로 바뀜
   (test_cp028_resilience V-194). 명령 수신 스레드는 그대로 알림만(Q-2).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.scenario.test_rv_loop import Mono, alerts_of, make_sup, world  # noqa: F401 — 픽스처 재사용
from tests.scenario.test_t020_app import ACCOUNT, World
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.ops.cli import status_main
from trading_system.storage.repository import Repository

T0 = datetime(2026, 10, 5, 0, 31, tzinfo=UTC)  # 09:31 KST


# --- 1. status 정지 발동 시각 KST -------------------------------------------------------------

def test_status_halt_time_shown_in_kst(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = Repository(tmp_path / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        with repo.tx():
            repo.insert_halt("K2", "KR-PAPER-01", "test", "손실", T0)
    finally:
        repo.close()
    assert status_main(["--mode", "PAPER", "--root", str(tmp_path)], clock=FixedClock(T0)) == 0
    out = capsys.readouterr().out
    assert "발동=10-05 09:31:00" in out
    assert "+00:00" not in out.split("해제되지 않은 정지", 1)[1].split("처리 대기 명령", 1)[0]


# --- 2. 텔레그램 스레드 생존 감시 ---------------------------------------------------------------

def _dead(name: str) -> threading.Thread:
    t = threading.Thread(target=lambda: None, name=name)
    t.start()
    t.join()
    return t


def test_dead_telegram_thread_alerts_once_without_halt(world: World, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    sup = make_sup(world, Mono(), watched_alert_only=[(_dead("telegram-poll"), "텔레그램 명령 수신")])
    sup.step()
    # 알림 반복 억제(60초)를 끄고 본다 — 억제가 아니라 감시가 1회만 내는지 (재검토 #1). 시계를 진행하면 시계 점프 K8이 걸려 끔
    monkeypatch.setattr(world.engine.alerts, "_window", None)
    sup.step(waited=1.0)
    world.engine._drain_calls()  # noqa: SLF001
    msgs = [m for m in alerts_of(world) if m.startswith("텔레그램 명령 수신 스레드 종료")]
    assert len(msgs) == 1                                   # 한 번만
    assert world.repo.active_halts(ACCOUNT) == []          # 정지 규칙은 바꾸지 않는다


def test_unstarted_telegram_thread_not_reported(world: World) -> None:  # noqa: F811
    t = threading.Thread(target=lambda: None, name="telegram-notify")   # 시작 전(ident None)
    sup = make_sup(world, Mono(), watched_alert_only=[(t, "텔레그램 알림 전송")])
    sup.step()
    assert not any("스레드 종료" in m for m in alerts_of(world))


def test_watched_threads_includes_telegram_poll_as_alert_only() -> None:
    """CP-028(A-074 Q-1 B·Q-2)로 바뀜: 알림 전송 스레드는 K11 감시(V-194), 명령 수신 스레드만 알림만."""
    from trading_system.app.main import watched_threads
    io = [threading.Thread(name="market-poller"), threading.Thread(name="telegram-notify"),
          threading.Thread(name="telegram-poll"), threading.Thread(name="other")]
    halting, k11, alert_only = watched_threads(io, None)
    assert [label for _, label in halting] == ["시세 조회"]
    assert [label for _, label in k11] == ["텔레그램 알림 전송"]
    assert [label for _, label in alert_only] == ["텔레그램 명령 수신"]
