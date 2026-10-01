"""TRADE_MODE 검증 (R-01, CLAUDE.md §7).

BACKTEST|PAPER|LIVE만 허용한다. 값이 없거나 잘못되면 ConfigError — 호출자는 브로커 연결 없이 종료한다.
`.env`에서는 TRADE_MODE 한 줄만 읽는다. 다른 줄(비밀 포함)은 해석하지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from trading_system.domain.enums import Mode

from .errors import ConfigError

_MODE_LINE = re.compile(r"^TRADE_MODE=(.*)$")


def parse_trade_mode(raw: str | None) -> Mode:
    if raw is None:
        raise ConfigError("TRADE_MODE가 없다 (BACKTEST|PAPER|LIVE)")
    try:
        return Mode(raw)
    except ValueError:
        shown = raw if len(raw) <= 20 else raw[:20] + "…"
        raise ConfigError(f"TRADE_MODE 값이 잘못되었다: {shown!r} (BACKTEST|PAPER|LIVE만 허용, 대문자 정확히)") from None


def _mode_line_from_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.lstrip("﻿").splitlines():
        match = _MODE_LINE.match(line.strip())
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return None


def read_trade_mode(environ: Mapping[str, str], env_path: Path | None) -> Mode:
    """환경 변수 → `.env` 순으로 읽는다. 환경 변수가 있으면 값이 틀려도 파일로 넘어가지 않는다."""
    if "TRADE_MODE" in environ:
        return parse_trade_mode(environ["TRADE_MODE"])
    raw = _mode_line_from_file(env_path) if env_path is not None else None
    return parse_trade_mode(raw)
