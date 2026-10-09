"""시장 설정 위치·시작 일치 표·DB 메타 (CP-024 D-096·D-097·D-098·D-100, T-M2-08). V-147·V-148·V-161·V-152.

- 시작 일치 허용 표: (모드, market, broker, 접속 도메인, 계좌 별칭 접두어)가 표의 한 행과 정확히 맞아야 시작.
- 앱 설정 market·broker: M1 설정 파일에는 키가 없다 → M1·kiwoom-mock(PAPER)/fake(BACKTEST). M1 설정 파일 변경 없음.
- config/m2/(limits·market·app·universe) 로더. 값은 A-064 결정값 또는 자리표시자, 비밀 없음.
- DB 메타 market·broker: 새 DB는 생성 시 기록, 다르면 거부, 메타 없는 기존 DB는 M1이면 허용(기록은 migrate가 백업 뒤 1회),
  M2면 거부.
- 바이낸스 도메인 상수는 테스트넷만(코드 전체).
모든 계좌 별칭·심볼은 가상이다.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from trading_system.config.app_file import load_app_settings, parse_app_settings
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.m2_files import load_m2_market, load_universe, parse_m2_market, parse_universe
from trading_system.config.market_scope import (
    BINANCE_TESTNET_HOSTS, BROKER_BINANCE_TESTNET, BROKER_FAKE, BROKER_KIWOOM_MOCK, KIWOOM_MOCK_HOSTS,
    MarketScopeError, check_start_scope, config_dir, default_broker, parse_market,
)
from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market
from trading_system.storage.market_meta import (
    MarketMismatch, bind_market, check_db_file, read_market_meta, stamp_market_meta,
)
from trading_system.storage.repository import ModeMismatch, Repository

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "trading_system"
M2_DIR = ROOT / "config" / "m2"

TESTNET = "testnet.binance.vision"
KIWOOM = "mockapi.kiwoom.com"


# --- 시작 일치 허용 표 (V-147) ------------------------------------------------------------------

@pytest.mark.parametrize("mode,market,broker,account,hosts", [
    (Mode.BACKTEST, Market.M1, BROKER_FAKE, "KR-BT-01", frozenset()),
    (Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK, "KR-PAPER-01", frozenset({KIWOOM})),
    (Mode.BACKTEST, Market.M2, BROKER_FAKE, "BN-BT-01", frozenset()),
    (Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET, "BN-PAPER-01", frozenset({TESTNET, "ws-api." + TESTNET})),
])
def test_start_table_accepts_exact_rows(mode: Mode, market: Market, broker: str, account: str,
                                        hosts: frozenset[str]) -> None:
    check_start_scope(mode, market, broker, account, hosts)


@pytest.mark.parametrize("mode,market,broker,account,hosts,why", [
    (Mode.PAPER, Market.M2, BROKER_KIWOOM_MOCK, "BN-PAPER-01", frozenset({TESTNET}), "broker"),
    (Mode.PAPER, Market.M1, BROKER_BINANCE_TESTNET, "KR-PAPER-01", frozenset({KIWOOM}), "broker"),
    (Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET, "KR-PAPER-01", frozenset({TESTNET}), "별칭"),
    (Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK, "BN-PAPER-01", frozenset({KIWOOM}), "별칭"),
    (Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET, "BN-PAPER-01", frozenset({"api." + "binance" + ".com"}), "도메인"),
    (Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET, "BN-PAPER-01", frozenset({"demo-api." + "binance" + ".com"}),
     "도메인"),
    (Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET, "BN-PAPER-01", frozenset({KIWOOM}), "도메인"),
    (Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK, "KR-PAPER-01", frozenset({"api.kiwoom.com"}), "도메인"),
    (Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK, "KR-PAPER-01", frozenset(), "도메인"),
    (Mode.BACKTEST, Market.M2, BROKER_FAKE, "BN-BT-01", frozenset({TESTNET}), "도메인"),
    (Mode.BACKTEST, Market.M1, BROKER_KIWOOM_MOCK, "KR-BT-01", frozenset(), "broker"),
    (Mode.LIVE, Market.M1, BROKER_KIWOOM_MOCK, "KR-LIVE-01", frozenset({KIWOOM}), "허용 표"),
    (Mode.LIVE, Market.M2, BROKER_BINANCE_TESTNET, "BN-LIVE-01", frozenset({TESTNET}), "허용 표"),
])
def test_start_table_mismatch_refused(mode: Mode, market: Market, broker: str, account: str,
                                      hosts: frozenset[str], why: str) -> None:
    with pytest.raises(MarketScopeError, match=why):
        check_start_scope(mode, market, broker, account, hosts)


def test_scope_error_is_config_error_so_start_refusal_paths_catch_it() -> None:
    assert issubclass(MarketScopeError, ConfigError)


def test_hosts_none_means_connection_not_built_only_for_refused_paths() -> None:
    """연결 대상을 아직 조립하지 않은 검사(None)는 도메인 검사를 건너뛴다 — 나머지 열은 그대로 검사."""
    check_start_scope(Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET, "BN-PAPER-01", None)
    with pytest.raises(MarketScopeError, match="별칭"):
        check_start_scope(Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET, "KR-PAPER-01", None)


def test_default_broker_and_market_parse() -> None:
    assert default_broker(Mode.PAPER, Market.M1) == BROKER_KIWOOM_MOCK
    assert default_broker(Mode.BACKTEST, Market.M1) == BROKER_FAKE
    assert default_broker(Mode.PAPER, Market.M2) == BROKER_BINANCE_TESTNET
    assert parse_market("M1") is Market.M1 and parse_market("M2") is Market.M2
    for bad in ("m2", "M3", "", None, 2):
        with pytest.raises(ConfigError):
            parse_market(bad)
    with pytest.raises(MarketScopeError):
        default_broker(Mode.LIVE, Market.M1)


def test_config_dir_m1_default_path_unchanged() -> None:
    assert config_dir(ROOT, Market.M1) == ROOT / "config"
    assert config_dir(ROOT, Market.M2) == ROOT / "config" / "m2"


def test_allowed_hosts_match_adapter_constants() -> None:
    """허용 표의 도메인 = 어댑터 상수의 호스트 (M1 키움 모의, M2 바이낸스 테스트넷)."""
    from trading_system.broker.binance.client import TESTNET_REST
    from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS
    assert urlsplit(MOCK_REST).hostname in KIWOOM_MOCK_HOSTS and urlsplit(MOCK_WS).hostname in KIWOOM_MOCK_HOSTS
    assert urlsplit(TESTNET_REST).hostname in BINANCE_TESTNET_HOSTS
    assert all(h.endswith(TESTNET) for h in BINANCE_TESTNET_HOSTS)


def test_no_live_or_demo_binance_domain_constants_anywhere_in_src() -> None:
    """D-098: 코드 전체(바이낸스 패키지 밖 포함)에 실전·Demo 바이낸스 도메인 상수가 없다."""
    host = re.compile(r"(?:[a-z0-9-]+\.)*binance\.(?:com|vision|us|me|info|cc)\b")
    bad, seen = [], 0
    for p in SRC.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                for h in host.findall(n.value.lower()):
                    seen += 1
                    if not h.endswith(TESTNET):
                        bad.append(f"{p.relative_to(SRC)}:{n.lineno}:{h}")
    assert bad == [] and seen > 0


# --- 앱 설정 market·broker (M1 기본값, V-152) ----------------------------------------------------

APP_BASE = {"app": {"account": "KR-PAPER-01", "loop_seconds": "0.5", "reconcile_minutes": 1,
                    "sntp_servers": ["127.0.0.1:9"]}}


def _raw(**app: object) -> dict:
    raw = {"app": dict(APP_BASE["app"]) | app}
    return raw


def test_m1_app_without_keys_defaults_to_m1_and_mode_broker(tmp_path: Path) -> None:
    s = parse_app_settings(_raw(), Mode.PAPER, tmp_path)
    assert (s.market, s.broker) == ("M1", BROKER_KIWOOM_MOCK)
    s = parse_app_settings(_raw(account="KR-BT-01"), Mode.BACKTEST, tmp_path)
    assert (s.market, s.broker) == ("M1", BROKER_FAKE)


def test_real_m1_app_paper_file_unchanged_and_reads_as_m1() -> None:
    """M1 설정 파일은 바꾸지 않는다 — market·broker 키가 없고, 읽으면 M1·kiwoom-mock."""
    text = (ROOT / "config" / "app.paper.toml").read_text(encoding="utf-8")
    keys = [ln.split("=")[0].strip() for ln in text.splitlines() if "=" in ln and not ln.lstrip().startswith("#")]
    assert "market" not in keys and "broker" not in keys
    s = load_app_settings(ROOT / "config" / "app.paper.toml", Mode.PAPER, ROOT)
    assert (s.market, s.broker, s.account) == ("M1", BROKER_KIWOOM_MOCK, "KR-PAPER-01")


def test_m2_app_keys_parsed(tmp_path: Path) -> None:
    s = parse_app_settings(_raw(account="BN-PAPER-01", market="M2", broker=BROKER_BINANCE_TESTNET), Mode.PAPER,
                           tmp_path)
    assert (s.market, s.broker) == ("M2", BROKER_BINANCE_TESTNET)
    assert s.instruments == {}


@pytest.mark.parametrize("app,why", [
    ({"market": "M2"}, "broker"),                       # M2는 broker를 명시해야 한다 (기본값은 M1만)
    ({"market": "M3"}, "market"),
    ({"market": "m2", "broker": BROKER_BINANCE_TESTNET}, "market"),
    ({"market": 2}, "market"),
    ({"broker": ""}, "broker"),
    ({"broker": 1}, "broker"),
])
def test_bad_market_broker_keys_refused(tmp_path: Path, app: dict, why: str) -> None:
    with pytest.raises(ConfigError, match=why):
        parse_app_settings(_raw(**app), Mode.PAPER, tmp_path)


def test_m2_app_refuses_instruments_table(tmp_path: Path) -> None:
    """M2 종목은 universe 파일에 둔다 (M1 [[instruments]]는 6자리 종목코드 형식)."""
    raw = _raw(account="BN-PAPER-01", market="M2", broker=BROKER_BINANCE_TESTNET)
    raw["instruments"] = [{"code": "005930", "kind": "STOCK", "leverage": "1", "lot_size": "1"}]
    with pytest.raises(ConfigError, match="universe"):
        parse_app_settings(raw, Mode.PAPER, tmp_path)


# --- config/m2/ 로더 ----------------------------------------------------------------------------

def test_universe_parse_and_errors() -> None:
    u = parse_universe({"quote_asset": "USDT", "symbols": ["TSTAUSDT", "TSTBUSDT"]})
    assert u.quote_asset == "USDT" and u.symbols == ("TSTAUSDT", "TSTBUSDT")
    assert u.base_asset("TSTAUSDT") == "TSTA" and u.base_asset("XUSDT") is None
    assert parse_universe({"quote_asset": "USDT", "symbols": []}).symbols == ()
    for raw, why in [({"quote_asset": "KRW", "symbols": []}, "USDT"),
                     ({"quote_asset": "USDT", "symbols": ["TSTAUSDT", "TSTAUSDT"]}, "중복"),
                     ({"quote_asset": "USDT", "symbols": ["TSTA-USDT"]}, "형식"),
                     ({"quote_asset": "USDT", "symbols": ["TSTABTC"]}, "USDT"),
                     ({"quote_asset": "USDT", "symbols": ["USDT"]}, "형식"),
                     ({"quote_asset": "USDT"}, "symbols"),
                     ({"quote_asset": "USDT", "symbols": [], "extra": 1}, "알 수 없는"),
                     ({"quote_asset": "USDT", "symbols": "TSTAUSDT"}, "symbols")]:
        with pytest.raises(ConfigError, match=why):
            parse_universe(raw)


M2_MARKET = {"gate": {"quote_max_age_seconds": 60, "buy_buffer": "0.005", "sell_buffer": "0.005",
                      "server_time_warn_ms": 500},
             "costs": {"commission_rate": "0.001"},
             "alerts": {"reject_surge_count": 10, "reject_surge_minutes": 5}}


def test_m2_market_parse_and_errors() -> None:
    s = parse_m2_market(M2_MARKET)
    assert (s.quote_max_age_seconds, s.buy_buffer, s.sell_buffer, s.server_time_warn_ms) == \
        (60, Decimal("0.005"), Decimal("0.005"), 500)
    assert s.commission_rate == Decimal("0.001") and s.alerts == (10, 5)
    for path, value, why in [(("gate", "buy_buffer"), 0.005, "문자열"), (("gate", "sell_buffer"), "1", "범위"),
                             (("gate", "quote_max_age_seconds"), 0, "quote_max_age_seconds"),
                             (("gate", "price_limit_ratio"), "0.3", "알 수 없는"),
                             (("costs", "sell_tax_rate"), "0", "알 수 없는"),
                             (("gate", "server_time_warn_ms"), True, "server_time_warn_ms")]:
        raw = {k: dict(v) for k, v in M2_MARKET.items()}
        raw[path[0]][path[1]] = value
        with pytest.raises(ConfigError, match=why):
            parse_m2_market(raw)
    raw = {k: dict(v) for k, v in M2_MARKET.items()}
    del raw["costs"]
    with pytest.raises(ConfigError, match="costs"):
        parse_m2_market(raw)


def test_repo_m2_config_files_load_with_placeholders_only() -> None:
    """config/m2/*.paper.toml: A-064 결정값·자리표시자만, 비밀 키 없음. 유니버스는 미결정(D-105) → 빈 목록."""
    lim = load_limits_file(M2_DIR / "limits.paper.toml", market="M2")
    app = load_app_settings(M2_DIR / "app.paper.toml", Mode.PAPER, ROOT)
    mk = load_m2_market(M2_DIR / "market.paper.toml")
    uni = load_universe(M2_DIR / "universe.paper.toml")
    assert app.account == lim.account == "BN-PAPER-01"
    assert (app.market, app.broker) == ("M2", BROKER_BINANCE_TESTNET)
    assert app.paper_connection_approved is False and app.telegram_enabled is False
    assert mk.quote_max_age_seconds == 60 and mk.buy_buffer == mk.sell_buffer == Decimal("0.005")
    assert mk.server_time_warn_ms == 500
    assert uni.symbols == ()
    for f in M2_DIR.glob("*.toml"):
        low = f.read_text(encoding="utf-8").lower()
        for word in ("secret", "api_key", "apikey", "token", "password"):
            assert f"{word} =" not in low and f"{word}=" not in low, f"{f.name}: {word}"


# --- DB 메타 (V-148, V-161) ---------------------------------------------------------------------

def _meta(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def _legacy_db(path: Path, mode: Mode = Mode.PAPER) -> Path:
    """T-M2-08 이전에 만든 DB와 같은 형태: 모드·스키마 메타만 있고 market·broker 메타 없음."""
    Repository(path, mode).close()
    assert "market" not in _meta(path)
    return path


def test_new_db_stamped_and_reopen_same_market_ok(tmp_path: Path) -> None:
    db = tmp_path / "trading.db"
    repo = Repository(db, Mode.PAPER)
    try:
        bind_market(repo.connection, Market.M2, BROKER_BINANCE_TESTNET, created=True)
        assert read_market_meta(repo.connection) == ("M2", BROKER_BINANCE_TESTNET)
        bind_market(repo.connection, Market.M2, BROKER_BINANCE_TESTNET, created=False)
    finally:
        repo.close()


@pytest.mark.parametrize("market,broker", [(Market.M1, BROKER_KIWOOM_MOCK), (Market.M2, BROKER_FAKE)])
def test_db_meta_mismatch_refused(tmp_path: Path, market: Market, broker: str) -> None:
    db = tmp_path / "trading.db"
    repo = Repository(db, Mode.PAPER)
    try:
        bind_market(repo.connection, Market.M2, BROKER_BINANCE_TESTNET, created=True)
        with pytest.raises(MarketMismatch, match="M2"):
            bind_market(repo.connection, market, broker, created=False)
    finally:
        repo.close()
    assert issubclass(MarketMismatch, ModeMismatch)   # 기존 ModeMismatch 처리 경로가 그대로 거부한다


def test_legacy_db_m1_allowed_without_write_m2_refused(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path / "trading.db")
    before = _meta(db)
    repo = Repository(db, Mode.PAPER)
    try:
        bind_market(repo.connection, Market.M1, BROKER_KIWOOM_MOCK, created=False)   # M1 동작 불변: 기록 안 함
        assert read_market_meta(repo.connection) is None
        with pytest.raises(MarketMismatch, match="메타 없는"):
            bind_market(repo.connection, Market.M2, BROKER_BINANCE_TESTNET, created=False)
    finally:
        repo.close()
    assert _meta(db) == before


def test_partial_meta_refused(tmp_path: Path) -> None:
    db = tmp_path / "trading.db"
    repo = Repository(db, Mode.PAPER)
    try:
        repo.connection.execute("INSERT INTO meta(key, value) VALUES('market', 'M1')")
        with pytest.raises(MarketMismatch, match="일부"):
            bind_market(repo.connection, Market.M1, BROKER_KIWOOM_MOCK, created=False)
    finally:
        repo.close()


def test_stamp_legacy_m1_with_backup_once(tmp_path: Path) -> None:
    """V-161: 메타 없는 M1 DB + M1 설정 → 백업 뒤 1회 기록. 두 번째는 기록 없이 확인만."""
    db = _legacy_db(tmp_path / "trading.db")
    backups = tmp_path / "backup"
    assert stamp_market_meta(db, backups, Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK) == "기록"
    assert len(list(backups.glob("trading-*.db"))) == 1
    assert "market" not in _meta(next(backups.glob("trading-*.db")))   # 백업은 기록 전 상태
    assert (_meta(db)["market"], _meta(db)["broker"]) == ("M1", BROKER_KIWOOM_MOCK)
    assert stamp_market_meta(db, backups, Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK) == "이미 기록됨"
    assert len(list(backups.glob("trading-*.db"))) == 1
    with pytest.raises(MarketMismatch):
        stamp_market_meta(db, backups, Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET)


def test_stamp_legacy_with_m2_refused_without_backup_or_write(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path / "trading.db")
    before = _meta(db)
    with pytest.raises(MarketMismatch, match="메타 없는"):
        stamp_market_meta(db, tmp_path / "backup", Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET)
    assert _meta(db) == before and not (tmp_path / "backup").exists()


def test_stamp_refuses_other_mode_db(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path / "trading.db", Mode.BACKTEST)
    with pytest.raises(ModeMismatch):
        stamp_market_meta(db, tmp_path / "backup", Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK)


def test_check_db_file_read_only(tmp_path: Path) -> None:
    db = tmp_path / "trading.db"
    check_db_file(db, Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET)       # 없음 = 생성 시 기록
    assert not db.exists()
    _legacy_db(db)
    check_db_file(db, Mode.PAPER, Market.M1, BROKER_KIWOOM_MOCK)
    with pytest.raises(MarketMismatch, match="메타 없는"):
        check_db_file(db, Mode.PAPER, Market.M2, BROKER_BINANCE_TESTNET)
    with pytest.raises(ModeMismatch):
        check_db_file(db, Mode.BACKTEST, Market.M1, BROKER_FAKE)
    assert "market" not in _meta(db)
