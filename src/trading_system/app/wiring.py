"""조립 지점 (T-020, 03 D-093). 실운영(main)과 시험이 같은 함수로 구성 요소를 만든다.

정지는 HaltManager(HaltSink·HaltControl) 하나, 알림은 AuditAlertSink(AlertSink) 하나로만 연결한다.
저장소 장애로 알림을 DB에 못 남기면 로컬 파일(var/<mode>/logs/alerts-fallback.log)에 남긴다.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path

from trading_system.broker.base import BrokerAdapter
from trading_system.config.app_file import StrategyDeploy
from trading_system.config.capabilities import CapabilityTable
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_params import load_params_file, validate_params
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.config.versioning import ChangeSource, SettingsManager
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
from trading_system.monitoring.watchers import AlertThresholds, AlertWatcher, DailyRiskTracker
from trading_system.ops.commands import OpsProcessor
from trading_system.reconciliation.reconciler import Reconciler, RecoveryChecker
from trading_system.reporting.daily import DailyReporter
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import Repository
from trading_system.strategy.base import Strategy

from .engine import AccountState, Engine, LoadedStrategy

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


def build_engine(*, mode: Mode, repo: Repository, clock: Clock, broker: BrokerAdapter, store: MarketDataStore,
                 account: str, account_limits: AccountLimits, gate_settings: GateSettings, caps: CapabilityTable,
                 strategies: list[StrategyInput], kill_file: Path, clock_health: ClockHealth,
                 alert_thresholds: tuple[int, int] | None = None, fallback_log: Path | None = None,
                 reconcile_every: timedelta = timedelta(minutes=1)) -> Engine:
    if mode not in BROKER_ENV:
        raise ConfigError(f"{mode.value}는 이 구현에서 실행할 수 없다 (실전 경로 없음)")
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

    tracker = DailyRiskTracker(repo, price_of)
    acct = AccountState(tracker)
    registry = StrategyRegistry(repo, clock, None)
    gate = Gate(repo, clock, GateInputs(settings, store, acct, registry, clock_health),
                GatePolicy.from_settings(gate_settings))
    executor = Executor(repo, broker, clock, kill_file, alerts=alerts)
    halts = HaltManager(repo, gate, executor, clock, alerts=alerts,
                        reference_time_of=lambda a: (lim.daily_reference_time
                                                     if (lim := settings.effective_limits(a)) else None))
    registry.attach_halts(halts)
    store.halts, store.alerts, store.account = halts, alerts, account
    resolver = UnknownResolver(repo, broker, executor, clock, wait=lambda s: None)
    reconciler = Reconciler(repo, broker, clock, halts, alerts)
    recovery = RecoveryChecker(repo, reconciler, halts, clock_health.clock_ok)
    ops = OpsProcessor(repo, gate, executor, halts, clock, reconciler=reconciler, resolver=resolver,
                       kill_file=kill_file)

    loaded: list[LoadedStrategy] = []
    for si in strategies:
        reg = registry.register(si.strategy.spec, dict(si.params), caps, account_limits, mode,
                                verification_approval=si.verification_approval)
        if not reg.ok:
            raise ConfigError([f"전략 등록 거부 {si.strategy.spec.owner}: {p}" for p in reg.problems])
        parsed = validate_params(si.strategy.spec, si.params)   # 전략에는 검증·형 변환된 값만 준다 (R-19)
        unfilled = None
        if "CAP-ORD-03" in si.strategy.spec.capabilities and "unfilled_max_minutes" in parsed:
            unfilled = int(str(parsed["unfilled_max_minutes"]))
        loaded.append(LoadedStrategy(si.strategy, parsed, si.eval_times, unfilled))

    watcher = None
    if alert_thresholds is not None:
        watcher = AlertWatcher(repo, alerts, AlertThresholds(account_limits.warn_usage_ratio, *alert_thresholds),
                               account_limits.daily_reference_time)
    return Engine(repo=repo, clock=clock, broker=broker, store=store, gate=gate, executor=executor, halts=halts,
                  resolver=resolver, reconciler=reconciler, recovery=recovery, ops=ops, registry=registry,
                  account_state=acct, reporter=DailyReporter(repo, alerts, gate_settings.cost_rates, price_of),
                  watcher=watcher, alerts=alerts, account=account,
                  limits=lambda: settings.effective_limits(account), strategies=loaded,
                  reconcile_every=reconcile_every)
