"""생존·지연 감시 (R-18, 03 §8.4·D-054·D-064, 04 V-61·V-111).

- 하트비트: 본 프로세스가 5초마다 `var/<mode>/heartbeat`에 시각을 쓴다.
- 루프 지연·시계 점프: 1초 타이머의 실제 간격이 5초를 넘거나, 벽시계와 단조 시계의 차이가 벌어지면
  (절전·일시 정지 의심) 재시작과 같은 복구 절차로 간다: K8 발동(위험 확대 중단) → 대조 → 사용자 재개 (D-064).
- watchdog(별도 프로세스)은 하트비트가 30초 넘게 멈추면 알림만 한다. 자동 재시작은 하지 않는다 (D-054).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_system.domain.enums import HaltType
from trading_system.domain.sinks import AlertSink, HaltControl

HEARTBEAT_INTERVAL = timedelta(seconds=5)
HEARTBEAT_STALE = timedelta(seconds=30)
LOOP_DELAY_LIMIT = 5.0      # 초
CLOCK_JUMP_LIMIT = 2.0      # 벽시계와 단조 시계 차이 (초)


HEARTBEAT_RETRIES = 3       # Windows: watchdog·preflight가 읽는 순간 교체가 PermissionError로 실패할 수 있다 (RV-06)


def write_heartbeat(path: Path, now: datetime, *, retries: int = HEARTBEAT_RETRIES,
                    sleep: Callable[[float], None] = time.sleep) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(now.astimezone(UTC).isoformat(), encoding="utf-8")
    for i in range(retries):
        try:
            tmp.replace(path)  # 원자적 교체: watchdog가 반쯤 쓴 파일을 읽지 않게
            return
        except PermissionError:
            if i == retries - 1:
                raise
            sleep(0.05)


@dataclass(frozen=True)
class WatchdogStatus:
    alive: bool
    age_seconds: float | None
    detail: str


def watchdog_check(path: Path, now: datetime, stale: timedelta = HEARTBEAT_STALE, *,
                   retries: int = HEARTBEAT_RETRIES, sleep: Callable[[float], None] = time.sleep) -> WatchdogStatus:
    if not path.exists():
        return WatchdogStatus(False, None, "하트비트 파일 없음 — 본 프로세스가 시작되지 않았거나 멈춤")
    at = None
    for i in range(retries):  # 쓰는 쪽 교체(replace) 순간 Windows 읽기가 실패할 수 있다 — 쓰기(RV-06)처럼 짧게 재시도
        try:
            at = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
            break
        except (OSError, ValueError):
            if i < retries - 1:
                sleep(0.05)
    if at is None:
        return WatchdogStatus(False, None, "하트비트 파일을 읽을 수 없음")
    age = (now - at).total_seconds()
    if age > stale.total_seconds():
        return WatchdogStatus(False, age, f"하트비트 {age:.0f}초 멈춤 (기준 {stale.total_seconds():.0f}초)")
    if age < -stale.total_seconds():
        return WatchdogStatus(False, age, "하트비트 시각이 미래 — 시계 이상 의심")
    return WatchdogStatus(True, age, "정상")


class LoopMonitor:
    """1초 타이머마다 tick(단조 시각, 벽시각)을 부른다. 지연·점프를 감지하면 K8 + 알림 (V-61).

    RV-05: 단조 시각과 벽시각은 **같은 순간**에 채취해 넘긴다. 감시 스레드(app/supervisor.py)가 부르며,
    `waited`(타이머를 건 뒤 실제로 깨어나기까지 걸린 초)를 주면 루프 지연을 그 값으로 판정한다 — 감시 자신의
    작업 시간(SNTP 조회 등)이나 판정·실행 스레드의 작업 시간은 지연이 아니다. 지연은 프로세스 전체가 멈춘 경우
    (절전·일시 정지)만 뜻한다 (D-064). 판정·실행 스레드의 멈춤은 감시 스레드가 따로 알림한다.
    """

    def __init__(self, halts: HaltControl | None, alerts: AlertSink | None) -> None:
        self._halts = halts
        self._alerts = alerts
        self._last: tuple[float, datetime] | None = None
        self.events: list[str] = []

    def tick(self, mono: float, wall: datetime, waited: float | None = None) -> str | None:
        prev, self._last = self._last, (mono, wall)
        if prev is None:
            return None
        d_mono = mono - prev[0]
        d_wall = (wall - prev[1]).total_seconds()
        delay = d_mono if waited is None else waited
        reason = None
        if delay > LOOP_DELAY_LIMIT:
            reason = f"루프 지연 {delay:.1f}초 (기준 {LOOP_DELAY_LIMIT}초)"
        elif abs(d_wall - d_mono) > CLOCK_JUMP_LIMIT:
            reason = f"시계 점프 {d_wall - d_mono:+.1f}초 (절전·일시 정지 의심)"
        if reason is None:
            return None
        self.events.append(reason)
        if self._halts is not None:
            self._halts.activate(HaltType.K8, "monitor", f"{reason} — 재시작과 같은 복구 필요 (D-064)", None)
        if self._alerts is not None:
            self._alerts.alert("EMERGENCY", f"{reason}: 위험 확대 중단, 대조 후 사용자 재개 필요", None)
        return reason
