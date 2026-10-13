"""바이낸스 테스트넷 시작 점검 부품 (T-M2-14, CP-024 D-098·D-106·D-112~D-115, V-145·V-146·V-147·V-158).

네트워크 없음: 합성 테스트넷(tests/support/binance_testnet_sim)이 응답한다.
- 가드(PreflightGuard): 테스트넷 REST 호스트 + GET 조회 경로 허용 목록만 보낸다. 주문 계열(신규·취소·정정·테스트 주문·OCO·
  cancelReplace·SOR·사용자 스트림)은 경로별로 전송 전 차단(전송 0)을 증명한다.
- 세션: 서버 시각 → ping → exchangeInfo(BTCUSDT) → 계좌 → 미체결 → 최근 주문·체결. 응답 분류(-1021·-1022·-2015·418·429)와
  심볼 없음·상태 BREAK 보고.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tests.contract.test_binance_rules import _symbol
from tests.support.binance_testnet_sim import (
    ACCOUNT, KEY, OPEN_ORDER, SECRET, T0, UID, TestnetSim, error, exchange_info, reply,
)
from trading_system.broker.base import BrokerQueryError
from trading_system.broker.binance import client as c
from trading_system.broker.binance import orders as o
from trading_system.broker.binance import preflight as pf
from trading_system.broker.binance.account import parse_account
from trading_system.broker.binance.signing import hmac_hex
from trading_system.broker.binance.weights import CallPriority as P, WeightBudget, parse_rate_limits
from trading_system.domain.enums import OrderType, SubmitOutcome

URL = "https://testnet.binance.vision"


class _Secret:
    def __init__(self, v: str) -> None:
        self._v = v

    def reveal(self) -> str:
        return self._v


class Env:
    def __init__(self, routes: dict | None = None) -> None:
        self.t = T0
        self.slept: list[float] = []
        self.sim = TestnetSim(routes)
        self.session = pf.open_testnet_session(URL, _Secret(KEY), _Secret(SECRET), self.sim.factory(),
                                               now_ms=lambda: self.t, sleep=self._sleep)

    def _sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += int(s * 1000)

    def run(self) -> dict:
        return self.session.run(["BTCUSDT"], "USDT")


def _guard() -> tuple[pf.PreflightGuard, TestnetSim]:
    sim = TestnetSim()
    return pf.PreflightGuard(sim), sim


# --- 허용 목록 (전송 전 차단) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(pf.QUERY_PATHS))
def test_guard_sends_only_allowed_get_queries(path: str) -> None:
    guard, sim = _guard()
    r = guard.send("GET", f"{URL}{path}?symbol=BTCUSDT", {})
    assert r.status == 200 and len(sim.sent) == 1
    assert [(x.method, x.path, x.status) for x in guard.records] == [("GET", path, 200)]
    assert guard.blocked == [] and guard.order_sent == 0


ORDER_FAMILY = [
    ("POST", "/api/v3/order"), ("DELETE", "/api/v3/order"), ("PUT", "/api/v3/order"),
    ("POST", "/api/v3/order/test"), ("POST", "/api/v3/order/oco"), ("POST", "/api/v3/orderList/oco"),
    ("POST", "/api/v3/orderList/oto"), ("POST", "/api/v3/orderList/otoco"), ("DELETE", "/api/v3/orderList"),
    ("POST", "/api/v3/order/cancelReplace"), ("PUT", "/api/v3/order/amend/keepPriority"),
    ("DELETE", "/api/v3/openOrders"), ("POST", "/api/v3/sor/order"), ("POST", "/api/v3/sor/order/test"),
    ("GET", "/api/v3/order"), ("GET", "/api/v3/orderList"), ("GET", "/api/v3/openOrderList"),
    ("POST", "/api/v3/account"), ("POST", "/api/v3/userDataStream"),
]


@pytest.mark.parametrize(("method", "path"), ORDER_FAMILY)
def test_guard_blocks_order_family_before_send(method: str, path: str) -> None:
    """주문 신규·취소·정정·테스트 주문·OCO·cancelReplace·SOR, 조회라도 허용 목록 밖, GET 외 메서드 → 전송 0."""
    guard, sim = _guard()
    with pytest.raises(c.PreSendError):
        guard.send(method, f"{URL}{path}?symbol=BTCUSDT", {"X-MBX-APIKEY": "x"})
    assert sim.sent == [] and guard.records == []
    assert guard.blocked == [f"{method} {path}"] and guard.order_sent == 0
    if path != "/api/v3/account" and path != "/api/v3/userDataStream":
        assert pf.is_order_family(method, path)


@pytest.mark.parametrize("url", [
    "https://api.binance.com/api/v3/time", "https://api1.binance.com/api/v3/time",
    "https://api-gcp.binance.com/api/v3/time", "https://demo-api.binance.com/api/v3/time",
    "https://api1.testnet.binance.vision/api/v3/time", "http://testnet.binance.vision/api/v3/time",
    "https://testnet.binance.vision:8443/api/v3/time", "https://127.0.0.1/api/v3/time",
    "https://user@testnet.binance.vision/api/v3/time",
])
def test_guard_refuses_any_host_but_testnet_rest(url: str) -> None:
    """실전·Demo·대체 테스트넷·평문·포트 지정·루프백 → 전송 전 거부 (D-098, A-096 testnet.binance.vision 계열)."""
    guard, sim = _guard()
    with pytest.raises(c.PreSendError, match="접속 대상"):
        guard.send("GET", url, {})
    assert sim.sent == [] and len(guard.blocked) == 1


@pytest.mark.parametrize("url", ["https://api.binance.com", "https://demo-api.binance.com",
                                 "http://127.0.0.1:9", "https://api1.testnet.binance.vision"])
def test_session_refuses_non_testnet_base_without_transport(url: str) -> None:
    sim = TestnetSim()
    with pytest.raises(c.EndpointError):
        pf.open_testnet_session(url, _Secret(KEY), _Secret(SECRET), sim.factory(), now_ms=lambda: T0,
                                sleep=lambda s: None)
    assert sim.factory_calls == 0


def test_client_order_change_through_guard_is_never_sent() -> None:
    """어댑터의 주문 신규·취소가 가드를 지나면 전송 전 실패(PRE_SEND_FAILURE) — 결과 불명이 아니다."""
    guard, sim = _guard()
    budget = WeightBudget(parse_rate_limits(exchange_info()["rateLimits"]), now_ms=lambda: T0)
    client = c.BinanceSpotClient(c.TESTNET_REST, c.Credentials(_Secret(KEY), _Secret(SECRET)), guard, budget,
                                 now_ms=lambda: T0)
    new = o.NewOrder("BTCUSDT", o.OrderSide.BUY, o.OrderKind.LIMIT_IOC, Decimal("0.001"), Decimal("61000.00"),
                     o.client_order_id("intent-1"))
    placed = client.place(new, P.INCREASE, qty_step=Decimal("0.00001"), tick=Decimal("0.01"))
    cancelled = client.cancel("BTCUSDT", P.REDUCE, order_id=1)
    assert placed.outcome is SubmitOutcome.PRE_SEND_FAILURE and cancelled.outcome is SubmitOutcome.PRE_SEND_FAILURE
    assert sim.sent == [] and guard.blocked == ["POST /api/v3/order", "DELETE /api/v3/order"]


def test_preflight_module_has_no_order_change_calls() -> None:
    src = (pf.__file__ and open(pf.__file__, encoding="utf-8").read()) or ""
    assert ".place(" not in src and ".cancel(" not in src


# --- 정상 점검 ------------------------------------------------------------------------------------------------

def test_session_runs_read_only_checks_in_order() -> None:
    env = Env()
    res = env.run()
    assert res["failures"] == [], res["failures"]
    assert env.sim.paths() == [("GET", "/api/v3/time"), ("GET", "/api/v3/ping"), ("GET", "/api/v3/exchangeInfo"),
                               ("GET", "/api/v3/account"), ("GET", "/api/v3/openOrders"),
                               ("GET", "/api/v3/allOrders"), ("GET", "/api/v3/myTrades")]
    assert res["server_time_offset_ms"] == 0
    btc = res["exchange"]["BTCUSDT"]
    assert btc["found"] and btc["status"] == "TRADING" and btc["trading"] and btc["rules_ok"]
    assert btc["filters"]["PRICE_FILTER"] == {"minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"}
    assert btc["filters"]["LOT_SIZE"] == {"minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"}
    assert btc["filters"]["NOTIONAL"]["minNotional"] == "5" and btc["filters"]["MIN_NOTIONAL"] is None
    assert btc["min_notional"] == "5" and btc["quote_asset_precision"] == 8
    assert {(r["type"], r["interval_ms"], r["limit"]) for r in res["rate_limits"]} >= {
        ("REQUEST_WEIGHT", 60_000, 6000), ("ORDERS", 10_000, 100), ("RAW_REQUESTS", 300_000, 300000)}
    assert [r["budget"] for r in res["rate_limits"] if r["type"] == "REQUEST_WEIGHT"] == [3000]
    assert res["account"]["can_trade"] is True
    assert res["account"]["balances"] == [{"asset": "BTC", "free": "1", "locked": "0"},
                                          {"asset": "ETH", "free": "1", "locked": "0"},
                                          {"asset": "USDT", "free": "10000", "locked": "0"}]
    assert res["open_orders"] == {"BTCUSDT": 0} and res["recent_orders"] == {"BTCUSDT": 0}
    assert res["recent_fills"] == {"BTCUSDT": 0}
    assert res["signed_ok"] and res["exchange_ok"]
    assert env.session.guard.order_sent == 0 and env.session.guard.blocked == []


def test_signed_queries_carry_hmac_and_key_header_only_where_needed() -> None:
    """V-158 방식: 보낸 쿼리(서명 앞부분)를 표준 hmac으로 다시 계산해 같아야 한다. 서명 없는 조회에는 키 헤더도 없다."""
    env = Env()
    env.run()
    for i, (method, url, headers) in enumerate(env.sim.sent):
        path = url.split("?")[0].removeprefix(URL)
        query = url.split("?", 1)[1] if "?" in url else ""
        if path in pf.SIGNED_PATHS:
            payload, sig = query.rsplit("&signature=", 1)
            assert sig == hmac_hex(SECRET, payload)
            assert headers == {"X-MBX-APIKEY": KEY}
            assert "recvWindow=5000" in payload and f"timestamp={T0}" in payload
        else:
            assert "signature" not in query and headers == {}
    acct = env.sim.params(3)
    assert acct["omitZeroBalances"] == "true"
    assert env.sim.params(2)["symbol"] == "BTCUSDT"


def test_result_has_no_key_secret_or_account_identifier() -> None:
    env = Env()
    text = json.dumps(env.run(), ensure_ascii=False) + repr(env.session) + json.dumps(env.session.calls())
    assert KEY not in text and SECRET not in text and str(UID) not in text and "uid" not in text


def test_parse_account_drops_identifier() -> None:
    a = parse_account(ACCOUNT)
    assert not hasattr(a, "uid") and str(UID) not in repr(a)
    assert a.balance_map()["USDT"] == (Decimal("10000"), Decimal("0"))


# --- 거래 규칙 실패 (PV-M2-03) ---------------------------------------------------------------------------------

def test_symbol_missing_in_exchange_info_fails() -> None:
    env = Env({("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info(_symbol("ETHUSDT")))]})
    res = env.run()
    assert any("BTCUSDT" in f and "심볼 없음" in f for f in res["failures"])
    assert res["exchange"]["BTCUSDT"]["found"] is False and not res["exchange_ok"]


def test_symbol_rejected_by_server_fails_with_code() -> None:
    env = Env({("GET", "/api/v3/exchangeInfo"): [error(400, -1121, "Invalid symbol.")]})
    res = env.run()
    assert any("-1121" in f for f in res["failures"]), res["failures"]
    # 호출 한도(rateLimits)를 모르면 서명 조회를 하지 않는다 (D-113 못 읽으면 시작 거부)
    assert ("GET", "/api/v3/account") not in env.sim.paths()
    assert any("rateLimits" in f for f in res["failures"])


@pytest.mark.parametrize("status", ["BREAK", "HALT", "END_OF_DAY", "CANCEL_ONLY"])
def test_symbol_not_trading_fails(status: str) -> None:
    env = Env({("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info(_symbol(status=status)))]})
    res = env.run()
    assert any(status in f and "TRADING" in f for f in res["failures"]), res["failures"]
    assert res["exchange"]["BTCUSDT"]["trading"] is False


def test_spot_trading_not_allowed_fails() -> None:
    env = Env({("GET", "/api/v3/exchangeInfo"): [
        reply(200, exchange_info(_symbol(isSpotTradingAllowed=False)))]})
    assert any("isSpotTradingAllowed" in f for f in env.run()["failures"])


def test_missing_notional_filter_is_warning_only() -> None:
    sym = _symbol()
    sym["filters"] = [f for f in sym["filters"] if f["filterType"] != "NOTIONAL"]
    env = Env({("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info(sym))]})
    res = env.run()
    assert res["failures"] == [] and any("NOTIONAL" in w for w in res["warnings"])


def test_rules_without_lot_size_fail() -> None:
    sym = _symbol()
    sym["filters"] = [f for f in sym["filters"] if f["filterType"] != "LOT_SIZE"]
    env = Env({("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info(sym))]})
    res = env.run()
    assert any("거래 규칙" in f and "LOT_SIZE" in f for f in res["failures"])


# --- 응답 분류 (D-112·D-113·D-115) --------------------------------------------------------------------------------

def test_open_orders_reported() -> None:
    env = Env({("GET", "/api/v3/openOrders"): [reply(200, [OPEN_ORDER])]})
    res = env.run()
    assert res["open_orders"] == {"BTCUSDT": 1}


@pytest.mark.parametrize(("status", "code", "words"), [
    (400, -1022, ("-1022", "서명")),
    (401, -2015, ("-2015", "IP", "권한")),
    (400, -1021, ("-1021", "시계")),
])
def test_signed_query_auth_and_clock_errors_stop_signed_steps(status: int, code: int, words: tuple[str, ...]) -> None:
    env = Env({("GET", "/api/v3/account"): [error(status, code, "synthetic")]})
    res = env.run()
    msg = " ".join(res["failures"])
    assert all(w in msg for w in words), msg
    assert not res["signed_ok"]
    # 인증·시각 오류는 재시도 없음, 이후 서명 조회도 보내지 않는다
    assert env.sim.paths().count(("GET", "/api/v3/account")) == 1
    assert ("GET", "/api/v3/openOrders") not in env.sim.paths()
    assert KEY not in msg and SECRET not in msg


def test_418_stops_every_call() -> None:
    env = Env({("GET", "/api/v3/ping"): [error(418, -1003, "Way too much request weight used; IP banned until 1.")]})
    res = env.run()
    assert env.sim.paths() == [("GET", "/api/v3/time"), ("GET", "/api/v3/ping")]
    msg = " ".join(res["failures"])
    assert "418" in msg and "사람 재개" in msg
    assert any(a.startswith("K5") for a in res["alerts"])


def test_429_waits_retry_after_then_succeeds() -> None:
    env = Env({("GET", "/api/v3/exchangeInfo"): [error(429, -1003, "Too many requests.", {"Retry-After": "2"}),
                                                  reply(200, exchange_info())]})
    res = env.run()
    assert res["failures"] == [] and 2.0 in env.slept
    calls = {x["path"]: x for x in env.session.calls()}
    assert calls["/api/v3/exchangeInfo"]["calls"] == 2 and calls["/api/v3/exchangeInfo"]["last_status"] == 200


def test_429_persistent_reports_rate_limit() -> None:
    env = Env({("GET", "/api/v3/time"): [error(429, -1003, "Too many requests.", {"Retry-After": "1"})]})
    res = env.run()
    assert any("429" in f and "호출 제한" in f for f in res["failures"]), res["failures"]


def test_5xx_and_transport_errors_reported_without_url() -> None:
    env = Env({("GET", "/api/v3/account"): [("AFTER", "read timeout"), reply(503, None), reply(503, None)]})
    res = env.run()
    msg = " ".join(res["failures"])
    assert "/api/v3/account" in msg and "signature" not in msg and KEY not in msg


def test_server_time_offset_measured() -> None:
    env = Env({("GET", "/api/v3/time"): [reply(200, {"serverTime": T0 + 700})]})
    res = env.run()
    assert res["server_time_offset_ms"] == 700
    assert not any("서버 시각" in a for a in res["alerts"])   # 경고 판정은 설정 값으로 호출자(app)가 한다


# --- 대조용 조회 전용 어댑터 -------------------------------------------------------------------------------------

def test_readonly_queries_convert_to_domain_records() -> None:
    filled = dict(OPEN_ORDER, orderId=78, clientOrderId="M2-abc", status="FILLED", executedQty="0.00100000",
                  cummulativeQuoteQty="50.00000000", type="MARKET", side="SELL")
    trade = {"symbol": "BTCUSDT", "id": 900, "orderId": 78, "orderListId": -1, "price": "50000.00",
             "qty": "0.00100000", "quoteQty": "50.00000000", "commission": "0.05000000", "commissionAsset": "USDT",
             "time": T0, "isBuyer": False, "isMaker": False, "isBestMatch": True}
    env = Env({("GET", "/api/v3/openOrders"): [reply(200, [OPEN_ORDER])],
               ("GET", "/api/v3/allOrders"): [reply(200, [OPEN_ORDER, filled])],
               ("GET", "/api/v3/myTrades"): [reply(200, [trade])]})
    env.run()
    q = env.session.queries(["BTCUSDT"])
    assert q.query_balances("BN-PAPER-01")["USDT"] == (Decimal("10000"), Decimal("0"))
    orders = {r.order_no: r for r in q.query_orders("BN-PAPER-01")}
    assert set(orders) == {"77", "78"}
    assert (orders["77"].state, orders["77"].order_type, orders["77"].kind) == ("ACCEPTED", OrderType.LIMIT, "NEW")
    assert (orders["78"].state, orders["78"].order_type, orders["78"].client_order_id) == (
        "FILLED", OrderType.MARKET, "M2-abc")
    (fill,) = q.query_fills("BN-PAPER-01")
    assert (fill.order_no, fill.fill_id, fill.quote_qty, fill.commission_asset, fill.instrument) == (
        "78", "900", Decimal("50"), "USDT", "BTCUSDT")
    windows = [env.sim.params(i) for i, p in enumerate(env.sim.paths()) if p[1] in ("/api/v3/allOrders",
                                                                                   "/api/v3/myTrades")]
    assert all(int(w["endTime"]) - int(w["startTime"]) <= 86_400_000 for w in windows)


def test_readonly_query_failure_is_broker_query_error() -> None:
    env = Env()
    env.run()
    env.sim.routes[("GET", "/api/v3/account")] = [error(401, -2015, "Invalid API-key, IP, or permissions for action.")]
    with pytest.raises(BrokerQueryError, match="-2015"):
        env.session.queries(["BTCUSDT"]).query_balances("BN-PAPER-01")


def test_readonly_unknown_order_status_is_query_error() -> None:
    env = Env({("GET", "/api/v3/openOrders"): [reply(200, [dict(OPEN_ORDER, status="SOMETHING_NEW")])]})
    env.run()
    with pytest.raises(BrokerQueryError, match="상태"):
        env.session.queries(["BTCUSDT"]).query_orders("BN-PAPER-01")
