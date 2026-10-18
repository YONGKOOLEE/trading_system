"""시장 분리 — 시작·운영 도구 (CP-024 D-096·D-097·D-100, T-M2-08). V-147·V-148·V-161·V-152.

임시 저장소 루트 두 개(M1 체크아웃·M2 체크아웃 흉내)로 시험한다. 네트워크 연결 없음: M1 PAPER 경로는 브로커 생성 함수를
가로채 호출 여부만 보고, M2는 실행 경로 자체가 아직 없다(엔진 조립·테스트넷 연결은 후속 작업·별도 승인).
계좌 별칭·심볼·자격정보 값은 모두 가상(자리표시자)이다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.scenario.test_t020_main import make_root
from trading_system.app import main as app_main
from trading_system.app.main import run_main
from trading_system.app.startup import check_m2_startup
from trading_system.config.errors import ConfigError
from trading_system.config.market_scope import BROKER_BINANCE_TESTNET, BROKER_FAKE, BROKER_KIWOOM_MOCK
from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market
from trading_system.ops.cli import kill_main, migrate_main, resume_main, status_main
from trading_system.storage.market_meta import bind_market
from trading_system.storage.repository import Repository

M2_LIMITS = """account = "{acct}"
[limits]
l1_max_loss_ratio = "0.005"
l2_max_symbol_ratio = "0.10"
l3_max_total_ratio = "0.50"
l4_daily_loss_ratio = "0.01"
l5_daily_new_orders = 5
l6_max_positions = 3
l7_max_order_amount = "100"
l10_leverage_total_ratio = "none"
warn_usage_ratio = "0.80"
[time]
order_window_start = "00:10"
order_window_end = "23:50"
daily_reference_time = "00:00"
report_time = "00:10"
"""
M2_MARKET = """[gate]
quote_max_age_seconds = 60
buy_buffer = "0.005"
sell_buffer = "0.005"
server_time_warn_ms = 500
[costs]
commission_rate = "0.001"
[alerts]
reject_surge_count = 10
reject_surge_minutes = 5
"""
M2_APP = """[app]
account = "{acct}"
market = "M2"
broker = "{broker}"
loop_seconds = "0.05"
reconcile_minutes = 1
sntp_servers = ["127.0.0.1:9"]
"""
M2_UNIVERSE = 'quote_asset = "USDT"\nsymbols = ["TSTAUSDT", "TSTBUSDT"]\n'


def make_m2_root(tmp: Path, mode: Mode, *, symbols: str = M2_UNIVERSE) -> Path:
    """M2 체크아웃 흉내: config/m2/ 네 파일 + (PAPER면) 가상 자격정보 파일. 비밀 값 없음."""
    root = tmp / "m2root"
    d = root / "config" / "m2"
    d.mkdir(parents=True)
    m = mode.value.lower()
    acct = "BN-BT-01" if mode is Mode.BACKTEST else "BN-PAPER-01"
    broker = BROKER_FAKE if mode is Mode.BACKTEST else BROKER_BINANCE_TESTNET
    (d / f"limits.{m}.toml").write_text(M2_LIMITS.format(acct=acct), encoding="utf-8")
    (d / f"market.{m}.toml").write_text(M2_MARKET, encoding="utf-8")
    (d / f"app.{m}.toml").write_text(M2_APP.format(acct=acct, broker=broker), encoding="utf-8")
    (d / f"universe.{m}.toml").write_text(symbols, encoding="utf-8")
    if mode is Mode.PAPER:
        (root / ".env.paper").write_text("TRADE_MODE=PAPER\nBINANCE_PAPER_API_KEY=fake-secret-sentinel\n", encoding="utf-8")
    return root


def _meta(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def _stamped_db(path: Path, mode: Mode, market: Market, broker: str) -> Path:
    repo = Repository(path, mode)
    try:
        bind_market(repo.connection, market, broker, created=True)
    finally:
        repo.close()
    return path


# --- M1 시작 (V-152, V-147) ---------------------------------------------------------------------

def test_m1_run_unchanged_and_new_db_stamped_m1(tmp_path: Path) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.2) == 0
    meta = _meta(root / "var" / "backtest" / "trading.db")
    assert (meta["market"], meta["broker"]) == ("M1", BROKER_FAKE)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1) == 0     # 같은 시장으로 다시 시작


def test_m1_run_on_legacy_db_allowed_without_writing_meta(tmp_path: Path) -> None:
    """메타 없는 기존 M1 DB(T-M2-08 이전): M1 설정이면 그대로 시작, 메타는 쓰지 않는다(기록은 migrate가 백업 뒤)."""
    root = make_root(tmp_path, Mode.BACKTEST)
    db = root / "var" / "backtest" / "trading.db"
    db.parent.mkdir(parents=True)
    Repository(db, Mode.BACKTEST).close()
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.2) == 0
    assert "market" not in _meta(db) and "broker" not in _meta(db)


def test_m1_paper_run_refused_before_broker_on_m2_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    """다른 시장 DB → 시작 거부, 브로커 생성(연결) 0."""
    root = make_root(tmp_path, Mode.PAPER, paper_approved=True)
    db = root / "var" / "paper" / "trading.db"
    db.parent.mkdir(parents=True)
    _stamped_db(db, Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET)
    calls: list[int] = []
    monkeypatch.setattr(app_main, "_paper_broker", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
        AssertionError("브로커 생성 호출")))
    assert run_main(root, {"TRADE_MODE": "PAPER"}) == 2
    out = capsys.readouterr().out
    assert "시작 거부(브로커 연결 없음)" in out and "M2" in out and "fake-secret-sentinel" not in out
    assert calls == []


@pytest.mark.parametrize("edit,why", [
    (("app.backtest.toml", 'account = "KR-BT-01"', 'account = "KR-BT-01"\nmarket = "M2"\nbroker = "fake"'), "M1"),
    (("app.backtest.toml", 'account = "KR-BT-01"', 'account = "KR-BT-01"\nbroker = "kiwoom-mock"'), "broker"),
])
def test_m1_config_with_wrong_market_or_broker_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                       edit: tuple[str, str, str], why: str) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    p = root / "config" / edit[0]
    p.write_text(p.read_text(encoding="utf-8").replace(edit[1], edit[2]), encoding="utf-8")
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.2) == 2
    out = capsys.readouterr().out
    assert "시작 거부" in out and why in out
    assert not (root / "var" / "backtest" / "trading.db").exists()


def test_m1_with_bn_alias_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    for f in ("app.backtest.toml", "limits.backtest.toml"):
        p = root / "config" / f
        p.write_text(p.read_text(encoding="utf-8").replace("KR-BT-01", "BN-BT-01"), encoding="utf-8")
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.2) == 2
    assert "별칭" in capsys.readouterr().out


# --- M2 시작 검사 (V-147, V-161) ----------------------------------------------------------------

@pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.PAPER])
def test_m2_startup_check_passes_with_m2_settings(tmp_path: Path, mode: Mode) -> None:
    root = make_m2_root(tmp_path, mode)
    m = mode.value.lower()
    env = None if mode is Mode.BACKTEST else root / ".env.paper"
    cfg = check_m2_startup(mode, root, root / "var" / m / "trading.db", env)
    assert cfg.market is Market.M2
    assert cfg.broker == (BROKER_FAKE if mode is Mode.BACKTEST else BROKER_BINANCE_TESTNET)
    assert cfg.account == ("BN-BT-01" if mode is Mode.BACKTEST else "BN-PAPER-01")
    assert cfg.universe.symbols == ("TSTAUSDT", "TSTBUSDT")
    assert not (root / "var").exists()        # 검사는 DB를 만들지 않는다
    assert "fake-secret-sentinel" not in repr(cfg)


@pytest.mark.parametrize("change,why", [
    (("app.paper.toml", 'account = "BN-PAPER-01"', 'account = "KR-PAPER-01"'), "계좌"),
    (("app.paper.toml", 'market = "M2"', 'market = "M1"'), "M2"),
    (("app.paper.toml", 'broker = "binance-spot-testnet"', 'broker = "kiwoom-mock"'), "broker"),
    (("universe.paper.toml", '["TSTAUSDT", "TSTBUSDT"]', "[]"), "유니버스"),
    (("limits.paper.toml", 'daily_reference_time = "00:00"', 'daily_reference_time = "08:30"'), "limits"),
])
def test_m2_startup_mismatch_refused(tmp_path: Path, change: tuple[str, str, str], why: str) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    p = root / "config" / "m2" / change[0]
    p.write_text(p.read_text(encoding="utf-8").replace(change[1], change[2]), encoding="utf-8")
    with pytest.raises(ConfigError, match=why):
        check_m2_startup(Mode.PAPER, root, root / "var" / "paper" / "trading.db", root / ".env.paper")


def test_m2_startup_refuses_live_and_other_mode_paths(tmp_path: Path) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    with pytest.raises(ConfigError, match="LIVE"):
        check_m2_startup(Mode.LIVE, root, root / "var" / "live" / "trading.db", root / ".env.live")
    with pytest.raises(ConfigError, match="저장소 경로"):
        check_m2_startup(Mode.PAPER, root, root / "var" / "backtest" / "trading.db", root / ".env.paper")


@pytest.mark.parametrize("existing", ["legacy", "m1"])
def test_m2_startup_refuses_db_without_meta_or_of_m1(tmp_path: Path, existing: str) -> None:
    """V-161: 메타 없는 DB + M2 설정 → 거부. V-148: M1 DB + M2 설정 → 거부."""
    root = make_m2_root(tmp_path, Mode.PAPER)
    db = root / "var" / "paper" / "trading.db"
    db.parent.mkdir(parents=True)
    if existing == "legacy":
        Repository(db, Mode.PAPER).close()
    else:
        _stamped_db(db, Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK)
    before = _meta(db)
    with pytest.raises(ConfigError, match="메타"):
        check_m2_startup(Mode.PAPER, root, db, root / ".env.paper")
    assert _meta(db) == before


def test_m2_run_refused_after_checks_without_db_or_connection(tmp_path: Path,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    """M2 실행 경로(엔진 조립·바이낸스 연결)는 아직 없다 — 검사 뒤 거부, DB·잠금 파일도 만들지 않는다."""
    root = make_m2_root(tmp_path, Mode.PAPER)
    assert run_main(root, {"TRADE_MODE": "PAPER"}, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부(브로커 연결 없음)" in out and "T-M2-14" in out and "fake-secret-sentinel" not in out
    assert not (root / "var").exists()


def test_run_unknown_market_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, market="M3") == 2
    assert "시장" in capsys.readouterr().out
    assert app_main.main(["run", "--root", str(root), "--market", "M3"]) == 2


# --- 운영 도구 (V-148, V-161) -------------------------------------------------------------------

def test_ops_first_line_shows_market_and_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, Mode.BACKTEST)
    assert status_main(["--mode", "BACKTEST", "--root", str(root)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"시장: M1 · 디렉터리: {root.resolve()}"
    assert kill_main(["--mode", "BACKTEST", "--root", str(root)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"시장: M1 · 디렉터리: {root.resolve()}"


def test_m2_kill_stays_in_m2_checkout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """V-148: M2 KILL·K1은 M2 체크아웃의 파일·DB에만. M1 체크아웃은 그대로."""
    m1 = make_root(tmp_path / "a", Mode.PAPER, paper_approved=True)
    m2 = make_m2_root(tmp_path / "b", Mode.PAPER)
    m1_db = _stamped_db(_mk(m1 / "var" / "paper" / "trading.db"), Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK)
    m1_before = _meta(m1_db), _count(m1_db, "ops_command")
    assert kill_main(["--mode", "PAPER", "--root", str(m2), "--market", "M2"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"시장: M2 · 디렉터리: {m2.resolve()}" and "K1 발동 명령 기록" in out
    assert (m2 / "var" / "paper" / "KILL").exists() and not (m1 / "var" / "paper" / "KILL").exists()
    m2_db = m2 / "var" / "paper" / "trading.db"
    assert (_meta(m2_db)["market"], _meta(m2_db)["broker"]) == ("M2", BROKER_BINANCE_TESTNET)
    assert _count(m2_db, "ops_command") == 1
    assert (_meta(m1_db), _count(m1_db, "ops_command")) == m1_before


def test_ops_tool_on_other_market_db_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """V-148: M1 도구 설정(--market 기본 M1)으로 M2 DB를 열면 거부 — 명령 기록 없음."""
    m2 = make_m2_root(tmp_path, Mode.PAPER)
    db = _stamped_db(_mk(m2 / "var" / "paper" / "trading.db"), Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET)
    assert status_main(["--mode", "PAPER", "--root", str(m2)]) == 2
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("시장: M1") and "거부" in out and "M2" in out
    assert resume_main(["--mode", "PAPER", "--root", str(m2), "K8"]) == 1
    assert "명령 기록 실패" in capsys.readouterr().out
    assert kill_main(["--mode", "PAPER", "--root", str(m2)]) == 1     # KILL 파일은 먼저 생김(D-051), 기록은 거부
    assert (m2 / "var" / "paper" / "KILL").exists()
    assert _count(db, "ops_command") == 0
    assert status_main(["--mode", "PAPER", "--root", str(m2), "--market", "M2"]) == 0


def test_ops_tool_m2_on_legacy_db_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    db = _mk(root / "var" / "paper" / "trading.db")
    Repository(db, Mode.PAPER).close()
    assert status_main(["--mode", "PAPER", "--root", str(root), "--market", "M2"]) == 2
    assert "메타 없는" in capsys.readouterr().out
    assert "market" not in _meta(db)


def test_migrate_stamps_legacy_m1_once_with_backup_and_refuses_m2(tmp_path: Path,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """V-161: 메타 없는 M1 DB는 migrate(백업 선행)로 1회 기록. M2 설정이면 거부."""
    root = make_root(tmp_path, Mode.PAPER, paper_approved=True)
    db = _mk(root / "var" / "paper" / "trading.db")
    Repository(db, Mode.PAPER).close()
    assert migrate_main(["--mode", "PAPER", "--root", str(root), "--market", "M2"]) == 1
    assert "메타 없는" in capsys.readouterr().out and "market" not in _meta(db)
    assert not (db.parent / "backup").exists()
    assert migrate_main(["--mode", "PAPER", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("시장: M1") and "시장 메타: 기록" in out
    assert (_meta(db)["market"], _meta(db)["broker"]) == ("M1", BROKER_KIWOOM_MOCK)
    assert len(list((db.parent / "backup").glob("trading-*.db"))) == 1
    assert migrate_main(["--mode", "PAPER", "--root", str(root)]) == 0
    assert "시장 메타: 이미 기록됨" in capsys.readouterr().out


def _mk(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()
