"""판정·실행 엔진 (T-020, 03 D-038 v0.5·D-092·§3·§6.6·§6.7).

판정·실행 스레드 하나가 `tick()`을 주기적으로 부른다. 한 tick 안에서 순서대로:
1. 운영 명령(명령 테이블 = 요청 큐, KILL 먼저) — 1초 안 처리 (loop_seconds ≤ 1 권장)
2. 실시간 통보 반영 (어댑터 큐 → 실행기)
3. 결과 불명 해소 한 단계 (의도별 다음 조회 시각이 지난 것만, D-092)
4. 일일 기준 시각: 기준 금액 고정·일일 보고, 일일 손실 L4 → K2
5. 주기 대조 (장중 주기·실시간 재접속 후 요청)
6. 예정 판정 (달력상 거래일, 예정 시각 + 유예 안, K8 등 전역 정지 중이면 건너뜀) → 게이트 → 실행기
7. 실행 정책: CAP-ORD-03을 선언한 전략의 미체결 주문이 정해진 시간을 넘으면 취소 요청(게이트 경유)
입출력 스레드(실시간 수신·텔레그램·시세 조회·시계 점검)는 큐·저장소에만 쓰고 주문하지 않는다.

시작(start): 실행기 재시작 정리(recover_on_start) → K8 발동 → 예수금 조회 → 복구 판정(대조 포함).
K8 해제는 사용자 재개(운영 명령)로만 한다 (01 §2.11, CLAUDE.md §8-9).
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from trading_system.broker.base import BrokerAdapter, BrokerQueryError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import KST, Clock, from_iso, last_kst_time_at_or_before
from trading_system.domain.enums import GateResult, HaltType, IntentState, RequestSource, RequestType, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.models import GateRequest
from trading_system.domain.sinks import AlertSink
from trading_system.domain.strategy import PositionView, evaluation_id_of
from trading_system.evaluation.runner import run_evaluation
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.execution.resolver import ResolveProgress, UnknownResolver
from trading_system.gate.gate import Gate
from trading_system.halt.manager import HaltManager
from trading_system.marketdata.store import MarketDataStore
from trading_system.monitoring.watchers import AlertWatcher, DailyRiskTracker
from trading_system.ops.commands import OpsProcessor
from trading_system.reconciliation.reconciler import Reconciler, RecoveryChecker, RecoveryReport
from trading_system.reporting.daily import DailyReporter
from trading_system.storage.repository import Repository
from trading_system.strategy.base import Strategy

EVAL_GRACE = timedelta(minutes=5)       # 예정 시각을 이만큼 넘기면 그 판정은 건너뛴다(늦은 판정 금지, DP-21)
GLOBAL_BLOCKS = {"K1", "K5", "K6", "K7", "K8"}   # 전역 정지 중에는 판정 자체를 하지 않는다(어차피 위험 확대 거부)


@dataclass
class LoadedStrategy:
    strategy: Strategy
    params: dict[str, object]
    eval_times: tuple[time, ...]
    unfilled_minutes: int | None = None   # CAP-ORD-03 선언 + 파라미터 unfilled_max_minutes 일 때만

    @property
    def owner(self) -> str:
        return self.strategy.spec.owner


class AccountState:
    """게이트 입력 AccountFacts의 실운영 구현: 일일 리스크(추적기) + 예수금 캐시(조회로 갱신)."""

    def __init__(self, tracker: DailyRiskTracker) -> None:
        self.tracker = tracker
        self._cash: dict = {}
        self._lock = threading.Lock()

    def daily_risk(self, account: str):  # type: ignore[no-untyped-def]
        return self.tracker.daily_risk(account)

    def cash(self, account: str):  # type: ignore[no-untyped-def]
        with self._lock:
            return self._cash.get(account)

    def set_cash(self, account: str, view) -> None:  # type: ignore[no-untyped-def]
        with self._lock:
            self._cash[account] = view
        self.tracker.update_cash(account, view.deposit)


@dataclass
class Engine:
    repo: Repository
    clock: Clock
    broker: BrokerAdapter
    store: MarketDataStore
    gate: Gate
    executor: Executor
    halts: HaltManager
    resolver: UnknownResolver
    reconciler: Reconciler
    recovery: RecoveryChecker
    ops: OpsProcessor
    registry: StrategyRegistry
    account_state: AccountState
    reporter: DailyReporter
    watcher: AlertWatcher | None
    alerts: AlertSink
    account: str
    limits: Callable[[], AccountLimits | None]
    strategies: list[LoadedStrategy]
    reconcile_every: timedelta = timedelta(minutes=1)
    eval_grace: timedelta = EVAL_GRACE
    on_tick: list[Callable[[datetime], None]] = field(default_factory=list)
    # 상태
    progress: dict[str, ResolveProgress] = field(default_factory=dict)
    evaluated: set[str] = field(default_factory=set)
    _day_start: datetime | None = None
    _last_reconcile: datetime | None = None
    _reconcile_requested: threading.Event = field(default_factory=threading.Event)
    errors: list[str] = field(default_factory=list)
    _calls: "queue.Queue[Callable[[], object]]" = field(default_factory=queue.Queue)

    # --- 시작 -------------------------------------------------------------------------
    def start(self) -> RecoveryReport:
        unknown = self.executor.recover_on_start()
        self.halts.on_start()
        self._refresh_cash()
        rep = self.recovery.check(self.account)
        self._last_reconcile = self.clock.now()
        self.alerts.alert("WARNING" if not rep.ok else "INFO",
                          f"시작: 결과 불명 {len(unknown)}건, 복구 판정 {'통과' if rep.ok else '실패'} — "
                          f"K8 재개는 사용자 명령으로 ({'; '.join(rep.failures) or '조건 충족'})", None)
        return rep

    def call_soon(self, fn: Callable[[], object]) -> None:
        """다른 스레드가 판정·실행 스레드에서 실행할 일을 맡긴다(정지 발동·조건 보고 등). 저장소 연결은 스레드마다 따로."""
        self._calls.put(fn)

    def _drain_calls(self) -> None:
        while True:
            try:
                fn = self._calls.get_nowait()
            except queue.Empty:
                return
            fn()

    def request_reconcile(self) -> None:
        """다른 스레드(실시간 재접속 등)가 부른다. 다음 tick에서 대조."""
        self._reconcile_requested.set()

    # --- 한 주기 ---------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock.now()
        steps: list[tuple[str, Callable[[], object]]] = [
            ("calls", self._drain_calls), ("ops", self._ops), ("events", self.executor.process_events),
            ("resolve", lambda: self._resolve_step(now)), ("daily", lambda: self._daily(now)),
            ("reconcile", lambda: self._reconcile_if_due(now)), ("evaluate", lambda: self._evaluate_due(now)),
            ("exec_policy", lambda: self._exec_policy(now))]
        for name, step in steps:
            try:
                step()
            except ExecutionBlocked:
                pass  # 실행기 차단(K7 등)은 이미 정지·알림됨
            except Exception as exc:  # noqa: BLE001 — 한 단계 오류로 루프를 멈추지 않는다(감시는 계속)
                self.errors.append(f"{name}: {type(exc).__name__}: {exc} | {traceback.format_exc(limit=5)}")
                del self.errors[:-50]
                self.alerts.alert("EMERGENCY", f"엔진 단계 오류 {name}: {type(exc).__name__} — K8, 대조 후 재개", None)
                self.halts.activate(HaltType.K8, "engine", f"엔진 단계 오류 {name}: {type(exc).__name__}", None)
        for hook in self.on_tick:
            hook(now)

    def _ops(self) -> None:
        self.ops.process_pending()

    # 결과 불명: 단계형 해소 (D-092) ----------------------------------------------------
    def _resolve_step(self, now: datetime) -> None:
        live = {i.intent_id for i in self.repo.intents_in_state(IntentState.UNKNOWN) if i.account == self.account}
        for gone in set(self.progress) - live:
            self.progress.pop(gone)
        for iid in sorted(live - set(self.progress)):
            self.progress[iid] = self.resolver.begin(iid)
        for iid, p in sorted(self.progress.items(), key=lambda kv: kv[1].next_at):
            if self.clock.now() < p.next_at:
                continue
            if self.resolver.step(p) is not None:
                self.progress.pop(iid, None)

    # 일일 ---------------------------------------------------------------------------
    def _daily(self, now: datetime) -> None:
        lim = self.limits()
        if lim is None:
            return
        start = last_kst_time_at_or_before(now, lim.daily_reference_time)
        if self._day_start is None or start > self._day_start:
            first = self._day_start is None
            self._day_start = start
            cash = self.account_state.cash(self.account)
            base = self.account_state.tracker.start_day(self.account, cash.deposit) if cash is not None else None
            if base is None:
                self.alerts.alert("WARNING", "일일 기준 금액을 정할 수 없음(예수금·보유 시세 없음) — 위험 확대 거부", None)
            if not first:
                self.reporter.generate(self.account, now, lim.daily_reference_time, lim.version_hash())
        self.halts.check_daily_loss(self.account, self.account_state.daily_risk(self.account), lim.l4_daily_loss_ratio)
        if self.watcher is not None:
            self.watcher.scan(self.account, now)

    # 대조 ---------------------------------------------------------------------------
    def _refresh_cash(self) -> None:
        try:
            with call_priority(Priority.RECONCILE):
                self.account_state.set_cash(self.account, self.broker.query_cash(self.account))
        except BrokerQueryError as exc:
            self.alerts.alert("WARNING", f"예수금 조회 실패: {exc}", None)

    def _reconcile_if_due(self, now: datetime) -> None:
        due = self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_every
        if not (due or self._reconcile_requested.is_set()):
            return
        self._reconcile_requested.clear()
        self._last_reconcile = now
        self._refresh_cash()
        self.reconciler.run(self.account, "PERIODIC")

    # 판정 ---------------------------------------------------------------------------
    def _blocked_globally(self) -> bool:
        return bool(GLOBAL_BLOCKS & set(self.repo.active_halts(self.account)))

    def _evaluate_due(self, now: datetime) -> None:
        today = now.astimezone(KST).date()
        session = self.store.session(today)
        if session is None or not session.is_open:
            return
        for ls in self.strategies:
            for t in ls.eval_times:
                at = datetime.combine(today, t, tzinfo=KST)
                key = f"{ls.owner}|{at.isoformat()}"
                if key in self.evaluated or now < at:
                    continue
                self.evaluated.add(key)
                if now - at > self.eval_grace:
                    self._audit("EVALUATION_SKIPPED", key, {"reason": "예정 시각 + 유예 초과 (늦은 판정 금지)"})
                    continue
                if self._blocked_globally():
                    self._audit("EVALUATION_SKIPPED", key, {"reason": "전역 정지 중"})
                    continue
                up = self.registry.signal_rejection(ls.owner, RequestType.ENTRY)
                down = self.registry.signal_rejection(ls.owner, RequestType.EXIT)
                if up is not None and down is not None:   # 이 모드에서 어떤 신호도 받지 않는 상태
                    self._audit("EVALUATION_SKIPPED", key, {"reason": up})
                    continue
                self._evaluate(ls, at, now)

    def _next_eval(self, ls: LoadedStrategy, at: datetime) -> datetime | None:
        later = [datetime.combine(at.astimezone(KST).date(), t, tzinfo=KST) for t in ls.eval_times]
        later = [x for x in later if x > at]
        return min(later) if later else None

    def _evaluate(self, ls: LoadedStrategy, at: datetime, now: datetime) -> None:
        spec = ls.strategy.spec
        ev_id = evaluation_id_of(spec.strategy_id, spec.version, "M1", at.isoformat())
        owner = ls.owner

        def pos_view(inst: str) -> PositionView | None:
            p = self.repo.position(self.account, inst)
            if p.quantity <= 0:
                return None
            own = self.repo.active_ownership(self.account, inst)
            return PositionView(p.quantity, p.avg_price, 0, own is not None and own.owner == owner)

        def pending(inst: str) -> bool:
            return bool(self.repo.active_new_intents(self.account, inst, Side.BUY.value)
                        or self.repo.active_new_intents(self.account, inst, Side.SELL.value))

        daily = self.account_state.daily_risk(self.account)
        base = daily.base_amount if daily is not None else Decimal("0")
        insts = sorted(set(spec.universe) & set(self._known_instruments())) if spec.universe_method == "U-A" \
            else sorted(self._known_instruments())
        snap = self.store.snapshot(ev_id, "M1", self.account, now, base, insts, pos_view, pending,
                                   self._next_eval(ls, at),
                                   history=lambda inst: self.repo.strategy_history(self.account, owner, inst))
        lots = {i: info.lot_size for i in insts if (info := self.store.instrument(i)) is not None}
        result = run_evaluation(ls.strategy, snap, ls.params, lots)
        self._audit("EVALUATION", ev_id, {"strategy": owner, "signals": len(result.signals),
                                          "dropped": len(result.dropped), "error": result.error})
        if result.error:
            self.alerts.alert("WARNING", f"전략 판정 오류 {owner}: {result.error}", ev_id)
        for s in result.signals:
            d = self.gate.decide(GateRequest.from_signal(s))
            if d.result is GateResult.APPROVED and d.approved is not None:
                self.executor.submit(d.approved)

    def _known_instruments(self) -> list[str]:
        return [i for i in self.store._data]  # noqa: SLF001 — 등록된 종목 = 배포 설정의 종목

    # 실행 정책 (CAP-ORD-03) --------------------------------------------------------------
    def _exec_policy(self, now: datetime) -> None:
        for ls in self.strategies:
            if ls.unfilled_minutes is None:
                continue
            for order in self.repo.open_orders(self.account):
                if order.intent_id is None:
                    continue
                intent = self.repo.intent(order.intent_id)
                if intent.owner != ls.owner or intent.request_type not in ("ENTRY", "INCREASE", "REDUCE", "EXIT"):
                    continue
                attempt = self.repo.attempt_for(intent.intent_id)
                if attempt is None or now - from_iso(attempt["created_at"]) < timedelta(minutes=ls.unfilled_minutes):
                    continue  # 미체결 시간 = 전송 시각부터
                req = GateRequest.cancel(RequestSource.EXEC_POLICY, self.account, order.instrument,
                                         Side(order.side), order.order_key,
                                         idem_key=f"exec:{order.order_key}:unfilled", owner=ls.owner)
                d = self.gate.decide(req)
                if d.result is GateResult.APPROVED and d.approved is not None:
                    self.executor.submit(d.approved)

    def _audit(self, kind: str, ref: str, summary: dict) -> None:
        with self.repo.tx():
            self.repo.audit(self.clock.now(), kind, "engine", ref, summary)

    # --- 스레드 ---------------------------------------------------------------------------
    def run(self, stop: threading.Event, loop_seconds: float) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(loop_seconds)
