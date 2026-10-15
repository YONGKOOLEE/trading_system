"""시장 프로파일 (CP-036 D-201, T-045): 엔진은 하나, 시장마다 다른 것만 프로파일이 바꾼다.

바꾸는 것 (D-201 (1)~(7)): 시장 시각(판정 시각·재판정 방지·다음 판정·일일 기준·보고 시각 해석), 세션 공급, 대조 일정,
판정 입력 구성, 게이트 전 요청 변환 훅, 계좌 사실(현금·일일 기준 금액), 판정 ID·스냅샷의 시장 코드.
- MarketProfile (M1) = 변경 전 엔진 동작 그대로 (V-152). 메서드는 엔진의 기존 단계 함수를 그대로 부른다.
- SpotProfile (M2) = UTC 시각(D-108)·24/7 세션·SpotReconcileCycle(시작·60초·스트림 재연결·UTC 일 경계, D-109)·
  D-207 판정 입력·M2 주문 유형·가격(apply_spot_terms, D-104·D-120)과 dust 선별(D-102)·SpotAccountFacts(D-103·D-108).
엔진 본문에 시장 분기를 흩뿌리지 않는다. 주문은 엔진(판정·실행 스레드)만 낸다 — 프로파일은 요청을 만들거나 버릴 뿐
게이트를 건너뛰지 않는다(CP-036 §7).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, last_kst_time_at_or_before
from trading_system.domain.enums import OrderType, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import SessionInfo
from trading_system.domain.market_time import M2_TIME, Market, daily_bar_is_final, session_247
from trading_system.domain.models import GateRequest
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.domain.trading_rules import (
    DustVerdict, SpotOrderPolicy, SymbolRules, apply_spot_terms, market_qty_step,
)
from trading_system.execution.dust import DustHandler
from trading_system.gate.ports import MarketFacts, SymbolRulesSource
from trading_system.monitoring.spot_cash import QuoteBalance, SpotAccountFacts
from trading_system.reconciliation.reconciler import ReconcileResult
from trading_system.reconciliation.spot import STARTUP, SpotReconcileCycle, SpotReconcileSchedule

if TYPE_CHECKING:
    from .engine import Engine, LoadedStrategy, _UbInput

ORDER_TERMS_UNAVAILABLE = "ORDER_TERMS_UNAVAILABLE"   # M2: 주문 유형·가격을 정할 수 없어 의도를 만들지 않음 (V-281 (e))


class SpotFacts(MarketFacts, SymbolRulesSource, Protocol):
    """M2 시세·규칙·1d 봉 공급 계약 (marketdata.spot_facts.SpotFactsStore, PAPER 공급자는 T-048)."""

    def daily_bars(self, symbol: str) -> tuple[Bar, ...]: ...


class MarketProfile:
    """M1(KRX) 프로파일 — 변경 전 엔진 동작 그대로 (D-201 "M1 프로파일은 현재 객체", V-152)."""

    market: Market = Market.M1
    tz: tzinfo = KST

    def attach(self, engine: Engine) -> None:
        self._engine = engine

    def last_at_or_before(self, at: datetime, t: time) -> datetime:
        return last_kst_time_at_or_before(at, t)

    def session(self, trade_date: date) -> SessionInfo | None:
        return self._engine.store.session(trade_date)

    def refresh_cash(self) -> None:
        self._engine._refresh_cash()   # noqa: SLF001 — 엔진의 M1 단계 함수

    def start_day_base(self) -> bool:
        return self._engine._cash_day_base()   # noqa: SLF001

    def day_report(self, now: datetime, lim: AccountLimits) -> None:
        """일일 기준 시각이 바뀔 때 방금 끝난 거래일 보고서."""
        e = self._engine
        e.reporter.generate(e.account, now, lim.daily_reference_time, lim.version_hash())

    def reconcile_step(self, now: datetime) -> None:
        self._engine._reconcile_if_due(now)   # noqa: SLF001

    def after_start(self, now: datetime) -> None:
        return None

    def evaluation_input(self, ls: LoadedStrategy, ev_id: str, at: datetime, now: datetime, base: Decimal,
                         ub: _UbInput | None) -> tuple[EvaluationSnapshot, dict[str, Decimal]]:
        return self._engine._store_input(ls, ev_id, at, now, base, ub)   # noqa: SLF001

    def prepare(self, req: GateRequest) -> GateRequest | None:
        """게이트 전 요청 변환 (D-201 (5)). M1 = 항등."""
        return req


class SpotProfile(MarketProfile):
    """M2(크립토 현물, 24/7, UTC) 프로파일 (D-201·D-207·D-208 이전 규칙은 T-046에서 통일)."""

    market = Market.M2
    tz = UTC

    def __init__(self, *, facts: SpotFacts, pairs: Mapping[str, tuple[str, str]], quote_asset: str,
                 account_facts: SpotAccountFacts, order_policy: SpotOrderPolicy, dust: DustHandler,
                 schedule: SpotReconcileSchedule) -> None:
        self.facts = facts
        self.pairs = dict(pairs)
        self.quote_asset = quote_asset
        self.account_facts = account_facts
        self.policy = order_policy
        self.dust = dust
        self.schedule = schedule
        self.cycle: SpotReconcileCycle | None = None   # 조립 지점이 대조기와 함께 연결

    # --- 시각·세션 (D-108) -------------------------------------------------------------------
    def last_at_or_before(self, at: datetime, t: time) -> datetime:
        return M2_TIME.last_at_or_before(at, t)

    def session(self, trade_date: date) -> SessionInfo | None:
        return session_247(trade_date)

    # --- 현금·일일 기준 금액 (D-103·D-108) --------------------------------------------------------
    def refresh_cash(self) -> None:
        e = self._engine
        try:
            with call_priority(Priority.RECONCILE):
                balances = e.broker.query_balances(e.account)  # type: ignore[attr-defined]
        except BrokerQueryError as exc:
            e.alerts.alert("WARNING", f"잔고 조회 실패: {exc}", None)
            return
        self.observe_balances(e.account, balances)

    def observe_balances(self, account: str, balances: Mapping[str, tuple[Decimal, Decimal]]) -> None:
        """거래소 잔고 조회 결과(대조·시작) → 계좌 사실의 호가 자산 잔고 (D-103: free + locked)."""
        free, locked = balances.get(self.quote_asset, (Decimal("0"), Decimal("0")))
        self.account_facts.update_balance(account, QuoteBalance(free, locked, self._engine.clock.now()))

    def start_day_base(self) -> bool:
        """일 경계 뒤 기준 금액: 경계 이후 조회한 잔고만 쓴다(spot_cash [제안](4)). 없으면 한 번 조회해서 다시."""
        a = self._engine.account
        if self.account_facts.start_day(a) is not None:
            return True
        self.refresh_cash()
        return self.account_facts.start_day(a) is not None

    def day_report(self, now: datetime, lim: AccountLimits) -> None:
        return None   # M2: 일 경계 대조(DAY_BOUNDARY) 뒤 day_closed가 마감 거래일 보고서를 만든다 (D-201 (3))

    def day_closed(self, trade_day: str, res: ReconcileResult) -> None:
        e = self._engine
        lim = e.limits()
        if lim is not None:
            e.reporter.generate(e.account, e.clock.now(), lim.daily_reference_time, lim.version_hash())

    # --- 대조 (D-109) -------------------------------------------------------------------------
    def reconcile_step(self, now: datetime) -> None:
        """시작·60초 주기·스트림 재연결(엔진 대조 요청)·UTC 일 경계. 대조 뒤 매도 잔량 dust 점검(D-102)."""
        e = self._engine
        requested = e._reconcile_requested.is_set()   # noqa: SLF001
        done = self.cycle is not None and self.cycle.tick(stream_reconnected=requested)
        if requested:
            # 대조가 예외 없이 끝난 뒤에만 지운다 — 대조 중 오류면 재연결 대조 요청이 다음 tick에 남는다(검토 지적, D-109)
            e._reconcile_requested.clear()   # noqa: SLF001
        if done:
            e._last_reconcile = now   # noqa: SLF001
            self.review_dust(sorted(self.pairs))

    def after_start(self, now: datetime) -> None:
        self.schedule.mark(STARTUP, now)   # 시작 대조는 복구 판정(RecoveryChecker)이 이미 했다

    def review_dust(self, instruments: list[str]) -> None:
        for inst in instruments:
            self.dust.review(self._engine.account, inst)

    # --- 판정 입력 (D-207 최소) ------------------------------------------------------------------
    def evaluation_input(self, ls: LoadedStrategy, ev_id: str, at: datetime, now: datetime, base: Decimal,
                         ub: _UbInput | None) -> tuple[EvaluationSnapshot, dict[str, Decimal]]:
        e = self._engine
        spec = ls.strategy.spec
        insts = sorted(set(spec.universe) & set(self.pairs)) if spec.universe_method == "U-A" else sorted(self.pairs)
        self.review_dust(insts)   # 매도 뒤 남은 dust는 보유 없음 취급 전에 소유권부터 정리 (D-102)
        views = tuple(self._view(i, ls.owner, now) for i in insts)
        snap = EvaluationSnapshot(ev_id, self.market.value, e.account, now, base, views, e._next_eval(ls, at))  # noqa: SLF001
        lots = {i: r.lot_step for i in insts if (r := self.facts.rules(i)) is not None}
        return snap, lots

    def _view(self, inst: str, owner: str, now: datetime) -> InstrumentView:
        e = self._engine
        rules = self.facts.rules(inst)
        bars = final_daily_bars(self.facts.daily_bars(inst), now)
        q = self.facts.quote(inst)
        statuses = self.facts.trading_status(inst)
        pending = bool(e.repo.active_new_intents(e.account, inst, Side.BUY.value)
                       or e.repo.active_new_intents(e.account, inst, Side.SELL.value))
        yesterday = M2_TIME.local(now).date() - timedelta(days=1)
        data_ok = rules is not None and statuses is not None and bool(bars) and bars[-1].at >= yesterday  # type: ignore[operator]
        return InstrumentView(inst, daily_bars=bars, last_price=q.price if q is not None and q.at <= now else None,
                              statuses=statuses or frozenset(), position=self._position(inst, owner, rules),
                              has_pending_order=pending, data_ok=data_ok)

    def _position(self, inst: str, owner: str, rules: SymbolRules | None) -> PositionView | None:
        """보유 표시 = 청산 가능 수량(실제로 보낼 매도 경로의 단위로 내림). 0·minQty 미만·확실한 dust면 보유 없음(D-102).
        owned_by_me = 소유권 소유자 비교(수동 보유는 False). 규칙을 모르면 보유 그대로(그 종목 data_ok False)."""
        e = self._engine
        p = e.repo.position(e.account, inst)
        if p.quantity <= 0:
            return None
        own = e.repo.active_ownership(e.account, inst)
        mine = own is not None and own.owner == owner
        if rules is None:
            return PositionView(p.quantity, p.avg_price, 0, mine)
        c = self.dust.check(e.account, inst)
        if c is None or c.verdict is not DustVerdict.SEND:
            return None
        return PositionView(c.quantity, p.avg_price, 0, mine)

    # --- 게이트 전 요청 변환 (D-201 (5)) -------------------------------------------------------
    def prepare(self, req: GateRequest) -> GateRequest | None:
        """신규 주문 요청에 M2 주문 유형·가격(의도 생성 시, 게이트 전)을 넣는다. 정할 수 없으면(시세·규칙 없음) 의도를
        만들지 않고 감사·알림. 청산·감액은 dust 선별 — 확실한 dust면 전송하지 않는다. 취소는 그대로."""
        if not req.request_type.is_new_order:
            return req
        e = self._engine
        terms = apply_spot_terms(req, self.facts.quote(req.instrument), e.clock.now(),
                                 self.facts.rules(req.instrument), self.policy)
        if terms is None:
            with e.repo.tx():
                e.repo.audit(e.clock.now(), ORDER_TERMS_UNAVAILABLE, "engine", req.idem_key, {
                    "instrument": req.instrument, "request_type": req.request_type.value, "side": req.side.value,
                    "owner": req.owner, "reason": "시세 또는 거래 규칙 없음 — 의도를 만들지 않음 (D-104·D-106)"})
            e.alerts.alert("WARNING", f"주문 유형·가격을 정할 수 없음 {req.instrument} {req.request_type.value} — "
                                      "의도를 만들지 않음 (시세·거래 규칙 없음)", req.idem_key)
            return None
        if self.dust.screen_exit(terms):
            return None
        return terms

    def prepare_ops(self, req: GateRequest) -> GateRequest:
        """운영 요청(/exit·/reduce, CP-036 D-211)의 게이트 전 M2 주문 조건 — 전략 신호와 같은 훅(prepare): 시세 정상 = 시장가,
        지연 = IOC 지정가(D-120), 청산이면 dust 선별. 사람이 정한 수량은 바꾸지 않는다: 보낼 경로의 수량 단위(시장가 =
        MARKET_LOT_SIZE, 0이면 LOT_SIZE — D-208 / 지정가 = LOT_SIZE)의 배수가 아니면 거부(자동 반올림 없음).
        정할 수 없거나(시세·규칙 없음) 확실한 dust면 거부. 거부는 ValueError(운영 처리기가 REJECTED로 기록)."""
        if not req.request_type.is_new_order:
            return req
        units = getattr(self.facts, "units", self.facts.rules)(req.instrument)
        if units is None:
            raise ValueError(f"{req.instrument}: 거래 규칙(수량 단위) 모름 — 운영 요청을 보내지 않음 (D-106)")
        self._check_units(req.quantity, units.lot_step)   # dust 선별(소유권 정리) 전에 먼저
        terms = self.prepare(req)
        if terms is None:
            raise ValueError(f"{req.instrument}: 주문 유형·가격을 정할 수 없음(시세·거래 규칙 없음) 또는 확실한 dust — "
                             "보내지 않음 (D-102·D-104·D-211)")
        if terms.order_type is OrderType.MARKET:
            self._check_units(terms.quantity, market_qty_step(units))
        return terms

    @staticmethod
    def _check_units(quantity: Decimal, step: Decimal) -> None:
        if step <= 0 or quantity % step != 0:
            raise ValueError(f"수량 {quantity}이 LOT_SIZE 단위 {step}의 배수가 아님 — 자동 반올림 없음, "
                             "사람이 수량을 다시 정한다 (D-211)")


def final_daily_bars(bars: tuple[Bar, ...], as_of: datetime) -> tuple[Bar, ...]:
    """as_of 시점에 확정된 1d 봉만 (D-108, V-142): 봉 시작(UTC 날짜) + 1일 ≤ as_of. 날짜형이 아닌 봉은 쓰지 않는다."""
    return tuple(b for b in bars if isinstance(b.at, date) and not isinstance(b.at, datetime)
                 and daily_bar_is_final(b.at, as_of))
