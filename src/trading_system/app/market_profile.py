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
from datetime import UTC, date, datetime, time, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, last_kst_time_at_or_before
from trading_system.domain.daily_input import DailyInput, final_daily, m2_daily_input
from trading_system.domain.enums import OrderType, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import Quote, SessionInfo
from trading_system.domain.market_time import M2_TIME, Market, session_247
from trading_system.domain.models import GateRequest
from trading_system.domain.signal_source import signal_basis_avg_price
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.domain.trading_rules import (
    DustVerdict, SpotOrderPolicy, SymbolRules, apply_spot_terms, market_qty_step,
)
from trading_system.execution.dust import DustHandler
from trading_system.gate.ports import MarketFacts, SymbolRulesSource
from trading_system.monitoring.spot_cash import QuoteBalance, SpotAccountFacts
from trading_system.reconciliation.reconciler import ReconcileResult
from trading_system.reconciliation.spot import STARTUP, SpotReconcileCycle, SpotReconcileSchedule

from .m2_minutes import MINUTE_CAPABILITY, M2MinuteInput, MinuteInput, disabled_inputs, minute_request_of

if TYPE_CHECKING:
    from .engine import Engine, LoadedStrategy, _UbInput

ORDER_TERMS_UNAVAILABLE = "ORDER_TERMS_UNAVAILABLE"   # M2: 주문 유형·가격을 정할 수 없어 의도를 만들지 않음 (V-281 (e))


class SignalPrices(Protocol):
    """[CP-039 D-248] 신호 출처 참조 시세 (marketdata/signal_feed.SignalFacts)."""

    def reference_quote(self, instrument: str) -> Quote | None: ...


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

    def minute_pending(self, ls: LoadedStrategy, at: datetime) -> frozenset[str]:
        """[CP-037 T-052] 주기 판정이 기다릴 1분봉 조회가 남은 심볼. M1은 분봉 공급이 없다(주기 판정 미개방)."""
        return frozenset()

    def prepare(self, req: GateRequest) -> GateRequest | None:
        """게이트 전 요청 변환 (D-201 (5)). M1 = 항등."""
        return req

    def evaluation_record(self, snap: EvaluationSnapshot) -> dict[str, object]:
        """판정 감사(EVALUATION)에 더할 항목. M1 = 없음(기록 불변)."""
        return {}


