"""체결 응답·사용자 스트림 파서, 체결 ID 중복 제거, WS 연결 수명 정책 (CP-024 D-101·D-109·D-114·D-118, BN §7).

- 체결 식별 = (심볼, tradeId). executionReport `t`, FULL 응답 fills `tradeId`, myTrades `id`가 같은 키다(orderId·tradeId는
  심볼 안에서만 유일 — B-U17 해소). 같은 체결이 여러 경로로 와도 한 번만 반영한다(FillLedger).
- 체결 금액(D-118): executionReport `Y`·myTrades `quoteQty`는 거래소 보고 금액(확정). FULL 응답 fills에는 금액 필드가
  없으므로 price × qty를 **임시값**(quote_confirmed=False)으로 두고, 같은 체결의 확정값이 오면 CONFIRMED로 바꾼다.
- 수수료·수수료 자산(D-101): `n`·`N`, fills·myTrades의 commission·commissionAsset. 순 수령 계산·K4 판정은 호출자.
- 메이커 여부(CP-040 D-267 (4), T-065 — 기록만): executionReport `m`, myTrades `isMaker`(BN 행 171·164). FULL 응답 fills에는
  maker 필드가 없다(BN 행 121) → None. 불(bool)이 아니거나 없으면 None — 기록 전용 값이라 체결 해석을 실패시키지 않는다.
  같은 체결의 값 비교(FillLedger CONFLICT)에는 넣지 않는다.
- TRADE_PREVENTION 실행 유형은 l·L·Y = 0이라 체결이 아니다. TRADE인데 체결 ID·수량·수수료 자산이 없으면 해석 불가.
- 주문 조회의 `cummulativeQuoteQty` < 0은 "데이터 없음"(None) — 0으로 보지 않는다.
- 해석할 수 없는 행이 하나라도 있으면 목록 전체를 실패로 본다("없음"으로 잘못 확정하지 않게).
- WS API 메시지는 `{"subscriptionId": n, "event": {...}}`로 감싸져 온다. 감싸지 않은 이벤트도 받는다.
  `serverShutdown` 이벤트의 정확한 메시지 형태는 원문 대조 안 함 [미확인] — `e == "serverShutdown"`으로만 판정한다.
- 전달 보장·순서·재연결 시 재전송은 미확인(B-U8) → 재연결 뒤 대조(D-109)로 보완. 연결은 24시간 유효이므로 23시간에
  교체한다 [제안]. ping/pong 응답은 WS 라이브러리 몫(T-M2-14에서 확인).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .numbers import BinanceParseError, parse_decimal, parse_int, parse_non_negative

ROTATE_AFTER_MS = 23 * 3_600_000
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def ms_to_utc(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def _str(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise BinanceParseError(f"{field}: 문자열 아님")
    return value


def _maker(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class Fill:
    symbol: str
    order_id: int
    trade_id: int
    quantity: Decimal
    price: Decimal
    quote_qty: Decimal
    quote_confirmed: bool
    commission: Decimal
    commission_asset: str
    at: datetime | None
    is_maker: bool | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.symbol, self.trade_id)


@dataclass(frozen=True)
class ExecutionReport:
    event_time: datetime
    symbol: str
    client_order_id: str          # 취소 이벤트면 원 주문의 ID(C), 그 밖에는 c
    side: str
    order_type: str
    time_in_force: str
    order_qty: Decimal
    order_price: Decimal
    execution_type: str           # x
    order_status: str             # X
    reject_reason: str            # r
    order_id: int
    cumulative_qty: Decimal       # z
    cumulative_quote: Decimal     # Z
    fill: Fill | None             # x == TRADE일 때만


@dataclass(frozen=True)
class AccountPosition:
    event_time: datetime
    balances: dict[str, tuple[Decimal, Decimal]]   # 자산 → (free, locked)


@dataclass(frozen=True)
class StreamTerminated:
    event_time_ms: int


@dataclass(frozen=True)
class ServerShutdown:
    event_time_ms: int


@dataclass(frozen=True)
class OtherEvent:
    event_type: str


StreamEvent = ExecutionReport | AccountPosition | StreamTerminated | ServerShutdown | OtherEvent


def parse_event(message: object) -> StreamEvent:
    ev = message.get("event") if isinstance(message, dict) and "event" in message else message
    if not isinstance(ev, dict):
        raise BinanceParseError("이벤트가 객체가 아님")
    etype = _str(ev.get("e"), "e")
    if etype == "executionReport":
        return _execution_report(ev)
    if etype == "outboundAccountPosition":
        rows = ev.get("B")
        if not isinstance(rows, list):
            raise BinanceParseError("B: 목록 아님")
        bal: dict[str, tuple[Decimal, Decimal]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise BinanceParseError("B: 항목 형식 아님")
            asset = _str(row.get("a"), "a")
            if asset in bal:
                raise BinanceParseError("B: 자산 중복")
            bal[asset] = (parse_non_negative(row.get("f"), "f"), parse_non_negative(row.get("l"), "l"))
        return AccountPosition(ms_to_utc(parse_int(ev.get("E"), "E")), bal)
    if etype == "eventStreamTerminated":
        return StreamTerminated(parse_int(ev.get("E"), "E"))
    if etype == "serverShutdown":
        e = ev.get("E")
        return ServerShutdown(e if isinstance(e, int) and not isinstance(e, bool) else 0)
    return OtherEvent(etype)


def _execution_report(ev: dict) -> ExecutionReport:
    symbol = _str(ev.get("s"), "s")
    order_id = parse_int(ev.get("i"), "i")
    x = _str(ev.get("x"), "x")
    c = _str(ev.get("c"), "c")
    orig = ev.get("C")
    cid = orig if x == "CANCELED" and isinstance(orig, str) and orig else c
    fill = None
    if x == "TRADE":
        trade_id = parse_int(ev.get("t"), "t")
        qty = parse_non_negative(ev.get("l"), "l")
        if trade_id < 0 or qty <= 0:
            raise BinanceParseError("TRADE인데 체결 ID·수량 없음")
        fill = Fill(symbol, order_id, trade_id, qty, parse_non_negative(ev.get("L"), "L"),
                    parse_non_negative(ev.get("Y"), "Y"), True, parse_non_negative(ev.get("n"), "n"),
                    _str(ev.get("N"), "N"), ms_to_utc(parse_int(ev.get("T"), "T")), _maker(ev.get("m")))
    else:
        for f in ("l", "Y"):
            parse_decimal(ev.get(f), f)   # 형식 확인만 (TRADE_PREVENTION 등은 0)
    return ExecutionReport(
        ms_to_utc(parse_int(ev.get("E"), "E")), symbol, cid, _str(ev.get("S"), "S"), _str(ev.get("o"), "o"),
        _str(ev.get("f"), "f"), parse_non_negative(ev.get("q"), "q"), parse_non_negative(ev.get("p"), "p"), x,
        _str(ev.get("X"), "X"), _str(ev.get("r"), "r"), order_id, parse_non_negative(ev.get("z"), "z"),
        parse_non_negative(ev.get("Z"), "Z"), fill)


@dataclass(frozen=True)
class OrderState:
    """FULL 주문 응답·주문 조회·allOrders 행."""
    symbol: str
    order_id: int
    client_order_id: str
    status: str
    order_type: str
    side: str
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal
    cumulative_quote: Decimal | None     # < 0이면 None (데이터 없음)
    fills: tuple[Fill, ...]


def parse_order_response(body: object) -> OrderState:
    if not isinstance(body, dict):
        raise BinanceParseError("주문 응답이 객체가 아님")
    symbol = _str(body.get("symbol"), "symbol")
    order_id = parse_int(body.get("orderId"), "orderId")
    cq = parse_decimal(body.get("cummulativeQuoteQty"), "cummulativeQuoteQty")
    fills_raw = body.get("fills", [])
    if not isinstance(fills_raw, list):
        raise BinanceParseError("fills: 목록 아님")
    fills = []
    for f in fills_raw:
        if not isinstance(f, dict):
            raise BinanceParseError("fills: 항목 형식 아님")
        price, qty = parse_non_negative(f.get("price"), "price"), parse_non_negative(f.get("qty"), "qty")
        fills.append(Fill(symbol, order_id, parse_int(f.get("tradeId"), "tradeId"), qty, price, price * qty, False,
                          parse_non_negative(f.get("commission"), "commission"),
                          _str(f.get("commissionAsset"), "commissionAsset"), None))
    return OrderState(symbol, order_id, _str(body.get("clientOrderId"), "clientOrderId"),
                      _str(body.get("status"), "status"), _str(body.get("type"), "type"), _str(body.get("side"), "side"),
                      parse_non_negative(body.get("price"), "price"), parse_non_negative(body.get("origQty"), "origQty"),
                      parse_non_negative(body.get("executedQty"), "executedQty"), None if cq < 0 else cq, tuple(fills))


def parse_order_list(body: object) -> list[OrderState]:
    if not isinstance(body, list):
        raise BinanceParseError("주문 목록이 아님")
    return [parse_order_response(row) for row in body]


def parse_my_trades(body: object) -> list[Fill]:
    if not isinstance(body, list):
        raise BinanceParseError("체결 목록이 아님")
    out = []
    for row in body:
        if not isinstance(row, dict):
            raise BinanceParseError("체결 행 형식 아님")
        out.append(Fill(_str(row.get("symbol"), "symbol"), parse_int(row.get("orderId"), "orderId"),
                        parse_int(row.get("id"), "id"), parse_non_negative(row.get("qty"), "qty"),
                        parse_non_negative(row.get("price"), "price"), parse_non_negative(row.get("quoteQty"), "quoteQty"),
                        True, parse_non_negative(row.get("commission"), "commission"),
                        _str(row.get("commissionAsset"), "commissionAsset"),
                        ms_to_utc(parse_int(row.get("time"), "time")), _maker(row.get("isMaker"))))
    return out


class Recorded(StrEnum):
    NEW = "NEW"
    CONFIRMED = "CONFIRMED"     # 임시 금액 → 거래소 보고 금액으로 확정
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"       # 같은 체결 ID인데 수량·가격·수수료가 다름 → 호출자가 K4 판단


class FillLedger:
    """체결 ID 중복 제거(메모리). 영속 저장·재시작 복원은 T-M2-04·05(저장소)에서 연결한다."""

    def __init__(self) -> None:
        self._fills: dict[tuple[str, int], Fill] = {}

    def get(self, key: tuple[str, int]) -> Fill:
        return self._fills[key]

    def record(self, fill: Fill) -> Recorded:
        old = self._fills.get(fill.key)
        if old is None:
            self._fills[fill.key] = fill
            return Recorded.NEW
        same = (old.order_id, old.quantity, old.price, old.commission, old.commission_asset) == \
            (fill.order_id, fill.quantity, fill.price, fill.commission, fill.commission_asset)
        if not same or (old.quote_confirmed and fill.quote_confirmed and old.quote_qty != fill.quote_qty):
            return Recorded.CONFLICT
        if not old.quote_confirmed and fill.quote_confirmed:
            self._fills[fill.key] = replace(fill, at=fill.at or old.at)
            return Recorded.CONFIRMED
        return Recorded.DUPLICATE


class SessionPolicy:
    """사용자 스트림 연결 교체 판단. 교체(재연결) 뒤에는 대조가 필요하다(D-109)."""

    def __init__(self, connected_at_ms: int, rotate_after_ms: int = ROTATE_AFTER_MS) -> None:
        self._start = connected_at_ms
        self._limit = rotate_after_ms
        self._forced = False

    def note(self, event: StreamEvent) -> None:
        if isinstance(event, (ServerShutdown, StreamTerminated)):
            self._forced = True

    def should_rotate(self, now_ms: int) -> bool:
        return self._forced or now_ms - self._start >= self._limit

    @property
    def needs_reconcile(self) -> bool:
        return True
