"""사용자 스트림·체결 응답 파서와 체결 ID 중복 제거 (CP-024 D-101·D-114·D-118, V-150(파서 부분)·V-156).

합성 메시지는 공식 user-data-stream.md executionReport 예시 형식(값은 합성, 계정 식별값 없음).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_system.broker.binance import stream as st
from trading_system.broker.binance.numbers import BinanceParseError


def _report(**kw: object) -> dict:
    ev: dict = {
        "e": "executionReport", "E": 1499405658658, "s": "ETHBTC", "c": "M2-abc", "S": "BUY", "o": "LIMIT",
        "f": "IOC", "q": "1.00000000", "p": "0.10264410", "P": "0.00000000", "F": "0.00000000", "g": -1,
        "C": "", "x": "TRADE", "X": "PARTIALLY_FILLED", "r": "NONE", "i": 4293153, "l": "0.40000000",
        "z": "0.40000000", "L": "0.10264410", "n": "0.00040000", "N": "ETH", "T": 1499405658657,
        "t": 717, "v": 3, "I": 8641984, "w": False, "m": False, "M": False, "O": 1499405658657,
        "Z": "0.04105764", "Y": "0.04105764", "Q": "0.00000000", "W": 1499405658657, "V": "NONE",
    }
    ev.update(kw)
    return ev


def test_trade_execution_report_parsed() -> None:
    r = st.parse_event({"subscriptionId": 0, "event": _report()})
    assert isinstance(r, st.ExecutionReport)
    assert r.symbol == "ETHBTC" and r.order_id == 4293153 and r.client_order_id == "M2-abc"
    assert r.execution_type == "TRADE" and r.order_status == "PARTIALLY_FILLED"
    assert r.cumulative_qty == Decimal("0.4")
    f = r.fill
    assert f is not None
    assert f.key == ("ETHBTC", 717)
    assert (f.quantity, f.price, f.quote_qty) == (Decimal("0.4"), Decimal("0.1026441"), Decimal("0.04105764"))
    assert (f.commission, f.commission_asset) == (Decimal("0.0004"), "ETH")
    assert f.quote_confirmed is True
    assert f.at == datetime(2017, 7, 7, 5, 34, 18, 657000, tzinfo=UTC)


def test_bare_event_also_parsed() -> None:
    assert isinstance(st.parse_event(_report()), st.ExecutionReport)


def test_non_trade_report_has_no_fill() -> None:
    r = st.parse_event(_report(x="NEW", X="NEW", l="0.00000000", L="0.00000000", n="0", N=None, t=-1,
                               Y="0.00000000", z="0.00000000"))
    assert isinstance(r, st.ExecutionReport) and r.fill is None


def test_trade_prevention_is_not_a_fill() -> None:
    # TRADE_PREVENTION: l·L·Y = 0, 체결 아님 (D-118, BN B-U19)
    r = st.parse_event(_report(x="TRADE_PREVENTION", X="EXPIRED", l="0.00000000", L="0.00000000",
                               Y="0.00000000", t=-1, n="0", N=None))
    assert isinstance(r, st.ExecutionReport) and r.fill is None


def test_expired_ioc_report() -> None:
    r = st.parse_event(_report(x="EXPIRED", X="EXPIRED", l="0.00000000", t=-1, n="0", N=None, Y="0.00000000"))
    assert isinstance(r, st.ExecutionReport) and r.order_status == "EXPIRED" and r.fill is None


def test_canceled_report_uses_original_client_id() -> None:
    r = st.parse_event(_report(x="CANCELED", X="CANCELED", c="cancel-req-1", C="M2-abc", t=-1, l="0", n="0",
                               N=None, Y="0"))
    assert isinstance(r, st.ExecutionReport) and r.client_order_id == "M2-abc"


@pytest.mark.parametrize("override", [
    {"t": -1},                   # TRADE인데 체결 ID 없음
    {"l": "0.00000000"},         # TRADE인데 수량 0
    {"N": None},                 # TRADE인데 수수료 자산 없음
    {"l": "4e-1"},               # 지수 표기
    {"i": "4293153"},            # orderId가 정수 아님
    {"s": ""},
    {"Y": 0.04},                 # float
])
def test_malformed_trade_report_raises(override: dict) -> None:
    with pytest.raises(BinanceParseError):
        st.parse_event(_report(**override))


def test_other_events() -> None:
    pos = st.parse_event({"subscriptionId": 0, "event": {
        "e": "outboundAccountPosition", "E": 1564034571105, "u": 1564034571073,
        "B": [{"a": "ETH", "f": "10000.000000", "l": "0.000000"}, {"a": "USDT", "f": "5.5", "l": "1"}]}})
    assert isinstance(pos, st.AccountPosition)
    assert pos.balances == {"ETH": (Decimal("10000"), Decimal("0")), "USDT": (Decimal("5.5"), Decimal("1"))}
    term = st.parse_event({"subscriptionId": 0, "event": {"e": "eventStreamTerminated", "E": 1728973001334}})
    assert isinstance(term, st.StreamTerminated)
    unknown = st.parse_event({"subscriptionId": 0, "event": {"e": "listStatus", "E": 1}})
    assert isinstance(unknown, st.OtherEvent) and unknown.event_type == "listStatus"


def test_parse_event_rejects_non_object() -> None:
    with pytest.raises(BinanceParseError):
        st.parse_event([1, 2])
    with pytest.raises(BinanceParseError):
        st.parse_event({"event": {"E": 1}})


# --- FULL 응답·myTrades·주문 조회 ---

FULL = {
    "symbol": "BTCUSDT", "orderId": 28, "orderListId": -1, "clientOrderId": "M2-abc",
    "transactTime": 1507725176595, "price": "61500.00", "origQty": "0.00200000", "executedQty": "0.00150000",
    "origQuoteOrderQty": "0.00000000", "cummulativeQuoteQty": "92.10000000", "status": "EXPIRED",
    "timeInForce": "IOC", "type": "LIMIT", "side": "BUY", "workingTime": 1507725176595,
    "selfTradePreventionMode": "NONE",
    "fills": [
        {"price": "61400.00", "qty": "0.00100000", "commission": "0.00000100", "commissionAsset": "BTC", "tradeId": 56},
        {"price": "61400.00", "qty": "0.00050000", "commission": "0.00000050", "commissionAsset": "BTC", "tradeId": 57},
    ],
}


def test_full_response_fills_are_provisional_quote() -> None:
    ack = st.parse_order_response(FULL)
    assert ack.order_id == 28 and ack.client_order_id == "M2-abc" and ack.status == "EXPIRED"
    assert ack.executed_qty == Decimal("0.0015") and ack.cumulative_quote == Decimal("92.1")
    assert [f.key for f in ack.fills] == [("BTCUSDT", 56), ("BTCUSDT", 57)]
    f = ack.fills[0]
    assert f.quote_qty == Decimal("61.4") and f.quote_confirmed is False    # fills에는 금액 필드 없음 → 임시값
    assert f.order_id == 28 and f.at is None


def test_query_order_negative_cumulative_quote_means_no_data() -> None:
    q = st.parse_order_response({**{k: v for k, v in FULL.items() if k != "fills"}, "cummulativeQuoteQty": "-1.00000000"})
    assert q.cumulative_quote is None and q.fills == ()


def test_my_trades_quote_qty_confirmed() -> None:
    fills = st.parse_my_trades([{
        "symbol": "BTCUSDT", "id": 56, "orderId": 28, "orderListId": -1, "price": "61400.00",
        "qty": "0.00100000", "quoteQty": "61.40000000", "commission": "0.00000100", "commissionAsset": "BTC",
        "time": 1507725176595, "isBuyer": True, "isMaker": False, "isBestMatch": True}])
    assert len(fills) == 1 and fills[0].quote_confirmed and fills[0].quote_qty == Decimal("61.4")
    assert fills[0].key == ("BTCUSDT", 56)


def test_my_trades_one_bad_row_fails_all() -> None:
    # 해석할 수 없는 행이 하나라도 있으면 전체 실패 ("없음"으로 잘못 확정하지 않게, M1 D-047과 같은 원칙)
    with pytest.raises(BinanceParseError):
        st.parse_my_trades([{"symbol": "BTCUSDT", "id": "x"}])


# --- 체결 ID 중복 제거 ---

def test_fill_ledger_dedup_and_confirm() -> None:
    led = st.FillLedger()
    ack = st.parse_order_response(FULL)
    assert led.record(ack.fills[0]) is st.Recorded.NEW
    assert led.record(ack.fills[0]) is st.Recorded.DUPLICATE
    rep = st.parse_event(_report(s="BTCUSDT", i=28, t=56, l="0.00100000", L="61400.00", n="0.00000100", N="BTC",
                                 Y="61.40000000"))
    assert isinstance(rep, st.ExecutionReport) and rep.fill is not None
    assert led.record(rep.fill) is st.Recorded.CONFIRMED          # 임시 금액 → 거래소 보고 금액으로 확정
    assert led.get(("BTCUSDT", 56)).quote_confirmed
    assert led.record(rep.fill) is st.Recorded.DUPLICATE


def test_fill_ledger_conflict_on_different_quantity() -> None:
    led = st.FillLedger()
    ack = st.parse_order_response(FULL)
    led.record(ack.fills[0])
    rep = st.parse_event(_report(s="BTCUSDT", i=28, t=56, l="0.00200000", L="61400.00", n="0.000002", N="BTC",
                                 Y="122.8"))
    assert isinstance(rep, st.ExecutionReport) and rep.fill is not None
    assert led.record(rep.fill) is st.Recorded.CONFLICT


def test_same_trade_id_on_other_symbol_is_distinct() -> None:
    led = st.FillLedger()
    ack = st.parse_order_response(FULL)
    led.record(ack.fills[0])
    other = st.parse_order_response({**FULL, "symbol": "ETHUSDT"})
    assert led.record(other.fills[0]) is st.Recorded.NEW


# --- WS 연결 수명 (D-114) ---

def test_session_rotation_policy() -> None:
    s = st.SessionPolicy(connected_at_ms=0)
    assert not s.should_rotate(now_ms=23 * 3_600_000 - 1)
    assert s.should_rotate(now_ms=23 * 3_600_000)               # 24시간 전에 교체(1시간 여유)
    s2 = st.SessionPolicy(connected_at_ms=0)
    s2.note(st.ServerShutdown(event_time_ms=5))
    assert s2.should_rotate(now_ms=10)
    s3 = st.SessionPolicy(connected_at_ms=0)
    s3.note(st.StreamTerminated(event_time_ms=5))
    assert s3.should_rotate(now_ms=10) and s3.needs_reconcile


def test_server_shutdown_event_shape() -> None:
    ev = st.parse_event({"event": {"e": "serverShutdown", "E": 1}})
    assert isinstance(ev, st.ServerShutdown)
