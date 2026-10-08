"""하트비트 읽기 — 단일 정의 (R-18, 03 D-054, 04 V-111, A-084 ②).

본 프로세스가 `var/<mode>/heartbeat`에 쓴 시각을 읽어 생존을 판정한다. watchdog(monitoring.liveness 재노출),
preflight(app), status(ops/cli)가 모두 이 함수를 쓴다 — ops는 monitoring을 import하지 못하므로 domain에 둔다.
Windows에서는 쓰는 쪽의 원자적 교체(replace) 순간 읽기가 PermissionError로 실패할 수 있어 짧게 재시도한다 (RV-06, 3일차 결함).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

HEARTBEAT_STALE = timedelta(seconds=30)   # 이보다 오래 멈추면 이상 (D-054). preflight·status도 같은 기준
HEARTBEAT_RETRIES = 3                      # 읽기·쓰기 재시도 횟수 (RV-06)


@dataclass(frozen=True)
class WatchdogStatus:
    alive: bool
    age_seconds: float | None
    detail: str
    at: datetime | None = None             # 읽은 하트비트 시각 (읽지 못했으면 None)


def watchdog_check(path: Path, now: datetime, stale: timedelta = HEARTBEAT_STALE, *,
                   retries: int = HEARTBEAT_RETRIES, sleep: Callable[[float], None] = time.sleep) -> WatchdogStatus:
    """하트비트 파일을 읽어 생존 판정. 없음·읽기 실패·시간대 없음·멈춤·미래 시각은 모두 alive=False."""
    if not path.exists():
        return WatchdogStatus(False, None, "없음 — 본 프로세스가 시작되지 않았거나 멈춤")
    at = None
    for i in range(retries):
        try:
            at = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
            break
        except (OSError, ValueError):
            if i < retries - 1:
                sleep(0.05)
    if at is None:
        return WatchdogStatus(False, None, "읽을 수 없음")
    if at.tzinfo is None:
        return WatchdogStatus(False, None, "시간대 없는 시각 — 판정 불가", at)
    age = (now - at).total_seconds()
    if age > stale.total_seconds():
        return WatchdogStatus(False, age, f"하트비트 {age:.0f}초 멈춤 (기준 {stale.total_seconds():.0f}초)", at)
    if age < -stale.total_seconds():
        return WatchdogStatus(False, age, "하트비트 시각이 미래 — 시계 이상 의심", at)
    return WatchdogStatus(True, age, "정상", at)
