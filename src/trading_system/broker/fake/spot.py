"""M2(바이낸스 현물) fake 브로커 — FakeBroker 확장 (CP-024 D-103·D-110·D-111, T-M2-05·06·09). 결정적·네트워크 없음.

FakeBroker(주문장·장애 주입·IOC·수수료 자산)에 M2 조회 계약(broker.base.SpotOrderQueries)을 더한다.
- 고객 주문 ID: 신규 주문 요청의 client_order_id를 주문장에 남기고 단건 조회·allOrders에 돌려준다.
- 단건 조회(lookup_order): 주문번호 또는 고객 주문 ID. `lookup_not_found` = 다음 N회는 있어도 -2013(접수 직후 반영 지연 —
  B-U3 미확인, 합성), `archived` = -2026. 조회 실패·단절은 FakeBroker와 같은 주입(query_failures·disconnected).
- allOrders·myTrades(orders_between·trades_between): 시각 창 안의 주문·체결. 체결에는 수수료·수수료 자산·보고 금액.
- 자산 잔고(query_balances): 지급 잔고(grant) + 체결 효과(받는 자산에서 수수료 차감, D-101) − 미체결 주문 잠금.
  `reset(grant)` = 테스트넷 리셋 모의(활성 주문 소실 + 잔고 재지급, D-099). `adjust` = 외부 입출금·수동 거래 모의.
- 모든 값은 시험이 정하는 가상값이다(실제 심볼·잔고·요율 아님).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from trading_system.broker.base import BrokerQueryError, LookupKind, OrderLookup
from trading_system.domain.enums import SubmitOutcome
from trading_system.domain.models import BrokerOrderRecord, FillRecord, OrderRequest, SubmitResult

from .broker import FakeBehavior, FakeBroker

_OPEN = ("ACCEPTED", "PARTIAL")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class ReplySpec:
    """합성 주문 응답 (T-M2-09 장애 주입). outcome·code는 D-112 4분류 — 어댑터 분류(broker/binance/classify)와 같은지
    tests/scenario/test_tm2_09_faults가 대조한다. received = 거래소가 실제로 주문을 받았는지(fake의 사실)."""

    status: int
    body: dict | None
    outcome: SubmitOutcome
    code: int | None
    received: bool = False
    banned: bool = False   # 418: 이후 모든 호출 중단 (D-113, 사람 재개)


REPLY_TABLE: dict[str, ReplySpec] = {
    "-1013": ReplySpec(400, {"code": -1013, "msg": "Filter failure: NOTIONAL"}, SubmitOutcome.REJECTED, -1013),
    "-1021": ReplySpec(400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."},
                       SubmitOutcome.REJECTED, -1021),
    "-2010": ReplySpec(400, {"code": -2010, "msg": "Account has insufficient balance for requested action."},
                       SubmitOutcome.REJECTED, -2010),
    "-2010dup": ReplySpec(400, {"code": -2010, "msg": "Duplicate order sent."}, SubmitOutcome.UNKNOWN, -2010),
    "429": ReplySpec(429, {"code": -1003, "msg": "Too many requests."}, SubmitOutcome.UNKNOWN, -1003),
    "418": ReplySpec(418, {"code": -1003, "msg": "IP banned."}, SubmitOutcome.UNKNOWN, -1003, banned=True),
    "5xx": ReplySpec(503, None, SubmitOutcome.UNKNOWN, None),
    "5xx+": ReplySpec(503, None, SubmitOutcome.UNKNOWN, None, received=True),
    "403": ReplySpec(403, None, SubmitOutcome.UNKNOWN, None),
    "-1022": ReplySpec(401, {"code": -1022, "msg": "Signature for this request is not valid."},
                       SubmitOutcome.UNKNOWN, -1022),
}


class FakeSpotBroker(FakeBroker):
    query_fills_trusted = True   # myTrades id = executionReport t = 같은 체결 키 (B-U17 해소)

    def __init__(self, now: Callable[[], datetime], behavior: FakeBehavior = FakeBehavior.FILL) -> None:
        super().__init__(behavior)
        self._now = now
        self._cid: dict[str, str] = {}               # 주문번호 → 고객 주문 ID
        self._created: dict[str, datetime] = {}      # 주문번호 → 접수 시각
        self._fill_at: dict[str, datetime] = {}      # 체결번호 → 체결 시각
        self._pending_cid: str | None = None
        self.lookups: list[tuple[str, dict[str, str | None]]] = []      # (심볼, 조회 키) — 시험 관찰용
        self.window_queries: list[tuple[str, str, datetime, datetime]] = []   # (종류, 심볼, 시작, 끝)
        self.lookup_not_found = 0   # 다음 단건 조회 N회는 주문이 있어도 -2013 (합성)
        self.archived = False       # 단건 조회가 -2026 (합성)
        self.grant: dict[str, Decimal] = {}          # 지급 잔고 (리셋 때 다시 지급)
        self._adjust: dict[str, Decimal] = {}        # 외부 입출금·수동 거래 (가상)
        self._effects_from = 0                       # 이 체결 순번부터 잔고 효과에 넣는다 (리셋 뒤 0부터)
        self.replies: list[str] = []                 # 다음 신규 주문들의 합성 응답 (REPLY_TABLE 키, T-M2-09)
        self.banned = False                          # 418 이후 호출 중단

    # --- 주문 ------------------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> SubmitResult:
        if self.banned:   # 418 뒤: 어댑터가 호출하지 않는다 → 전송 전 실패 (D-113)
            return SubmitResult(SubmitOutcome.PRE_SEND_FAILURE, reason="fake: 418 차단 중 — 호출 중단")
        spec = REPLY_TABLE[self.replies.pop(0)] if self.replies else None
        if spec is not None and not spec.received:
            self.sent.append(request)
            self.banned = self.banned or spec.banned
            return SubmitResult(spec.outcome, reason=f"fake 합성 HTTP {spec.status}"
                                + (f", 코드 {spec.code}" if spec.code is not None else ""), code=spec.code)
        self._pending_cid = request.client_order_id
        try:
            result = super().place_order(request)
        finally:
            self._pending_cid = None
        if spec is not None and result.outcome is SubmitOutcome.ACCEPTED:   # 받았지만 응답은 5XX (실행 상태 불명)
            return SubmitResult(spec.outcome, reason=f"fake 합성 HTTP {spec.status} (거래소 접수됨)", code=spec.code)
        return result

    def _query(self) -> None:
        if self.banned:
            self.queries += 1
            raise BrokerQueryError("fake: 418 차단 중 — 조회 중단")
        super()._query()

    def _new_order(self, account: str, kind: str, instrument: str, side: str, quantity: Decimal, price: Decimal,
                   order_type, original: str | None, state: str = "ACCEPTED") -> str:  # type: ignore[no-untyped-def]
        no = super()._new_order(account, kind, instrument, side, quantity, price, order_type, original, state)
        self._created[no] = self._now()
        if kind == "NEW" and self._pending_cid is not None:
            self._cid[no] = self._pending_cid
        return no

    def add_external_order(self, account: str, instrument: str, side: str, quantity: Decimal,
                           price: Decimal = Decimal("10000"), client_order_id: str | None = None) -> str:
        """시스템 밖 주문 (거래소 앱 직접 거래). 실제 거래소는 고객 주문 ID를 자동으로 붙인다 — 시험이 줄 수 있다."""
        self._pending_cid = client_order_id
        try:
            return super().add_external_order(account, instrument, side, quantity, price)
        finally:
            self._pending_cid = None

    def fill(self, order_no: str, quantity: Decimal | None = None, price: Decimal | None = None) -> None:
        super().fill(order_no, quantity, price)
        self._fill_at[f"F{self._fill_seq:07d}"] = self._now()

    def _record(self, no: str) -> BrokerOrderRecord:
        return replace(self._book[no].record, client_order_id=self._cid.get(no))

    def _visible(self, no: str) -> bool:
        return self.queries > self._book[no].visible_after

    # --- M2 조회 (SpotOrderQueries) ------------------------------------------------------
    def lookup_order(self, account: str, instrument: str, *, order_no: str | None,
                     client_order_id: str | None) -> OrderLookup:
        self._query()
        self.lookups.append((instrument, {"order_no": order_no} if order_no is not None
                             else {"client_order_id": client_order_id}))
        if self.archived:
            return OrderLookup(LookupKind.ARCHIVED)
        if self.lookup_not_found > 0:
            self.lookup_not_found -= 1
            return OrderLookup(LookupKind.NOT_FOUND)
        for no, o in self._book.items():
            r = o.record
            if o.account != account or r.instrument != instrument or r.kind != "NEW" or not self._visible(no):
                continue
            if (order_no is not None and r.order_no == order_no) or \
                    (order_no is None and client_order_id is not None and self._cid.get(no) == client_order_id):
                return OrderLookup(LookupKind.FOUND, self._record(no))
        return OrderLookup(LookupKind.NOT_FOUND)

    def orders_between(self, account: str, instrument: str, start: datetime, end: datetime) -> list[BrokerOrderRecord]:
        self._query()
        self.window_queries.append(("allOrders", instrument, start, end))
        return [self._record(no) for no, o in self._book.items()
                if o.account == account and o.record.instrument == instrument and o.record.kind == "NEW"
                and start <= self._created[no] <= end and self._visible(no)]

    def trades_between(self, account: str, instrument: str, start: datetime, end: datetime) -> list[FillRecord]:
        self._query()
        self.window_queries.append(("myTrades", instrument, start, end))
        out: list[FillRecord] = []
        for a, d, f in self._fills:
            o = self._find(d, f.order_no)
            at = self._fill_at.get(f.fill_id)
            if a != account or o.record.instrument != instrument or at is None or not start <= at <= end:
                continue
            if not self._visible(f.order_no if d == self._day else f"d{d}-{f.order_no}"):
                continue
            fee, asset = self._fees.get(f.fill_id, (None, None))
            out.append(FillRecord(f.order_no, f.fill_id, f.quantity, f.price, at, commission=fee,
                                  commission_asset=asset, quote_qty=self._reported(f.quantity, f.price),
                                  instrument=instrument))
        return out

    def query_orders(self, account: str) -> list[BrokerOrderRecord]:
        """대조용 주문 조회(범위는 FakeBroker와 같음) + 고객 주문 ID (D-111 보류 짝짓기)."""
        return [replace(r, client_order_id=self._cid.get(r.order_no)) if r.kind == "NEW" else r
                for r in super().query_orders(account)]

    def query_fills(self, account: str) -> list[FillRecord]:
        """대조용 체결 조회(오늘 범위는 FakeBroker와 같음) + M2 수수료·수수료 자산·보고 금액·심볼 (D-101·D-118)."""
        out: list[FillRecord] = []
        for f in super().query_fills(account):
            o = self._find(self._day, f.order_no).record
            fee, asset = self._fees.get(f.fill_id, (None, None))
            out.append(replace(f, at=self._fill_at.get(f.fill_id, f.at), commission=fee, commission_asset=asset,
                               quote_qty=self._reported(f.quantity, f.price), instrument=o.instrument))
        return out

    def _reported(self, qty: Decimal, px: Decimal) -> Decimal:
        return self.quote_report(qty, px) if self.quote_report is not None else qty * px

    # --- 자산 잔고 (D-103) ---------------------------------------------------------------
    def balance_totals(self) -> dict[str, Decimal]:
        """지급 + 외부 조정 + 리셋 이후 체결 효과 (조회 장애와 무관). 모든 계좌 합 — fake는 계좌 하나로 쓴다."""
        total: dict[str, Decimal] = dict(self.grant)
        for asset, v in self._adjust.items():
            total[asset] = total.get(asset, _ZERO) + v
        if self.spot_fees is None:
            return total
        for _a, d, f in self._fills[self._effects_from:]:
            o = self._find(d, f.order_no).record
            base, quote = self.spot_fees.pairs[o.instrument]
            fee, asset = self._fees.get(f.fill_id, (_ZERO, quote))
            amount = self._reported(f.quantity, f.price)
            sign = Decimal("1") if o.side == "BUY" else Decimal("-1")
            total[base] = total.get(base, _ZERO) + sign * f.quantity
            total[quote] = total.get(quote, _ZERO) - sign * amount
            total[asset] = total.get(asset, _ZERO) - fee
        return total

    def query_balances(self, account: str) -> dict[str, tuple[Decimal, Decimal]]:
        self._query()
        total = self.balance_totals()
        locked: dict[str, Decimal] = {}
        if self.spot_fees is not None:
            for o in self._book.values():
                r = o.record
                if o.account != account or r.state not in _OPEN or r.kind != "NEW":
                    continue
                base, quote = self.spot_fees.pairs[r.instrument]
                left = r.quantity - r.filled_quantity
                asset, amount = (quote, left * r.price) if r.side == "BUY" else (base, left)
                locked[asset] = locked.get(asset, _ZERO) + amount
        return {a: (v - locked.get(a, _ZERO), locked.get(a, _ZERO)) for a, v in total.items()
                if v != 0 or locked.get(a)}

    def adjust(self, asset: str, delta: Decimal) -> None:
        """외부 입출금·거래소 앱 직접 거래 모의 (통보 없음, 잔고만 바뀜)."""
        self._adjust[asset] = self._adjust.get(asset, _ZERO) + delta

    def reset(self, grant: dict[str, Decimal]) -> None:
        """테스트넷 리셋 모의 (D-099, B-U7 실제 증상 미확인): 활성 주문 소실 + 잔고 재지급(체결 효과·조정 초기화)."""
        for o in self._book.values():
            if o.record.state in _OPEN:   # 소실: 조회에 안 보이고 체결도 안 됨 (기록은 남겨 지난 체결 계산은 유지)
                o.record = replace(o.record, state="VANISHED")
                o.visible_after = 10 ** 12
        self.grant = dict(grant)
        self._adjust.clear()
        self._effects_from = len(self._fills)
