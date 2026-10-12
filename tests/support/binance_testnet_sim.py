"""합성 바이낸스 Spot 테스트넷 (T-M2-14 시험용, 네트워크 없음).

(메서드, 경로) → 응답 목록. 목록이 하나 남으면 그 응답을 반복한다. 등록 안 된 경로는 시험 실패로 본다(보내지면 안 되는 요청).
응답 형식은 공식 문서 예시 형식(값은 합성). 키·시크릿·계좌 식별자(uid)는 가상 값이다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from tests.contract.test_binance_rules import _symbol
from trading_system.broker.binance.client import AfterSendError, PreSendError, RawReply

T0 = 1_760_000_040_000
KEY = "synthetic-testnet-api-key-7f3a9c"
SECRET = "synthetic-testnet-secret-0b5e2d"
UID = 354937868   # 가상 계좌 식별자 — 결과 파일·출력에 나오면 안 된다

RATE_LIMITS = [
    {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
    {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 100},
    {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 200000},
    {"rateLimitType": "RAW_REQUESTS", "interval": "MINUTE", "intervalNum": 5, "limit": 300000},
]


def exchange_info(*symbols: dict) -> dict:
    return {"timezone": "UTC", "serverTime": T0, "rateLimits": RATE_LIMITS, "exchangeFilters": [],
            "symbols": list(symbols) if symbols else [_symbol()]}


ACCOUNT = {
    "makerCommission": 0, "takerCommission": 0, "buyerCommission": 0, "sellerCommission": 0,
    "commissionRates": {"maker": "0.00000000", "taker": "0.00000000", "buyer": "0.00000000", "seller": "0.00000000"},
    "canTrade": True, "canWithdraw": False, "canDeposit": False, "brokered": False,
    "requireSelfTradePrevention": False, "preventSor": False, "updateTime": T0, "accountType": "SPOT",
    "balances": [{"asset": "BTC", "free": "1.00000000", "locked": "0.00000000"},
                 {"asset": "ETH", "free": "1.00000000", "locked": "0.00000000"},
                 {"asset": "USDT", "free": "10000.00000000", "locked": "0.00000000"}],
    "permissions": ["SPOT"], "uid": UID,
}

OPEN_ORDER = {"symbol": "BTCUSDT", "orderId": 77, "orderListId": -1, "clientOrderId": "ext-1", "price": "50000.00",
              "origQty": "0.00100000", "executedQty": "0.00000000", "cummulativeQuoteQty": "0.00000000",
              "status": "NEW", "timeInForce": "GTC", "type": "LIMIT", "side": "BUY", "time": T0, "updateTime": T0,
              "isWorking": True}

Reply = RawReply | tuple[str, str]   # ("PRE"|"AFTER", 문구) = 전송 오류


def reply(status: int, body: object, headers: Mapping[str, str] | None = None) -> RawReply:
    return RawReply(status, dict(headers or {}), json.dumps(body) if body is not None else "<html>")


def error(status: int, code: int, msg: str, headers: Mapping[str, str] | None = None) -> RawReply:
    return reply(status, {"code": code, "msg": msg}, headers)


def default_routes() -> dict[tuple[str, str], list[Reply]]:
    return {
        ("GET", "/api/v3/time"): [reply(200, {"serverTime": T0})],
        ("GET", "/api/v3/ping"): [reply(200, {})],
        ("GET", "/api/v3/exchangeInfo"): [reply(200, exchange_info())],
        ("GET", "/api/v3/account"): [reply(200, ACCOUNT)],
        ("GET", "/api/v3/openOrders"): [reply(200, [])],
        ("GET", "/api/v3/allOrders"): [reply(200, [])],
        ("GET", "/api/v3/myTrades"): [reply(200, [])],
    }


class TestnetSim:
    """합성 전송. sent = 실제로 '보낸' 요청(메서드, URL, 헤더). 가드가 막은 요청은 여기에 오지 않는다."""

    __test__ = False

    def __init__(self, routes: Mapping[tuple[str, str], list[Reply]] | None = None) -> None:
        self.routes = default_routes()
        self.routes.update({k: list(v) for k, v in (routes or {}).items()})
        self.sent: list[tuple[str, str, dict[str, str]]] = []
        self.pre: type[Exception] = PreSendError
        self.after: type[Exception] = AfterSendError
        self.factory_calls = 0

    def factory(self) -> Callable[[type[Exception], type[Exception]], TestnetSim]:
        """app.m2_preflight의 make_transport 자리. 호출 수로 '네트워크 객체 생성 여부'를 본다."""
        def make(pre: type[Exception], after: type[Exception]) -> TestnetSim:
            self.factory_calls += 1
            self.pre, self.after = pre, after
            return self
        return make

    def send(self, method: str, url: str, headers: Mapping[str, str]) -> RawReply:
        self.sent.append((method, url, dict(headers)))
        path = urlsplit(url).path
        queue = self.routes.get((method, path))
        assert queue, f"합성 테스트넷에 없는 요청: {method} {path}"
        r = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(r, tuple):
            raise (self.pre if r[0] == "PRE" else self.after)(r[1])
        return r

    def paths(self) -> list[tuple[str, str]]:
        return [(m, urlsplit(u).path) for m, u, _ in self.sent]

    def params(self, i: int) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.sent[i][1]).query))
