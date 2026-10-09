"""M2 대조 (CP-024 D-099·D-103·D-109·D-111·D-118, T-M2-06, V-130·V-143·V-144·V-153·V-155). M1 Reconciler는 그대로 둔다.

공통 원칙은 M1과 같다: 원장을 자동으로 고치지 않고 차이는 불일치 항목 + K4, 조회 실패는 불일치가 아님, 재조회 1회,
결과 불명에 맡긴 브로커 주문은 보류(K3 재개 근거 아님). 다른 점:
- 비교 값(D-103): 유니버스 기초 자산 = 거래소 free + locked ↔ 내부 포지션(순 수령), USDT = free + locked ↔ 내부 기대 USDT
  (기준선 + 체결 흐름, 금액은 거래소 보고 금액 Y·quoteQty — D-118, monitoring.spot_cash). 기준선이 없으면 불일치(사람 확정).
- 범위 밖 자산: 시작·일 경계마다 스냅샷(감사 ASSET_SNAPSHOT), 그 뒤 변화는 정보 알림만(불일치 아님).
- 체결의 수수료 자산이 기초·USDT 외면 불일치(D-101 → K4, 실시간 통보를 놓친 체결 포함).
- 브로커 주문 식별 = (계좌, 심볼, orderId)(D-110). 결과 불명 보류 = 전송 후 24시간 이내(D-111; M1은 "오늘 전송한").
  고객 주문 ID가 있으면 그것으로 의도와 짝짓고, 없으면 (종류, 종목, 방향, 수량)으로.
- 거래일로 오래된 주문(STALE_ORDER)·장 마감 만료는 없다(24/7). 대신 1분 넘게 열린 주문은 경고 알림(주문당 1회, D-109).
- 대조 결과 필드(02 ENT-24 M2): recon_asset 표에 자산별 내부값·거래소 free·locked·합·범위 여부를 남긴다.
- 대조 시점(D-109): 시작·60초 주기·사용자 스트림 재연결 후·UTC 일 경계 1회(일일 확정·백업) — SpotReconcileSchedule·Cycle.
- 테스트넷 리셋(D-099): 자동 기준선 재설정 없음. 사람이 confirm_reset("리셋 확인")으로 기준선 재설정 + 감사 + PAPER 기간
  재시작 표시. K4 해제는 그 뒤 대조 일치 + 사용자 재개.
[제안] 리셋 확인 때 재지급된 유니버스 기초 자산은 수동 보유(manual, 전략 신호 금지 INV-16)로 기록 — 전략에 넘길지는 사람이
따로 정한다. 리셋 확인은 활성 의도·열린 주문이 있으면 거부한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trading_system.broker.base import BrokerAdapter, SpotOrderQueries
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import Clock, from_iso
from trading_system.domain.enums import IntentState
from trading_system.domain.market_time import M2_TIME, MarketTime
from trading_system.domain.models import MANUAL_OWNER, BrokerPosition
from trading_system.domain.sinks import AlertSink, HaltControl
from trading_system.storage.repository import Repository

from .reconciler import OPEN, ItemKind, ReconcileResult, Reconciler, _avg_price, _Comparison

STARTUP, PERIODIC, STREAM_RECONNECT, DAY_BOUNDARY = "STARTUP", "PERIODIC", "STREAM_RECONNECT", "DAY_BOUNDARY"
UNKNOWN_HOLD = timedelta(hours=24)        # 결과 불명 보류 = 전송 후 24시간 (D-111)
OPEN_ALERT_AFTER = timedelta(minutes=1)   # 1분 넘게 열린 주문 알림 (D-109)
RESET_RELEASE_REASON = "리셋 확인"
_ZERO = Decimal("0")


class SpotItemKind:
    QUOTE_CASH_MISMATCH = "QUOTE_CASH_MISMATCH"          # USDT 기대값 ≠ 거래소 free + locked
    QUOTE_BASELINE_MISSING = "QUOTE_BASELINE_MISSING"    # 기대 USDT 모름(기준선 없음·해석 불가 체결) — 사람 확정
    FEE_ASSET_UNAPPROVED = "FEE_ASSET_UNAPPROVED"        # 수수료 자산이 기초·USDT 외 (D-101)
    UNKNOWN_BROKER_ORDER = ItemKind.UNKNOWN_BROKER_ORDER


class ResetError(RuntimeError):
    """리셋 확인 절차를 진행할 수 없음 (활성 의도·열린 주문 등)."""


class SpotReconciler(Reconciler):
    def __init__(self, repo: Repository, broker: BrokerAdapter, clock: Clock, halts: HaltControl | None,
                 alerts: AlertSink, *, universe: Mapping[str, tuple[str, str]], quote_asset: str,
                 expected_quote: Callable[[str], Decimal | None],
                 drain_events: Callable[[], object] | None = None, requery_wait: Callable[[], None] | None = None,
                 open_alert_after: timedelta = OPEN_ALERT_AFTER) -> None:
        super().__init__(repo, broker, clock, halts, alerts, drain_events, requery_wait)
        self._universe = dict(universe)
        self._quote = quote_asset
        self._expected = expected_quote
        self._open_after = open_alert_after
        self._balances: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
        self._open_alerted: set[str] = set()
        self._scope_alerted: dict[str, dict[str, str]] = {}

    # --- 조회 ---------------------------------------------------------------------------
    def _query(self, account: str) -> tuple[list, list, list]:  # type: ignore[type-arg]
        broker: SpotOrderQueries = self._broker  # type: ignore[assignment]
        with call_priority(Priority.RECONCILE):   # 03 D-048 / D-113
            balances = broker.query_balances(account)
            orders = self._broker.query_orders(account)
            fills = self._broker.query_fills(account)
        self._balances[account] = balances
        positions = [BrokerPosition(inst, self._total(balances, base), _ZERO)
                     for inst, (base, _q) in sorted(self._universe.items())]
        return positions, orders, fills

    @staticmethod
    def _total(balances: Mapping[str, tuple[Decimal, Decimal]], asset: str) -> Decimal:
        free, locked = balances.get(asset, (_ZERO, _ZERO))
        return free + locked

    # --- 비교 ---------------------------------------------------------------------------
    def _compare(self, account: str, positions: list, orders: list, fills: list,  # type: ignore[type-arg]
                 now: datetime, closing: bool = False) -> _Comparison:
        found: list[tuple[str, str | None, str | None, str | None, str | None, dict]] = []
        internal = {i: p.quantity for i, p in self._repo.positions(account).items() if p.quantity != 0}
        broker_pos = {p.instrument: p.quantity for p in positions if p.quantity != 0}
        for inst in sorted(set(self._universe) | set(internal)):
            iq, bq = internal.get(inst, _ZERO), broker_pos.get(inst, _ZERO)
            if iq != bq:
                base = self._universe.get(inst, (None, None))[0]
                found.append((ItemKind.QTY_MISMATCH, inst, None, str(iq), str(bq), {"asset": base}))
        self._compare_quote(account, found)
        inst_of = {o.order_no: o.instrument for o in orders}
        for f in fills:
            inst = f.instrument or inst_of.get(f.order_no)
            pair = self._universe.get(inst or "")
            if f.commission_asset is not None and (pair is None or f.commission_asset not in pair):
                found.append((SpotItemKind.FEE_ASSET_UNAPPROVED, inst, f.order_no, None,
                              f"{f.commission} {f.commission_asset}", {"fill_id": f.fill_id}))

        cmp = _Comparison(found, broker_pos)
        totals = self._spot_fill_totals(fills, orders)
        cmp.fill_totals = totals  # type: ignore[assignment]  # M2: 키 = (심볼, orderId)
        by_ref = {(o.instrument, o.order_no): o for o in orders}
        held_cids, held_terms = self._unknown_intents(account, now)
        for o in orders:
            if o.kind == "CANCEL" or o.order_no in self._repo.order_nos_of(account, o.instrument):
                continue
            if (o.client_order_id is not None and o.client_order_id in held_cids) or (
                    o.client_order_id is None and (o.kind, o.instrument, o.side, o.quantity) in held_terms):
                cmp.deferred.append(o.order_no)   # 결과 불명 해소에 맡김 (전송 후 24시간 이내, D-111)
                continue
            found.append((ItemKind.UNKNOWN_BROKER_ORDER, o.instrument, o.order_no, None,
                          f"{o.side} {o.quantity} {o.state}",
                          {"side": o.side, "quantity": str(o.quantity), "trade_date": M2_TIME.local(now).date()
                           .isoformat(), "detected_at": now.isoformat(), "client_order_id": o.client_order_id}))
        for io in self._repo.open_orders(account):
            b = by_ref.get((io.instrument, io.order_no))
            if b is None:
                found.append((ItemKind.INTERNAL_ONLY_ORDER, io.instrument, io.order_no, io.state, None,
                              {"order_key": io.order_key}))
            elif b.state not in OPEN:
                found.append((ItemKind.STATE_MISMATCH, io.instrument, io.order_no, io.state, b.state,
                              {"order_key": io.order_key, "broker_state": b.state}))
            self._alert_if_open_long(io.order_key, io.instrument, io.order_no, now)
        self._compare_fill_totals(account, fills, totals, found)
        return cmp

    def _compare_quote(self, account: str, found: list) -> None:  # type: ignore[type-arg]
        balances = self._balances.get(account, {})
        free, locked = balances.get(self._quote, (_ZERO, _ZERO))
        exp = self._expected(account)
        if exp is None:
            found.append((SpotItemKind.QUOTE_BASELINE_MISSING, self._quote, None, None, str(free + locked),
                          {"reason": "내부 기대 USDT 모름 — 기준선 없음 또는 해석할 수 없는 체결 (D-103)"}))
        elif exp != free + locked:
            found.append((SpotItemKind.QUOTE_CASH_MISMATCH, self._quote, None, str(exp), str(free + locked),
                          {"free": str(free), "locked": str(locked), "diff": str(free + locked - exp)}))

    def _unknown_intents(self, account: str, now: datetime) -> tuple[set[str], set[tuple]]:  # type: ignore[type-arg]
        """전송 후 24시간 이내 결과 불명 의도의 고객 주문 ID와 조건 (D-111 — UTC 자정과 무관)."""
        cids: set[str] = set()
        terms: set[tuple] = set()  # type: ignore[type-arg]
        for i in self._repo.intents_in_state(IntentState.UNKNOWN):
            if i.account != account:
                continue
            attempt = self._repo.attempt_for(i.intent_id)
            if attempt is None or now - from_iso(attempt["created_at"]) > UNKNOWN_HOLD:
                continue
            cid = self._repo.client_order_of(i.intent_id)
            if cid is not None:
                cids.add(cid)
            terms.add(("NEW" if i.request_type not in ("CANCEL", "MODIFY") else i.request_type,
                       i.instrument, i.side, i.quantity))
        return cids, terms

    @staticmethod
    def _spot_fill_totals(fills: list, orders: list) -> dict[tuple[str, str], Decimal]:  # type: ignore[type-arg]
        inst_of = {o.order_no: o.instrument for o in orders}
        totals: dict[tuple[str, str], Decimal] = {}
        for f in fills:
            inst = f.instrument or inst_of.get(f.order_no)
            if f.fill_id and inst:
                totals[(inst, f.order_no)] = totals.get((inst, f.order_no), _ZERO) + f.quantity
        for o in orders:
            key = (o.instrument, o.order_no)
            if o.kind != "CANCEL" and o.filled_quantity > totals.get(key, _ZERO):
                totals[key] = o.filled_quantity
        return totals

    def _compare_fill_totals(self, account: str, fills: list, totals: dict[tuple[str, str], Decimal],  # type: ignore[type-arg]
                             found: list) -> None:  # type: ignore[type-arg]
        rows_by: dict[tuple[str, str], list] = {}  # type: ignore[type-arg]
        for f in fills:
            if f.fill_id and f.instrument:
                rows_by.setdefault((f.instrument, f.order_no), []).append(f)
        for (inst, order_no), broker_total in sorted(totals.items()):
            order = self._repo.broker_order_by_no(order_no, "", account, instrument=inst)
            if order is None or order.intent_id is None:
                continue   # 외부 주문 체결은 수량 대조로
            internal_total = order.filled_quantity
            if broker_total == internal_total:
                continue
            if broker_total < internal_total:
                found.append((ItemKind.EXCESS_FILL, inst, order_no, str(internal_total), str(broker_total),
                              {"order_key": order.order_key, "broker_total": str(broker_total),
                               "internal_total": str(internal_total)}))
                continue
            rows = rows_by.get((inst, order_no), [])
            detail = {"order_key": order.order_key, "fill_id": f"Q{order_no}-{broker_total}",
                      "quantity": str(broker_total - internal_total), "broker_total": str(broker_total),
                      "internal_total": str(internal_total), "basis": "주문별 체결 수량 합 (M2)"}
            shown = str(broker_total)
            if rows:
                avg = _avg_price(rows)
                detail.update(price=str(avg), at=max(rows, key=lambda f: f.at).at.isoformat())
                shown = f"{broker_total}@{avg}"
            found.append((ItemKind.MISSING_FILL, inst, order_no, str(internal_total), shown, detail))

    def _alert_if_open_long(self, order_key: str, instrument: str, order_no: str, now: datetime) -> None:
        if order_key in self._open_alerted:
            return
        rows = self._repo.select("SELECT created_at FROM broker_order WHERE order_key=?", (order_key,))
        if rows and now - from_iso(rows[0]["created_at"]) > self._open_after:
            self._open_alerted.add(order_key)
            self._alerts.alert("WARNING", f"1분 넘게 열린 주문 {instrument} {order_no} — IOC라 정상은 없음, 확인 필요 "
                                          "(D-109)", order_key)

    # --- 자동 종결 (M2 식별 범위) ----------------------------------------------------------
    def _close_known_order_items(self, account: str, now: datetime) -> list[str]:
        closed: list[str] = []
        for item in self._repo.open_recon_items(account):
            if item["kind"] != ItemKind.UNKNOWN_BROKER_ORDER or not item["ref"]:
                continue
            order = self._repo.broker_order_by_no(item["ref"], "", account, instrument=item["instrument"])
            if order is None or order.intent_id is None:
                continue
            self._repo.resolve_recon_item(item["item_id"], "AUTO_ADOPTED", "reconciler", now)
            self._repo.audit(now, "RECON_ITEM_AUTO_RESOLVED", "reconciler", item["item_id"],
                             {"order_no": item["ref"], "intent_id": order.intent_id, "instrument": item["instrument"]})
            closed.append(item["item_id"])
        return closed

    def _close_matched_fill_items(self, account: str, now: datetime,  # type: ignore[override]
                                  totals: dict) -> list[str]:  # type: ignore[type-arg]
        closed: list[str] = []
        for item in self._repo.open_recon_items(account):
            if item["kind"] not in (ItemKind.MISSING_FILL, ItemKind.EXCESS_FILL):
                continue
            try:
                detail = json.loads(item["detail"] or "{}")
                order = self._repo.broker_order(detail["order_key"])
                if order is None:
                    continue
                broker_total = totals.get((order.instrument, order.order_no))
                if broker_total is None:
                    broker_total = Decimal(detail["broker_total"]) if detail.get("broker_total") else None
            except (ValueError, KeyError, ArithmeticError):
                continue
            if broker_total is None or order.filled_quantity != broker_total:
                continue
            self._repo.resolve_recon_item(item["item_id"], "AUTO_MATCHED", "reconciler", now)
            self._repo.audit(now, "RECON_ITEM_AUTO_RESOLVED", "reconciler", item["item_id"],
                             {"order_key": order.order_key, "broker_total": str(broker_total)})
            closed.append(item["item_id"])
        return closed

    # --- 대조 뒤 기록 -----------------------------------------------------------------------
    def run(self, account: str, trigger: str) -> ReconcileResult:
        res = super().run(account, trigger)
        if res.result != "QUERY_FAILED":
            self._record_assets(account, trigger, res)
        return res

    def _record_assets(self, account: str, trigger: str, res: ReconcileResult) -> None:
        balances = self._balances.get(account, {})
        bases = {base: inst for inst, (base, _q) in self._universe.items()}
        positions = self._repo.positions(account)
        exp = self._expected(account)
        rows = []
        for asset in sorted(set(balances) | set(bases) | {self._quote}):
            free, locked = balances.get(asset, (_ZERO, _ZERO))
            if asset in bases:
                internal: str | None = str(positions.get(bases[asset]).quantity if bases[asset] in positions else _ZERO)
            elif asset == self._quote:
                internal = None if exp is None else str(exp)
            else:
                internal = None
            rows.append((asset, internal, free, locked, asset in bases or asset == self._quote))
        now = self._clock.now()
        scope_out = {a: str(f + lk) for a, _i, f, lk, inside in rows if not inside}
        with self._repo.tx():
            self._repo.insert_recon_assets(res.recon_id, account, rows)
            if trigger in (STARTUP, DAY_BOUNDARY):
                self._repo.audit(now, "ASSET_SNAPSHOT", "reconciler", account,
                                 {"trigger": trigger, "recon_id": res.recon_id, "assets": scope_out})
        if trigger in (STARTUP, DAY_BOUNDARY):
            self._scope_alerted[account] = scope_out
            return
        last = self._last_snapshot(account)
        if last is None or scope_out == last or scope_out == self._scope_alerted.get(account):
            return
        changed = sorted(a for a in set(last) | set(scope_out) if last.get(a) != scope_out.get(a))
        self._scope_alerted[account] = scope_out
        self._alerts.alert("INFO", "범위 밖 자산 변화(기록만, 대조 불일치 아님): "
                           + ", ".join(f"{a} {last.get(a, '0')}→{scope_out.get(a, '0')}" for a in changed),
                           res.recon_id)

    def _last_snapshot(self, account: str) -> dict[str, str] | None:
        rows = self._repo.select("SELECT summary FROM audit_event WHERE kind='ASSET_SNAPSHOT' AND ref=? "
                                 "ORDER BY seq DESC LIMIT 1", (account,))
        return json.loads(rows[0]["summary"]).get("assets") if rows else None


# --- 리셋 확인 (D-099) ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ResetReport:
    resolved_items: list[str]
    quote_baseline: Decimal
    positions: dict[str, str] = field(default_factory=dict)


def confirm_reset(repo: Repository, clock: Clock, broker: BrokerAdapter, account: str, *,
                  universe: Mapping[str, tuple[str, str]], quote_asset: str,
                  set_baseline: Callable[[str, Decimal, datetime], None], operator: str) -> ResetReport:
    """테스트넷 리셋 확인 (사람이 실행, D-099): 기준선 재설정 + 감사 기록 + PAPER 기간 재시작 표시.

    - 활성 의도(결과 불명 포함)·열린 내부 주문이 있으면 거부 — 먼저 해소한다.
    - 열린 대조 항목을 RESET_CONFIRMED로 해소, 유니버스 기초 자산 포지션 = 거래소 free + locked, 전략 소유권은
      "리셋 확인"으로 해제하고 남은 보유는 수동 보유(manual), USDT 기준선 = 거래소 USDT free + locked.
    - K4는 풀지 않는다 — 다음 대조 일치 뒤 사용자 재개."""
    active = [i for i in repo.intents() if i.account == account and not i.state.is_terminal]
    if active:
        raise ResetError(f"활성 의도 {len(active)}건 — 결과 불명·미체결을 먼저 해소한 뒤 리셋 확인")
    if repo.open_orders(account):
        raise ResetError("열린 내부 주문이 있음 — 먼저 해소한 뒤 리셋 확인")
    spot: SpotOrderQueries = broker  # type: ignore[assignment]
    balances = spot.query_balances(account)
    now = clock.now()
    resolved: list[str] = []
    positions: dict[str, str] = {}
    quote_total = SpotReconciler._total(balances, quote_asset)
    with repo.tx():
        for item in repo.open_recon_items(account):
            repo.resolve_recon_item(item["item_id"], "RESET_CONFIRMED", operator, now)
            resolved.append(item["item_id"])
        for inst, (base, _q) in sorted(universe.items()):
            qty = SpotReconciler._total(balances, base)
            own = repo.active_ownership(account, inst)
            if own is not None and (own.owner != MANUAL_OWNER or qty == 0):
                repo.set_ownership_state(own.ownership_id, "RELEASED", RESET_RELEASE_REASON, now)
                own = None
            repo.set_position(account, inst, qty, _ZERO)
            if qty > 0 and own is None:
                repo.insert_ownership(account, inst, MANUAL_OWNER, None, now)
                new_own = repo.active_ownership(account, inst)
                if new_own is not None:
                    repo.set_ownership_state(new_own.ownership_id, "CONFIRMED", None)
            positions[inst] = str(qty)
        set_baseline(account, quote_total, now)
        repo.audit(now, "TESTNET_RESET_CONFIRMED", operator, account,
                   {"resolved_items": resolved, "positions": positions, "quote_baseline": str(quote_total),
                    "basis": "D-099 리셋 확인 — 자동 기준선 재설정 없음, 사람 확인"})
        repo.audit(now, "PAPER_PERIOD_RESTART", operator, account,
                   {"at": now.isoformat(), "reason": "테스트넷 리셋 — PAPER 기간은 이 시점부터 다시 계산 (A-062①)"})
    return ResetReport(resolved, quote_total, positions)


# --- 대조 시점 (D-109) ----------------------------------------------------------------------------

class SpotReconcileSchedule:
    """M2 대조 시점: 시작 1회, 상시 주기(기본 60초), 사용자 스트림 재연결 후, 일 경계(UTC 00:00) 뒤 1회.

    일 경계는 시작 뒤 처음 맞는 거래일 변경마다 한 번(KST 자정 무관). 일 경계 대조가 그 시점의 주기 대조를 겸한다."""

    def __init__(self, period: timedelta = timedelta(seconds=60), market_time: MarketTime = M2_TIME,
                 reference_time: time = time(0, 0)) -> None:
        if period <= timedelta(0):
            raise ValueError("period는 0보다 커야 한다")
        self._period = period
        self._mt = market_time
        self._ref = reference_time
        self._last: datetime | None = None
        self.day: date | None = None   # 마지막 시작·일 경계 대조의 거래일

    def due(self, now: datetime, *, stream_reconnected: bool = False) -> list[str]:
        if self._last is None:
            return [STARTUP]
        if self.day is not None and self._mt.trade_date(now, self._ref) > self.day:
            return [DAY_BOUNDARY]
        if stream_reconnected:
            return [STREAM_RECONNECT]
        if now - self._last >= self._period:
            return [PERIODIC]
        return []

    def mark(self, trigger: str, now: datetime) -> None:
        self._last = now
        if trigger in (STARTUP, DAY_BOUNDARY):
            self.day = self._mt.trade_date(now, self._ref)


class SpotReconcileCycle:
    """시점표에 따라 대조를 돌린다. 일 경계 대조 뒤 on_day_close(마감한 거래일, 결과) — 일일 확정·백업(호출자)."""

    def __init__(self, reconciler: Reconciler, account: str, schedule: SpotReconcileSchedule,
                 on_day_close: Callable[[str, ReconcileResult], None] | None = None) -> None:
        self._rec = reconciler
        self._account = account
        self._schedule = schedule
        self._on_day_close = on_day_close

    def tick(self, *, stream_reconnected: bool = False) -> list[str]:
        now = self._rec._clock.now()   # noqa: SLF001 — 같은 시계를 쓴다
        triggers = self._schedule.due(now, stream_reconnected=stream_reconnected)
        for t in triggers:
            closing_day = self._schedule.day
            res = self._rec.run(self._account, t)
            self._schedule.mark(t, now)
            if t == DAY_BOUNDARY and self._on_day_close is not None and closing_day is not None:
                self._on_day_close(closing_day.isoformat(), res)
        return triggers