class SpotProfile(MarketProfile):
    """M2(크립토 현물, 24/7, UTC) 프로파일 (D-201·D-207·D-208 이전 규칙은 T-046에서 통일)."""

    market = Market.M2
    tz = UTC

    def __init__(self, *, facts: SpotFacts, pairs: Mapping[str, tuple[str, str]], quote_asset: str,
                 account_facts: SpotAccountFacts, order_policy: SpotOrderPolicy, dust: DustHandler,
                 schedule: SpotReconcileSchedule, minutes: M2MinuteInput | None = None,
                 signal: SignalPrices | None = None) -> None:
        self.facts = facts
        # [CP-039 D-248] 신호 출처 binance-public이면 실서버 참조 시세(봉은 facts에 신호 출처 봉만 들어온다 — 조립 지점).
        # None = testnet 출처(종전 동작, reference_price = 실행 출처 최근가)
        self.signal = signal
        self._avg_fallback: set[str] = set()   # [CP-039 D-252] 원장 평균으로 대신한 종목(경고 1회)
        self.minutes = minutes   # [CP-037 D-220] 1분봉 저장·판정 입력 (None = 분봉 공급 꺼짐)
        self.pairs = dict(pairs)
        self.quote_asset = quote_asset
        self.account_facts = account_facts
        self.policy = order_policy
        self.dust = dust
        self.schedule = schedule
        self.cycle: SpotReconcileCycle | None = None   # 조립 지점이 대조기와 함께 연결
        self._daily_alerted: dict[tuple[str, str], tuple] = {}   # [DP-82 (나)] (종목, 전략) → 마지막으로 알린 결측 상태

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
        if self.minutes is not None:   # [CP-037 D-220] 받은 1분봉 → minute_bar (판정·실행 스레드만 저장소에 쓴다)
            self.minutes.flush(e.repo, e.alerts, now)

    def after_start(self, now: datetime) -> None:
        self.schedule.mark(STARTUP, now)   # 시작 대조는 복구 판정(RecoveryChecker)이 이미 했다

    def review_dust(self, instruments: list[str]) -> None:
        for inst in instruments:
            self.dust.review(self._engine.account, inst)

    # --- 판정 입력 (D-207 최소) ------------------------------------------------------------------
    def _instruments(self, ls: LoadedStrategy) -> list[str]:
        spec = ls.strategy.spec
        return sorted(set(spec.universe) & set(self.pairs)) if spec.universe_method == "U-A" else sorted(self.pairs)

    def minute_pending(self, ls: LoadedStrategy, at: datetime) -> frozenset[str]:
        """[CP-037 T-052, DP-79] CAP-DAT-02 전략의 판정 시각 at 조회가 아직 끝나지 않은 심볼. 분봉 공급이 꺼져 있으면 기다릴
        조회가 없다(입력은 DISABLED)."""
        if MINUTE_CAPABILITY not in ls.strategy.spec.capabilities or self.minutes is None:
            return frozenset()
        return self.minutes.pending_fetch(self._instruments(ls), at)

    def evaluation_input(self, ls: LoadedStrategy, ev_id: str, at: datetime, now: datetime, base: Decimal,
                         ub: _UbInput | None, *, unfetched: frozenset[str] = frozenset(),
                         ) -> tuple[EvaluationSnapshot, dict[str, Decimal]]:
        e = self._engine
        spec = ls.strategy.spec
        insts = self._instruments(ls)
        self.review_dust(insts)   # 매도 뒤 남은 dust는 보유 없음 취급 전에 소유권부터 정리 (D-102)
        mins: dict[str, MinuteInput] = {}
        if MINUTE_CAPABILITY in spec.capabilities:   # [CP-037 D-220] CAP-DAT-02 선언 전략만 1분봉 입력
            mins = (self.minutes.inputs(e.repo, e.alerts, insts, at, now, ev_id, unfetched,
                                        request=minute_request_of(ls.params))   # 집계 봉·흐름 열 (CP-040 D-259·D-260)
                    if self.minutes is not None else disabled_inputs(e.repo, insts, at, now, ev_id))
        views = tuple(self._view(i, ls.owner, now, mins.get(i)) for i in insts)
        snap = EvaluationSnapshot(ev_id, self.market.value, e.account, now, base, views, e._next_eval(ls, at))  # noqa: SLF001
        lots = {i: r.lot_step for i in insts if (r := self.facts.rules(i)) is not None}
        return snap, lots

    def _view(self, inst: str, owner: str, now: datetime, minute: MinuteInput | None = None) -> InstrumentView:
        e = self._engine
        rules = self.facts.rules(inst)
        di = m2_daily_input(self.facts.daily_bars(inst), now)   # 백테스트와 같은 함수 (D-088, DP-82)
        q = self.facts.quote(inst)
        statuses = self.facts.trading_status(inst)
        pending = bool(e.repo.active_new_intents(e.account, inst, Side.BUY.value)
                       or e.repo.active_new_intents(e.account, inst, Side.SELL.value))
        # [DP-82 (가), A-126 ②] 일봉 결측은 data_ok가 아니라 daily_ok·daily_exit로 전달한다 — 판정 실행기가 진입·증액만 버리고
        # 감액·청산은 빈 날을 건너뛴 확정 봉으로 판정한다(전략에게 진입용 입력은 종전과 같게 data_ok AND daily_ok로 보인다).
        data_ok = rules is not None and statuses is not None
        position = self._position(inst, owner, rules)
        self._daily_warning(inst, owner, di, position)
        # [DP-78 — DP-75 (6) 해소, CLAUDE.md §8-3] 1분봉 누락·결측·품질·저장 실패·조회 시한 초과·공급 꺼짐(D-220)은 data_ok가
        # 아니라 minute_ok False로 전달한다(minute_bars ()). 판정 실행기가 이 종목 신규 진입·증액을 버리고, 청산·감액은 분봉 외
        # 입력(data_ok)으로만 검사한다 — 분봉이 무효라고 위험 축소 신호까지 조용히 막지 않는다.
        last = q.price if q is not None and q.at <= now else None
        return InstrumentView(inst, daily_bars=di.bars, minute_bars=minute.bars if minute is not None else (),
                              last_price=last,
                              statuses=statuses or frozenset(), position=position,
                              has_pending_order=pending, data_ok=data_ok,
                              minute_ok=minute.ok if minute is not None else True,
                              agg_bars=minute.agg_bars if minute is not None else (),
                              agg_missing=minute.agg_missing if minute is not None else (),
                              daily_ok=di.ok, daily_exit=di.daily_exit,
                              reference_price=last if self.signal is None else self._reference(inst, now))

    def _reference(self, inst: str, now: datetime) -> Decimal | None:
        """[CP-039 D-248] 신호 출처 참조 시세 — 미래 시각이거나 시세 신선도(A-064 60초)를 넘겨 늙었으면 None."""
        assert self.signal is not None
        r = self.signal.reference_quote(inst)
        if r is None or r.at > now or now - r.at > self.policy.quote_max_age or r.price <= 0:
            return None
        return r.price

    def evaluation_record(self, snap: EvaluationSnapshot) -> dict[str, object]:
        """[CP-039 D-248·D-251] 신호 출처 binance-public이면 판정 감사에 출처·판정 시점 P_s·P_x·사용 일봉 범위·지문.
        testnet 출처면 없음(기록 불변, V-326)."""
        if self.signal is None:
            return {}
        prices: dict[str, dict[str, str | None]] = {}
        daily: dict[str, dict[str, object]] = {}
        for v in snap.instruments:
            r = self.signal.reference_quote(v.instrument)
            prices[v.instrument] = {"signal": None if v.reference_price is None else str(v.reference_price),
                                    "signal_at": None if r is None else r.at.isoformat(),
                                    "exec": None if v.last_price is None else str(v.last_price)}
            bars = v.daily_bars
            daily[v.instrument] = {"count": len(bars), "first": bars[0].at.isoformat() if bars else None,
                                   "last": bars[-1].at.isoformat() if bars else None,
                                   "fingerprint": daily_fingerprint(bars)}
        return {"signal_source": "binance-public", "prices": prices, "daily": daily}

    def _daily_warning(self, inst: str, owner: str, di: DailyInput, position: PositionView | None) -> None:
        """[DP-82 (나)] 일봉 판정 입력이 불완전하면 텔레그램 경고(WARNING — 감사 ALERT를 텔레그램이 보냄) 1건, 보유 여부 포함.
        반복 억제: 같은 (종목·전략)에 대해 빠진 날짜·보유 상태가 바뀔 때만 다시 알린다. 완전해지면 회복 INFO 1건."""
        key = (inst, owner)
        if di.complete:
            if self._daily_alerted.pop(key, None) is not None:
                self._engine.alerts.alert("INFO", f"일봉 판정 입력 회복 {inst} ({owner}): 빈 날 없음 — 신규 진입 판정 재개 "
                                                  "(DP-82)", None)
            return
        if position is None:
            held = "보유 없음"
        elif position.owned_by_me:
            held = f"보유 있음(이 전략 소유, 수량 {position.quantity})"
        else:
            held = f"보유 있음(이 전략 소유 아님, 수량 {position.quantity})"
        state = (di.missing, held, len(di.exit_bars) == 0)
        if self._daily_alerted.get(key) == state:
            return
        self._daily_alerted[key] = state
        gaps = ", ".join(d.isoformat() for d in di.missing[-5:]) + (f" 외 {len(di.missing) - 5}일"
                                                                    if len(di.missing) > 5 else "")
        what = "확정 일봉 없음" if not di.exit_bars else f"빈 날 {gaps or '없음'}"
        self._engine.alerts.alert("WARNING", f"일봉 판정 입력 불완전 {inst} ({owner}): {what} — {held}. 신규 진입·증액은 빈 날 뒤 "
                                             "연속 구간(어제 봉까지)으로만 판정 — 전략 창이 모자라면 진입 없음, 어제 봉이 없으면 진입 차단. "
                                             "감액·청산 판정은 빈 날을 건너뛴 확정 봉으로 계속 (DP-82, A-126 ② — 해석 확인 대기)", None)

    def sellable_position(self, inst: str, owner: str) -> PositionView | None:
        """[CP-040 T-067] 손절 감시의 청산 수량 = 판정 입력과 같은 보유 표시(청산 가능 수량, dust 제외 — D-102).
        거래 규칙을 모르면 None(수량 단위를 모르면 보내지 않음 — 불확실하면 주문하지 않음)."""
        rules = self.facts.rules(inst)
        if rules is None:
            return None
        return self._position(inst, owner, rules)

    def _position(self, inst: str, owner: str, rules: SymbolRules | None) -> PositionView | None:
        """보유 표시 = 청산 가능 수량(실제로 보낼 매도 경로의 단위로 내림). 0·minQty 미만·확실한 dust면 보유 없음(D-102).
        owned_by_me = 소유권 소유자 비교(수동 보유는 False). 규칙을 모르면 보유 그대로(그 종목 data_ok False)."""
        e = self._engine
        p = e.repo.position(e.account, inst)
        if p.quantity <= 0:
            return None
        own = e.repo.active_ownership(e.account, inst)
        mine = own is not None and own.owner == owner
        avg = p.avg_price if self.signal is None else self._signal_avg(inst, p.avg_price)
        if rules is None:
            return PositionView(p.quantity, avg, 0, mine)
        c = self.dust.check(e.account, inst)
        if c is None or c.verdict is not DustVerdict.SEND:
            return None
        return PositionView(c.quantity, avg, 0, mine)

    def _signal_avg(self, inst: str, ledger: Decimal) -> Decimal:
        """[CP-039 D-252, A-115 Q-5 (가)] 전략 상태 입력 평균 매입가 = 체결 시각 P_s를 체결 수량으로 가중한 값(원장은 그대로).
        지금 보유를 만든 체결 가운데 P_s가 없는 것이 있으면 원장 평균(테스트넷 체결가)으로 대신하고 경고 1회 [임시 판단 —
        청산 판정을 막지 않기 위해, 불확실성은 알림으로]."""
        e = self._engine
        rows = e.repo.fill_signal_prices(e.account, inst)
        avg = signal_basis_avg_price((r["side"], Decimal(r["quantity"]),
                                      None if r["ref_price"] is None else Decimal(r["ref_price"])) for r in rows)
        if avg is not None:
            self._avg_fallback.discard(inst)
            return avg
        if inst not in self._avg_fallback:
            self._avg_fallback.add(inst)
            e.alerts.alert("WARNING", f"신호 기준 평균 매입가를 정할 수 없음 {inst} (체결 시각 신호 출처 시세 없음 또는 신호 출처 "
                                      "기록 전 보유) — 원장 평균(테스트넷 체결가)으로 판정 (CP-039 D-252)", None)
        return ledger

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


def daily_fingerprint(bars: tuple[Bar, ...]) -> str:
    """[CP-039 D-248] 판정에 쓴 일봉 창의 지문(16자) — 날짜·OHLCV 문자열의 SHA-256 앞부분. 재수집본과 대조할 때 쓴다."""
    import hashlib
    text = "|".join(f"{b.at.isoformat()},{b.open},{b.high},{b.low},{b.close},{b.volume}" for b in bars)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def final_daily_bars(bars: tuple[Bar, ...], as_of: datetime) -> tuple[Bar, ...]:
    """as_of 시점에 확정된 1d 봉만 (D-108, V-142) — domain.daily_input.final_daily와 같음(판정 입력은 m2_daily_input)."""
    return final_daily(bars, as_of)
