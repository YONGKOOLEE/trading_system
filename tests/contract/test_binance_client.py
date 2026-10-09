"""바이낸스 Spot 테스트넷 REST 클라이언트 계약 (CP-024 D-098·D-110~D-115, V-137·V-145·V-146(-1021)·V-158·V-159).

네트워크 없음: 합성 전송(SynthTransport)이 공식 문서 형식의 응답을 돌려준다. 주문 변경 요청은 한 번만 보낸다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import pytest

from trading_system.broker.binance import client as c
from trading_system.broker.binance import orders as o
from trading_system.broker.binance.classify import Flag, QueryKind
from trading_system.broker.binance.weights import CallPriority as P, WeightBudget, parse_rate_limits
from trading_system.domain.enums import SubmitOutcome as O

KEY = "synthetic-api-key-0123456789abcdef"
SECRET = "synthetic-secret-fedcba9876543210"
T0 = 1_760_000_040_000


class _Secret:
    def __init__(self, v: str) -> None:
        self._v = v

    def reveal(self) -> str:
        return self._v


class SynthTransport:
    def __init__(self, replies: list[object]) -> None:
        self.replies = list(replies)
        self.sent: list[tuple[str, str, dict[str, str]]] = []

    def send(self, method: str, url: str, headers: Mapping[str, str]) -> c.RawReply:
        self.sent.append((method, url, dict(headers)))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        assert isinstance(r, c.RawReply)
        return r


def _reply(status: int, body: object, headers: Mapping[str, str] | None = None) -> c.RawReply:
    return c.RawReply(status, dict(headers or {}), json.dumps(body) if body is not None else "<html>")


RATE_LIMITS = [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 1000},
               {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 20}]


class Env:
    def __init__(self, replies: list[object]) -> None:
        self.t = T0
        self.slept: list[float] = []
        self.alerts: list[tuple[str, str]] = []
        self.transport = SynthTransport(replies)
        self.budget = WeightBudget(parse_rate_limits(RATE_LIMITS), now_ms=lambda: self.t)
        self.client = c.BinanceSpotClient(
            c.TESTNET_REST, c.Credentials(_Secret(KEY), _Secret(SECRET)), self.transport, self.budget,
            now_ms=lambda: self.t, sleep=self._sleep, on_alert=lambda k, m: self.alerts.append((k, m)))

    def _sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += int(s * 1000)


def _order() -> o.NewOrder:
    return o.NewOrder("BTCUSDT", o.OrderSide.BUY, o.OrderKind.LIMIT_IOC, Decimal("0.001"), Decimal("61000.00"),
                      o.client_order_id("intent-1"))


STEPS = dict(qty_step=Decimal("0.00001"), tick=Decimal("0.01"))
ACK = {"symbol": "BTCUSDT", "orderId": 28, "orderListId": -1, "clientOrderId": "x", "transactTime": T0,
       "price": "61000.00", "origQty": "0.00100000", "executedQty": "0.00100000",
       "cummulativeQuoteQty": "60.9", "status": "FILLED", "timeInForce": "IOC", "type": "LIMIT", "side": "BUY",
       "fills": [{"price": "60900.00", "qty": "0.00100000", "commission": "0.00000100",
                  "commissionAsset": "BTC", "tradeId": 9}]}


def _all_text(env: Env, *extra: object) -> str:
    return " ".join([repr(env.client), str(env.alerts), *map(str, extra)])


# --- 접속 대상 ---

@pytest.mark.parametrize("url", ["https://example.com", "https://testnet.binance.vision.example.com",
                                 "http://testnet.binance.vision", "https://api1.example.org/api"])
def test_only_testnet_or_loopback_endpoint(url: str) -> None:
    with pytest.raises(c.EndpointError):
        c.check_endpoint(url)


def test_testnet_and_loopback_allowed() -> None:
    assert c.check_endpoint(c.TESTNET_REST) == "https://testnet.binance.vision"
    assert c.check_endpoint("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


# --- 주문 전송 ---

def test_place_accepted_signed_once() -> None:
    env = Env([_reply(200, ACK, {"X-MBX-USED-WEIGHT-1M": "3", "X-MBX-ORDER-COUNT-10S": "1"})])
    r = env.client.place(_order(), P.INCREASE, **STEPS)
    assert r.outcome is O.ACCEPTED and r.order_id == 28 and r.ack is not None
    assert r.ack.fills[0].key == ("BTCUSDT", 9)
    (method, url, headers), = env.transport.sent
    assert method == "POST" and urlsplit(url).path == "/api/v3/order"
    q = dict(parse_qsl(urlsplit(url).query))
    assert q["timestamp"] == str(T0) and q["recvWindow"] == "5000" and len(q["signature"]) == 64
    assert q["newClientOrderId"] == _order().client_order_id and q["timeInForce"] == "IOC"
    assert headers["X-MBX-APIKEY"] == KEY
    assert env.budget.used_weight(60_000) == 3 and env.budget.order_count(10_000) == 1


def test_build_error_is_pre_send_failure_without_send() -> None:
    env = Env([])
    bad = o.NewOrder("BTCUSDT", o.OrderSide.BUY, o.OrderKind.LIMIT_IOC, Decimal("0.0011111"), Decimal("1"), "M2-x")
    r = env.client.place(bad, P.INCREASE, **STEPS)
    assert r.outcome is O.PRE_SEND_FAILURE and env.transport.sent == []


def test_connect_failure_is_pre_send_and_transport_loss_is_unknown() -> None:
    env = Env([c.PreSendError("연결 실패"), c.AfterSendError("읽기 타임아웃")])
    assert env.client.place(_order(), P.INCREASE, **STEPS).outcome is O.PRE_SEND_FAILURE
    assert env.client.place(_order(), P.INCREASE, **STEPS).outcome is O.UNKNOWN


def test_unparseable_body_is_unknown() -> None:
    env = Env([c.RawReply(200, {}, "not json")])
    assert env.client.place(_order(), P.INCREASE, **STEPS).outcome is O.UNKNOWN


def test_order_429_is_unknown_and_not_retried() -> None:
    env = Env([_reply(429, {"code": -1003, "msg": "Too many requests"}, {"Retry-After": "5"})])
    r = env.client.place(_order(), P.REDUCE, **STEPS)
    assert r.outcome is O.UNKNOWN and r.flag is Flag.RATE_LIMITED
    assert len(env.transport.sent) == 1 and env.slept == []
    # 쿨다운 중 다음 주문은 보내지 않는다 (전송 전 실패)
    r2 = env.client.place(_order(), P.REDUCE, **STEPS)
    assert r2.outcome is O.PRE_SEND_FAILURE and len(env.transport.sent) == 1


def test_order_418_unknown_and_all_calls_stop_with_k5_alert() -> None:
    env = Env([_reply(418, {"code": -1003, "msg": "IP banned"})])
    r = env.client.place(_order(), P.REDUCE, **STEPS)
    assert r.outcome is O.UNKNOWN and r.flag is Flag.BANNED
    assert env.budget.banned and any(k == "K5" for k, _ in env.alerts)
    with pytest.raises(c.BinanceQueryError) as e:
        env.client.query_order("BTCUSDT", order_id=28)
    assert e.value.kind is QueryKind.BANNED
    assert len(env.transport.sent) == 1          # 차단 후 아무것도 보내지 않음


def test_budget_shortage_holds_back_risk_increase_only() -> None:
    env = Env([_reply(200, ACK)])
    env.budget.observe_headers({"X-MBX-ORDER-COUNT-10S": "8"})       # 10초 예산 10 중 8 사용
    held = env.client.place(_order(), P.INCREASE, **STEPS)
    assert held.outcome is O.PRE_SEND_FAILURE and "예산" in (held.reason or "")
    assert env.client.place(_order(), P.REDUCE, **STEPS).outcome is O.ACCEPTED


def test_minus_1021_three_in_a_row_requests_k6_check() -> None:
    clock_err = {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
    env = Env([_reply(400, clock_err), _reply(400, clock_err), _reply(400, clock_err)])
    for _ in range(3):
        assert env.client.place(_order(), P.INCREASE, **STEPS).outcome is O.REJECTED
    assert [k for k, _ in env.alerts].count("K6") == 1


def test_minus_1021_streak_resets_on_other_reply() -> None:
    clock_err = {"code": -1021, "msg": "x"}
    env = Env([_reply(400, clock_err), _reply(400, clock_err), _reply(200, ACK), _reply(400, clock_err)])
    for _ in range(4):
        env.client.place(_order(), P.INCREASE, **STEPS)
    assert "K6" not in [k for k, _ in env.alerts]


def test_auth_error_on_order_is_unknown() -> None:
    env = Env([_reply(401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."})])
    r = env.client.place(_order(), P.INCREASE, **STEPS)
    assert r.outcome is O.UNKNOWN and r.flag is Flag.AUTH


def test_cancel_once_and_minus_2011_unknown() -> None:
    env = Env([_reply(400, {"code": -2011, "msg": "Unknown order sent."})])
    r = env.client.cancel("BTCUSDT", P.REDUCE, order_id=28)
    assert r.outcome is O.UNKNOWN and r.flag is Flag.CANCEL_UNCONFIRMED
    (method, url, _), = env.transport.sent
    assert method == "DELETE" and dict(parse_qsl(urlsplit(url).query))["orderId"] == "28"


# --- 조회 ---

def test_query_429_waits_retry_after_then_succeeds() -> None:
    env = Env([_reply(429, None, {"Retry-After": "2"}), _reply(200, {**ACK, "fills": []})])
    res = env.client.query_order("BTCUSDT", client_order_id="M2-x")
    assert res.kind is QueryKind.OK and res.order is not None and res.order.order_id == 28
    assert env.slept == [2.0] and len(env.transport.sent) == 2


def test_query_429_three_times_raises_for_k5() -> None:
    env = Env([_reply(429, None, {"Retry-After": "1"})] * 3)
    with pytest.raises(c.BinanceQueryError) as e:
        env.client.query_order("BTCUSDT", order_id=1)
    assert e.value.kind is QueryKind.RATE_LIMITED and len(env.transport.sent) == 3


def test_query_not_found_and_archived_are_results_not_errors() -> None:
    env = Env([_reply(400, {"code": -2013, "msg": "Order does not exist."}),
               _reply(400, {"code": -2026, "msg": "Order was canceled or expired ..."})])
    assert env.client.query_order("BTCUSDT", order_id=1).kind is QueryKind.NOT_FOUND
    assert env.client.query_order("BTCUSDT", order_id=1).kind is QueryKind.ARCHIVED


def test_query_auth_failure_raises_without_retry() -> None:
    env = Env([_reply(400, {"code": -1022, "msg": "Signature for this request is not valid."})])
    with pytest.raises(c.BinanceQueryError) as e:
        env.client.query_order("BTCUSDT", order_id=1)
    assert e.value.kind is QueryKind.AUTH and len(env.transport.sent) == 1


def test_query_transport_failure_raises_after_bounded_attempts() -> None:
    env = Env([c.AfterSendError("끊김")] * 3)
    with pytest.raises(c.BinanceQueryError) as e:
        env.client.query_order("BTCUSDT", order_id=1)
    assert e.value.kind is QueryKind.FAILED and len(env.transport.sent) == 3


def test_my_trades_and_all_orders() -> None:
    trade = {"symbol": "BTCUSDT", "id": 9, "orderId": 28, "orderListId": -1, "price": "60900.00",
             "qty": "0.00100000", "quoteQty": "60.90000000", "commission": "0.00000100", "commissionAsset": "BTC",
             "time": T0, "isBuyer": True, "isMaker": False, "isBestMatch": True}
    env = Env([_reply(200, [trade]), _reply(200, [{**ACK, "fills": []}])])
    fills = env.client.my_trades("BTCUSDT", start_ms=T0 - 1000, end_ms=T0 + 1000)
    assert fills[0].quote_confirmed and fills[0].quote_qty == Decimal("60.9")
    orders = env.client.all_orders("BTCUSDT", start_ms=T0 - 1000, end_ms=T0 + 1000)
    assert orders[0].order_id == 28


def test_server_time_offset_warning() -> None:
    env = Env([_reply(200, {"serverTime": T0 + 600}), _reply(200, {"serverTime": T0 + 100})])
    assert env.client.server_time_offset_ms() == 600
    assert any(k == "CLOCK" for k, _ in env.alerts)
    env.alerts.clear()
    assert env.client.server_time_offset_ms() == 100
    assert env.alerts == []


# --- 비밀값 비노출 (V-158) ---

def test_secrets_never_in_repr_reasons_alerts_or_errors() -> None:
    env = Env([_reply(500, None), _reply(418, None), _reply(200, ACK)])
    outs = [env.client.place(_order(), P.INCREASE, **STEPS), env.client.place(_order(), P.INCREASE, **STEPS)]
    try:
        env.client.query_order("BTCUSDT", order_id=1)
    except c.BinanceQueryError as exc:
        outs.append(exc)  # type: ignore[arg-type]
    text = _all_text(env, *outs, *(r.reason for r in outs if isinstance(r, c.OrderSubmit)))
    sig = dict(parse_qsl(urlsplit(env.transport.sent[0][1]).query))["signature"]
    for secret in (KEY, SECRET, sig):
        assert secret not in text
