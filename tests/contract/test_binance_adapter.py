"""바이낸스 Spot 테스트넷 브로커 어댑터 계약 (CP-036 D-203·D-204, T-047). V-284, V-285 (b)(d)·전송 계층 차단.

네트워크 없음: 합성 거래소(tests/support/binance_exchange_sim)가 공식 문서 형식의 응답을 돌려준다. 키·시크릿은 가상 값.
주문 변경 요청은 한 번만 보내고, 429·5XX·타임아웃·418도 재전송하지 않는다(CLAUDE.md §8-7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.contract.test_binance_rules import _symbol
from tests.support.binance_exchange_sim import RATE_LIMITS, ExchangeSim
from trading_system.broker.base import BrokerQueryError, LookupKind
from trading_system.broker.binance.adapter import CANCEL_SUFFIX, BinanceSpotAdapter
from trading_system.broker.binance.client import TESTNET_REST, BinanceSpotClient, PreSendError
from trading_system.broker.binance.facts import to_domain_rules
from trading_system.broker.binance.orders import client_order_id as bn_client_order_id
from trading_system.broker.binance.rules import parse_exchange_info
from trading_system.broker.binance.run_guard import RunGuard
from trading_system.broker.binance.signing import Credentials
from trading_system.broker.binance.stream import parse_event
from trading_system.broker.binance.weights import WeightBudget, parse_rate_limits
from trading_system.domain.enums import OrderType, SubmitOutcome as O
from trading_system.domain.models import OrderRequest, client_order_id_of

D = Decimal
KEY = "synthetic-adapter-api-key-5c1e"
SECRET = "synthetic-adapter-secret-9d7b"
ACCT = "BN-PAPER-01"
SYM = "BTCUSDT"
T0 = 1_760_000_040_000


class _Secret:
    def __init__(self, v: str) -> None:
        self._v = v

    def reveal(self) -> str:
        return self._v


def _units():  # type: ignore[no-untyped-def]
    ex = parse_exchange_info({"timezone": "UTC", "serverTime": T0, "rateLimits": RATE_LIMITS, "exchangeFilters": [],
                              "symbols": [_symbol()]})
    return {SYM: to_domain_rules(ex.symbols[SYM])}


class Rig:
    def __init__(self, *, orders: bool = True, **sim_kw: object) -> None:
        self.t = T0
        self.sim = ExchangeSim(lambda: self.t, **sim_kw)  # type: ignore[arg-type]
        self.alerts: list[tuple[str, str]] = []
        self.filters: list[str] = []
        self.guard = RunGuard(self.sim, orders_enabled=orders)
        self.budget = WeightBudget(parse_rate_limits(RATE_LIMITS), now_ms=lambda: self.t)
        self.units = _units()
        self.client = BinanceSpotClient(TESTNET_REST, Credentials(_Secret(KEY), _Secret(SECRET)), self.guard,
                                        self.budget, now_ms=lambda: self.t, sleep=self._sleep,
                                        on_alert=self._alert)
        self.adapter = self.make_adapter(orders)

    def make_adapter(self, orders: bool) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(self.client, [SYM], account=ACCT, now_ms=lambda: self.t, units=self.units.get,
                                  orders_enabled=orders, on_alert=self._alert, on_filter_reject=self.filters.append,
                                  budget=self.budget)

    def _alert(self, kind: str, msg: str) -> None:
        self.alerts.append((kind, msg))

    def _sleep(self, s: float) -> None:
        self.t += int(s * 1000)

    def posts(self) -> list[dict[str, str]]:
        return self.sim.order_posts()


def buy(qty: str = "0.00100", price: str = "60300.00", idem: str = "intent-1") -> OrderRequest:
    return OrderRequest("NEW", ACCT, SYM, "BUY", D(qty), OrderType.LIMIT, D(price),
                        client_order_id=client_order_id_of(idem))


def sell(qty: str, *, limit: str | None = None, idem: str = "intent-s") -> OrderRequest:
    return OrderRequest("NEW", ACCT, SYM, "SELL", D(qty), OrderType.LIMIT if limit else OrderType.MARKET,
                        D(limit) if limit else D("0"), client_order_id=client_order_id_of(idem))


@pytest.fixture
def rig() -> Rig:
    return Rig()


# --- V-284 신규 주문 4분류·이벤트 ---------------------------------------------------------------------

def test_full_fill_ioc_buy_sent_once_with_full_response_events(rig: Rig) -> None:
    r = rig.adapter.place_order(buy())
    assert r.outcome is O.ACCEPTED and r.order_no is not None and r.order_no.isdigit()
    [p] = rig.posts()
    assert (p["type"], p["timeInForce"], p["side"], p["quantity"], p["price"], p["newOrderRespType"]) == \
        ("LIMIT", "IOC", "BUY", "0.00100", "60300.00", "FULL")
    assert p["newClientOrderId"] == client_order_id_of("intent-1") == bn_client_order_id("intent-1")
    [ev] = rig.adapter.poll_events()
    assert (ev.kind, ev.order_no, ev.quantity, ev.price, ev.commission, ev.commission_asset) == \
        ("FILL", r.order_no, D("0.001"), D("60000"), D("0.000001"), "BTC")
    assert (ev.instrument, ev.account, ev.quote_qty, ev.quote_confirmed) == (SYM, ACCT, D("60.000"), False)
    assert rig.adapter.poll_events() == []


def test_ioc_partial_fill_then_expired(rig: Rig) -> None:
    rig.sim.liquidity[SYM] = D("0.0004")
    r = rig.adapter.place_order(buy())
    evs = rig.adapter.poll_events()
    assert r.outcome is O.ACCEPTED and [e.kind for e in evs] == ["FILL", "EXPIRED"]
    assert evs[0].quantity == D("0.0004") and evs[1].order_no == r.order_no and "IOC" in (evs[1].reason or "")


def test_ioc_zero_fill_expired_releases(rig: Rig) -> None:
    rig.sim.liquidity[SYM] = D("0")
    r = rig.adapter.place_order(buy())
    assert r.outcome is O.ACCEPTED
    assert [(e.kind, e.order_no, e.instrument) for e in rig.adapter.poll_events()] == [("EXPIRED", r.order_no, SYM)]


def test_minus_1013_explicit_reject_requests_rule_reload(rig: Rig) -> None:
    rig.sim.order_replies = ["-1013"]
    r = rig.adapter.place_order(buy())
    assert (r.outcome, r.code) == (O.REJECTED, -1013) and len(rig.posts()) == 1
    assert rig.filters == [SYM] and any(k == "FILTER" for k, _ in rig.alerts)


@pytest.mark.parametrize("spec,outcome", [("-2010", O.REJECTED), ("-2010dup", O.UNKNOWN), ("5xx", O.UNKNOWN),
                                          ("5xx+", O.UNKNOWN), ("timeout", O.UNKNOWN), ("timeout+", O.UNKNOWN),
                                          ("pre", O.PRE_SEND_FAILURE)])
def test_four_way_outcomes_and_single_send(rig: Rig, spec: str, outcome: O) -> None:
    rig.sim.order_replies = [spec]
    r = rig.adapter.place_order(buy())
    assert r.outcome is outcome
    assert len(rig.posts()) == (0 if spec == "pre" else 1)    # 재전송 0 ("pre"는 연결 수립 전 실패 — 서버 미도달)
    assert rig.adapter.poll_events() == []


def test_429_unknown_no_retry_and_next_order_held_before_send(rig: Rig) -> None:
    rig.sim.order_replies = ["429"]
    assert rig.adapter.place_order(buy()).outcome is O.UNKNOWN
    second = rig.adapter.place_order(buy(idem="intent-2"))
    assert second.outcome is O.PRE_SEND_FAILURE and "429" in (second.reason or "")
    assert len(rig.posts()) == 1


def test_418_unknown_k5_alert_then_everything_blocked_until_human_resume(rig: Rig) -> None:
    rig.sim.order_replies = ["418"]
    assert rig.adapter.place_order(buy()).outcome is O.UNKNOWN
    assert any(k == "K5" for k, _ in rig.alerts)
    n = len(rig.sim.sent)
    assert rig.adapter.place_order(buy(idem="intent-2")).outcome is O.PRE_SEND_FAILURE
    with pytest.raises(BrokerQueryError):
        rig.adapter.query_balances(ACCT)
    assert len(rig.sim.sent) == n                                   # 아무것도 보내지 않음
    rig.adapter.resume_after_ban()                                   # 사람 재개(confirm_ip_ban_cleared가 부름)
    assert rig.adapter.query_balances(ACCT)["USDT"] == (D("10000"), D("0"))


def test_minus_1021_three_in_a_row_k6(rig: Rig) -> None:
    rig.sim.order_replies = ["-1021"] * 3
    for i in range(3):
        assert rig.adapter.place_order(buy(idem=f"i-{i}")).outcome is O.REJECTED
    kinds = [k for k, _ in rig.alerts]
    assert kinds.count("CLOCK") == 1 and kinds.count("K6") == 1 and len(rig.posts()) == 3


def test_modify_refused_before_send(rig: Rig) -> None:
    req = OrderRequest("MODIFY", ACCT, SYM, "BUY", D("0.001"), OrderType.LIMIT, D("60000"), target_order_no="1001")
    r = rig.adapter.modify_order(req)
    assert r.outcome is O.PRE_SEND_FAILURE and "정정" in (r.reason or "") and rig.sim.sent == []


@pytest.mark.parametrize("req,needle", [
    (OrderRequest("NEW", ACCT, SYM, "BUY", D("0.001"), OrderType.MARKET, D("0"),
                  client_order_id=client_order_id_of("m")), "시장가 매수"),
    (buy(qty="0.000015"), "quantity"),
    (OrderRequest("NEW", ACCT, SYM, "BUY", D("0.001"), OrderType.LIMIT, D("60000")), "고객 주문 ID"),
    (OrderRequest("NEW", ACCT, "ETHUSDT", "BUY", D("0.001"), OrderType.LIMIT, D("3000"),
                  client_order_id=client_order_id_of("e")), "유니버스"),
])
def test_pre_send_refusals_send_nothing(rig: Rig, req: OrderRequest, needle: str) -> None:
    r = rig.adapter.place_order(req)
    assert r.outcome is O.PRE_SEND_FAILURE and needle in (r.reason or "") and rig.sim.sent == []


def test_unknown_units_pre_send(rig: Rig) -> None:
    rig.units.clear()
    r = rig.adapter.place_order(buy())
    assert r.outcome is O.PRE_SEND_FAILURE and "단위" in (r.reason or "") and rig.sim.sent == []


def test_market_sell_uses_lot_step_when_market_step_zero_no_price(rig: Rig) -> None:
    rig.sim.free["BTC"] = D("0.00015984")
    r = rig.adapter.place_order(sell("0.00015"))
    [p] = rig.posts()
    assert r.outcome is O.ACCEPTED and (p["type"], p["quantity"]) == ("MARKET", "0.00015") and "price" not in p
    assert "timeInForce" not in p


def test_stale_quote_ioc_limit_sell(rig: Rig) -> None:
    rig.sim.free["BTC"] = D("0.001")
    rig.adapter.place_order(sell("0.00100", limit="59700.00"))
    [p] = rig.posts()
    assert (p["type"], p["timeInForce"], p["price"]) == ("LIMIT", "IOC", "59700.00")


def test_client_order_id_same_param_after_restart(rig: Rig) -> None:
    rig.sim.order_replies = ["5xx"]
    rig.adapter.place_order(buy(idem="same-intent"))
    rig.make_adapter(True).place_order(buy(idem="same-intent"))
    a, b = rig.posts()
    assert a["newClientOrderId"] == b["newClientOrderId"] == client_order_id_of("same-intent")


# --- FULL 응답 체결 ↔ 사용자 스트림 체결 중복 제거 (D-203, FillLedger) -------------------------------------

def test_full_fill_and_stream_fill_same_trade_reflected_once_then_confirmed(rig: Rig) -> None:
    rig.adapter.place_order(buy())
    first = rig.adapter.poll_events()
    for raw in rig.sim.events:
        rig.adapter.ingest_stream(parse_event(raw))
    evs = rig.adapter.poll_events()
    fills = [e for e in evs if e.kind == "FILL"]
    assert len(first) == 1 and len(fills) == 1                        # 같은 체결 ID → 확정 통보 한 번 (임시 → 확정)
    assert (fills[0].fill_id, fills[0].quote_confirmed, fills[0].quote_qty) == (first[0].fill_id, True, D("60.0"))
    for raw in rig.sim.events:                                        # 같은 통보 반복 → 아무것도 없음
        rig.adapter.ingest_stream(parse_event(raw))
    assert [e for e in rig.adapter.poll_events() if e.kind == "FILL"] == []


def test_trade_prevention_is_not_a_fill(rig: Rig) -> None:
    ev = {"e": "executionReport", "E": T0, "s": SYM, "c": "x", "S": "BUY", "o": "LIMIT", "f": "IOC", "q": "0.001",
          "p": "60000", "C": "", "x": "TRADE_PREVENTION", "X": "EXPIRED_IN_MATCH", "r": "NONE", "i": 1001,
          "l": "0", "z": "0", "L": "0", "n": "0", "N": None, "T": T0, "t": -1, "Z": "0", "Y": "0"}
    rig.adapter.ingest_stream(parse_event(ev))
    assert [(e.kind, e.order_no) for e in rig.adapter.poll_events()] == [("EXPIRED", "1001")]


# --- 취소 ---------------------------------------------------------------------------------------------

def test_cancel_sent_once_cancelled_event_for_target(rig: Rig) -> None:
    rig.sim.hold_open = True
    placed = rig.adapter.place_order(buy())
    req = OrderRequest("CANCEL", ACCT, SYM, "BUY", D("0"), OrderType.LIMIT, D("0"), target_order_no=placed.order_no)
    r = rig.adapter.cancel_order(req)
    assert r.outcome is O.ACCEPTED and r.order_no == f"{placed.order_no}{CANCEL_SUFFIX}"
    assert [m for m, p, _ in rig.sim.sent if p == "/api/v3/order"].count("DELETE") == 1
    evs = rig.adapter.poll_events()
    assert ("CANCELLED", placed.order_no) in [(e.kind, e.order_no) for e in evs]


def test_cancel_minus_2011_unknown_no_retry(rig: Rig) -> None:
    req = OrderRequest("CANCEL", ACCT, SYM, "BUY", D("0"), OrderType.LIMIT, D("0"), target_order_no="999")
    assert rig.adapter.cancel_order(req).outcome is O.UNKNOWN
    assert [m for m, p, _ in rig.sim.sent if p == "/api/v3/order"] == ["DELETE"]


# --- 조회 (SpotOrderQueries·BrokerAdapter) -----------------------------------------------------------------

def test_lookup_by_client_id_and_order_no_and_not_found(rig: Rig) -> None:
    r = rig.adapter.place_order(buy())
    cid = client_order_id_of("intent-1")
    lk = rig.adapter.lookup_order(ACCT, SYM, order_no=None, client_order_id=cid)
    assert lk.kind is LookupKind.FOUND and lk.record is not None
    assert (lk.record.order_no, lk.record.client_order_id, lk.record.state) == (r.order_no, cid, "FILLED")
    assert rig.adapter.lookup_order(ACCT, SYM, order_no=r.order_no, client_order_id=None).kind is LookupKind.FOUND
    assert rig.adapter.lookup_order(ACCT, SYM, order_no=None, client_order_id="M2-none").kind is LookupKind.NOT_FOUND


def test_windows_and_balances_positions_cash(rig: Rig) -> None:
    r = rig.adapter.place_order(buy())
    start = datetime.fromtimestamp(T0 / 1000, UTC) - timedelta(minutes=1)
    end = start + timedelta(minutes=2)
    [o] = rig.adapter.orders_between(ACCT, SYM, start, end)
    [t] = rig.adapter.trades_between(ACCT, SYM, start, end)
    assert o.order_no == t.order_no == r.order_no and t.quote_qty == D("60") and t.commission_asset == "BTC"
    bal = rig.adapter.query_balances(ACCT)
    assert bal["BTC"] == (D("0.000999"), D("0"))
    [pos] = rig.adapter.query_positions(ACCT)
    assert (pos.instrument, pos.quantity) == (SYM, D("0.000999"))
    cash = rig.adapter.query_cash(ACCT)
    assert cash.broker_orderable == D("9940") == cash.deposit
    with pytest.raises(BrokerQueryError):
        rig.adapter.orders_between(ACCT, SYM, start, start + timedelta(hours=25))   # 24시간 창 밖


def test_secrets_never_in_reasons_alerts_or_repr(rig: Rig) -> None:
    rig.sim.order_replies = ["-2010", "5xx", "418"]
    reasons = [rig.adapter.place_order(buy(idem=f"s-{i}")).reason or "" for i in range(4)]
    text = " ".join(reasons) + repr(rig.adapter) + repr(rig.guard) + " ".join(m for _, m in rig.alerts)
    assert KEY not in text and SECRET not in text and "signature" not in text


# --- V-285 주문 전송 스위치 (D-204) ----------------------------------------------------------------------

def test_v285b_switch_off_order_blocked_before_send_with_alert() -> None:
    rig = Rig(orders=False)
    r = rig.adapter.place_order(buy())
    assert r.outcome is O.PRE_SEND_FAILURE and "D-204" in (r.reason or "")
    assert rig.sim.sent == [] and any(k == "ORDER_BLOCKED" for k, _ in rig.alerts)
    req = OrderRequest("CANCEL", ACCT, SYM, "BUY", D("0"), OrderType.LIMIT, D("0"), target_order_no="1001")
    assert rig.adapter.cancel_order(req).outcome is O.PRE_SEND_FAILURE and rig.sim.sent == []
    assert rig.adapter.query_balances(ACCT)["USDT"][0] == D("10000")    # 조회는 허용


def test_v285b_transport_guard_blocks_order_family_even_if_adapter_bypassed() -> None:
    rig = Rig(orders=False)
    url = f"{TESTNET_REST}/api/v3/order?symbol=BTCUSDT"
    for method in ("POST", "DELETE"):
        with pytest.raises(PreSendError):
            rig.guard.send(method, url, {})
    assert rig.sim.sent == [] and rig.guard.order_sent == 0 and len(rig.guard.blocked) == 2
    rig.guard.send("GET", f"{TESTNET_REST}/api/v3/order?symbol=BTCUSDT&orderId=1", {})   # 단건 조회는 허용
    assert rig.sim.paths() == [("GET", "/api/v3/order")]


@pytest.mark.parametrize("method,path", [("POST", "/api/v3/order/test"), ("POST", "/api/v3/order/oco"),
                                         ("POST", "/api/v3/order/cancelReplace"), ("DELETE", "/api/v3/openOrders"),
                                         ("POST", "/api/v3/sor/order"), ("POST", "/api/v3/userDataStream"),
                                         ("GET", "/sapi/v1/capital/config/getall")])
def test_guard_blocks_other_order_family_and_unknown_paths_even_when_enabled(method: str, path: str) -> None:
    rig = Rig(orders=True)
    with pytest.raises(PreSendError):
        rig.guard.send(method, f"{TESTNET_REST}{path}", {})
    assert rig.sim.sent == []


@pytest.mark.parametrize("url", ["https://api.binance.com/api/v3/time", "https://demo-api.binance.com/api/v3/time",
                                 "http://testnet.binance.vision/api/v3/time",
                                 "https://testnet.binance.vision:8443/api/v3/time"])
def test_guard_blocks_hosts_outside_testnet(url: str) -> None:
    rig = Rig(orders=True)
    with pytest.raises(PreSendError):
        rig.guard.send("GET", url, {})
    assert rig.sim.sent == []


def test_v285d_switch_on_order_counted_by_guard(rig: Rig) -> None:
    rig.adapter.place_order(buy())
    assert rig.guard.order_sent == 1 and rig.guard.blocked == []
