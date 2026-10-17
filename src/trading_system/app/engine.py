"""판정·실행 엔진 (T-020, 03 D-038 v0.5·D-092·§3·§6.6·§6.7).

판정·실행 스레드 하나가 `tick()`을 주기적으로 부른다. 한 tick 안에서 순서대로:
1. 운영 명령(명령 테이블 = 요청 큐, KILL 먼저) — 1초 안 처리 (loop_seconds ≤ 1 권장). KILL의 K1 행은 감시 스레드가
   먼저 기록한다(fast_kill, RV-22) — 이 스레드가 주문 응답을 기다리는 동안에도 게이트에 보이게
2. 실시간 통보 반영 (어댑터 큐 → 실행기)
3. 결과 불명 해소 한 단계 (의도별 다음 조회 시각이 지난 것만, D-092)
4. 일일 기준 시각: 기준 금액 고정·일일 보고, 일일 손실 L4 → K2
5. 대조 (장중 주기·장 마감 후 1회·요청 시 — RV-25)
6. 예정 판정 (달력상 거래일, 예정 시각 + 유예 안, K8 등 전역 정지 중이면 건너뜀) → 게이트 → 실행기
   [CP-037 D-219] CAP-TRG-02 전략(M2만)은 주문 창 안 N분 격자 주기 판정(_evaluate_periodic — 분봉 조회 대기·시한, DP-79)
7. 실행 정책: CAP-ORD-03b(취소, CP-032 D-175)를 선언한 전략의 미체결 주문이 정해진 시간을 넘으면 취소 요청(게이트 경유)
입출력·감시 스레드(실시간 수신·텔레그램·시세 조회·감시)는 큐·저장소에만 쓰고 주문하지 않는다.
단계·훅 오류는 루프 밖으로 내보내지 않는다(기록 실패 시 로컬 파일, RV-06). last_progress = 감시 스레드의 멈춤 판정 근거.

시작(start): 오늘 판정 완료 표시 복원(감사 기록, T-044) → 실행기 재시작 정리(recover_on_start) → K8 발동 → 예수금 조회
→ 복구 판정(대조 포함).
[CP-036 D-201] 시장 프로파일(app/market_profile): 시장 시각·세션·대조 일정·판정 입력·게이트 전 요청 변환·계좌 사실·
판정 ID 시장 코드만 시장마다 다르다. 기본 = M1 프로파일(변경 전 동작 그대로, V-152). M2는 SpotProfile(UTC·24/7).
K8 해제는 사용자 재개(운영 명령)로만 한다 (01 §2.11, CLAUDE.md §8-9).
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic

from trading_system.broker.base import BrokerAdapter, BrokerQueryError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.allocation import INDEX_KOSPI, AllocationDecision, IndexView
from trading_system.domain.clock import KST, Clock, from_iso, to_iso
from trading_system.domain.enums import GateResult, HaltType, IntentState, RequestSource, RequestType, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import CashView
from trading_system.domain.minute_series import PeriodGrid
from trading_system.domain.models import GateRequest, OrderEvent
from trading_system.domain.sinks import AlertSink
from trading_system.domain.strategy import EvaluationSnapshot, PositionView, evaluation_id_of
from trading_system.domain.universe import listed_since, select_version
from trading_system.evaluation.runner import run_evaluation
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.execution.resolver import ResolveOutcome, ResolveProgress, UnknownResolver
from trading_system.gate.gate import Gate
from trading_system.halt.manager import K5_LINK_REST, K5_LINK_THREAD, HaltManager
from trading_system.domain.investor_flow import CAPABILITY as FLOW_CAPABILITY, InvestorFlow
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.investor_flow import flow_days_of, flow_window
from trading_system.marketdata.store import MarketDataStore
from trading_system.monitoring.watchers import AlertWatcher, DailyRiskTracker
from trading_system.ops.commands import OpsProcessor
from trading_system.reconciliation.reconciler import (
    CLOSE_TRIGGER, ReconcileResult, Reconciler, RecoveryChecker, RecoveryReport,
)
from trading_system.reporting.daily import DailyReporter
from trading_system.storage.oplog import log_exception
from trading_system.storage.repository import new_id, BrokerOrderRow, Repository
from trading_system.strategy.base import Strategy

from .market_profile import MarketProfile

EVAL_GRACE = timedelta(minutes=5)       # 예정 시각을 이만큼 넘기면 그 판정은 건너뛴다(늦은 판정 금지, DP-21)
GLOBAL_BLOCKS = {"K1", "K5", "K6", "K7", "K8"}   # 전역 정지 중에는 판정 자체를 하지 않는다(어차피 위험 확대 거부)


@dataclass
class LoadedAllocation:
    """배분 규칙 (CP-034 D-199): spec(kind ALLOCATION)·decide(IndexView, params) → AllocationDecision."""

    rule: object
    params: dict[str, object]

    @property
    def owner(self) -> str:
        return self.rule.spec.owner  # type: ignore[attr-defined]


@dataclass(frozen=True)
class _UbInput:
    """U-B 전략 한 판정의 유니버스 입력 (CP-034 D-198)."""

    instruments: list[str]                    # 스냅샷 종목 = 적용 목록 + 이 전략 보유 (배포 설정 종목만)
    entry: frozenset[str]                     # 신규 진입·증액 허용 종목 = 적용 목록
    listed_at: dict[str, datetime | None]     # 종목 → 연속 등재 시작 시각(KST)
    record: dict[str, object]                 # 판정 기록에 남길 목록 버전


@dataclass
class LoadedStrategy:
    strategy: Strategy
    params: dict[str, object]
    eval_times: tuple[time, ...]
    unfilled_minutes: int | None = None   # CAP-ORD-03b 선언 + 파라미터 unfilled_max_minutes 일 때만 (D-175)
    # [CP-037 D-219] CAP-TRG-02 주기 판정 격자 (M2만 — 조립 지점 period_grid_of가 만든다). None = 정해진 시각 판정(eval_times)
    period: PeriodGrid | None = None

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


EXEC_RETRY = timedelta(minutes=1)   # 실행 정책 취소가 게이트에 막혔을 때 재판정 간격 (6차 검토 F-3)
BASE_RETRY_WINDOW = timedelta(minutes=10)   # 일일 기준 금액 실패 시 tick마다 재시도하는 기간 (재검토 F5, 임시값)
BASE_SLOW_RETRY = timedelta(minutes=5)      # 그 뒤 재시도 간격 (재검토2 #3, 임시값)
K5_RETRY_EVERY = timedelta(minutes=5)       # 장 밖 K5 대조 재시도 간격 (CP-028 D-150, A-074 Q-6 A — 값 변경은 사용자)
K5_RETRY_MAX = 12                           # 장 밖 K5 대조 재시도 최대 횟수 (5분 × 12 = 1시간, CLAUDE.md §8-7 종료 조건)


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
    _exec_tried: dict[str, datetime] = field(default_factory=dict)   # 실행 정책 취소 마지막 판정 시각 (6차 F-3)
    _base_retry_until: datetime | None = None   # 일일 기준 금액 빠른 재시도 마감 (재검토 F5)
    _base_next_try: datetime = datetime.min.replace(tzinfo=UTC)   # 다음 재시도 시각 (느린 재시도, 재검토2 #3)
    _reconcile_requested: threading.Event = field(default_factory=threading.Event)
    errors: list[str] = field(default_factory=list)
    _calls: "queue.Queue[Callable[[], object]]" = field(default_factory=queue.Queue)
    fallback_file: Path | None = None            # 저장소에 못 쓸 때의 로컬 기록 (RV-06)
    log_dir: Path | None = None                  # 운영 로그(예외 traceback, storage/oplog) — var/<mode>/logs
    start_unknown: int = 0                       # 시작 시 결과 불명 의도 수 (시작 출력용)
    mono: Callable[[], float] = monotonic
    last_progress: float = 0.0                   # 판정·실행 스레드 마지막 진행(단조 시각) — 감시 스레드가 읽음
    _fast_killed: set[str] = field(default_factory=set)
    _closed_reconciled: date | None = None       # 장 마감 후 1회 대조를 한 거래일 (RV-25)
    _k5_retries: int = 0                         # 장 밖 K5 대조 재시도 횟수 (CP-028 D-150, 메모리 — 재시작 시 0)
    _last_cash_observed: tuple | None = None      # 직전 CASH_OBSERVED 값 (같은 값 반복 기록 방지)
    _report_at: datetime | None = None           # 마지막으로 지난 보고 시각 (RV-21)
    _ub_warned: set[tuple[str, date]] = field(default_factory=set)   # U-B 빈 목록 경고 (목록, 거래일) — 하루 1회
    # [CP-034 D-199] 배분 규칙과 지수 일봉 공급자(CAP-DAT-05d). 없으면(기본) 기존 동작 그대로
    allocations: list[LoadedAllocation] = field(default_factory=list)
    index_source: Callable[[str, datetime], IndexView] | None = None
    # [CP-036 D-201] 시장 프로파일. 기본 M1 = 변경 전 동작 그대로
    profile: MarketProfile = field(default_factory=MarketProfile)
    # [CP-037 D-219] 주기 판정 상태 (CAP-TRG-02 전략만 씀 — 정해진 시각 판정은 무관)
    _started_at: datetime | None = None                                       # start() 시각: 이전 예정 시각은 판정 안 함
    _period_last: dict[str, datetime] = field(default_factory=dict)          # 전략별 마지막으로 처리한 예정 시각

    def __post_init__(self) -> None:
        self.profile.attach(self)
        self.last_progress = self.mono()
        # 대조 뒤 실행 중 K8 복구 판정 (3차 검토 #1) — 주기·요청·마감 후·운영 명령(MANUAL) 대조 모두
        self.reconciler.listeners.append(self._after_reconcile)
        # 마감 후 대조의 만료 종결은 실행기의 소멸 통보 반영 경로로 (03 §6.4, 원장 변경은 실행기만)
        self.reconciler.expire_order = self._expire_order

    def _after_reconcile(self, account: str, trigger: str, r: ReconcileResult) -> None:
        """실행 중 K8(D-064 루프 지연·시계 점프, 엔진 단계 오류)이 해제되지 않았으면 방금 대조로 복구 조건을 다시
        판정해 recovery_ok를 보고한다 — 사용자 재개(RESUME K8)가 가능해진다. 자동 해제는 하지 않는다.
        시작 대조(STARTUP)는 RecoveryChecker.check가 직접 판정한다. tick마다가 아니라 대조마다 한 번."""
        if trigger == "STARTUP" or account != self.account:
            return
        if not any(row["halt_type"] == HaltType.K8.value for row in self.repo.halt_rows(self.account)):
            return
        self.recovery.evaluate(self.account, r)

    def _expire_order(self, order: BrokerOrderRow, trade_date: str) -> None:
        with self.repo.tx():
            self.executor.apply_event(OrderEvent(kind="EXPIRED", order_no=order.order_no, account=order.account,
                                                 trade_date=trade_date))

    # --- 시작 -------------------------------------------------------------------------
    def start(self) -> RecoveryReport:
        self.halts.send_thread = threading.get_ident()   # 이 스레드가 판정·실행 스레드 — 정지 취소 재시도는 여기서만 (4차 검토 F-B)
        self._started_at = self.clock.now()              # [CP-037 D-219] 주기 판정: 시작 전 예정 시각은 판정하지 않는다
        self._seed_evaluated(self._started_at)           # 재시작 뒤 같은 판정 시각 재판정 방지 (T-044)
        unknown = self.executor.recover_on_start()
        self.start_unknown = len(unknown)
        self.halts.on_start()
        # 새 프로세스 = 입출력 스레드 새로 시작: 스레드 종료 K5 경로 정상 (재검토 F2). 대조(아래)가 이 보고보다 뒤라
        # reconciled 조건도 이번 시작 대조로 판정된다. 해제 후에도 K8은 사용자 재개 전까지 유지
        self.halts.report_condition(HaltType.K5, self.account, K5_LINK_THREAD, True, "프로세스 시작 — 스레드 새로 시작")
        self.last_progress = self.mono()   # 시작 단계마다 진행 표시 — 긴 시작이 멈춤으로 보이지 않게 (재검토 F9)
        self.profile.refresh_cash()
        self.last_progress = self.mono()
        rep = self.recovery.check(self.account)
        self.last_progress = self.mono()
        self._last_reconcile = self.clock.now()
        self.profile.after_start(self._last_reconcile)
        self.alerts.alert("WARNING" if not rep.ok else "INFO",
                          f"시작: 결과 불명 {len(unknown)}건, 복구 판정 {'통과' if rep.ok else '실패'} — "
                          f"K8 재개는 사용자 명령으로 ({'; '.join(rep.failures) or '조건 충족'})", None)
        return rep

    def _seed_evaluated(self, now: datetime) -> None:
        """재시작 뒤 같은 판정 시각을 다시 판정하지 않게 판정 완료 표시(evaluated)를 저장된 감사 기록으로 채운다
        (T-044, CP-035 Q-3, A-105, DP-21). 표시는 메모리뿐이라, 판정 시각 + 유예(eval_grace) 안에 재시작·K8 재개하면
        이미 판정(EVALUATION, 참조 = 판정 ID)했거나 건너뛴(EVALUATION_SKIPPED, 참조 = 판정 키) 판정 시각을 다시 판정해
        두 번째 신호를 낼 수 있었다. 한 번 실행 안에서는 판정·건너뜀 모두 다시 판정하지 않으므로 같은 규칙이다.
        오늘(시장 시각 — M1 KST, M2 UTC) 판정 시각만 본다 — 판정 키에 날짜가 들어 있다. 기록을 읽지 못하면 오늘 이미 지난 판정 시각을 모두
        판정 완료로 보고(판정하지 않음, 불확실하면 주문하지 않는다) 알린다. 앞으로 올 판정 시각은 그대로 판정한다."""
        tz = self.profile.tz   # [CP-036 D-201] M1 = KST, M2 = UTC
        today = now.astimezone(tz).date()
        refs: dict[str, str] = {}      # 감사 참조 → 판정 키
        past: dict[str, str] = {}      # 지난 판정 키 → "HH:MM"
        for ls in self.strategies:
            for t in ls.eval_times:
                at = datetime.combine(today, t, tzinfo=tz)
                key = self._eval_key(ls, at)
                refs[self._eval_id(ls, at)] = key
                refs[key] = key
                if at <= now:
                    past[key] = t.strftime("%H:%M")
        if not refs:
            return
        try:
            done = self.repo.audited_refs(("EVALUATION", "EVALUATION_SKIPPED"), list(refs))
        except Exception as exc:  # noqa: BLE001 — 읽기 실패 = 판정 여부 불명 → 지난 판정 시각은 판정하지 않는다
            self.evaluated.update(past)
            log_exception(self.log_dir, "engine.seed_evaluated", exc)
            kind = type(exc).__name__
            slots = ", ".join(sorted(set(past.values()))) or "없음"
            self._best_effort(lambda: self.alerts.alert(
                "WARNING", f"재시작 판정 기록 확인 실패({kind}) — 오늘 이미 지난 판정 시각({slots})은 판정하지 않음"
                           f"(재판정 방지, T-044). 이후 판정 시각은 정상", None), "알림", f"{kind}: {exc}")
            return
        self.evaluated.update(refs[r] for r in done if r in refs)

    @staticmethod
    def _eval_key(ls: LoadedStrategy, at: datetime) -> str:
        return f"{ls.owner}|{at.isoformat()}"

    def _eval_id(self, ls: LoadedStrategy, at: datetime) -> str:
        spec = ls.strategy.spec
        return evaluation_id_of(spec.strategy_id, spec.version, self.profile.market.value, at.isoformat())

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

    def fast_kill(self) -> int:
        """감시 스레드가 부른다 (RV-22, 03 D-038): 대기 중인 KILL 명령의 K1 정지 행을 바로 기록한다.

        판정·실행 스레드가 주문 응답·호출 제한을 기다리는 동안에도 K1이 게이트에 보이게 한다. 이 스레드에서는
        HaltManager.activate를 부르지 않는다 — K1 발동은 매수 미체결 취소(주문)를 내기 때문이다. 취소와 명령 완료
        기록은 판정·실행 스레드의 운영 명령 처리가 하며, 그때 activate는 record_only가 남긴 취소 대기 표시를 보고
        한 번 취소한다(재검토 F1).
        안전성: 저장소는 연결 하나를 RLock으로 직렬화하고(tx 전체가 잠금 안, BEGIN IMMEDIATE), 브로커 전송은
        트랜잭션 밖에서만 하므로(INV-03) 여기서 잠금을 기다리는 시간은 다른 스레드의 짧은 저장 단위 하나뿐이다.
        같은 범위(계좌, 종목·전략 없음)의 미해제 K1이 있으면 새로 만들지 않는다(HaltManager.record_only).
        """
        n = 0
        for cmd in self.repo.pending_commands():
            cid = cmd["command_id"]
            if cmd["command"] != "KILL" or cid in self._fast_killed:
                continue
            self._fast_killed.add(cid)
            try:
                args = json.loads(cmd["args"])
            except ValueError:
                continue   # 형식 오류는 운영 명령 처리가 거부로 기록
            if not isinstance(args, dict) or cmd["channel"] not in ("LOCAL", "REMOTE"):
                continue
            account, actor = args.get("account"), f"{cmd['channel']}:{cmd['actor']}"
            reason = args.get("reason") or "운영자 킬 스위치"
            # 행·감사만 기록하고 취소 대기 표시를 남긴다 — 취소는 판정·실행 스레드의 activate가 한 번 (재검토 F1)
            halt_id = self.halts.record_only(HaltType.K1, actor, reason, account)
            if halt_id is not None:
                n += 1
                self.alerts.alert("EMERGENCY", f"정지 K1 발동: {reason} (매수 미체결 취소는 판정·실행 스레드가 처리)",
                                  halt_id)
        return n

    def request_reconcile(self) -> None:
        """다른 스레드(실시간 재접속 등)가 부른다. 다음 tick에서 대조."""
        self._reconcile_requested.set()

    # --- 한 주기 ---------------------------------------------------------------------
    def tick(self) -> None:
        self.last_progress = self.mono()
        now = self.clock.now()
        steps: list[tuple[str, Callable[[], object]]] = [
            ("calls", self._drain_calls), ("ops", self._ops), ("events", self.executor.process_events),
            ("resolve", lambda: self._resolve_step(now)), ("daily", lambda: self._daily(now)),
            ("reconcile", lambda: self.profile.reconcile_step(now)), ("evaluate", lambda: self._evaluate_due(now)),
            ("exec_policy", lambda: self._exec_policy(now))]
        for name, step in steps:
            self.last_progress = self.mono()
            try:
                step()
            except ExecutionBlocked:
                pass  # 실행기 차단(K7 등)은 이미 정지·알림됨
            except Exception as exc:  # noqa: BLE001 — 한 단계 오류로 루프를 멈추지 않는다(감시는 계속)
                self._step_failed(name, exc)
        for hook in self.on_tick:
            try:
                hook(now)
            except Exception as exc:  # noqa: BLE001 — 훅 실패는 기록·알림만 (RV-06)
                log_exception(self.log_dir, "engine.hook", exc)
                self._record_error(f"hook: {type(exc).__name__}: {exc} | {traceback.format_exc(limit=5)}")
                kind = type(exc).__name__
                self._best_effort(lambda: self.alerts.alert("WARNING", f"엔진 훅 오류: {kind}", None), "알림")
        self.flush_alerts()
        self.last_progress = self.mono()

    def flush_alerts(self) -> None:
        """알림 중복 억제 요약 기록 (AuditAlertSink.flush가 있으면). 실패는 삼킨다."""
        flush = getattr(self.alerts, "flush", None)
        if flush is not None:
            try:
                flush()
            except Exception as exc:  # noqa: BLE001
                log_exception(self.log_dir, "alerts.flush", exc)

    def _record_error(self, msg: str) -> None:
        self.errors.append(msg)
        del self.errors[:-50]

    def _step_failed(self, name: str, exc: Exception) -> None:
        """단계 오류 → 알림 + K8. 기록이 실패해도(StorageFailure 등) 예외를 루프 밖으로 내보내지 않는다 (RV-06).
        K8 기록 실패는 로컬 대체 파일에 남긴다 (실행기는 저장 실패 시 K7로 이미 막힘)."""
        kind = type(exc).__name__
        detail = f"{name}: {kind}: {exc} | {traceback.format_exc(limit=5)}"
        log_exception(self.log_dir, f"engine.step:{name}", exc)
        self._record_error(detail)
        self._best_effort(lambda: self.alerts.alert(
            "EMERGENCY", f"엔진 단계 오류 {name}: {kind} — K8, 대조 후 재개", None), "알림", detail)
        self._best_effort(lambda: self.halts.activate(
            HaltType.K8, "engine", f"엔진 단계 오류 {name}: {kind}", None), "K8 기록", detail)

    def _best_effort(self, fn: Callable[[], object], what: str, context: str = "") -> bool:
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001
            self._fallback(f"{what} 실패: {type(exc).__name__}: {exc} / {context}")
            return False

    def _fallback(self, text: str) -> None:
        from .supervisor import write_fallback
        write_fallback(self.fallback_file, text)

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
            out = self.resolver.step(p)
            if out is not None:
                self.progress.pop(iid, None)
                if out is ResolveOutcome.FOUND:
                    # 정지 중 접수로 확정된 매수는 취소 대상이 된다 (6차 검토 F-1)
                    try:   # 실패해도 다른 해소 회차를 막지 않고 사람에게 알린다 (8차 F-1, 사람 확정 경로와 같게)
                        self.halts.cancel_newly_open_buys(f"결과 불명 해소 {iid}", self.repo.intent(iid).broker_order_key)
                    except Exception as exc:  # noqa: BLE001
                        self._best_effort(lambda: self.alerts.alert(
                            "EMERGENCY", f"해소 뒤 정지 취소 실패 {type(exc).__name__} — 매수 미체결 확인 필요", iid), "알림")

    # 일일 ---------------------------------------------------------------------------
    def _daily(self, now: datetime) -> None:
        lim = self.limits()
        if lim is None:
            return
        start = self.profile.last_at_or_before(now, lim.daily_reference_time)
        if self._day_start is None or start > self._day_start:
            first = self._day_start is None
            self._day_start = start
            if not self._start_day_base():
                self.alerts.alert("WARNING", "일일 기준 금액을 정할 수 없음(예수금·보유 시세 없음) — 위험 확대 거부, "
                                             f"{int(BASE_RETRY_WINDOW.total_seconds() // 60)}분 동안 다시 시도", None)
                self._base_retry_until = now + BASE_RETRY_WINDOW
                self._base_next_try = now
            else:
                self._base_retry_until = None   # 새 기준 시각에 확정되면 전날 재시도 상태는 끝
            if not first:
                self.profile.day_report(now, lim)
        elif self._base_retry_until is not None and now >= self._base_next_try:
            # 기준 금액 실패 재시도 (재검토 F5): 시작 직후 첫 tick엔 아직 시세가 없을 수 있다. 처음 10분은 tick마다,
            # 그 뒤엔 5분마다 다음 기준 시각까지 계속(재검토2 #3 — 일시 장애로 하루 전체가 막히지 않게)
            if self._start_day_base():
                self._base_retry_until = None
                self.alerts.alert("INFO", "일일 기준 금액 확정(재시도)", None)
            elif now >= self._base_retry_until:
                if self._base_next_try <= self._base_retry_until:
                    self.alerts.alert("WARNING", "일일 기준 금액 아직 미확정 — 위험 확대 거부 유지, "
                                                 f"{int(BASE_SLOW_RETRY.total_seconds() // 60)}분마다 재시도, 사용자 확인", None)
                self._base_next_try = now + BASE_SLOW_RETRY
        self._report_if_due(now, lim)
        self.halts.check_daily_loss(self.account, self.account_state.daily_risk(self.account), lim.l4_daily_loss_ratio)
        if self.watcher is not None:
            self.watcher.scan(self.account, now)

    def _start_day_base(self) -> bool:
        return self.profile.start_day_base()

    def _cash_day_base(self) -> bool:
        """M1: 조회한 예수금 + 보유 평가액으로 일일 기준 금액 (DailyRiskTracker)."""
        cash = self.account_state.cash(self.account)
        if cash is None:
            return False
        return self.account_state.tracker.start_day(self.account, cash.deposit) is not None

    def _report_if_due(self, now: datetime, lim: AccountLimits) -> None:
        """일일 보고 시각(report_time, 예 16:00) 경과 시 그날 보고서 (RV-21). 시작 직후 이미 지난 시각은 건너뛴다.
        기준 시각(08:30) 경계의 보고서는 그대로 둔다(같은 거래일의 다음 판, 저장소가 판 번호를 올림)."""
        at = self.profile.last_at_or_before(now, lim.report_time)
        if self._report_at is None:
            self._report_at = at
            return
        if at <= self._report_at:
            return
        self._report_at = at
        self.reporter.generate(self.account, now, lim.daily_reference_time, lim.version_hash(), current_day=True)

    # 대조 ---------------------------------------------------------------------------
    def _refresh_cash(self) -> None:
        try:
            with call_priority(Priority.RECONCILE):
                cash = self.broker.query_cash(self.account)
        except BrokerQueryError as exc:
            self.alerts.alert("WARNING", f"예수금 조회 실패: {exc}", None)
            return
        self.account_state.set_cash(self.account, cash)
        try:
            self._observe_cash(cash)
        except Exception as exc:  # noqa: BLE001 — 관찰 전용: 기록 실패가 대조·시작을 막지 않게 (독립 검토 #1)
            log_exception(self.log_dir, "engine.cash_observed", exc)
            self._fallback(f"CASH_OBSERVED 기록 실패: {type(exc).__name__}: {exc}")

    def _observe_cash(self, cash: CashView) -> None:
        """조회한 현금 값이 직전 기록과 다를 때만 CASH_OBSERVED로 남긴다 (관찰 전용, DP-31 재결정 입력).
        재시작하면 첫 조회를 다시 기록한다. 기록에 실패하면 직전 값을 갱신하지 않아 다음 조회에서 다시 기록한다."""
        key = (cash.deposit, cash.broker_orderable, cash.observed)
        if key == self._last_cash_observed:
            return
        with self.repo.tx():
            self.repo.audit(self.clock.now(), "CASH_OBSERVED", "engine", self.account,
                            {"account": self.account, "deposit": str(cash.deposit),
                             "orderable": str(cash.broker_orderable), "observed": dict(cash.observed)})
        self._last_cash_observed = key

    def _reconcile_if_due(self, now: datetime) -> None:
        """대조 주기 (01 "장중 주기 대조, 체결 통보 후, 장 마감 후 1회", RV-25): 주기 대조는 정규장 안에서만,
        장 마감 뒤 한 번, 요청(재접속·체결 통보 후 등 request_reconcile)은 언제든.
        장 밖에서 K5가 대조만 남았으면 5분 간격 최대 12회 재시도 (CP-028 D-150)."""
        requested = self._reconcile_requested.is_set()
        today = now.astimezone(KST).date()
        s = self.store.session(today)
        is_open = s is not None and s.is_open and s.regular_open is not None and s.regular_close is not None
        in_session = is_open and s.regular_open <= now <= s.regular_close  # type: ignore[union-attr,operator]
        after_close = is_open and now > s.regular_close  # type: ignore[union-attr,operator]
        interval_due = self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_every
        periodic = in_session and interval_due
        # 마감 후 1회 대조(CLOSE): 그날 CLOSE를 아직 완료하지 않았으면 마감 뒤 첫 대조(요청 대조 포함)를 CLOSE로
        # (CP-028 D-149 — 요청 대조가 CLOSE를 밀거나 소멸 주문을 PERIODIC으로 보아 오탐 K4를 내지 않게).
        # 요청이 없으면 종전처럼 간격 조건. 조회가 실패하면 완료로 치지 않고 다음 대조를 다시 CLOSE로 (4차 검토 F-C)
        closing = after_close and self._closed_reconciled != today and (requested or interval_due)
        k5_retry = False
        if in_session:
            self._k5_retries = 0                      # 정규장: 정기 대조가 넘겨받는다 (D-150)
        elif not (requested or closing):
            k5_retry = self._k5_retry_due(now)
        if not (requested or periodic or closing or k5_retry):
            return
        self._reconcile_requested.clear()
        self._last_reconcile = now
        self._refresh_cash()
        # 장 마감 후 1회 대조는 CLOSE: 소멸한 오늘 잔량 만료 종결·남은 미체결 알림 (03 §6.4)
        result = self.reconciler.run(self.account, CLOSE_TRIGGER if closing else "PERIODIC")
        if closing and getattr(result, "result", None) != "QUERY_FAILED":
            self._closed_reconciled = today
        if k5_retry:
            self._k5_retries += 1
            if self._k5_retries >= K5_RETRY_MAX and self._k5_awaits_reconcile():
                self.alerts.alert("WARNING", f"장 밖 K5 대조 재시도 {K5_RETRY_MAX}회 실패 — 재시도 중단, "
                                             f"정규장 정기 대조까지 K5 유지", None)

    def _k5_retry_due(self, now: datetime) -> bool:
        """장 밖 K5 대조 재시도 시점인지 (CP-028 D-150, A-074 Q-6 A). 대조 실행 시점만 정한다 — 해제 조건은 불변.

        조건: 이 계좌 K5의 남은 해제 조건이 대조로 풀리는 것뿐(실시간·스레드 경로 정상, reconciled 미충족 —
        조회 경로 link:rest 끊김은 대조 성공이 풀므로 허용, 보완 A-076) + 재시도 횟수 < 12
        + 마지막 대조 뒤 5분. K5가 없거나 다른 조건이 남으면 횟수를 0으로(다음 K5는 새로 12회). 횟수는 메모리."""
        if not self._k5_awaits_reconcile():
            self._k5_retries = 0
            return False
        if self._k5_retries >= K5_RETRY_MAX:
            return False
        return self._last_reconcile is None or now - self._last_reconcile >= K5_RETRY_EVERY

    def _k5_awaits_reconcile(self) -> bool:
        """K5의 남은 해제 조건이 모두 대조 성공으로 풀리는 것인지 (D-150, 보완 A-076).

        대조로 풀리는 조건: reconciled(경로 복구 이후 대조 포함)와 조회 경로 link:rest — 대조기가 조회 3종 성공 시
        link:rest 정상을 보고한다(조회 연속 3회 실패로 끊김 기록된 경우 포함). 실시간(link:realtime)·스레드
        (link:thread)·경로 구분 없는 이전 행(broker_connected)이 남아 있으면 대조로 풀 수 없으므로 False."""
        for row in self.repo.halt_rows(self.account):
            if row["halt_type"] != HaltType.K5.value:
                continue
            missing = HaltManager._k5_missing(json.loads(row["conditions"] or "{}"))  # noqa: SLF001
            if missing and all(m.startswith("reconciled") or m == f"broker_connected({K5_LINK_REST})"
                               for m in missing):
                return True
        return False

    # 판정 ---------------------------------------------------------------------------
    def _blocked_globally(self) -> bool:
        return bool(GLOBAL_BLOCKS & set(self.repo.active_halts(self.account)))

    def _evaluate_due(self, now: datetime) -> None:
        tz = self.profile.tz
        today = now.astimezone(tz).date()
        session = self.profile.session(today)
        if session is None or not session.is_open:
            return
        for ls in self.strategies:
            if ls.period is not None:   # [CP-037 D-219] CAP-TRG-02 주기 판정 (M2만)
                self._evaluate_periodic(ls, ls.period, now)
                continue
            for t in ls.eval_times:
                at = datetime.combine(today, t, tzinfo=tz)
                key = self._eval_key(ls, at)   # 시작 때 감사 기록으로도 채운다 (_seed_evaluated, T-044)
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

    def _evaluate_periodic(self, ls: LoadedStrategy, grid: PeriodGrid, now: datetime) -> None:
        """CAP-TRG-02 주기 판정 (CP-037 D-219·D-220, DP-79). 판정 시각 = 주문 창 안 격자(UTC 00:00 + k×N분 + 확정 여유).

        - 가장 늦은 예정 시각 at 하나만 본다. 시작(start) 전 예정 시각은 판정하지 않는다(재시작 뒤 지난 주기 판정 0, 기록 없음 —
          판정 여부는 이전 프로세스의 감사 기록이 말한다). 판정 ID = 전략·버전·M2·UTC 예정 시각.
        - 직전 처리 시각과 at 사이에 지나간 예정 시각(루프 지연)은 하나씩 판정하지 않고 감사 EVALUATION_SKIPPED 1건(건수)으로.
        - 예정 시각 + 유예(min(60초, N분의 절반)) 초과 → 건너뜀(늦은 판정 금지, DP-21과 같은 원칙). 전역 정지 → 건너뜀.
        - CAP-DAT-02 전략: 그 판정 시각의 1분봉 조회가 끝날 때까지 기다린다(판정 표시 안 함 → 다음 tick 재확인). 기다림 상한
          (PeriodGrid.fetch_wait = 유예의 절반)을 넘으면 끝나지 않은 심볼을 FETCH_TIMEOUT(분봉 입력 무효)으로 판정한다 —
          신규 진입·증액은 판정 실행기가 버리고 청산·감액은 낼 수 있다(DP-78)."""
        at = grid.latest_at_or_before(now)
        if at is None:
            return
        key = self._eval_key(ls, at)
        if key in self.evaluated:
            return
        if self._started_at is not None and at < self._started_at:
            self.evaluated.add(key)
            self._period_last[ls.owner] = at
            return
        last = self._period_last.get(ls.owner)
        if last is None or last != at:
            missed = grid.count_between(last, at) if last is not None and last < at else 0
            self._period_last[ls.owner] = at
            if missed:
                self._audit("EVALUATION_SKIPPED", f"{ls.owner}|gap|{last.isoformat()}|{at.isoformat()}",  # type: ignore[union-attr]
                            {"reason": "주기 판정 건너뜀 (루프 지연 — 지나간 예정 시각은 판정하지 않음)", "count": missed,
                             "after": last.isoformat(), "before": at.isoformat()})  # type: ignore[union-attr]
        if now - at > grid.grace:
            self.evaluated.add(key)
            self._audit("EVALUATION_SKIPPED", key, {"reason": "예정 시각 + 유예 초과 (늦은 판정 금지)",
                                                    "grace_s": grid.grace.total_seconds()})
            return
        if self._blocked_globally():
            self.evaluated.add(key)
            self._audit("EVALUATION_SKIPPED", key, {"reason": "전역 정지 중"})
            return
        up = self.registry.signal_rejection(ls.owner, RequestType.ENTRY)
        down = self.registry.signal_rejection(ls.owner, RequestType.EXIT)
        if up is not None and down is not None:
            self.evaluated.add(key)
            self._audit("EVALUATION_SKIPPED", key, {"reason": up})
            return
        pending = self.profile.minute_pending(ls, at)
        if pending and now - at <= grid.fetch_wait:
            return   # 그 판정 시각 1분봉 조회 대기 — 판정 표시하지 않고 다음 tick에 다시
        self.evaluated.add(key)
        self._evaluate(ls, at, now, unfetched=pending)

    def _next_eval(self, ls: LoadedStrategy, at: datetime) -> datetime | None:
        if ls.period is not None:
            return ls.period.next_after(at)
        tz = self.profile.tz
        later = [datetime.combine(at.astimezone(tz).date(), t, tzinfo=tz) for t in ls.eval_times]
        later = [x for x in later if x > at]
        return min(later) if later else None

    def _evaluate(self, ls: LoadedStrategy, at: datetime, now: datetime,
                  unfetched: frozenset[str] | None = None) -> None:
        spec = ls.strategy.spec
        ev_id = self._eval_id(ls, at)
        owner = ls.owner
        daily = self.account_state.daily_risk(self.account)
        base = daily.base_amount if daily is not None else Decimal("0")
        # 배분 규칙: 판정 직렬화 안에서 전략 판정 전에 평가 (D-199). 지정되지 않은 전략은 영향 없음
        block, alloc = self._allocation_block(ls, at) if self.allocations else (None, None)
        ub = self._universe_b(ls, at) if spec.universe_method == "U-B" else None
        if unfetched is None:
            snap, lots = self.profile.evaluation_input(ls, ev_id, at, now, base, ub)   # [CP-036 D-201 (4)]
        else:   # [CP-037 D-219] 주기 판정 — 기다림 상한을 넘긴 분봉 조회 심볼을 함께 넘긴다 (M2 프로파일만)
            snap, lots = self.profile.evaluation_input(ls, ev_id, at, now, base, ub,  # type: ignore[call-arg]
                                                       unfetched=unfetched)
        result = run_evaluation(ls.strategy, snap, ls.params, lots,
                                **({"entry_universe": ub.entry} if ub is not None else {}),
                                **({"entry_block": block} if block is not None else {}))
        summary: dict[str, object] = {"strategy": owner, "signals": len(result.signals),
                                      "dropped": len(result.dropped), "error": result.error}
        if ub is not None:
            summary["universe_list"] = ub.record   # 판정 입력에 쓴 목록 버전 (D-198 (3))
        if alloc is not None:
            summary["allocation"] = {k: alloc[k] for k in ("rule", "allow_entry", "failed")}
        if unfetched is not None:   # [CP-037] 주기 판정 기록: 예정 시각·판정 지연·분봉 조회 시한 초과·버린 신호 사유
            summary["scheduled_at"] = at.isoformat()
            summary["delay_s"] = (now - at).total_seconds()
            if unfetched:
                summary["minute_unfetched"] = sorted(unfetched)
            if result.dropped:
                summary["dropped_reasons"] = [f"{d.instrument} {d.rule_id}: {d.reason}" for d in result.dropped][:5]
        self._audit("EVALUATION", ev_id, summary)
        if result.error:
            self.alerts.alert("WARNING", f"전략 판정 오류 {owner}: {result.error}", ev_id)
        for s in result.signals:
            req = self.profile.prepare(GateRequest.from_signal(s))   # [CP-036 D-201 (5)] M1 = 항등
            if req is None:
                continue
            d = self.gate.decide(req)
            if d.result is GateResult.APPROVED and d.approved is not None:
                self.executor.submit(d.approved)

    def _store_input(self, ls: LoadedStrategy, ev_id: str, at: datetime, now: datetime, base: Decimal,
                     ub: _UbInput | None) -> tuple[EvaluationSnapshot, dict[str, Decimal]]:
        """M1 판정 입력: MarketDataStore 스냅샷(KRX 일봉·시세) + 종목별 수량 단위."""
        spec = ls.strategy.spec
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

        if ub is not None:
            insts = ub.instruments
        else:
            insts = sorted(set(spec.universe) & set(self._known_instruments())) if spec.universe_method == "U-A" \
                else sorted(self._known_instruments())
        # [CP-033 D-193] CAP-DAT-05a 선언 전략만 투자자별 순매수 입력(일자 < d·확정값만). 미선언 전략은 기존 그대로
        flows = ({"investor_flows": investor_flow_source(self.repo, self.store.calendar, now,
                                                         flow_days_of(ls.strategy))}
                 if FLOW_CAPABILITY in spec.capabilities else {})
        snap = self.store.snapshot(ev_id, self.profile.market.value, self.account, now, base, insts, pos_view, pending,
                                   self._next_eval(ls, at),
                                   history=lambda inst: self.repo.strategy_history(self.account, owner, inst),
                                   **({"listed_at": ub.listed_at.get} if ub is not None else {}), **flows)
        lots = {i: info.lot_size for i in insts if (info := self.store.instrument(i)) is not None}
        return snap, lots

    def _allocation_block(self, ls: LoadedStrategy, at: datetime) -> tuple[str | None, dict | None]:
        """이 전략을 지정한 배분 규칙을 평가한다 (CP-034 D-199). 하나라도 불허·판정 실패면 신규 진입·증액 거부 사유.

        판정 실패 = 배분 규칙 상태가 이 모드 신호 불가, 지수 공급자 없음, 지수 결측·지연·품질 실패(D-189), 규칙 오류 →
        진입 거부 + 알림(조용히 멈추지 않게). 청산·감액은 막지 않는다. 판정마다 ALLOCATION_EVALUATION 감사."""
        sid = ls.strategy.spec.strategy_id
        block: str | None = None
        first: dict | None = None
        for a in self.allocations:
            if sid not in a.rule.spec.allocation_targets:  # type: ignore[attr-defined]
                continue
            failed, allow, reason, evidence, last = True, False, "", {}, None
            rejection = self.registry.signal_rejection(a.owner, RequestType.ENTRY)
            if rejection is not None:
                reason = f"배분 규칙 상태: {rejection}"
            elif self.index_source is None:
                reason = "지수 공급자 없음 (CAP-DAT-05d)"
            else:
                try:
                    view = self.index_source(getattr(a.rule, "index_code", INDEX_KOSPI), at)
                    last = view.bars[-1].at.isoformat() if view.bars else None
                    if not view.data_ok:
                        reason = view.issue or "지수 일봉 품질 실패"
                    else:
                        d = a.rule.decide(view, a.params)  # type: ignore[attr-defined]
                        if not isinstance(d, AllocationDecision):
                            raise TypeError("배분 결정 형식이 아님")
                        failed, allow, reason, evidence = False, d.allow_entry, d.reason, dict(d.evidence)
                except Exception as exc:  # noqa: BLE001 — 규칙·공급자 오류 = 판정 실패(진입 거부)
                    reason = f"배분 규칙 오류: {type(exc).__name__}: {exc}"
            rec = {"rule": a.owner, "target": ls.owner, "allow_entry": allow and not failed, "failed": failed,
                   "reason": reason, "evidence": evidence, "index_last": last, "at": to_iso(at)}
            self._audit("ALLOCATION_EVALUATION", a.owner, rec)
            if failed:
                self.alerts.alert("WARNING", f"배분 규칙 판정 실패 {a.owner} → {ls.owner} 신규 진입 거부: {reason} "
                                             "(D-199)", a.owner)
            if not rec["allow_entry"] and block is None:
                block = f"배분 규칙 {a.owner}: 신규 진입 불허 ({'판정 실패 — ' if failed else ''}{reason}) (D-199)"
                first = rec
            first = first or rec
        return block, first

    def _universe_b(self, ls: LoadedStrategy, at: datetime) -> _UbInput:
        """U-B 판정 입력 (CP-034 D-198). 판정 시각 at − 10분 이전 제출 최신 버전의 종목 + 이 전략 보유(목록에서 빠져도
        청산·감액 판정 대상). 신규 진입·증액은 적용 목록 안에서만. 빈 목록·버전 없음 → 진입 0, 경고 하루 1회."""
        list_id = ls.strategy.spec.universe_list or ""
        versions = self.repo.universe_versions(list_id)
        applied = select_version(versions, at)
        codes = applied.codes if applied is not None else ()
        known = set(self._known_instruments())
        owned = {o["instrument"] for o in self.repo.active_ownerships_of(ls.owner) if o["account"] == self.account}
        listed = {c: listed_since(versions, applied, c) for c in codes} if applied is not None else {}
        record: dict[str, object] = {
            "list": list_id, "version": applied.version if applied is not None else None,
            "hash": applied.content_hash if applied is not None else None,
            "submitted_at": to_iso(applied.submitted_at) if applied is not None else None, "count": len(codes)}
        unknown = sorted(set(codes) - known)
        if unknown:
            record["not_configured"] = unknown   # 배포 설정에 없는 종목은 데이터가 없어 판정하지 않는다
        if not codes:
            day = at.astimezone(KST).date()
            if (list_id, day) not in self._ub_warned:
                self._ub_warned.add((list_id, day))
                state = "비어 있음" if applied is not None else "없음(적용 가능한 제출 버전 없음)"
                self.alerts.alert("WARNING", f"U-B 목록 {list_id} {state} — 신규 진입 없음, 보유 종목 청산 판정은 계속 "
                                             "(D-198)", list_id)
        return _UbInput(sorted((set(codes) | owned) & known), frozenset(codes), listed, record)

    def _known_instruments(self) -> list[str]:
        return [i for i in self.store._data]  # noqa: SLF001 — 등록된 종목 = 배포 설정의 종목

    # 실행 정책 (CAP-ORD-03b) -------------------------------------------------------------
    def _exec_policy(self, now: datetime) -> None:
        if self._exec_tried:   # 미체결이 끝난 주문의 재판정 기록 정리 (8차 F-3)
            live = {o.order_key for o in self.repo.open_orders(self.account)}
            for k in [k for k in self._exec_tried if k not in live]:
                del self._exec_tried[k]
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
                # 이미 취소 의도가 있으면(보냈거나 진행 중·브로커 거부) 다시 판정하지 않는다 — §8-7, 판정 행 누적 방지.
                # 의도 없이 게이트에서 거부(정지 등)됐으면 1분 간격으로 새 키(재시작해도 겹치지 않는 무작위)로 재판정 (6·7차)
                # "보낸 적 있음" = 전송 시도가 NOT_SENT(전송 전 실패)가 아닌 취소 — 정지 경로와 같은 규칙(6차 F-2)
                if self.repo.select("SELECT 1 FROM intent i JOIN attempt a ON a.intent_id=i.intent_id "
                                    "WHERE i.request_type='CANCEL' AND i.target_order_key=? AND a.state<>'NOT_SENT' "
                                    "LIMIT 1", (order.order_key,)):
                    continue
                if self.repo.select("SELECT 1 FROM intent WHERE request_type='CANCEL' AND target_order_key=? "
                                    "AND state IN ('PENDING_SEND','SENT','UNKNOWN','ACCEPTED') LIMIT 1",
                                    (order.order_key,)):
                    continue   # 진행 중인 취소가 있음
                last = self._exec_tried.get(order.order_key)
                if last is not None and now - last < EXEC_RETRY:
                    continue
                self._exec_tried[order.order_key] = now
                req = GateRequest.cancel(RequestSource.EXEC_POLICY, self.account, order.instrument,
                                         Side(order.side), order.order_key,
                                         idem_key=f"exec:{order.order_key}:unfilled:{new_id('try')}", owner=ls.owner)
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


def investor_flow_source(repo: Repository, calendar: KrxCalendar | None, as_of: datetime,
                         n: int) -> Callable[[str], tuple[tuple[InvestorFlow, ...], bool]]:
    """[CP-033 D-189·D-193] PAPER 판정 입력: 저장소(02 ENT-33)의 회차·관측으로 종목별 (확정값 최근 n개, 창 완전 여부).
    백테스트(InvestorFlowBook)와 같은 규칙 함수(marketdata.investor_flow.flow_window)를 쓴다."""
    d = as_of.astimezone(KST).date()
    since, until = d - timedelta(days=n * 3 + 62), d - timedelta(days=1)

    def source(instrument: str) -> tuple[tuple[InvestorFlow, ...], bool]:
        return flow_window(repo.investor_flow_revisions(instrument, since=since, until=until), as_of, calendar, n)
    return source
