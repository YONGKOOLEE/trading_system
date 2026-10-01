"""시작 검사 (04 V-100~V-102, R-01, 03 §8.2, D-066 일부).

TRADE_MODE·저장소·자격정보·브로커 환경이 모두 같은 모드일 때만 시작한다.
LIVE는 승인 기록·활성화 설정이 모두 필요하고 이 구현에는 실전 경로가 없으므로 항상 거부한다.
테스트에는 가짜 값만 쓴다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.limits_fixtures import TOML
from trading_system.app.startup import LiveNotAvailable, ModeConsistencyError, check_startup, expected_broker_env
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import Mode

FAKE = "FAKE-VALUE-NOT-A-REAL-SECRET"


def make_root(tmp_path: Path, mode: str = "paper") -> Path:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "var" / mode).mkdir(parents=True)
    (root / "config" / f"limits.{mode}.toml").write_text(TOML, encoding="utf-8")
    return root


def paper_env(root: Path, body: str | None = None) -> Path:
    p = root / ".env.paper"
    p.write_text(body if body is not None else f"TRADE_MODE=PAPER\nKIWOOM_PAPER_APP_KEY={FAKE}\n", encoding="utf-8")
    return p


# --- V-102: 모드 값만 바꿔 LIVE로 시작 --------------------------------------------

def test_live_is_always_refused_without_reading_anything(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(LiveNotAvailable):
        check_startup(Mode.LIVE, root, root / "var" / "live" / "t.db", root / ".env.live")  # 파일이 없어도 LIVE 거부가 먼저


def test_live_refused_even_with_all_files_present(tmp_path: Path) -> None:
    root = make_root(tmp_path, "live")
    (root / ".env.live").write_text("TRADE_MODE=LIVE\nKIWOOM_LIVE_APP_KEY=" + FAKE + "\n", encoding="utf-8")
    with pytest.raises(LiveNotAvailable):
        check_startup(Mode.LIVE, root, root / "var" / "live" / "t.db", root / ".env.live")


# --- 정상 시작 ------------------------------------------------------------------

def test_backtest_starts_without_credentials(tmp_path: Path) -> None:
    root = make_root(tmp_path, "backtest")
    cfg = check_startup(Mode.BACKTEST, root, root / "var" / "backtest" / "run1" / "t.db", None)
    assert cfg.mode is Mode.BACKTEST and cfg.secrets is None and cfg.broker_env == "fake"
    assert cfg.account == "KR-BT-01"


def test_paper_starts_with_matching_files(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    cfg = check_startup(Mode.PAPER, root, root / "var" / "paper" / "trading.db", paper_env(root))
    assert cfg.broker_env == "mock" and cfg.secrets is not None
    assert cfg.secrets.has("KIWOOM_PAPER_APP_KEY")
    assert FAKE not in repr(cfg)


def test_expected_broker_env() -> None:
    assert expected_broker_env(Mode.BACKTEST) == "fake"
    assert expected_broker_env(Mode.PAPER) == "mock"
    assert expected_broker_env(Mode.LIVE) == "real"


# --- V-101: 모드·저장소·자격정보 불일치 ------------------------------------------------

def test_paper_with_live_storage_path_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "var" / "live").mkdir(parents=True)
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, root / "var" / "live" / "trading.db", paper_env(root))


def test_paper_with_backtest_storage_path_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, root / "var" / "backtest" / "trading.db", paper_env(root))


def test_storage_outside_var_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, tmp_path / "elsewhere" / "trading.db", paper_env(root))


def test_storage_path_traversal_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "var" / "live").mkdir(parents=True)
    sneaky = root / "var" / "paper" / ".." / "live" / "trading.db"
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, sneaky, paper_env(root))


def test_paper_with_live_credentials_file_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    live_env = root / ".env.live"
    live_env.write_text(f"TRADE_MODE=LIVE\nKIWOOM_LIVE_APP_KEY={FAKE}\n", encoding="utf-8")
    with pytest.raises(ModeConsistencyError) as exc:
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", live_env)
    assert FAKE not in str(exc.value)


def test_credentials_file_outside_repo_root_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    other = tmp_path / ".env.paper"
    other.write_text(f"TRADE_MODE=PAPER\nK={FAKE}\n", encoding="utf-8")
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", other)


def test_trade_mode_inside_credentials_file_must_match(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    env = paper_env(root, f"TRADE_MODE=LIVE\nKIWOOM_PAPER_APP_KEY={FAKE}\n")
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", env)


def test_trade_mode_missing_inside_credentials_file_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    env = paper_env(root, f"KIWOOM_PAPER_APP_KEY={FAKE}\n")
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", env)


@pytest.mark.parametrize("foreign_key", ["KIWOOM_LIVE_APP_KEY", "TELEGRAM_LIVE_BOT_TOKEN", "KIWOOM_BACKTEST_X"])
def test_other_mode_keys_in_credentials_file_refused(tmp_path: Path, foreign_key: str) -> None:
    root = make_root(tmp_path)
    env = paper_env(root, f"TRADE_MODE=PAPER\nKIWOOM_PAPER_APP_KEY={FAKE}\n{foreign_key}={FAKE}\n")
    with pytest.raises(ModeConsistencyError) as exc:
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", env)
    assert foreign_key in str(exc.value)
    assert FAKE not in str(exc.value)


def test_backtest_must_not_receive_credentials_file(tmp_path: Path) -> None:
    root = make_root(tmp_path, "backtest")
    env = root / ".env.backtest"
    env.write_text("TRADE_MODE=BACKTEST\n", encoding="utf-8")
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.BACKTEST, root, root / "var" / "backtest" / "r" / "t.db", env)


def test_paper_requires_credentials_file(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    with pytest.raises(ModeConsistencyError):
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", None)


def test_missing_credentials_file_is_error_without_leak(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    with pytest.raises(ConfigError) as exc:
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", root / ".env.paper")
    assert ".env.paper" in str(exc.value)


# --- 한도 설정 파일 (기본값 없음) ----------------------------------------------------

def test_missing_limits_file_refuses_start(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "config" / "limits.paper.toml").unlink()
    with pytest.raises(ConfigError):
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", paper_env(root))


def test_invalid_limits_file_refuses_start(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "config" / "limits.paper.toml").write_text(TOML.replace('l5_daily_new_orders = 10\n', ""), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        check_startup(Mode.PAPER, root, root / "var" / "paper" / "t.db", paper_env(root))
    assert "l5_daily_new_orders" in str(exc.value)
