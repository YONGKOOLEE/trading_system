"""감시 스레드 (RV-22·RV-05·RV-06·RV-18, 03 D-038 v0.5 "주문 호출이 응답을 기다려도 킬 스위치·감시는 멈추지 않는다").

판정·실행 스레드(엔진 tick)와 따로 1초마다 돈다. 하는 일:
1. KILL 명령 즉시 반영: K1 정지 행을 바로 기록한다(`Engine.fast_kill`). 매수 미체결 취소와 명령 완료 기록은
   판정·실행 스레드의 운영 명령 처리가 한다(주문 경로 하나, CLAUDE.md §8-2).
2. 루프 지연·시계 점프(LoopMonitor): 단조·벽시각을 같은 순간에 채취하고, 지연은 이 스레드의 타이머 늦음으로 판정(RV-05).
3. 판정·실행 스레드 멈춤: 마지막 진행 후 STALL_ALERT_SECONDS 초과 → 긴급 알림 1회 + 하트비트 중단(watchdog도 알림).
   K8은 걸지 않는다 — 01 K8은 "재시작 복구 대기"이고, 느리지만 살아 있는 스레드는 내부 상태를 잃지 않으며
   위험 확대는 그 스레드의 게이트·실행기에서만 일어나므로 멈춘 동안 새 위험이 생기지 않는다.
4. 하트비트 기록(재시도 포함), 10분마다 SNTP 시계 점검, 입출력 스레드 생존 감시(죽으면 긴급 알림 + K5,
   텔레그램 알림 전송 스레드는 긴급 알림 + K11 — CP-028 D-147, 텔레그램 명령 수신 스레드는 긴급 알림만 — Q-2).

정지 발동·조건 보고는 QueuedHalts로 판정·실행 스레드에 맡긴다(K1 행 기록만 예외 — Engine.fast_kill 설명 참조).
각 작업은 예외를 삼키고 기록한다: 감시 스레드가 죽으면 안 된다 (RV-06).
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from trading_system.domain.enums import HaltType
from trading_system.monitoring.liveness import LoopMonitor, write_heartbeat
from trading_system.storage.oplog import log_exception

if TYPE_CHECKING:
    from .engine import Engine

# 판정·실행 스레드 한 단계의 정당한 최대 대기: 호출 제한 대기 30초 + 주문 응답 13초 + 토큰 발급 최대 약 39초(RV-19)
# ≈ 82초. 여유를 두고 120초.
STALL_ALERT_SECONDS = 120.0
CLOCK_CHECK_SECONDS = 600.0       # 01 §2.7 10분마다


class QueuedHalts:
    """다른 스레드용 HaltControl: 실제 발동은 판정·실행 스레드에서 (정지 발동이 취소 주문을 낼 수 있음 — 주문 경로 하나)."""

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None, **kw: object) -> str:
        self._engine.call_soon(lambda: self._engine.halts.activate(halt_type, actor, reason, account, **kw))
        return ""

    def report_condition(self, halt_type: HaltType, account: str | None, condition: str, ok: bool,
                         detail: str = "", **kw: object) -> None:
        self._engine.call_soon(lambda: self._engine.halts.report_condition(halt_type, account, condition, ok,
                                                                           detail, **kw))


class _ClockCheck(Protocol):
    def check(self) -> object: ...


class Supervisor:
    def __init__(self, engine: "Engine", *, heartbeat: Path | None, fallback: Path | None,
                 clock_monitor: _ClockCheck | None = None, mono: Callable[[], float] = time.monotonic,
                 interval: float = 1.0, clock_every: float = CLOCK_CHECK_SECONDS,
                 stall_limit: float = STALL_ALERT_SECONDS,
                 watched: Sequence[tuple[threading.Thread, str]] = (),
                 watched_k11: Sequence[tuple[threading.Thread, str]] = (),
                 watched_alert_only: Sequence[tuple[threading.Thread, str]] = ()) -> None:
        self.engine = engine
        self.halts = QueuedHalts(engine)
        self.loop = LoopMonitor(self.halts, engine.alerts)
        self._heartbeat = heartbeat
        self._fallback = fallback
        self._clock_monitor = clock_monitor
        self._mono = mono
        self._interval = interval
        self._clock_every = clock_every
        self._stall_limit = stall_limit
        self._watched = list(watched)
        self._watched_k11 = list(watched_k11)                 # 알림 전송 스레드: 죽으면 알림 + K11 (CP-028 D-147)
        self._watched_alert_only = list(watched_alert_only)   # 명령 수신 스레드: 죽으면 알림만 (CP-028 Q-2)
        self._dead_reported: set[str] = set()
        self._stalled = False
        self._last_clock = mono()
        self.errors: list[str] = []

    # --- 한 주기 ---------------------------------------------------------------------
    def step(self, waited: float | None = None) -> None:
        wall = self.engine.clock.now()
        mono = self._mono()                               # 같은 순간 (RV-05)
        self._guard("KILL", self.engine.fast_kill)
        self._guard("루프 감시", lambda: self.loop.tick(mono, wall, waited))
        stalled = self._guard("판정 스레드 감시", lambda: self._check_stall(mono))
        if not stalled:
            self._guard("하트비트", lambda: self._write_heartbeat(wall))
        self._guard("입출력 스레드 감시", self._check_threads)
        if self._clock_monitor is not None and mono - self._last_clock >= self._clock_every:
            self._last_clock = mono
            self._guard("시계 점검", self._clock_monitor.check)

    def run(self, stop: threading.Event) -> None:
        waited: float | None = None
        while not stop.is_set():
            self.step(waited)
            t0 = self._mono()
            stop.wait(self._interval)
            waited = self._mono() - t0

    # --- 작업 -------------------------------------------------------------------------
    def _write_heartbeat(self, wall: datetime) -> None:
        if self._heartbeat is not None:
            write_heartbeat(self._heartbeat, wall)

    def _check_stall(self, mono: float) -> bool:
        age = mono - self.engine.last_progress
        if age <= self._stall_limit:
            if self._stalled:
                self._stalled = False
                self.engine.alerts.alert("INFO", "판정·실행 스레드 진행 재개", None)
            return False
        if not self._stalled:
            self._stalled = True
            self.engine.alerts.alert("EMERGENCY", f"판정·실행 스레드 {age:.0f}초 진행 없음 (기준 {self._stall_limit:.0f}초) "
                                                  f"— 하트비트 중단, 사용자 확인 필요", None)
        return True

    def _check_threads(self) -> None:
        for t, label in self._watched:
            if t.is_alive() or label in self._dead_reported or t.ident is None:
                continue
            self._dead_reported.add(label)
            self.engine.alerts.alert("EMERGENCY", f"{label} 스레드 종료 — K5, 재시작 필요", None)
            self.halts.activate(HaltType.K5, "thread", f"{label} 스레드 종료", self.engine.account)  # 대조로 안 풀림 (F2)
        for t, label in self._watched_k11:
            if t.is_alive() or label in self._dead_reported or t.ident is None:
                continue
            self._dead_reported.add(label)
            # 알림 채널 장애 = K11(위험 확대 차단, 위험 축소 허용). 해제는 알림 전송 성공(notify_ok)뿐이라 스레드가
            # 죽었으면 재시작 전까지 풀리지 않는다. 자동 재시작은 하지 않는다(D-054). 이 알림은 텔레그램으로는 못 가고
            # 콘솔·운영 로그·감사 ALERT(status)로만 보인다. 발동은 판정·실행 스레드에서(QueuedHalts, D-038)
            self.engine.alerts.alert("EMERGENCY", f"{label} 스레드 종료 — K11, 재시작 필요", None)
            self.halts.activate(HaltType.K11, "thread", f"{label} 스레드 종료", None)
        for t, label in self._watched_alert_only:
            if t.is_alive() or label in self._dead_reported or t.ident is None:
                continue
            self._dead_reported.add(label)
            # 정지는 걸지 않는다(CP-028 Q-2: 명령 수신 스레드 — 알림은 계속 가므로 사람이 안다)
            self.engine.alerts.alert("EMERGENCY", f"{label} 스레드 종료 — 재시작 필요(정지 없음)", None)

    def _guard(self, name: str, fn: Callable[[], object]) -> object:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — 감시 스레드는 죽지 않는다 (RV-06)
            msg = f"{name}: {type(exc).__name__}: {exc}"
            self.errors.append(msg)
            del self.errors[:-50]
            write_fallback(self._fallback, f"감시 작업 실패 {msg}\n{traceback.format_exc(limit=5)}")
            log_exception(self.engine.log_dir, f"supervisor:{name}", exc)
            try:
                self.engine.alerts.alert("WARNING", f"감시 작업 실패 {name}: {type(exc).__name__}", None)
            except Exception:  # noqa: BLE001
                pass
            return None


def write_fallback(path: Path | None, text: str) -> None:
    """저장소와 독립된 로컬 파일 기록 (var/<mode>/logs). 실패하면 조용히 넘어간다(최후 수단)."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(UTC).isoformat()}\t{text}\n")
    except OSError:
        pass


__all__ = ["QueuedHalts", "Supervisor", "STALL_ALERT_SECONDS", "write_fallback"]
