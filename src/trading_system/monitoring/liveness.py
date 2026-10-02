"""생존·지연 감시 (R-18, 03 §8.4·D-054·D-064, 04 V-61·V-111).

- 하트비트: 본 프로세스가 5초마다 `var/<mode>/heartbeat`에 시각을 쓴다.
- 루프 지연·시계 점프: 1초 타이머의 실제 간격이 5초를 넘거나, 벽시계와 단조 시계의 차이가 벌어지면
  (절전·일시 정지 의심) 재시작과 같은 복구 절차로 간다: K8 발동(위험 확대 중단) → 대조 → 사용자 재개 (D-064).
- watchdog(별도 프로세스)은 하트비트가 30초 넘게 멈추면 알림만 한다. 자동 재시작은 하지 않는다 (D-054).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_system.domain.enums import HaltType
from trading_system.domain.sinks import AlertSink, HaltControl

HEARTBEAT_INTERVAL = timedelta(seconds=5)
HEARTBEAT_STALE = timedelta(seconds=30)
LOOP_DELAY_LIMIT = 5.0      # 초
CLOCK_JUMP_LIMIT = 2.0      # 벽시계와 단조 시계 차이 (초)


def write_heartbeat(path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(now.astimezone(UTC).isoformat(), encoding="utf-8")
    tmp.replace(path)  # 원자적 교체: watchdog가 반쯤 쓴 파일을 읽지 않게


@dataclass(frozen=True)
class WatchdogStatus:
    alive: bool
    age_seconds: float | None
    detail: str


def watchdog_check(path: Path, now: datetime, stale: timedelta = HEARTBEAT_STALE) -> WatchdogStatus:
    if not path.exists():
        return WatchdogStatus(False, None, "하트비트 파일 없음 — 본 프로세스가 시작되지 않았거나 멈춤")
    try:
        at = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return WatchdogStatus(False, None, "하트비트 파일을 읽을 수 없음")
    age = (now - at).total_seconds()
    if age > stale.total_seconds():
        return WatchdogStatus(False, age, f"하트비트 {age:.0f}초 멈춤 (기준 {stale.total_seconds():.0f}초)")
    if age < -stale.total_seconds():
        return WatchdogStatus(False, age, "하트비트 시각이 미래 — 시계 이상 의심")
    return WatchdogStatus(True, age, "정상")


class LoopMonitor:
    """1초 타이머마다 tick(단조 시각, 벽시각)을 부른다. 지연·점프를 감지하면 K8 + 알림 (V-61)."""

    def __init__(self, halts: HaltControl | None, alerts: AlertSink | None) -> None:
        self._halts = halts
        self._alerts = alerts
        self._last: tuple[float, datetime] | None = None
        self.events: list[str] = []

    def tick(self, mono: float, wall: datetime) -> str | None:
        prev, self._last = self._last, (mono, wall)
        if prev is None:
            return None
        d_mono = mono - prev[0]
        d_wall = (wall - prev[1]).total_seconds()
        reason = None
        if d_mono > LOOP_DELAY_LIMIT:
            reason = f"루프 지연 {d_mono:.1f}초 (기준 {LOOP_DELAY_LIMIT}초)"
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
