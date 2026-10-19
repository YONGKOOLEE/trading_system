"""조립 지점 (T-020, 03 D-093). 실운영(main)과 시험이 같은 함수로 구성 요소를 만든다.

정지는 HaltManager(HaltSink·HaltControl) 하나, 알림은 AuditAlertSink(AlertSink) 하나로만 연결한다.
저장소 장애로 알림을 DB에 못 남기면 로컬 파일(var/<mode>/logs/alerts-fallback.log)에 남긴다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import importlib
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from trading_system.broker.base import BrokerAdapter
from trading_system.config.app_file import StrategyDeploy, check_strategy_slots
from trading_system.config.capabilities import CapabilityTable, uses_unfilled_cancel
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_params import load_params_file, validate_params
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.config.versioning import ChangeSource, SettingsManager
from trading_system.domain.allocation import IndexView
from trading_system.domain.clock import Clock
from trading_system.domain.enums import Mode
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import GateSettings
from trading_system.domain.models import Account
from trading_system.execution.executor import Executor
from trading_system.execution.resolver import UnknownResolver
from trading_system.gate.gate import Gate
from trading_system.gate.ports import ClockHealth, GateInputs, GatePolicy, SignalCheck
from trading_system.halt.manager import HaltManager
from trading_system.marketdata.store import MarketDataStore
from trading_system.monitoring.market_log import MarketDataRecorder
from trading_system.monitoring.watchers import AlertThresholds, AlertWatcher, ConservativeCashFacts, DailyRiskTracker
from trading_system.ops.commands import OpsProcessor
from trading_system.ops.reconcile import ReconcileResolver
from trading_system.reconciliation.reconciler import Reconciler, RecoveryChecker
from trading_system.reporting.daily import DailyReporter
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import OrderIdentity, Repository
from trading_system.strategy.base import Strategy
from trading_system.domain.investor_flow import CAPABILITY as FLOW_CAPABILITY
from trading_system.domain.market import Quote
from trading_system.domain.market_time import M2_TIME, Market
from trading_system.domain.minute_series import DEFAULT_CONFIRM_MARGIN, PeriodGrid
from trading_system.domain.sinks import AlertSink
from trading_system.domain.stop_watch import STOP_CAPABILITY
from trading_system.domain.trading_rules import SpotOrderPolicy, SymbolRules
from trading_system.execution.dust import DustHandler
from trading_system.execution.spot_resolver import SpotUnknownResolver
from trading_system.monitoring.spot_cash import SpotAccountFacts
from trading_system.reconciliation.spot import SpotReconcileCycle, SpotReconcileSchedule, SpotReconciler

from .engine import AccountState, Engine, LoadedAllocation, LoadedStrategy
from .m2_minutes import MINUTE_CAPABILITY, M2MinuteInput, minute_request_of
from .stop_monitor import StopMonitor, stop_strategies
from .market_profile import SpotFacts, SpotProfile

BROKER_ENV = {Mode.BACKTEST: "fake", Mode.PAPER: "mock"}


def load_strategy(dep: StrategyDeploy) -> tuple[Strategy, dict[str, object]]:
    """'패키지.모듈:클래스' → 인스턴스(인자 없는 생성자) + 검증된 파라미터."""
    mod_name, _, cls_name = dep.module.partition(":")
    try:
        obj = getattr(importlib.import_module(mod_name), cls_name)()
    except (ImportError, AttributeError, TypeError) as exc:
        raise ConfigError(f"전략을 불러올 수 없음 {dep.module}: {type(exc).__name__}") from None
    if not hasattr(obj, "spec") or not hasattr(obj, "evaluate"):
        raise ConfigError(f"전략 형식이 아님(spec·evaluate 필요): {dep.module}")
    return obj, load_params_file(obj.spec, dep.params)


@dataclass(frozen=True)
class StrategyInput:
    strategy: Strategy
    params: dict[str, object]
    eval_times: tuple[time, ...]
    verification_approval: str | None = None   # PAPER-0 검증 전략 승인 A-ID (DP-24 a)
    l6_slots: int | None = None                # [CP-034 D-197] 전략별 보유 슬롯. None = 슬롯 검사 없음


class SignalPriceSource(Protocol):
    """[CP-039 D-248·D-251] 신호 출처 참조 시세 공급 계약 (marketdata/signal_feed.SignalFacts)."""

    def reference_quote(self, instrument: str) -> Quote | None: ...

    def fill_reference(self, instrument: str) -> tuple[Decimal, datetime] | None: ...


@dataclass(frozen=True)
class SignalInputs:
    """[CP-039] 신호 출처 binance-public일 때만 (승인 A-115). prices: 실서버 공개 참조 시세, max_divergence: G11-S θ(비율).
    None(기본) = 신호 출처 testnet — 판정 입력·게이트·체결 기록 모두 종전과 같다(V-326)."""

    prices: SignalPriceSource
    max_divergence: Decimal


@dataclass(frozen=True)
class SpotInputs:
    """M2(크립토 현물) 조립 입력 (CP-036 D-202). 값은 시작 경로(run --market M2)가 설정·거래소 규칙에서 만든다.

    facts: 시세·규칙·1d 봉 공급(MarketFacts + SymbolRulesSource + daily_bars), pairs: 유니버스 심볼 → (기초, 호가),
    order_policy: 매수·지연 매도 완충·시세 신선도(A-064·A-065), cash_max_age: 거래소 잔고 조회 신선도(G8 현금)."""

    facts: SpotFacts
    pairs: Mapping[str, tuple[str, str]]
    quote_asset: str
    order_policy: SpotOrderPolicy
    cash_max_age: timedelta = timedelta(minutes=3)
    minutes: M2MinuteInput | None = None   # [CP-037 D-220] 1분봉 저장·판정 입력. None = 분봉 공급 꺼짐(기본)
    signal: SignalInputs | None = None     # [CP-039 D-243] 신호 출처 binance-public. None = testnet(기본, 종전 동작)


def spot_execution(repo: Repository, broker: BrokerAdapter, clock: Clock, kill_file: Path, gate: Gate, *,
                   asset_pair: Callable[[str], tuple[str, str] | None],
                   reference_time_of: Callable[[str], time | None],
                   rules_of: Callable[[str], SymbolRules | None],
                   market_prices_of: Callable[[str], Sequence[Decimal]],
                   quote_of: Callable[[str], Quote | None], order_policy: SpotOrderPolicy,
                   alerts: AlertSink | None = None,
                   dust_alerts: AlertSink | None = None) -> tuple[Executor, HaltManager, DustHandler]:
    """M2 실행 구성 (CP-024 D-102·D-108·D-110, CP-036 D-202): 실행기(고객 주문 ID·순 수령) + 정지 관리(UTC) + dust 처리.
    실운영 조립(build_engine market M2)과 시험 조립(tests/support/m2_fakes)이 같은 함수로 만든다(03 D-093)."""
    executor = Executor(repo, broker, clock, kill_file, alerts=alerts, asset_pair=asset_pair, client_ids=True)
    halts = HaltManager(repo, gate, executor, clock, reference_time_of=reference_time_of, alerts=alerts,
                        market_time=M2_TIME)
    dust = DustHandler(repo, clock, dust_alerts or AuditAlertSink(repo, clock, actor="dust"), rules_of=rules_of,
                       market_prices_of=market_prices_of, quote_of=quote_of, policy=order_policy)
    executor.reject_listeners.append(dust.after_reject)   # -1013 명시 거부 뒤 dust 확정 (D-102 (나))
    return executor, halts, dust


def build_engine(*, mode: Mode, repo: Repository, clock: Clock, broker: BrokerAdapter,
                 store: MarketDataStore | None = None,
                 account: str, account_limits: AccountLimits, gate_settings: GateSettings, caps: CapabilityTable,
                 strategies: list[StrategyInput], kill_file: Path, clock_health: ClockHealth,
                 alert_thresholds: tuple[int, int] | None = None, fallback_log: Path | None = None,
                 reconcile_every: timedelta = timedelta(minutes=1),
                 requery_wait: Callable[[], None] | None = None,
                 market: str = "M1", spot: SpotInputs | None = None) -> Engine:
    """market: M1(기본, 변경 전 조립 그대로) | M2(CP-036 D-202 — spot 입력 필수, store 없음)."""
    if market != Market.M1.value:
        return _build_spot_engine(mode=mode, repo=repo, clock=clock, broker=broker, account=account,
                                  account_limits=account_limits, gate_settings=gate_settings, caps=caps,
                                  strategies=strategies, kill_file=kill_file, clock_health=clock_health,
                                  alert_thresholds=alert_thresholds, fallback_log=fallback_log,
                                  reconcile_every=reconcile_every, requery_wait=requery_wait, market=market,
                                  spot=spot)
    if store is None:
        raise ConfigError("M1 조립에는 시세 저장소(MarketDataStore)가 필요하다")
    if mode not in BROKER_ENV:
        raise ConfigError(f"{mode.value}는 이 구현에서 실행할 수 없다 (실전 경로 없음)")
    check_strategy_slots(strategies, account_limits.l6_max_positions)   # [CP-034 D-197] 시작 검사 (V-272)
    slot_table = None if all(si.l6_slots is None for si in strategies) else \
        {si.strategy.spec.owner: si.l6_slots for si in strategies if si.l6_slots is not None}
    existing = repo.account(account)
    if existing is None:
        repo.register_account(Account(account, mode, BROKER_ENV[mode]))
    elif existing.mode is not mode or existing.broker_env != BROKER_ENV[mode]:
        raise ConfigError("저장소의 계좌 모드·브로커 환경이 시작 모드와 다르다 (R-01)")

    alerts = AuditAlertSink(repo, clock, fallback=fallback_log)
    settings = SettingsManager(repo, clock)
    settings.submit_account_limits(account, account_limits, ChangeSource.LOCAL)  # 완화는 다음 기준 시각부터 (01 §2.3.1)

    def price_of(inst: str):  # type: ignore[no-untyped-def]
        q = store.quote(inst)
        return q.price if q is not None else None

    quote_age = timedelta(seconds=gate_settings.quote_max_age_seconds)

    def fresh_price_of(inst: str):  # type: ignore[no-untyped-def]
        # 일일 손익(L4·K2)은 신선한 시세로만 (RV-18). 낡으면 평가 불능 → 게이트가 위험 확대 거부.
        # 일일 보고서(장 마감 후)는 마지막 시세(price_of)를 그대로 쓴다
        return store.fresh_price(inst, clock.now(), quote_age)

    tracker = DailyRiskTracker(repo, fresh_price_of, clock=clock, cost_rates=gate_settings.cost_rates,  # RV-23 보정
                               alerts=alerts)   # 일일 기준 기록 손상 알림 (재검토 M-1)
    acct = AccountState(tracker)
    # G8 현금: 조회 신선도 + 미반영 당일 매수 체결 보수 차감 (RV-23, PAPER-0에서 entr·d2_entra 의미 실측 후 재결정).
    # 신선도 한도(임시): max(대조 주기 × 3, 10분). 현금은 대조 때마다 조회하므로 조회 실패가 이어지면 모름이 된다.
    # 조회 이후 매수 체결은 따로 차감하므로 한도 안의 낡음은 외부 거래분만 남는다
    gate_account = ConservativeCashFacts(acct, tracker, clock, max(reconcile_every * 3, timedelta(minutes=10)))
    registry = StrategyRegistry(repo, clock, None)
    gate = Gate(repo, clock, GateInputs(settings, store, gate_account, registry, clock_health,
                                        strategy_slots=slot_table),
                GatePolicy.from_settings(gate_settings))
    executor = Executor(repo, broker, clock, kill_file, alerts=alerts)
    halts = HaltManager(repo, gate, executor, clock, alerts=alerts,
                        reference_time_of=lambda a: (lim.daily_reference_time
                                                     if (lim := settings.effective_limits(a)) else None))
    registry.attach_halts(halts)
    store.halts, store.alerts, store.account = halts, alerts, account
    resolver = UnknownResolver(repo, broker, executor, clock, wait=lambda s: None)
    reconciler = Reconciler(repo, broker, clock, halts, alerts, drain_events=executor.process_events,  # RV-24
                            requery_wait=requery_wait)
    recovery = RecoveryChecker(repo, reconciler, halts, clock_health.clock_ok)
    # RECONCILE_RESOLVE는 대조 불일치 해소기(resolve(item_id, method, actor))로 — 결과 불명 해소기가 아님 (RV-01)
    ops = OpsProcessor(repo, gate, executor, halts, clock, reconciler=reconciler,
                       resolver=ReconcileResolver(repo, executor, clock), kill_file=kill_file, registry=registry)

    loaded = _register_strategies(registry, strategies, caps, account_limits, mode, account)

    watcher = None
    if alert_thresholds is not None:
        watcher = AlertWatcher(repo, alerts, AlertThresholds(account_limits.warn_usage_ratio, *alert_thresholds),
                               account_limits.daily_reference_time)
    engine = Engine(repo=repo, clock=clock, broker=broker, store=store, gate=gate, executor=executor, halts=halts,
                    resolver=resolver, reconciler=reconciler, recovery=recovery, ops=ops, registry=registry,
                    account_state=acct, reporter=DailyReporter(repo, alerts, gate_settings.cost_rates, price_of),
                    watcher=watcher, alerts=alerts, account=account,
                    limits=lambda: settings.effective_limits(account), strategies=loaded,
                    reconcile_every=reconcile_every)
    # 시세 기록 (T-023, CP-027 draft): tick 끝의 훅으로 원장을 읽기만 하고 진단 표에 쓴다. 주문 경로 밖 (D-143)
    recorder = MarketDataRecorder(repo, clock, account, store.quote_at, store.tick_of, store.poll_stats.drain,
                                  alerts=alerts, log_dir_of=lambda: engine.log_dir)
    engine.on_tick.append(recorder.on_tick)
    return engine


PERIODIC_CAPABILITY = "CAP-TRG-02"   # 주기 판정 (CP-037 D-219)
PERIOD_PARAM = "period_minutes"      # 주기 판정 격자 N분 — 전략 파라미터 (D-219)
# [CP-040 D-258, Q-1 (나) A-115] M2 전략 등록이 받는 판정 주기. 엔진 격자(PeriodGrid)는 D-219대로 1440의 약수를 다루고,
# 등록 검사만 이 집합으로 좁힌다 — 다른 값은 CP로 추가
M2_PERIODS_ALLOWED: frozenset[int] = frozenset({15, 60})


def period_grid_of(si: StrategyInput, market: str, *, minute_period: int | None = None,
                   margin: timedelta = DEFAULT_CONFIRM_MARGIN, registration: bool = True) -> PeriodGrid | None:
    """[CP-037 D-219, DP-79] 전략의 판정 일정 검사 → CAP-TRG-02면 주기 격자, 아니면 None(정해진 시각 판정). 문제면 ConfigError.

    - CAP-TRG-02는 M2 프로파일에서만 연다 — M1은 거부(capabilities.md M1 "미지원"과 별개로 코드도 막는다, CP-032 T-029 몫).
    - CAP-TRG-02 전략: 배포 설정 eval_times는 비어 있어야 하고(일정은 파라미터 period_minutes), N은 1440의 약수이며 격자
      시각이 주문 창 안에 있어야 한다(DP-77). CAP-DAT-02도 선언했고 1분봉 조회가 켜져 있으면 N은 조회 격자(설정 [minute]
      period_minutes)의 배수 — 판정 시각마다 그 시각의 조회가 있어야 한다.
    - [CP-040 D-258] M2 등록은 period_minutes ∈ M2_PERIODS_ALLOWED(15·60)만 — 그 밖은 거부 사유를 남긴다. registration
      False(1분봉 백테스트 CLI — 연구·T-053 실행기)는 이 범위 검사를 하지 않는다(엔진 격자 D-219 검사는 그대로).
    - 그 밖의 전략: eval_times가 하나 이상(M2 설정은 빈 목록을 허용하므로 여기서 막는다)."""
    spec = si.strategy.spec
    owner = spec.owner
    if PERIODIC_CAPABILITY not in spec.capabilities:
        if not si.eval_times:
            raise ConfigError([f"{owner}: eval_times가 비어 있음 — 빈 목록은 {PERIODIC_CAPABILITY} 주기 판정 전략만"])
        return None
    if market != Market.M2.value:
        raise ConfigError([f"{owner}: {PERIODIC_CAPABILITY} 주기 판정은 M1 프로파일에서 열지 않는다 "
                           "(CP-037 D-219 — M1은 CP-032 T-029 키움 호출 예산과 함께)"])
    problems: list[str] = []
    if si.eval_times:
        problems.append(f"{owner}: {PERIODIC_CAPABILITY} 전략의 eval_times는 빈 목록이어야 함 — 판정 일정은 파라미터 "
                        f"{PERIOD_PARAM} (D-219)")
    n = si.params.get(PERIOD_PARAM)
    grid: PeriodGrid | None = None
    if isinstance(n, bool) or not isinstance(n, int):
        problems.append(f"{owner}: {PERIODIC_CAPABILITY} 전략은 정수 파라미터 {PERIOD_PARAM}이 필요 (D-219)")
    else:
        try:
            grid = PeriodGrid(n, margin)
        except ValueError as exc:
            problems.append(f"{owner}: {PERIOD_PARAM} {n} — {exc}")
        if grid is not None and MINUTE_CAPABILITY in spec.capabilities and minute_period is not None \
                and n % minute_period:
            problems.append(f"{owner}: {PERIOD_PARAM} {n}이 1분봉 조회 격자 {minute_period}분의 배수가 아님 — 판정 시각에 그 "
                            "시각 조회가 없다 (config/m2/market.<mode>.toml [minute], DP-79)")
        if registration and n not in M2_PERIODS_ALLOWED:
            allowed = "·".join(str(x) for x in sorted(M2_PERIODS_ALLOWED))
            problems.append(f"{owner}: {PERIOD_PARAM} {n} — M2 판정 주기는 {allowed}분만 등록한다 (CP-040 D-258, A-115 Q-1)")
    if problems:
        raise ConfigError(problems)
    return grid


def _register_strategies(registry: StrategyRegistry, strategies: list[StrategyInput], caps: CapabilityTable,
                         account_limits: AccountLimits, mode: Mode, account: str, market: str = Market.M1.value,
                         minutes: M2MinuteInput | None = None) -> list[LoadedStrategy]:
    loaded: list[LoadedStrategy] = []
    for si in strategies:
        # [CP-040 D-269 (1)] CAP-HLD-02 손절 감시는 M2 프로파일에서만 연다 — M1은 표 상태와 무관하게 등록 전에 거부(CAP-TRG-02와 같음)
        if STOP_CAPABILITY in si.strategy.spec.capabilities and market != Market.M2.value:
            raise ConfigError([f"{si.strategy.spec.owner}: {STOP_CAPABILITY} 손절 감시는 {market} 프로파일에서 열지 않는다 "
                               "(CP-040 D-269 — M2만, capabilities.md M1 미지원)"])
        reg = registry.register(si.strategy.spec, dict(si.params), caps, account_limits, mode,
                                verification_approval=si.verification_approval)
        if not reg.ok:
            raise ConfigError([f"전략 등록 거부 {si.strategy.spec.owner}: {p}" for p in reg.problems])
        parsed = validate_params(si.strategy.spec, si.params)   # 전략에는 검증·형 변환된 값만 준다 (R-19)
        unfilled = None
        # [CP-032 D-175] 03b(취소) 선언이면 미체결 시간 초과 취소. "CAP-ORD-03"(03a 포함)은 등록에서 이미 거부된다
        if uses_unfilled_cancel(si.strategy.spec.capabilities) and "unfilled_max_minutes" in parsed:
            unfilled = int(str(parsed["unfilled_max_minutes"]))
        # [CP-037 D-219] 판정 일정: CAP-TRG-02(M2만)면 주기 격자. M1은 CAP-TRG-02를 선언하지 않은 전략만 여기 온다(등록 거부)
        period = None
        if PERIODIC_CAPABILITY in si.strategy.spec.capabilities or market != Market.M1.value:
            period = period_grid_of(replace(si, params=parsed), market,
                                    minute_period=minutes.period_minutes if minutes is not None else None,
                                    margin=minutes.margin if minutes is not None else DEFAULT_CONFIRM_MARGIN)
        # [CP-040 D-259·D-260] CAP-DAT-02 전략의 집계 봉·흐름 열 파라미터 검사 (M2만 — M1은 CAP-DAT-02 미개방)
        if MINUTE_CAPABILITY in si.strategy.spec.capabilities and market != Market.M1.value:
            try:
                minute_request_of(parsed)
            except ValueError as exc:
                raise ConfigError([f"{si.strategy.spec.owner}: {exc}"]) from None
        loaded.append(LoadedStrategy(si.strategy, parsed, si.eval_times, unfilled, period))
    # [CP-035 D-200, DP-49] 보유·예약을 가진 전략을 배포 설정에서 빼면(교체 전이 없이 설정만 바꿈) 시작 거부
    orphans = registry.undeployed_holding_problems(account, {si.strategy.spec.owner for si in strategies})
    if orphans:
        raise ConfigError(orphans)
    return loaded


def _spot_strategy_problems(strategies: list[StrategyInput], pairs: Mapping[str, tuple[str, str]]) -> list[str]:
    """M2 엔진에 올릴 수 없는 전략 (보수적 시작 거부, CP-036 D-202·D-207). 등록부 검사(능력 표)에 더해:
    시장 M2 미선언, 종목 목록(U-A) 외 방식(U-B 목록·판정 입력은 M1 전용), 유니버스 밖 종목, 투자자별 순매수 입력(M1 전용)."""
    out: list[str] = []
    for si in strategies:
        spec = si.strategy.spec
        if "M2" not in spec.markets:
            out.append(f"{spec.owner}: markets에 M2가 없다 — M2 엔진에 올릴 수 없음")
        if spec.universe_method != "U-A":
            out.append(f"{spec.owner}: M2 전략은 종목 목록 U-A만 (판정 입력 D-207, {spec.universe_method} 미지원)")
        outside = sorted(set(spec.universe) - set(pairs))
        if spec.universe_method == "U-A" and outside:
            out.append(f"{spec.owner}: 유니버스 밖 심볼 {', '.join(outside)} (config/m2/universe, D-105)")
        if FLOW_CAPABILITY in spec.capabilities:
            out.append(f"{spec.owner}: {FLOW_CAPABILITY}(투자자별 순매수)는 M1 전용 입력")
    return out


SIGNAL_PRICE_REFERENCE = "reference_price"   # [CP-039 D-249 (3)] 전략 클래스 속성 SIGNAL_PRICE_FIELD 값


def signal_price_problems(strategies: list[StrategyInput], *, public: bool) -> list[str]:
    """[CP-039 D-249 (3), 재검토 2026-10-19] 신호 출처 binance-public이면 신호 조건을 reference_price(신호 출처 참조 시세)로
    판정한다고 밝힌 전략(클래스 속성 SIGNAL_PRICE_FIELD = "reference_price")만 시작한다. last_price(테스트넷)로 신호 조건을
    보는 전략은 봉(실서버)과 현재가(테스트넷)가 섞여 청산·손절이 틀릴 수 있다 — 불확실하면 시작하지 않음(주문 0).
    testnet 출처(기본)는 검사하지 않는다(종전 동작)."""
    if not public:
        return []
    return [f"{si.strategy.spec.owner}: 신호 출처 binance-public에는 신호 조건을 reference_price로 판정하는 전략만 "
            f"(SIGNAL_PRICE_FIELD = \"{SIGNAL_PRICE_REFERENCE}\" 선언 없음 — CP-039 D-249 (3))"
            for si in strategies if getattr(si.strategy, "SIGNAL_PRICE_FIELD", None) != SIGNAL_PRICE_REFERENCE]


LEGACY_RULES_MULTI_SYMBOL = "종목별 규칙 검사 불가 — validate_symbol_rules 필요 (CP-041 D-271)"


def spot_rule_problems(strategies: list[StrategyInput], rules: Mapping[str, SymbolRules],
                       pairs: Mapping[str, tuple[str, str]]) -> list[str]:
    """시작 시 전략 규칙 검사 (CP-036 D-209, B-8 → CP-041 D-270·D-271): 거래소 규칙 적재 뒤, 등록 전에 부른다.
    범위 = 전략 명세 유니버스(U-A) ∩ 유니버스 `pairs`(U-A가 아니면 전체 유니버스) (D-271 (1)).
    - validate_symbol_rules(params, symbol, SymbolRules)가 있으면 그것만, 범위 심볼마다 한 번 부른다 (D-270, D-271 (2)).
    - 없고 기존 validate_rules(params, LOT_SIZE stepSize, minNotional)만 있으면 범위 심볼이 1개일 때만 기존대로 부른다.
      2개 이상이면 종목별 검사를 할 수 없으므로 시작 거부 사유 (D-271 (3), F-3 — 서명에 종목이 없다).
    - 둘 다 없으면 검사 없음 (D-271 (4), 판정 실행기의 수량 단위 검사는 그대로).
    규칙이 없는 심볼은 불일치로 본다(불확실하면 시작하지 않음). 문구는 "<전략> <심볼>: <사유>". 문제 목록이 비어 있지 않으면
    호출자가 시작을 거부한다(주문 0). M2 조립(main._run_m2)·M2 백테스트 CLI에서만 부른다 (D-280)."""
    out: list[str] = []
    for si in strategies:
        per_symbol = getattr(si.strategy, "validate_symbol_rules", None)
        legacy = getattr(si.strategy, "validate_rules", None)
        if per_symbol is None and legacy is None:
            continue
        spec = si.strategy.spec
        symbols = sorted(set(spec.universe) & set(pairs)) if spec.universe_method == "U-A" else sorted(pairs)
        if per_symbol is None and len(symbols) > 1:
            out.append(f"{spec.owner} {', '.join(symbols)}: {LEGACY_RULES_MULTI_SYMBOL}")
            continue
        for sym in symbols:
            r = rules.get(sym)
            if r is None:
                out.append(f"{spec.owner} {sym}: 거래 규칙 없음")
                continue
            found = per_symbol(si.params, sym, r) if per_symbol is not None else \
                legacy(si.params, r.lot_step, r.min_notional)
            out.extend(f"{spec.owner} {sym}: {p}" for p in found)
    return out


def _build_spot_engine(*, mode: Mode, repo: Repository, clock: Clock, broker: BrokerAdapter, account: str,
                       account_limits: AccountLimits, gate_settings: GateSettings, caps: CapabilityTable,
                       strategies: list[StrategyInput], kill_file: Path, clock_health: ClockHealth,
                       alert_thresholds: tuple[int, int] | None, fallback_log: Path | None,
                       reconcile_every: timedelta, requery_wait: Callable[[], None] | None, market: str,
                       spot: SpotInputs | None) -> Engine:
    """M2 조립 (CP-036 D-202): SYMBOL 식별 저장소(D-110), UTC 게이트·정지·보고·감시(D-108), 고객 주문 ID 실행기,
    SpotUnknownResolver(D-111), SpotReconciler + SpotReconcileCycle(D-109), SpotAccountFacts(D-103), dust(D-102),
    M2 등록부. 엔진은 M1과 같은 클래스에 SpotProfile만 다르다(D-201)."""
    if market != Market.M2.value:
        raise ConfigError(f"알 수 없는 시장: {market} (M1|M2)")
    if spot is None:
        raise ConfigError("M2 조립에는 현물 입력(SpotInputs: 시세·규칙 공급, 자산 쌍, 주문 가격 규칙)이 필요하다")
    if mode not in BROKER_ENV:
        raise ConfigError(f"{mode.value}는 이 구현에서 실행할 수 없다 (실전 경로 없음)")
    if repo.order_identity is not OrderIdentity.SYMBOL:
        raise ConfigError("M2 저장소는 브로커 주문 식별 SYMBOL(계좌·심볼·orderId, D-110)이어야 한다")
    problems = _spot_strategy_problems(strategies, spot.pairs)
    if problems:
        raise ConfigError(problems)
    check_strategy_slots(strategies, account_limits.l6_max_positions)   # [CP-034 D-197]
    slot_table = None if all(si.l6_slots is None for si in strategies) else \
        {si.strategy.spec.owner: si.l6_slots for si in strategies if si.l6_slots is not None}
    existing = repo.account(account)
    if existing is None:
        repo.register_account(Account(account, mode, BROKER_ENV[mode]))
    elif existing.mode is not mode or existing.broker_env != BROKER_ENV[mode]:
        raise ConfigError("저장소의 계좌 모드·브로커 환경이 시작 모드와 다르다 (R-01)")

    facts = spot.facts
    alerts = AuditAlertSink(repo, clock, fallback=fallback_log)
    settings = SettingsManager(repo, clock, market_time=M2_TIME)
    settings.submit_account_limits(account, account_limits, ChangeSource.LOCAL)   # 완화는 다음 UTC 00:00부터
    quote_age = timedelta(seconds=gate_settings.quote_max_age_seconds)

    def price_of(inst: str):  # type: ignore[no-untyped-def]
        q = facts.quote(inst)
        return q.price if q is not None else None

    def fresh_price_of(inst: str) -> Decimal | None:   # 일일 손익·기준 금액은 신선한 시세로만 (RV-18)
        q = facts.quote(inst)
        now = clock.now()
        return None if q is None or q.at > now or now - q.at > quote_age else q.price

    acct = SpotAccountFacts(repo, clock, quote_asset=spot.quote_asset, asset_pair=spot.pairs.get,
                            price_of=fresh_price_of, cost_rates=gate_settings.cost_rates,
                            reference_time=account_limits.daily_reference_time, max_age=spot.cash_max_age,
                            market_time=M2_TIME)
    registry = StrategyRegistry(repo, clock, None, market=Market.M2.value)
    signal_check = SignalCheck(spot.signal.prices, spot.signal.max_divergence) if spot.signal is not None else None
    gate = Gate(repo, clock, GateInputs(settings, facts, acct, registry, clock_health, spot_rules=facts,
                                        strategy_slots=slot_table, signal=signal_check),
                GatePolicy(quote_age, gate_settings.price_limit_ratio, gate_settings.cost_rates,
                           market_time=M2_TIME))
    executor, halts, dust = spot_execution(
        repo, broker, clock, kill_file, gate, asset_pair=spot.pairs.get,
        reference_time_of=lambda a: (lim.daily_reference_time if (lim := settings.effective_limits(a)) else None),
        rules_of=facts.rules, market_prices_of=facts.market_prices, quote_of=facts.quote,
        order_policy=spot.order_policy, alerts=alerts, dust_alerts=alerts)
    registry.attach_halts(halts)
    if spot.signal is not None:   # [CP-039 D-251] 체결 시각 P_s 기록 (testnet 출처면 기록 없음)
        executor.fill_reference = spot.signal.prices.fill_reference
    for name, value in (("halts", halts), ("alerts", alerts), ("account", account)):
        if hasattr(facts, name):
            setattr(facts, name, value)   # 공급 품질 실패 → K10 (공급자가 쓴다)
    resolver = SpotUnknownResolver(repo, broker, executor, clock, wait=lambda s: None)
    reconciler = SpotReconciler(repo, broker, clock, halts, alerts, universe=spot.pairs, quote_asset=spot.quote_asset,
                                expected_quote=acct.expected_quote, drain_events=executor.process_events,
                                requery_wait=requery_wait)
    recovery = RecoveryChecker(repo, reconciler, halts, clock_health.clock_ok)
    ops = OpsProcessor(repo, gate, executor, halts, clock, reconciler=reconciler,
                       resolver=ReconcileResolver(repo, executor, clock), kill_file=kill_file, registry=registry)
    loaded = _register_strategies(registry, strategies, caps, account_limits, mode, account, market, spot.minutes)
    stop_owners = [ls.owner for ls in stop_strategies(loaded)]
    if stop_owners:   # [CP-040 D-263 (2)] 손절 감시는 1분 봉 조회가 1분 격자로 켜져 있어야 한다 — 없으면 보호 없이 돌지 않게 시작 거부
        if spot.minutes is None or spot.minutes.period_minutes != 1:
            got = "꺼짐" if spot.minutes is None else f"{spot.minutes.period_minutes}분"
            raise ConfigError([f"{o}: {STOP_CAPABILITY} 손절 감시에는 1분봉 조회 격자 1분이 필요 (현재 {got} — "
                               "config/m2/market.<mode>.toml [minute], D-263 (2)·DP-74 예산 검사)" for o in stop_owners])

    watcher = None
    if alert_thresholds is not None:
        watcher = AlertWatcher(repo, alerts, AlertThresholds(account_limits.warn_usage_ratio, *alert_thresholds),
                               account_limits.daily_reference_time, market_time=M2_TIME)
    schedule = SpotReconcileSchedule(reconcile_every, M2_TIME, account_limits.daily_reference_time)
    profile = SpotProfile(facts=facts, pairs=spot.pairs, quote_asset=spot.quote_asset, account_facts=acct,
                          order_policy=spot.order_policy, dust=dust, schedule=schedule, minutes=spot.minutes,
                          signal=spot.signal.prices if spot.signal is not None else None)
    reconciler.balance_listeners.append(profile.observe_balances)
    ops.prepare = profile.prepare_ops   # [CP-036 D-211] 운영 요청도 M2 주문 조건(유형·가격·dust·수량 단위)을 탄다
    profile.cycle = SpotReconcileCycle(reconciler, account, schedule, on_day_close=profile.day_closed)
    engine = Engine(repo=repo, clock=clock, broker=broker, store=facts, gate=gate,  # type: ignore[arg-type]
                  executor=executor, halts=halts, resolver=resolver, reconciler=reconciler, recovery=recovery,
                  ops=ops, registry=registry, account_state=acct,  # type: ignore[arg-type]
                  reporter=DailyReporter(repo, alerts, gate_settings.cost_rates, price_of, market_time=M2_TIME),
                  watcher=watcher, alerts=alerts, account=account,
                  limits=lambda: settings.effective_limits(account), strategies=loaded,
                  reconcile_every=reconcile_every, profile=profile)
    if stop_owners and spot.minutes is not None:   # [CP-040 T-067] 감시 → 게이트 → 실행기 (엔진 단계 "stop")
        engine.stop_monitor = StopMonitor(engine, profile, spot.minutes, loaded)
    return engine


@dataclass(frozen=True)
class AllocationInput:
    """배분 규칙 (CP-034 D-199): kind ALLOCATION 명세 + decide(IndexView, params)."""

    rule: object
    params: dict[str, object]


def add_allocations(engine: Engine, inputs: list[AllocationInput], caps: CapabilityTable,
                    account_limits: AccountLimits | None, mode: Mode,
                    index_source: Callable[[str, datetime], IndexView] | None = None) -> None:
    """배분 규칙을 등록 검증(전략과 같은 등록부·능력 기준)한 뒤 엔진에 붙인다. 하나라도 거부되면 아무것도 붙이지 않는다.

    PAPER 배포 설정 연결([[allocations]])은 이 작업 범위 밖 — CAP-DAT-05d 표 반영·지수 수집 경로와 함께 [제안]."""
    loaded: list[LoadedAllocation] = []
    for ai in inputs:
        spec = getattr(ai.rule, "spec", None)
        if spec is None or getattr(spec, "kind", "") != "ALLOCATION" or not hasattr(ai.rule, "decide"):
            raise ConfigError(f"배분 규칙이 아님(kind ALLOCATION·decide 필요): {getattr(spec, 'owner', ai.rule)}")
        reg = engine.registry.register(spec, dict(ai.params), caps, account_limits, mode)
        if not reg.ok:
            raise ConfigError([f"배분 규칙 등록 거부 {spec.owner}: {p}" for p in reg.problems])
        loaded.append(LoadedAllocation(ai.rule, validate_params(spec, ai.params)))
    engine.allocations.extend(loaded)
    if index_source is not None:
        engine.index_source = index_source
