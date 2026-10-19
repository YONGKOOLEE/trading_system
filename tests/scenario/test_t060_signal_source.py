"""CP-039 T-060 (A-115, A-127) — 실서버 공개 시세 모듈·가드·데이터 출처 시작 표·별도 예산 (D-243~D-247).

V-314 공개 시세 가드, V-315 자격정보 격리, V-316 D-098 개정(tests/unit/test_tm2_08_market_scope.py와 함께), V-317 데이터 출처
시작 표, V-327 실서버 호출 예산. **네트워크 없음** — 가짜 전송만(합성 응답). 실서버 연결 관찰(T-064)은 별도 연결 승인.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.config.app_file import parse_app_settings
from trading_system.config.errors import ConfigError
from trading_system.config.market_scope import BINANCE_TESTNET_HOSTS, START_TABLE, check_signal_source
from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market
from trading_system.domain.signal_source import KIND_PUBLIC, KIND_TESTNET, SignalSourceSettings
from trading_system.marketdata import binance_public as bp
from trading_system.marketdata.binance_public import (
    DATA_SOURCE_TABLE, KLINES_PATH, PUBLIC_HOSTS, PUBLIC_REST, TICKER_PATH, BinancePublicClient, DataSourceRefused,
    PublicBudget, PublicDataGuard, PublicDataRefused, PublicMinutePlan, PublicQueryError, SignalPlan,
    data_source_rule, public_budget_problem, schedule_weight_per_minute,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "trading_system"
PUBLIC_MODULE = SRC / "marketdata" / "binance_public.py"


@dataclass
class Reply:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""


class FakeTransport:
    """하위 전송 흉내 — 받은 요청만 기록한다(네트워크 없음)."""

    def __init__(self, replies: list[Reply] | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.replies = list(replies or [])

    def send(self, method: str, url: str, headers):  # type: ignore[no-untyped-def]
        self.calls.append((method, url, dict(headers)))
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return Reply(200, {}, '{"symbol":"BTCUSDT","price":"100.00"}')


def guard(inner: FakeTransport | None = None) -> tuple[PublicDataGuard, FakeTransport]:
    t = inner or FakeTransport()
    return PublicDataGuard(t, data_source_rule(Mode.PAPER, Market.M2, KIND_PUBLIC)), t


# --- V-314 공개 시세 가드 ---------------------------------------------------------------------------------------

OK_URLS = [
    f"{PUBLIC_REST}{KLINES_PATH}?symbol=BTCUSDT&interval=1d&limit=200",
    f"{PUBLIC_REST}{KLINES_PATH}?symbol=BTCUSDT&interval=1m&startTime=1&endTime=2&limit=10",
    f"{PUBLIC_REST}{TICKER_PATH}?symbol=BTCUSDT",
]


@pytest.mark.parametrize("url", OK_URLS)
def test_v314_allowed_public_get_passes(url: str) -> None:
    g, t = guard()
    g.send("GET", url, {})
    assert len(t.calls) == 1 and g.sent == 1 and g.blocked == []


BAD = [
    ("POST", f"{PUBLIC_REST}{KLINES_PATH}?symbol=BTCUSDT", {}),                       # 메서드
    ("DELETE", f"{PUBLIC_REST}/api/v3/order?symbol=BTCUSDT", {}),
    ("GET", f"http://api.binance.com{TICKER_PATH}?symbol=BTCUSDT", {}),               # 평문
    ("GET", f"https://testnet.binance.vision{TICKER_PATH}?symbol=BTCUSDT", {}),       # 테스트넷도 이 가드에서는 거부
    ("GET", f"https://api1.binance.com{TICKER_PATH}?symbol=BTCUSDT", {}),             # 대체 호스트
    ("GET", f"https://api.binance.com.evil.example{TICKER_PATH}?symbol=BTCUSDT", {}),
    ("GET", f"https://api.binance.com:443{TICKER_PATH}?symbol=BTCUSDT", {}),          # 포트
    ("GET", f"https://user@api.binance.com{TICKER_PATH}?symbol=BTCUSDT", {}),         # 사용자 정보
    ("GET", f"{PUBLIC_REST}/api/v3/account", {}),                                     # 계좌
    ("GET", f"{PUBLIC_REST}/api/v3/order?symbol=BTCUSDT", {}),                        # 주문
    ("GET", f"{PUBLIC_REST}/api/v3/myTrades?symbol=BTCUSDT", {}),
    ("GET", f"{PUBLIC_REST}/api/v3/avgPrice?symbol=BTCUSDT", {}),                     # Q-2 범위 밖
    ("GET", f"{PUBLIC_REST}/api/v3/depth?symbol=BTCUSDT", {}),
    ("GET", f"{PUBLIC_REST}/api/v3/exchangeInfo?symbol=BTCUSDT", {}),                 # Q-2: klines·현재가만 [임시]
    ("GET", f"{PUBLIC_REST}/api/v3/time", {}),
    ("GET", f"{PUBLIC_REST}/sapi/v1/capital/config/getall", {}),
    ("GET", f"{PUBLIC_REST}{KLINES_PATH}?symbol=BTCUSDT&signature=ab", {}),           # 서명 쿼리
    ("GET", f"{PUBLIC_REST}{KLINES_PATH}?symbol=BTCUSDT&timestamp=1", {}),
    ("GET", f"{PUBLIC_REST}{KLINES_PATH}?symbol=BTCUSDT&recvWindow=5000", {}),
    ("GET", f"{PUBLIC_REST}{TICKER_PATH}?symbol=BTCUSDT&interval=1m", {}),            # 경로별 허용 키 밖
    ("GET", f"{PUBLIC_REST}{TICKER_PATH}?symbol=BTCUSDT#x", {}),                      # 프래그먼트
    ("GET", f"{PUBLIC_REST}{TICKER_PATH}?symbol=BTCUSDT", {"X-MBX-APIKEY": "synthetic"}),   # 키 헤더 1개
]


@pytest.mark.parametrize(("method", "url", "headers"), BAD)
def test_v314_everything_else_refused_before_send(method: str, url: str, headers: dict) -> None:
    g, t = guard()
    with pytest.raises(PublicDataRefused):
        g.send(method, url, headers)
    assert t.calls == [] and g.sent == 0 and len(g.blocked) == 1
    assert "synthetic" not in g.blocked[0] and "=ab" not in g.blocked[0] and "BTCUSDT" not in g.blocked[0]  # 값 없음


# --- V-315 자격정보 격리 ----------------------------------------------------------------------------------------

def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append(("." * n.level) + (n.module or ""))
    return out


def test_v315_public_module_has_no_credential_parameters() -> None:
    bad = []
    for name, obj in vars(bp).items():
        if getattr(obj, "__module__", None) != bp.__name__ or not callable(obj):
            continue
        targets = [obj] + ([m for _n, m in inspect.getmembers(obj, inspect.isfunction)] if inspect.isclass(obj) else [])
        for f in targets:
            try:
                params = inspect.signature(f).parameters
            except (TypeError, ValueError):
                continue
            bad += [f"{name}.{p}" for p in params if any(w in p.lower() for w in
                                                          ("key", "secret", "credential", "sign", "token"))]
    assert bad == []


def test_v315_import_direction_public_module_and_binance_package() -> None:
    mods = _imports(PUBLIC_MODULE)
    assert not any("binance" in m or "signing" in m or "envfile" in m or "secrets" in m for m in mods), mods
    for p in (SRC / "broker" / "binance").rglob("*.py"):
        assert not any("binance_public" in m or "signal_feed" in m for m in _imports(p)), p.name
    feed = SRC / "marketdata" / "signal_feed.py"
    assert not any("broker.binance" in m or m.startswith("..broker") and "binance" in m for m in _imports(feed))


def test_v315_public_hosts_never_reach_execution_allow_lists() -> None:
    assert PUBLIC_HOSTS.isdisjoint(BINANCE_TESTNET_HOSTS)
    for rule in START_TABLE.values():
        assert PUBLIC_HOSTS.isdisjoint(rule.hosts)
    assert all(not k[0] is Mode.LIVE for k in DATA_SOURCE_TABLE)          # LIVE 행 없음


def test_v315_run_guard_refuses_public_host() -> None:
    from trading_system.broker.binance.client import PreSendError
    from trading_system.broker.binance.run_guard import RunGuard
    inner = FakeTransport()
    rg = RunGuard(inner, orders_enabled=True)
    with pytest.raises(PreSendError):
        rg.send("GET", f"{PUBLIC_REST}{TICKER_PATH}?symbol=BTCUSDT", {})
    with pytest.raises(PreSendError):
        rg.send("POST", f"{PUBLIC_REST}/api/v3/order?symbol=BTCUSDT", {})
    assert inner.calls == []


# --- V-317 데이터 출처 시작 표·설정 ---------------------------------------------------------------------------------

APP_M2 = {"app": {"account": "BN-PAPER-01", "loop_seconds": "0.5", "reconcile_minutes": 1,
                  "sntp_servers": ["127.0.0.1:9"], "market": "M2", "broker": "binance-spot-testnet"}}
APP_M1 = {"app": {"account": "KR-PAPER-01", "loop_seconds": "0.5", "reconcile_minutes": 1,
                  "sntp_servers": ["127.0.0.1:9"]}}


def _app(base: dict, signal: dict | None, mode: Mode = Mode.PAPER, tmp: Path = Path(".")):  # type: ignore[no-untyped-def]
    raw = {k: dict(v) for k, v in base.items()}
    if signal is not None:
        raw["signal_source"] = signal
    if mode is Mode.BACKTEST:
        raw["app"]["broker"] = "fake" if raw["app"].get("market") == "M2" else raw["app"].get("broker", "fake")
        raw["app"]["account"] = raw["app"]["account"].replace("PAPER", "BT")
    return parse_app_settings(raw, mode, tmp)


def test_v317_missing_section_is_testnet_unchanged() -> None:
    s = _app(APP_M2, None).signal_source
    assert s == SignalSourceSettings() and s.kind == KIND_TESTNET and s.max_divergence == Decimal("0.01")
    check_signal_source(Mode.PAPER, Market.M2, s)                       # 거부 없음


def test_v317_public_with_approval_parsed_and_accepted() -> None:
    s = _app(APP_M2, {"kind": KIND_PUBLIC, "approval": "A-900"}).signal_source
    assert (s.kind, s.approval, s.max_divergence) == (KIND_PUBLIC, "A-900", Decimal("0.01"))
    check_signal_source(Mode.PAPER, Market.M2, s)
    assert data_source_rule(Mode.PAPER, Market.M2, KIND_PUBLIC).hosts == PUBLIC_HOSTS


def test_v317_divergence_percent_setting() -> None:
    s = _app(APP_M2, {"kind": KIND_PUBLIC, "approval": "A-900", "max_divergence_percent": "0.5"}).signal_source
    assert s.max_divergence == Decimal("0.005")


@pytest.mark.parametrize("signal", [
    {"kind": KIND_PUBLIC},                                          # 승인 A-ID 없음
    {"kind": KIND_PUBLIC, "approval": "A900"},                      # 형식 오류
    {"kind": KIND_PUBLIC, "approval": "a-900"},
    {"kind": "binance-live"},                                       # 알 수 없는 출처
    {"kind": KIND_PUBLIC, "approval": "A-900", "host": "x"},        # 알 수 없는 키
    {"kind": KIND_PUBLIC, "approval": "A-900", "max_divergence_percent": 1.0},   # 부동소수
    {"kind": KIND_PUBLIC, "approval": "A-900", "max_divergence_percent": "0"},
    {"kind": KIND_PUBLIC, "approval": "A-900", "max_divergence_percent": "10.1"},
])
def test_v317_bad_section_refused(signal: dict) -> None:
    with pytest.raises(ConfigError):
        _app(APP_M2, signal)


def test_v317_m1_with_section_refused() -> None:
    with pytest.raises(ConfigError, match="signal_source"):
        _app(APP_M1, {"kind": KIND_TESTNET})


def test_v317_backtest_public_refused_testnet_ok() -> None:
    s = _app(APP_M2, {"kind": KIND_PUBLIC, "approval": "A-900"}, Mode.BACKTEST).signal_source
    with pytest.raises(ConfigError, match="BACKTEST"):
        check_signal_source(Mode.BACKTEST, Market.M2, s)
    check_signal_source(Mode.BACKTEST, Market.M2, SignalSourceSettings())


@pytest.mark.parametrize(("mode", "market"), [(Mode.LIVE, Market.M2), (Mode.PAPER, Market.M1), (Mode.LIVE, Market.M1)])
def test_v317_no_row_refused(mode: Mode, market: Market) -> None:
    with pytest.raises(ConfigError):
        check_signal_source(mode, market, SignalSourceSettings(KIND_PUBLIC, "A-900", configured=True))
    with pytest.raises(DataSourceRefused):
        data_source_rule(mode, market, KIND_PUBLIC)


def test_v317_testnet_kind_has_no_data_source_row() -> None:
    with pytest.raises(DataSourceRefused):
        data_source_rule(Mode.PAPER, Market.M2, KIND_TESTNET)        # 실행 출처 = 데이터 출처, 추가 호스트 없음


# --- V-327 별도 호출 예산 ------------------------------------------------------------------------------------------

def test_v327_schedule_estimate_and_start_refusal() -> None:
    plan = SignalPlan(minute=PublicMinutePlan(1, 60))
    one = schedule_weight_per_minute(plan, 1)
    assert one == 16                                                 # 참조 시세 6회×2 + 1m 2 + 1d 2 (최대 분)
    assert public_budget_problem(plan, 1) is None
    assert public_budget_problem(plan, 3) is None                    # 48 ≤ 60
    problem = public_budget_problem(plan, 4)                         # 64 > 60
    assert problem is not None and "60" in problem


def test_v327_budget_sliding_window_separate_from_execution_budget() -> None:
    now = [0]
    b = PublicBudget(lambda: now[0], cap_per_minute=10)
    assert all(b.take(2) for _ in range(5)) and not b.take(2)
    now[0] = 59_999
    assert not b.take(1)
    now[0] = 60_000
    assert b.take(2)


def test_v327_client_refuses_when_budget_exhausted_without_sending() -> None:
    g, t = guard()
    b = PublicBudget(lambda: 0, cap_per_minute=2)
    c = BinancePublicClient(g, b, now_ms=lambda: 0, sleep=lambda s: None, on_alert=lambda k, m: None)
    assert c.ticker_price("BTCUSDT") == Decimal("100.00")
    with pytest.raises(PublicQueryError) as ei:
        c.ticker_price("BTCUSDT")
    assert ei.value.kind == "BUDGET" and len(t.calls) == 1


# --- 클라이언트 오류 정책 (D-247 (3)(4)) — 장애 처리 상세는 T-061 시험 -------------------------------------------------

def test_d247_429_stops_all_calls_without_retry_one_alert() -> None:
    now = [1_000]
    alerts: list[tuple[str, str]] = []
    g, t = guard(FakeTransport([Reply(429, {"Retry-After": "30"}, "")]))
    c = BinancePublicClient(g, PublicBudget(lambda: now[0]), now_ms=lambda: now[0], sleep=lambda s: None,
                            on_alert=lambda k, m: alerts.append((k, m)))
    with pytest.raises(PublicQueryError) as ei:
        c.ticker_price("BTCUSDT")
    assert ei.value.kind == "RATE_LIMIT" and len(t.calls) == 1
    with pytest.raises(PublicQueryError):
        c.klines("BTCUSDT", "1d", limit=10)                          # 중단 중 — 보내지 않음
    assert len(t.calls) == 1 and len(alerts) == 1 and alerts[0][0] == "EMERGENCY"
    now[0] = 1_000 + 30_000
    c.ticker_price("BTCUSDT")                                        # Retry-After 뒤 재개
    assert len(t.calls) == 2


def test_d247_418_without_retry_after_waits_for_human() -> None:
    now = [0]
    g, t = guard(FakeTransport([Reply(418, {}, "")]))
    c = BinancePublicClient(g, PublicBudget(lambda: now[0]), now_ms=lambda: now[0], sleep=lambda s: None,
                            on_alert=lambda k, m: None)
    with pytest.raises(PublicQueryError):
        c.ticker_price("BTCUSDT")
    now[0] = 10 ** 12
    with pytest.raises(PublicQueryError) as ei:
        c.ticker_price("BTCUSDT")
    assert ei.value.kind == "BANNED" and len(t.calls) == 1
    c.resume_after_ban()
    c.ticker_price("BTCUSDT")
    assert len(t.calls) == 2


def test_d247_5xx_and_transport_errors_retry_three_times_with_backoff() -> None:
    from trading_system.marketdata.binance_public import PublicTransportError
    slept: list[float] = []
    g, t = guard(FakeTransport([Reply(500), PublicTransportError("x"), Reply(503), Reply(502)]))
    c = BinancePublicClient(g, PublicBudget(lambda: 0, cap_per_minute=1000), now_ms=lambda: 0,
                            sleep=slept.append, on_alert=lambda k, m: None)
    with pytest.raises(PublicQueryError) as ei:
        c.ticker_price("BTCUSDT")
    assert ei.value.kind == "RETRY_EXHAUSTED" and len(t.calls) == 4 and slept == [1.0, 2.0, 4.0]


def test_d247_other_4xx_no_retry() -> None:
    g, t = guard(FakeTransport([Reply(400, {}, '{"code":-1121,"msg":"Invalid symbol."}')]))
    c = BinancePublicClient(g, PublicBudget(lambda: 0), now_ms=lambda: 0, sleep=lambda s: None,
                            on_alert=lambda k, m: None)
    with pytest.raises(PublicQueryError) as ei:
        c.ticker_price("BTCUSDT")
    assert ei.value.kind == "HTTP" and len(t.calls) == 1


def test_client_sends_no_headers_and_only_allowed_query() -> None:
    g, t = guard(FakeTransport([Reply(200, {}, "[]")]))
    c = BinancePublicClient(g, PublicBudget(lambda: 0), now_ms=lambda: 0, sleep=lambda s: None,
                            on_alert=lambda k, m: None)
    assert c.klines("BTCUSDT", "1m", limit=5, start_ms=1, end_ms=2) == []
    method, url, headers = t.calls[0]
    assert method == "GET" and headers == {} and url.startswith(PUBLIC_REST + KLINES_PATH + "?")


def test_run_m2_script_public_transport_hosts_only_public_module_constant() -> None:
    """scripts/run-m2.py: 실서버 공개 시세 전송의 허용 호스트 = 공개 시세 모듈 상수(그 밖 없음), 테스트넷 실행 전송과 분리."""
    from tests.scenario.test_t049_review_fixes import _load_script
    mod = _load_script("run_m2_script_t060", "run-m2.py")
    t = mod.make_public_transport()                                  # 세션 객체만 — 연결 없음
    assert t._hosts == PUBLIC_HOSTS and PUBLIC_HOSTS.isdisjoint(mod.REST_HOSTS | mod.WS_HOSTS)  # noqa: SLF001
    assert t.calls == 0
