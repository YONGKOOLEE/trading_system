"""조립 지점 (T-020, 03 D-093). 실운영(main)과 시험이 같은 함수로 구성 요소를 만든다.

정지는 HaltManager(HaltSink·HaltControl) 하나, 알림은 AuditAlertSink(AlertSink) 하나로만 연결한다.
저장소 장애로 알림을 DB에 못 남기면 로컬 파일(var/<mode>/logs/alerts-fallback.log)에 남긴다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import importlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

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
from trading_system.gate.ports import ClockHealth, GateInputs, GatePolicy
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
from trading_system.domain.sinks import AlertSink
from trading_system.domain.trading_rules import SpotOrderPolicy, SymbolRules
from trading_system.execution.dust import DustHandler
from trading_system.execution.spot_resolver import SpotUnknownResolver
from trading_system.monitoring.spot_cash import SpotAccountFacts
from trading_system.reconciliation.spot import SpotReconcileCycle, SpotReconcileSchedule, SpotReconciler

from .engine import AccountState, Engine, LoadedAllocation, LoadedStrategy
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


def _register_strategies(registry: StrategyRegistry, strategies: list[StrategyInput], caps: CapabilityTable,
                         account_limits: AccountLimits, mode: Mode, account: str) -> list[LoadedStrategy]:
    loaded: list[LoadedStrategy] = []
    for si in strategies:
        reg = registry.register(si.strategy.spec, dict(si.params), caps, account_limits, mode,
                                verification_approval=si.verification_approval)
        if not reg.ok:
            raise ConfigError([f"전략 등록 거부 {si.strategy.spec.owner}: {p}" for p in reg.problems])
        parsed = validate_params(si.strategy.spec, si.params)   # 전략에는 검증·형 변환된 값만 준다 (R-19)
        unfilled = None
        # [CP-032 D-175] 03b(취소) 선언이면 미체결 시간 초과 취소. "CAP-ORD-03"(03a 포함)은 등록에서 이미 거부된다
        if uses_unfilled_cancel(si.strategy.spec.capabilities) and "unfilled_max_minutes" in parsed:
            unfilled = int(str(parsed["unfilled_max_minutes"]))
        loaded.append(LoadedStrategy(si.strategy, parsed, si.eval_times, unfilled))
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
    gate = Gate(repo, clock, GateInputs(settings, facts, acct, registry, clock_health, spot_rules=facts,
                                        strategy_slots=slot_table),
                GatePolicy(quote_age, gate_settings.price_limit_ratio, gate_settings.cost_rates,
                           market_time=M2_TIME))
    executor, halts, dust = spot_execution(
        repo, broker, clock, kill_file, gate, asset_pair=spot.pairs.get,
        reference_time_of=lambda a: (lim.daily_reference_time if (lim := settings.effective_limits(a)) else None),
        rules_of=facts.rules, market_prices_of=facts.market_prices, quote_of=facts.quote,
        order_policy=spot.order_policy, alerts=alerts, dust_alerts=alerts)
    registry.attach_halts(halts)
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
    loaded = _register_strategies(registry, strategies, caps, account_limits, mode, account)

    watcher = None
    if alert_thresholds is not None:
        watcher = AlertWatcher(repo, alerts, AlertThresholds(account_limits.warn_usage_ratio, *alert_thresholds),
                               account_limits.daily_reference_time, market_time=M2_TIME)
    schedule = SpotReconcileSchedule(reconcile_every, M2_TIME, account_limits.daily_reference_time)
    profile = SpotProfile(facts=facts, pairs=spot.pairs, quote_asset=spot.quote_asset, account_facts=acct,
                          order_policy=spot.order_policy, dust=dust, schedule=schedule)
    reconciler.balance_listeners.append(profile.observe_balances)
    profile.cycle = SpotReconcileCycle(reconciler, account, schedule, on_day_close=profile.day_closed)
    return Engine(repo=repo, clock=clock, broker=broker, store=facts, gate=gate,  # type: ignore[arg-type]
                  executor=executor, halts=halts, resolver=resolver, reconciler=reconciler, recovery=recovery,
                  ops=ops, registry=registry, account_state=acct,  # type: ignore[arg-type]
                  reporter=DailyReporter(repo, alerts, gate_settings.cost_rates, price_of, market_time=M2_TIME),
                  watcher=watcher, alerts=alerts, account=account,
                  limits=lambda: settings.effective_limits(account), strategies=loaded,
                  reconcile_every=reconcile_every, profile=profile)


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
