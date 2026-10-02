"""T-001 시나리오 공통 준비. 모든 값은 가상이다 (05-plan §3).

가상 계좌 KR-BT-01 (기준 금액 10,000,000원), 가상 종목 TST-A~TST-E 각 10,000원,
가상 한도 L1 20%, L2 20%, L3 50% (나머지는 tests/support/gate_fakes.scenario_limits).
T-001 시나리오의 금액 경계를 유지하려고 가상 상한가를 현재가와 같게 둔다(시장가 예약 가격 = 상한가, 03 §6.2).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.gate_fakes import TEST_POLICY, Facts, make_facts
from tests.support.invariants import check_invariants
from trading_system.broker.fake.broker import FakeBroker
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.models import Account, Signal
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.halt.manager import HaltManager
from trading_system.storage.repository import Repository

ACCOUNT = "KR-BT-01"
PRICES = {inst: Decimal("10000") for inst in ("TST-A", "TST-B", "TST-C", "TST-D", "TST-E")}
T0 = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)  # 09:05 KST


@dataclass
class Env:
    repo: Repository
    clock: FixedClock
    broker: FakeBroker
    gate: Gate
    executor: Executor
    halts: HaltManager
    kill_file: Path
    facts: Facts


def build_env(tmp_path: Path) -> Env:
    """시나리오 환경 하나를 만든다 (fixture와 무작위 실행기가 함께 쓴다)."""
    repo = Repository(tmp_path / "trading.db", Mode.BACKTEST)
    repo.register_account(Account(alias=ACCOUNT, mode=Mode.BACKTEST, broker_env="fake"))
    clock = FixedClock(T0)
    broker = FakeBroker()
    facts = make_facts(ACCOUNT, clock.now)
    for inst, price in PRICES.items():
        facts.market.add(inst, str(price), upper=str(price))
    gate = Gate(repo, clock, facts.inputs(), TEST_POLICY)
    kill_file = tmp_path / "KILL"
    executor = Executor(repo, broker, clock, kill_file)
    halts = HaltManager(repo, gate, executor, clock,
                        reference_time_of=lambda a: (lim.daily_reference_time
                                                     if (lim := facts.limits.by_account.get(a)) else None))
    return Env(repo, clock, broker, gate, executor, halts, kill_file, facts)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = build_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def make_signal(
    kind: RequestType,
    instrument: str = "TST-A",
    quantity: str = "100",
    strategy: str = "t001-fixed",
    evaluation: str = "eval-1",
    rule: str = "R1",
    valid_for: timedelta = timedelta(minutes=30),
) -> Signal:
    """테스트 전용 고정 전략 t001-fixed의 신호 (실제 매매전략 아님)."""
    return Signal(
        strategy_id=strategy,
        strategy_version=1,
        evaluation_id=evaluation,
        account=ACCOUNT,
        instrument=instrument,
        rule_id=rule,
        kind=kind,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        price=PRICES[instrument],
        as_of=T0 - timedelta(minutes=5),
        valid_until=T0 + valid_for,
    )
