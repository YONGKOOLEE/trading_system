"""M2 시작 점검 — 바이낸스 Spot 테스트넷 접속, 조회만 (T-M2-14, 승인 A-096). V-147(실설정)·V-146(서버 시각)·V-158·V-145.

임시 M2 체크아웃 흉내(config/m2/ + 자격정보 파일, 값은 가상)와 합성 테스트넷으로 시험한다. 네트워크 없음.
- 연결 조건(하나라도 어긋나면 전송 객체를 만들지 않고 종료 코드 2): TRADE_MODE=PAPER, --market M2, config/m2 시작 일치 표
  (PAPER·M2 → binance-spot-testnet·테스트넷 호스트·별칭 BN-), [paper] connection_approved = true, 비밀 키 2개, DB 메타,
  실행 중 엔진 없음. 실전·Demo 도메인은 어떤 경우에도 거부.
- 점검: 서버 시각 차이·SNTP, exchangeInfo(BTCUSDT 존재·TRADING·필터·rateLimits), 계좌 잔고, 미체결 0, 시작 대조,
  주문 API 전송 0. 결과 파일 var/paper/preflight/<시각>-m2-preflight.md·json. 종료 코드 0/1/2.
- 키·시크릿·계좌 식별값이 출력·결과 파일에 없다.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.contract.test_binance_rules import _symbol
from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.binance_testnet_sim import (
    KEY, OPEN_ORDER, SECRET, T0, UID, TestnetSim, error, exchange_info, reply,
)
from trading_system.app import m2_preflight as m2
from trading_system.broker.binance.preflight import open_testnet_session
from trading_system.config.market_scope import BROKER_BINANCE_TESTNET
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market
from trading_system.domain.models import Account
from trading_system.monitoring.spot_cash import QUOTE_BASELINE
from trading_system.storage.market_meta import bind_market
from trading_system.storage.repository import OrderIdentity, Repository

REPO = Path(__file__).resolve().parents[2]
NOW = datetime.fromtimestamp(T0 / 1000, UTC)
ENV = {"TRADE_MODE": "PAPER"}
UNIVERSE = 'quote_asset = "USDT"\nsymbols = ["BTCUSDT"]\n'
SECRETS = f"TRADE_MODE=PAPER\nBINANCE_TESTNET_API_KEY={KEY}\nBINANCE_TESTNET_SECRET_KEY={SECRET}\n"


def make_root(tmp: Path, *, approved: bool = True, secrets: str = SECRETS) -> Path:
    root = make_m2_root(tmp, Mode.PAPER, symbols=UNIVERSE)
    app = root / "config" / "m2" / "app.paper.toml"
    app.write_text(app.read_text(encoding="utf-8") + f"\n[paper]\nconnection_approved = {str(approved).lower()}\n",
                   encoding="utf-8")
    (root / ".env.paper").write_text(secrets, encoding="utf-8")
    return root


def sntp_ok(host: str, port: int) -> Decimal:
    return Decimal("0.010")


class Run:
    def __init__(self, tmp: Path, routes: dict | None = None, *, root: Path | None = None) -> None:
        self.root = root or make_root(tmp)
        self.sim = TestnetSim(routes)
        self.opened = 0

    def open_session(self, *a: object, **kw: object):  # type: ignore[no-untyped-def]
        self.opened += 1
        return open_testnet_session(*a, **kw)  # type: ignore[arg-type]

    def __call__(self, environ: dict | None = None, *, market: str | None = "M2", rest_url: str = m2.TESTNET_REST_URL,
                 sntp=sntp_ok) -> int:
        return m2.m2_preflight_main(self.root, self.open_session, ENV if environ is None else environ, market=market,
                                    clock=FixedClock(NOW), rest_url=rest_url, make_transport=self.sim.factory(),
                                    sntp_query=sntp, sleep=lambda s: None)

    def files(self) -> list[Path]:
        d = self.root / "var" / "paper" / "preflight"
        return sorted(d.iterdir()) if d.exists() else []

    def report(self) -> dict:
        (js,) = [p for p in self.files() if p.suffix == ".json"]
        return json.loads(js.read_text(encoding="utf-8"))

    def texts(self) -> str:
        return "".join(p.read_text(encoding="utf-8") for p in self.files())


def _no_secret(*texts: str) -> None:
    for t in texts:
        assert KEY not in t and SECRET not in t and str(UID) not in t


# --- 정상 ---------------------------------------------------------------------------------------------------

def test_preflight_passes_read_only_and_writes_md_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path)
    assert run() == 0
    out = capsys.readouterr().out
    names = [p.name for p in run.files()]
    assert len(names) == 2 and all(n.endswith(("-m2-preflight.json", "-m2-preflight.md")) for n in names)
    rep = run.report()
    assert rep["ok"] is True and rep["failures"] == []
    assert (rep["market"], rep["broker"], rep["host"], rep["account"]) == (
        "M2", BROKER_BINANCE_TESTNET, "testnet.binance.vision", "BN-PAPER-01")
    assert rep["server_time_offset_ms"] == 0 and rep["server_time_warn_ms"] == 500
    assert rep["clock_offset"] == "0.010"
    btc = rep["exchange"]["BTCUSDT"]
    assert btc["status"] == "TRADING" and btc["filters"]["LOT_SIZE"]["stepSize"] == "0.00001"
    assert any(r["type"] == "REQUEST_WEIGHT" and r["limit"] == 6000 for r in rep["rate_limits"])
    assert {b["asset"]: b["free"] for b in rep["account_snapshot"]["balances"]}["USDT"] == "10000"
    assert rep["open_orders"] == {"BTCUSDT": 0}
    assert rep["reconcile"] == "NO_BASELINE" and "대조 기준 없음" in rep["reconcile_detail"]
    assert rep["order_api_sent"] == 0 and rep["order_api_blocked"] == []
    # 조회만: 보낸 요청은 전부 GET 허용 경로
    assert {m for m, _ in run.sim.paths()} == {"GET"}
    assert not (run.root / "var" / "paper" / "trading.db").exists()   # 시작 점검은 DB를 만들지 않는다
    assert "결과: 통과" in out and "주문 API 전송: 0건" in out and "대조 기준 없음" in out
    md = [p for p in run.files() if p.suffix == ".md"][0].read_text(encoding="utf-8")
    assert "BTCUSDT" in md and "TRADING" in md and "주문 API 전송" in md and "10000" in md
    _no_secret(out, run.texts())


def test_cli_requires_market_m2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path)
    kw = dict(environ=ENV, clock=FixedClock(NOW), make_transport=run.sim.factory(), sntp_query=sntp_ok,
              sleep=lambda s: None)
    assert m2.m2_preflight_cli(run.root, [], run.open_session, **kw) == 2
    assert m2.m2_preflight_cli(run.root, ["--market", "M1"], run.open_session, **kw) == 2
    assert run.sim.factory_calls == 0 and run.opened == 0
    assert m2.m2_preflight_cli(run.root, ["--market", "M2"], run.open_session, **kw) == 0


# --- 연결 조건: 하나라도 어긋나면 전송 객체 없이 거부 (종료 코드 2) ---------------------------------------------------

def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _set_app(root: Path, old: str, new: str) -> None:
    _rewrite(root / "config" / "m2" / "app.paper.toml", old, new)


REFUSALS = {
    "TRADE_MODE 없음": dict(environ={}),
    "TRADE_MODE BACKTEST": dict(environ={"TRADE_MODE": "BACKTEST"}),
    "TRADE_MODE LIVE": dict(environ={"TRADE_MODE": "LIVE"}),
    "TRADE_MODE 소문자": dict(environ={"TRADE_MODE": "paper"}),
    "--market 없음": dict(market=None),
    "--market M1": dict(market="M1"),
    "실전 도메인": dict(rest_url="https://api.binance.com"),
    "실전 대체 도메인": dict(rest_url="https://api1.binance.com"),
    "Demo 도메인": dict(rest_url="https://demo-api.binance.com"),
    "평문 http": dict(rest_url="http://testnet.binance.vision"),
    "포트 지정": dict(rest_url="https://testnet.binance.vision:443"),
    "WS API 호스트": dict(rest_url="https://ws-api.testnet.binance.vision"),
    "대체 테스트넷": dict(rest_url="https://api1.testnet.binance.vision"),
    "루프백": dict(rest_url="http://127.0.0.1:9"),
    "경로 포함": dict(rest_url="https://testnet.binance.vision/api"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refused_before_any_network_object(tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str) -> None:
    run = Run(tmp_path)
    assert run(**REFUSALS[case]) == 2
    out = capsys.readouterr().out
    assert "시작 점검 거부(연결 없음)" in out
    assert run.sim.factory_calls == 0 and run.opened == 0 and run.sim.sent == []
    assert run.files() == []
    _no_secret(out)


@pytest.mark.parametrize(("case", "edit", "why"), [
    ("접속 스위치 꺼짐", lambda r: _set_app(r, "connection_approved = true", "connection_approved = false"),
     "connection_approved"),
    ("접속 스위치 없음", lambda r: _set_app(r, "[paper]\nconnection_approved = true\n", ""), "connection_approved"),
    ("브로커 키움", lambda r: _set_app(r, 'broker = "binance-spot-testnet"', 'broker = "kiwoom-mock"'), "broker"),
    ("시장 M1", lambda r: _set_app(r, 'market = "M2"', 'market = "M1"'), "M2"),
    ("별칭 KR-", lambda r: (_set_app(r, "BN-PAPER-01", "KR-PAPER-01"),
                           _rewrite(r / "config" / "m2" / "limits.paper.toml", "BN-PAPER-01", "KR-PAPER-01")), "BN-"),
    ("API 키 없음", lambda r: (r / ".env.paper").write_text(
        f"TRADE_MODE=PAPER\nBINANCE_TESTNET_SECRET_KEY={SECRET}\n", encoding="utf-8"), "BINANCE_TESTNET_API_KEY"),
    ("시크릿 빈 값", lambda r: (r / ".env.paper").write_text(
        f"TRADE_MODE=PAPER\nBINANCE_TESTNET_API_KEY={KEY}\nBINANCE_TESTNET_SECRET_KEY=\n", encoding="utf-8"),
     "BINANCE_TESTNET_SECRET_KEY"),
    ("자격정보 파일 없음", lambda r: (r / ".env.paper").unlink(), "자격정보"),
    ("자격정보 파일 모드 다름", lambda r: (r / ".env.paper").write_text(
        f"TRADE_MODE=LIVE\nBINANCE_TESTNET_API_KEY={KEY}\nBINANCE_TESTNET_SECRET_KEY={SECRET}\n", encoding="utf-8"),
     "TRADE_MODE"),
    ("LIVE 키 섞임", lambda r: (r / ".env.paper").write_text(
        SECRETS + "BINANCE_LIVE_API_KEY=placeholder\n", encoding="utf-8"), "LIVE"),
    ("유니버스 비어 있음", lambda r: (r / "config" / "m2" / "universe.paper.toml").write_text(
        'quote_asset = "USDT"\nsymbols = []\n', encoding="utf-8"), "유니버스"),
])
def test_refused_on_config_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, edit, why: str) -> None:  # type: ignore[no-untyped-def]
    run = Run(tmp_path)
    edit(run.root)
    assert run() == 2, case
    out = capsys.readouterr().out
    assert "시작 점검 거부(연결 없음)" in out and why in out, out
    assert run.sim.factory_calls == 0 and run.opened == 0 and run.files() == []
    _no_secret(out)


@pytest.mark.parametrize("market", [Market.M1, None])
def test_refused_on_m1_or_legacy_db(tmp_path: Path, capsys: pytest.CaptureFixture[str], market: Market | None) -> None:
    """M1 체크아웃에서 실행했거나 다른 시장 DB면 DB 메타로 거부 (V-148)."""
    run = Run(tmp_path)
    repo = Repository(run.root / "var" / "paper" / "trading.db", Mode.PAPER)
    if market is not None:
        bind_market(repo.connection, market, "kiwoom-mock", created=True)
    repo.close()
    assert run() == 2
    assert "메타" in capsys.readouterr().out
    assert run.sim.factory_calls == 0


def test_refused_while_engine_alive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path)
    hb = run.root / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((NOW - timedelta(seconds=5)).isoformat(), encoding="utf-8")
    assert run() == 2
    assert run.sim.factory_calls == 0


def test_testnet_only_constant_and_no_live_hosts_in_app_module() -> None:
    src = (REPO / "src" / "trading_system" / "app" / "m2_preflight.py").read_text(encoding="utf-8")
    assert m2.TESTNET_REST_URL == "https://testnet.binance.vision"
    assert "api.binance.com" not in src and "demo-api" not in src


# --- 점검 실패 (종료 코드 1, 결과 파일은 남김) -------------------------------------------------------------------

@pytest.mark.parametrize(("routes", "words"), [
    ({("GET", "/api/v3/account"): [error(401, -2015, "Invalid API-key, IP, or permissions for action.")]},
     ("-2015", "IP")),
    ({("GET", "/api/v3/account"): [error(400, -1022, "Signature for this request is not valid.")]}, ("-1022", "서명")),
    ({("GET", "/api/v3/account"): [error(400, -1021, "Timestamp for this request is outside of the recvWindow.")]},
     ("-1021", "시계")),
    ({("GET", "/api/v3/time"): [error(418, -1003, "Way too much request weight used; IP banned until 1.")]},
     ("418", "사람 재개")),
    ({("GET", "/api/v3/exchangeInfo"): [error(429, -1003, "Too many requests.", {"Retry-After": "1"})]},
     ("429", "호출 제한")),
    ({("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info(_symbol("ETHUSDT")))]}, ("BTCUSDT", "심볼 없음")),
    ({("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info(_symbol(status="BREAK")))]}, ("BREAK", "TRADING")),
    ({("GET", "/api/v3/openOrders"): [reply(200, [OPEN_ORDER])]}, ("미체결 1건",)),
])
def test_check_failures_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str], routes: dict,
                                 words: tuple[str, ...]) -> None:
    run = Run(tmp_path, routes)
    assert run() == 1
    out = capsys.readouterr().out
    rep = run.report()
    msg = " ".join(rep["failures"])
    assert all(w in msg for w in words), msg
    assert all(w in out for w in words) and "결과: 실패" in out
    assert rep["order_api_sent"] == 0
    _no_secret(out, run.texts())


def test_server_time_offset_warning_uses_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path, {("GET", "/api/v3/time"): [reply(200, {"serverTime": T0 - 600})]})
    assert run() == 0
    rep = run.report()
    assert rep["server_time_offset_ms"] == -600
    assert any("-600ms" in w and "500ms" in w for w in rep["warnings"])


def test_sntp_offset_over_one_second_fails(tmp_path: Path) -> None:
    run = Run(tmp_path)
    assert run(sntp=lambda h, p: Decimal("1.5")) == 1
    assert any("SNTP" in f or "시계 오차" in f for f in run.report()["failures"])


def test_l7_below_min_notional_fails(tmp_path: Path) -> None:
    sym = _symbol()
    for f in sym["filters"]:
        if f["filterType"] == "NOTIONAL":
            f["minNotional"] = "150.00000000"
    run = Run(tmp_path, {("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info(sym))]})
    assert run() == 1
    assert any("L7" in f and "150" in f for f in run.report()["failures"])


def test_transport_failure_reported_without_query_string(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path, {("GET", "/api/v3/time"): [("PRE", "연결 실패")]})
    assert run() == 1
    out = capsys.readouterr().out
    assert "signature" not in out and "signature" not in run.texts()


def test_unexpected_session_error_still_writes_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class _Guard:
        blocked: list[str] = []
        order_sent = 0

    class _Broken:
        guard = _Guard()

        def run(self, symbols: list[str], quote: str) -> dict:
            raise RuntimeError(f"boom {KEY}")

        def calls(self) -> list:
            return []

    root = make_root(tmp_path)
    code = m2.m2_preflight_main(root, lambda *a, **k: _Broken(), ENV, market="M2", clock=FixedClock(NOW),
                                make_transport=TestnetSim().factory(), sntp_query=sntp_ok, sleep=lambda s: None)
    assert code == 1
    rep = json.loads(next((root / "var" / "paper" / "preflight").glob("*.json")).read_text(encoding="utf-8"))
    assert any("RuntimeError" in f for f in rep["failures"])
    _no_secret(capsys.readouterr().out, json.dumps(rep))


# --- 시작 대조: 기존 M2 DB면 SpotReconciler ---------------------------------------------------------------------

def _m2_db(root: Path, *, baseline: str | None, btc: str = "1") -> Path:
    db = root / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        bind_market(repo.connection, Market.M2, BROKER_BINANCE_TESTNET, created=True)
        repo.register_account(Account(alias="BN-PAPER-01", mode=Mode.PAPER, broker_env="mock"))
        with repo.tx():
            if baseline is not None:
                repo.audit(NOW - timedelta(hours=1), QUOTE_BASELINE, "operator", "BN-PAPER-01",
                           {"market": "M2", "asset": "USDT", "amount": baseline,
                            "at": (NOW - timedelta(hours=1)).isoformat()})
            repo.set_position("BN-PAPER-01", "BTCUSDT", Decimal(btc), Decimal("0"))
    finally:
        repo.close()
    return db


def test_existing_db_reconciles_match(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    _m2_db(root, baseline="10000")
    run = Run(tmp_path, root=root)
    assert run() == 0, run.report()["failures"]
    rep = run.report()
    assert rep["reconcile"] == "MATCH" and rep["reconcile_id"]


def test_existing_db_reconcile_mismatch_fails(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    _m2_db(root, baseline="9000")
    run = Run(tmp_path, root=root)
    assert run() == 1
    rep = run.report()
    assert rep["reconcile"] == "MISMATCH" and any("대조 불일치" in f for f in rep["failures"])


def test_empty_existing_db_has_no_baseline(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    db = root / "var" / "paper" / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    bind_market(repo.connection, Market.M2, BROKER_BINANCE_TESTNET, created=True)
    repo.close()
    run = Run(tmp_path, root=root)
    assert run() == 0
    assert run.report()["reconcile"] == "NO_BASELINE"


# --- 스크립트 -----------------------------------------------------------------------------------------------

def test_script_wires_testnet_session_into_app_cli() -> None:
    """scripts/binance-preflight.py: 바이낸스 세션 생성 함수를 app CLI에 넘기기만 한다(실행하지 않고 구조만 확인)."""
    src = (REPO / "scripts" / "binance-preflight.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {(n.module, a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert ("trading_system.app.m2_preflight", "m2_preflight_cli") in imported
    assert ("trading_system.broker.binance.preflight", "open_testnet_session") in imported
    assert "__main__" in src and "sys.argv[1:]" in src
