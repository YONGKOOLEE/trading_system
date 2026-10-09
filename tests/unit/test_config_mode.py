"""TRADE_MODE 검증 (04 V-100, R-01, CLAUDE.md §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_system.config.errors import ConfigError
from trading_system.config.modes import parse_trade_mode, read_trade_mode
from trading_system.domain.enums import Mode


@pytest.mark.parametrize("raw", ["BACKTEST", "PAPER", "LIVE"])
def test_valid_modes(raw: str) -> None:
    assert parse_trade_mode(raw) is Mode(raw)


@pytest.mark.parametrize("raw", [None, "", " ", "live", "Paper", "TEST", "LIVE ", " PAPER", "BACKTEST\n", "PROD"])
def test_invalid_modes_rejected(raw: str | None) -> None:
    with pytest.raises(ConfigError):
        parse_trade_mode(raw)


def test_error_message_is_bounded() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_trade_mode("X" * 5000)
    assert len(str(exc.value)) < 300


def test_environment_variable_wins_over_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TRADE_MODE=LIVE\n", encoding="utf-8")
    assert read_trade_mode({"TRADE_MODE": "BACKTEST"}, env) is Mode.BACKTEST


def test_invalid_environment_value_does_not_fall_back_to_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TRADE_MODE=BACKTEST\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        read_trade_mode({"TRADE_MODE": "live"}, env)


def test_reads_only_trade_mode_line_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("this line is not valid at all\nKIWOOM_PAPER_APP_KEY=SECRET\nTRADE_MODE=PAPER\n", encoding="utf-8")
    assert read_trade_mode({}, env) is Mode.PAPER


def test_missing_everywhere_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        read_trade_mode({}, tmp_path / ".env")
    with pytest.raises(ConfigError):
        read_trade_mode({}, None)


def test_env_file_value_must_be_valid(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TRADE_MODE=live\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        read_trade_mode({}, env)


def test_utf16_mode_file_is_treated_as_missing(tmp_path: Path) -> None:
    """2026-10-09: PowerShell 5.1의 `>`·Out-File 기본 인코딩(UTF-16)으로 만든 모드 파일은 읽기 예외가 아니라
    '모드 없음'(ConfigError, 시작 거부)으로 처리한다."""
    env = tmp_path / "mode-file"
    env.write_text("TRADE_MODE=PAPER\n", encoding="utf-16")
    with pytest.raises(ConfigError):
        read_trade_mode({}, env)


def test_ascii_mode_file_from_powershell_set_content(tmp_path: Path) -> None:
    """사용 설명서 명령 `Set-Content -Encoding ascii`로 만든 파일(줄 끝 CRLF)은 정상으로 읽힌다."""
    env = tmp_path / "mode-file"
    env.write_bytes(b"TRADE_MODE=PAPER\r\n")
    assert read_trade_mode({}, env) is Mode.PAPER
