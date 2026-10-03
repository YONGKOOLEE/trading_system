"""운영 로그 (RV-16): 알림·예외를 사람이 바로 볼 수 있게 콘솔(stderr)과 운영 로그 파일에 남긴다.

- 파일: `var/<mode>/logs/ops-YYYYMMDD.jsonl` (JsonLogger 재사용, KST 날짜, 보존·압축은 archive-logs와 같다).
- 쓰기 전에 Masker로 가린다(계좌번호 형태·토큰·Bearer 등). 원문을 먼저 남기지 않는다.
- 이 모듈의 함수는 예외를 밖으로 내보내지 않는다. 운영 로그 실패가 본 루프를 멈추게 하지 않는다.
"""

from __future__ import annotations

import re
import sys
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from trading_system.domain.clock import KST

from .jsonlog import JsonLogger, Masker

OPS_LOG_NAME = "ops"
TRACEBACK_MAX = 8000
HALT_TYPE = re.compile(r"(?<![A-Za-z0-9])K(?:1[01]|[1-9])(?![0-9])")

_MASKER = Masker()


def halt_type_of(message: str) -> str | None:
    """알림 문구 속 정지 유형(K1~K11). 없으면 None."""
    m = HALT_TYPE.search(message)
    return m.group(0) if m else None


def console_line(at: datetime, level: str, message: str, ref: str | None = None) -> str:
    """콘솔 한 줄: `[YYYY-MM-DD HH:MM:SS KST] LEVEL Kx 문구 (ref)` — 가림 처리 후."""
    kt = halt_type_of(message)
    stamp = at.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    text = _MASKER.text(message)
    tail = f" ({_MASKER.text(ref)})" if ref else ""
    return f"[{stamp}] {level}{' ' + kt if kt else ''} {text}{tail}"


def write_console(line: str) -> None:
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def write_ops(log_dir: Path | None, event: Mapping[str, object], at: datetime | None = None) -> bool:
    """운영 로그 한 줄. 실패하면 False (예외 없음)."""
    if log_dir is None:
        return False
    try:
        JsonLogger(log_dir, OPS_LOG_NAME, _MASKER).write(event, at)
        return True
    except (OSError, TypeError, ValueError):
        return False


def log_exception(log_dir: Path | None, where: str, exc: BaseException, *, at: datetime | None = None,
                  console: bool = True) -> None:
    """예외 traceback을 가려서 운영 로그에 남기고, 콘솔에는 한 줄 요약만 쓴다. 예외를 내보내지 않는다.

    엔진·main에서 except 블록 안에서 부른다: `log_exception(base / "logs", "engine.step:<이름>", exc)`.
    """
    now = at or datetime.now(UTC)
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-TRACEBACK_MAX:]
        write_ops(log_dir, {"kind": "EXCEPTION", "where": where, "error": type(exc).__name__,
                            "message": str(exc), "traceback": tb}, now)
        if console:
            write_console(console_line(now, "ERROR", f"예외 {where}: {type(exc).__name__}: {exc}"))
    except Exception:  # noqa: BLE001 — 로그 실패로 본 루프를 멈추지 않는다
        pass
