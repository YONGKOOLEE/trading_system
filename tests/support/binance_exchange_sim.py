"""상태를 가진 합성 바이낸스 Spot 테스트넷 (CP-036 T-047·T-048 시험용, 네트워크 없음).

Transport 자리(send(method, url, headers) → RawReply)에 넣는 메모리 거래소다. 응답 형식은 공식 문서 예시 형식을 따르고
값은 모두 합성이다(가상 키·잔고·가격·수수료율). 실제 접속·실제 자격정보 없음.

- 주문: LIMIT IOC(매수·매도), MARKET 매도(수량 지정). 체결가 = 현재 가격(지정가 매수는 지정가 이하일 때만). IOC는 즉시 체결
  가능한 수량(liquidity)만 체결하고 나머지는 EXPIRED. 수수료 = 받는 자산(매수 = 기초 자산, 매도 = USDT) × 요율.
- `order_replies`: 다음 신규 주문의 합성 응답 — "429"·"418"·"5xx"(미접수)·"5xx+"(접수됨·응답만 5XX)·"-1013"·"-1021"·
  "-2010"(잔고 부족)·"-2010dup"·"timeout"(미접수)·"timeout+"(접수됨·응답 유실)·"pre"(연결 수립 전 실패).
- `hold_open`: 지정가 주문을 체결 없이 열린 채로 둔다(취소 시험용 합성 — 실제 IOC는 남지 않는다).
- `events`: 사용자 스트림 이벤트(executionReport·outboundAccountPosition) — T-048 가짜 WS 서버가 내보낸다.
- `sent`: 실제로 받은 요청 (메서드, 경로, 파라미터 dict). 서명·키 값은 남기지 않는다(파라미터에서 signature 제외).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

from tests.contract.test_binance_rules import _symbol
from trading_system.broker.binance.client import AfterSendError, PreSendError, RawReply

D = Decimal
RATE_LIMITS = [
    {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
    {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 100},
    {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 200000},
    {"rateLimitType": "RAW_REQUESTS", "interval": "MINUTE", "intervalNum": 5, "limit": 300000},
]
_OPEN = ("NEW", "PARTIALLY_FILLED")


def fmt(v: Decimal) -> str:
    return f"{v:.8f}"


def _reply(status: int, body: object, headers: dict[str, str] | None = None) -> RawReply:
    return RawReply(status, dict(headers or {}), json.dumps(body) if body is not None else "<html>")


def _error(status: int, code: int, msg: str) -> RawReply:
    return _reply(status, {"code": code, "msg": msg})


class ExchangeSim:
    __test__ = False

    def __init__(self, now_ms: Callable[[], int], *, symbols: Iterable[str] = ("BTCUSDT",),
                 balances: dict[str, Decimal] | None = None, price: Decimal = D("60000.00"),
                 fee_rate: Decimal = D("0.001"), exchange_body: dict | None = None) -> None:
        self.now_ms = now_ms
        self.symbols = tuple(symbols)
        self.price = {s: price for s in self.symbols}
        self.liquidity: dict[str, Decimal] = {}
        self.fee_rate = fee_rate
        self.free: dict[str, Decimal] = dict(balances if balances is not None else {"USDT": D("10000")})
        self.locked: dict[str, Decimal] = {}
        self.orders: dict[int, dict] = {}
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.order_replies: list[str] = []
        self.hold_open = False
        self.exchange_body = exchange_body
        self.server_offset_ms = 0
        self.market_down = False      # 시세 조회 5XX (T-048 공급 지연)
        self.bad_klines = False       # klines OHLC 불일치 (품질 실패 → K10)
        # 1m klines (CP-037 T-051): 지금 분(진행 중 봉)까지 limit개. 빠질 봉 시작(ms)·종가 가감·본문 통째 교체
        self.minute_skip: set[int] = set()
        self.minute_close_delta = D("0")
        self.minute_body: object | None = None
        self.sent: list[tuple[str, str, dict[str, str]]] = []
        self.headers: list[dict[str, str]] = []
        self._next_order = 1000
        self._next_trade = 5000
        self.pre: type[Exception] = PreSendError
        self.after: type[Exception] = AfterSendError

    # --- 전송 자리 ---------------------------------------------------------------------------------
    def factory(self) -> Callable[[type[Exception], type[Exception]], ExchangeSim]:
        def make(pre: type[Exception], after: type[Exception]) -> ExchangeSim:
            self.pre, self.after = pre, after
            return self
        return make

    def send(self, method: str, url: str, headers: dict[str, str]) -> RawReply:
        p = urlsplit(url)
        params = {k: v for k, v in parse_qsl(p.query) if k != "signature"}
        self.sent.append((method, p.path, params))
        self.headers.append(dict(headers))
        route = getattr(self, f"_{method.lower()}_{p.path.rsplit('/', 1)[-1]}", None)
        if route is None:
            raise AssertionError(f"합성 거래소에 없는 요청: {method} {p.path}")
        return route(params)

    def paths(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.sent]

    def order_posts(self) -> list[dict[str, str]]:
        return [prm for m, p, prm in self.sent if (m, p) == ("POST", "/api/v3/order")]

    # --- 조회 --------------------------------------------------------------------------------------
    def _get_time(self, _p: dict) -> RawReply:
        return _reply(200, {"serverTime": self.now_ms() + self.server_offset_ms})

    def _get_ping(self, _p: dict) -> RawReply:
        return _reply(200, {})

    def _get_exchangeInfo(self, p: dict) -> RawReply:
        if self.exchange_body is not None:
            return _reply(200, self.exchange_body)
        sym = p.get("symbol")
        syms = [_symbol(sym)] if sym in self.symbols else []
        return _reply(200, {"timezone": "UTC", "serverTime": self.now_ms(), "rateLimits": RATE_LIMITS,
                            "exchangeFilters": [], "symbols": syms})

    def _get_account(self, _p: dict) -> RawReply:
        assets = sorted(set(self.free) | set(self.locked))
        bal = [{"asset": a, "free": fmt(self.free.get(a, D(0))), "locked": fmt(self.locked.get(a, D(0)))}
               for a in assets if self.free.get(a, D(0)) or self.locked.get(a, D(0))]
        return _reply(200, {"canTrade": True, "accountType": "SPOT", "balances": bal, "permissions": ["SPOT"],
                            "uid": 123456789})

    def _get_openOrders(self, p: dict) -> RawReply:
        return _reply(200, [self._view(o) for o in self.orders.values()
                            if o["symbol"] == p.get("symbol") and o["status"] in _OPEN])

    def _get_allOrders(self, p: dict) -> RawReply:
        s, e = int(p["startTime"]), int(p["endTime"])
        return _reply(200, [self._view(o) for o in self.orders.values()
                            if o["symbol"] == p.get("symbol") and s <= o["time"] <= e])

    def _get_myTrades(self, p: dict) -> RawReply:
        s, e = int(p["startTime"]), int(p["endTime"])
        return _reply(200, [{k: v for k, v in t.items() if not k.startswith("_")} for t in self.trades
                            if t["symbol"] == p.get("symbol") and s <= t["time"] <= e])

    def _get_order(self, p: dict) -> RawReply:
        o = self._find(p)
        if o is None:
            return _error(400, -2013, "Order does not exist.")
        return _reply(200, self._view(o))

    def _find(self, p: dict) -> dict | None:
        if "orderId" in p:
            o = self.orders.get(int(p["orderId"]))
            return o if o is not None and o["symbol"] == p.get("symbol") else None
        cid = p.get("origClientOrderId")
        return next((o for o in self.orders.values() if o["clientOrderId"] == cid and o["symbol"] == p.get("symbol")),
                    None)

    # --- 시세 (T-048 공급 스레드) ---------------------------------------------------------------------
    def _get_price(self, p: dict) -> RawReply:      # /api/v3/ticker/price
        if self.market_down:
            return _reply(503, None)
        sym = p.get("symbol")
        return _reply(200, {"symbol": sym, "price": fmt(self.price[sym])})

    def _get_avgPrice(self, p: dict) -> RawReply:
        if self.market_down:
            return _reply(503, None)
        return _reply(200, {"mins": 5, "price": fmt(self.price[p["symbol"]]), "closeTime": self.now_ms()})

    def _get_klines(self, p: dict) -> RawReply:
        """1d 봉: 오늘(진행 중) 포함 limit개. bad_klines면 OHLC 불일치 행(품질 실패). 1m은 _minute_klines."""
        if p.get("interval") == "1m":
            return self._minute_klines(p)
        day = 86_400_000
        today = self.now_ms() // day * day
        n = int(p.get("limit", "500"))
        rows = []
        for i in range(n - 1, -1, -1):
            o = today - i * day
            px = fmt(self.price[p["symbol"]])
            high = fmt(self.price[p["symbol"]] + 100) if not self.bad_klines else fmt(D("1"))
            rows.append([o, px, high, fmt(self.price[p["symbol"]] - 100), px, "10.0", o + day - 1, "1000", 10,
                         "5.0", "500", "0"])
        return _reply(200, rows)

    def _minute_klines(self, p: dict) -> RawReply:
        """1m 봉: 지금 분(진행 중) 포함 limit개(BN §8 행 형식), minute_skip 봉은 뺀다. minute_body면 그 본문 그대로."""
        if self.market_down:
            return _reply(503, None)
        if self.minute_body is not None:
            return _reply(200, self.minute_body)
        step = 60_000
        cur = self.now_ms() // step * step
        n = int(p.get("limit", "500"))
        px = self.price[p["symbol"]]
        rows = []
        if "startTime" in p:   # 나눔 조회 (CP-040 T-066): startTime부터 endTime(·지금 분)까지 최대 limit개
            first = -(-int(p["startTime"]) // step) * step
            stop = min(int(p.get("endTime", cur)), cur)
            opens = [o for o in range(first, stop + 1, step)][:n]
        else:
            opens = [cur - i * step for i in range(n - 1, -1, -1)]
        for o in opens:
            if o in self.minute_skip:
                continue
            c = px + self.minute_close_delta
            rows.append([o, fmt(px), fmt(max(px, c) + 10), fmt(min(px, c) - 10), fmt(c), "1.5", o + step - 1, "90000",
                         7, "0.7", "42000", "0"])
        return _reply(200, rows)

    # --- 주문 변경 ---------------------------------------------------------------------------------
    def _post_order(self, p: dict) -> RawReply:
        spec = self.order_replies.pop(0) if self.order_replies else None
        if spec == "pre":
            self.sent.pop()   # 연결 수립 전 실패 — 서버에 닿지 않았다
            raise self.pre("합성: 연결 수립 실패")
        if spec == "timeout":
            raise self.after("합성: 읽기 타임아웃")
        fixed = {"429": _error(429, -1003, "Too many requests; current limit is 6000 request weight per 1 MINUTE."),
                 "418": _error(418, -1003, "Way too many requests; IP banned."),
                 "5xx": _reply(503, None),
                 "-1013": _error(400, -1013, "Filter failure: NOTIONAL"),
                 "-1021": _error(400, -1021, "Timestamp for this request is outside of the recvWindow."),
                 "-2010": _error(400, -2010, "Account has insufficient balance for requested action."),
                 "-2010dup": _error(400, -2010, "Duplicate order sent.")}
        if spec in fixed:
            return fixed[spec]
        cid = p["newClientOrderId"]
        if any(o["clientOrderId"] == cid for o in self.orders.values()):
            return _error(400, -2010, "Duplicate order sent.")
        body = self._execute(p)
        if isinstance(body, RawReply):
            return body
        if spec == "5xx+":
            return _reply(503, None)
        if spec == "timeout+":
            raise self.after("합성: 접수 뒤 응답 유실")
        return _reply(200, body)

    def _execute(self, p: dict) -> dict | RawReply:
        sym, side, otype = p["symbol"], p["side"], p["type"]
        qty = D(p["quantity"])
        px_mkt = self.price[sym]
        base, quote = sym[:-4], sym[-4:]
        limit = D(p["price"]) if otype == "LIMIT" else None
        if side == "BUY" and self.free.get(quote, D(0)) < qty * (limit or px_mkt):
            return _error(400, -2010, "Account has insufficient balance for requested action.")
        if side == "SELL" and self.free.get(base, D(0)) < qty:
            return _error(400, -2010, "Account has insufficient balance for requested action.")
        self._next_order += 1
        oid = self._next_order
        now = self.now_ms()
        o = {"symbol": sym, "orderId": oid, "orderListId": -1, "clientOrderId": p["newClientOrderId"],
             "price": fmt(limit or D(0)), "origQty": fmt(qty), "executedQty": fmt(D(0)),
             "cummulativeQuoteQty": fmt(D(0)), "status": "NEW", "timeInForce": p.get("timeInForce", "GTC"),
             "type": otype, "side": side, "time": now, "updateTime": now, "isWorking": True,
             "_exec": D(0), "_quote": D(0)}
        self.orders[oid] = o
        self._report(o, "NEW", None)
        fills = []
        if otype == "LIMIT" and self.hold_open:
            asset, amount = (quote, qty * limit) if side == "BUY" else (base, qty)   # type: ignore[operator]
            self.free[asset] -= amount
            self.locked[asset] = self.locked.get(asset, D(0)) + amount
        else:
            can = otype == "MARKET" or (side == "BUY" and limit >= px_mkt) or (side == "SELL" and limit <= px_mkt)  # type: ignore[operator]
            avail = self.liquidity.get(sym, qty) if can else D(0)
            fq = min(qty, max(avail, D(0)))
            if fq > 0:
                fills.append(self._trade(o, fq, px_mkt))
            o["status"] = "FILLED" if o["_exec"] == qty else "EXPIRED"
            if o["status"] == "EXPIRED":
                self._report(o, "EXPIRED", None)
        view = self._view(o)
        view.update(transactTime=now, workingTime=now, selfTradePreventionMode="EXPIRE_MAKER",
                    fills=[{"price": fmt(t["_px"]), "qty": fmt(t["_qty"]), "commission": t["commission"],
                            "commissionAsset": t["commissionAsset"], "tradeId": t["id"]} for t in fills])
        return view

    def _trade(self, o: dict, qty: Decimal, px: Decimal) -> dict:
        base, quote = o["symbol"][:-4], o["symbol"][-4:]
        amount = qty * px
        if o["side"] == "BUY":
            fee, fee_asset = qty * self.fee_rate, base
            self.free[quote] = self.free.get(quote, D(0)) - amount
            self.free[base] = self.free.get(base, D(0)) + qty - fee
        else:
            fee, fee_asset = amount * self.fee_rate, quote
            self.free[base] = self.free.get(base, D(0)) - qty
            self.free[quote] = self.free.get(quote, D(0)) + amount - fee
        self._next_trade += 1
        o["_exec"] += qty
        o["_quote"] += amount
        o["status"] = "FILLED" if o["_exec"] == D(o["origQty"]) else "PARTIALLY_FILLED"
        t = {"symbol": o["symbol"], "id": self._next_trade, "orderId": o["orderId"], "orderListId": -1,
             "price": fmt(px), "qty": fmt(qty), "quoteQty": fmt(amount), "commission": fmt(fee),
             "commissionAsset": fee_asset, "time": self.now_ms(), "isBuyer": o["side"] == "BUY", "isMaker": False,
             "isBestMatch": True, "_px": px, "_qty": qty}
        self.trades.append(t)
        self._report(o, "TRADE", t)
        return t

    def fill_open(self, order_id: int, qty: Decimal | None = None) -> None:
        """hold_open으로 열린 주문을 나중에 체결(합성 — 스트림 체결 통보 시험용)."""
        o = self.orders[order_id]
        left = D(o["origQty"]) - o["_exec"]
        q = left if qty is None else qty
        base, quote = o["symbol"][:-4], o["symbol"][-4:]
        asset, amount = (quote, q * D(o["price"])) if o["side"] == "BUY" else (base, q)
        self.locked[asset] -= amount
        self.free[asset] = self.free.get(asset, D(0)) + amount
        self._trade(o, q, D(o["price"]))
        o["status"] = "FILLED" if o["_exec"] == D(o["origQty"]) else "PARTIALLY_FILLED"

    def _delete_order(self, p: dict) -> RawReply:
        o = self._find(p)
        if o is None or o["status"] not in _OPEN:
            return _error(400, -2011, "Unknown order sent.")
        left = D(o["origQty"]) - o["_exec"]
        base, quote = o["symbol"][:-4], o["symbol"][-4:]
        asset, amount = (quote, left * D(o["price"])) if o["side"] == "BUY" else (base, left)
        self.locked[asset] -= amount
        self.free[asset] = self.free.get(asset, D(0)) + amount
        o["status"] = "CANCELED"
        self._report(o, "CANCELED", None)
        view = self._view(o)
        view.update(origClientOrderId=o["clientOrderId"], clientOrderId="cancel-" + str(o["orderId"]),
                    transactTime=self.now_ms())
        return _reply(200, view)

    # --- 표현 --------------------------------------------------------------------------------------
    def _view(self, o: dict) -> dict:
        v = {k: val for k, val in o.items() if not k.startswith("_")}
        v.update(executedQty=fmt(o["_exec"]), cummulativeQuoteQty=fmt(o["_quote"]))
        return v

    def _report(self, o: dict, x: str, t: dict | None) -> None:
        now = self.now_ms()
        ev = {"e": "executionReport", "E": now, "s": o["symbol"], "c": o["clientOrderId"], "S": o["side"],
              "o": o["type"], "f": o["timeInForce"], "q": o["origQty"], "p": o["price"], "P": "0.00000000",
              "F": "0.00000000", "g": -1, "C": o["clientOrderId"] if x == "CANCELED" else "", "x": x,
              "X": o["status"], "r": "NONE", "i": o["orderId"], "l": fmt(t["_qty"]) if t else fmt(D(0)),
              "z": fmt(o["_exec"]), "L": fmt(t["_px"]) if t else fmt(D(0)), "n": t["commission"] if t else "0",
              "N": t["commissionAsset"] if t else None, "T": now, "t": t["id"] if t else -1, "w": False,
              "m": False, "M": False, "O": o["time"], "Z": fmt(o["_quote"]),
              "Y": t["quoteQty"] if t else fmt(D(0)), "Q": "0.00000000"}
        if x == "CANCELED":
            ev["c"] = "cancel-" + str(o["orderId"])
        self.events.append({"subscriptionId": 0, "event": ev})
