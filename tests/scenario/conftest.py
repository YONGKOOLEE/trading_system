"""T-001 시나리오 공통 준비. 모든 값은 가상이다 (05-plan §3).

가상 계좌 KR-BT-01 (기준 금액 10,000,000원), 가상 종목 TST-A 10,000원·TST-B 10,000원·TST-C 10,000원,
가상 한도 L2 20%, L3 50%.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from trading_system.broker.fake.broker import FakeBroker
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.models import Account, Limits, Signal
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.halt.manager import HaltManager
from trading_system.storage.repository import Repository

ACCOUNT = "KR-BT-01"
PRICES = {"TST-A": Decimal("10000"), "TST-B": Decimal("10000"), "TST-C": Decimal("10000")}
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


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    repo = Repository(tmp_path / "trading.db", Mode.BACKTEST)
    repo.register_account(Account(alias=ACCOUNT, mode=Mode.BACKTEST, broker_env="fake"))
    clock = FixedClock(T0)
    broker = FakeBroker()
    limits = Limits(base_amount=Decimal("10000000"), max_symbol_ratio=Decimal("0.20"), max_total_ratio=Decimal("0.50"))
    gate = Gate(repo, clock, {ACCOUNT: limits}, PRICES.__getitem__)
    kill_file = tmp_path / "KILL"
    executor = Executor(repo, broker, clock, kill_file)
    halts = HaltManager(repo, gate, executor, clock)
    e = Env(repo, clock, broker, gate, executor, halts, kill_file)
    yield e
    violations = check_invariants(repo.connection, Mode.BACKTEST.value)
    repo.close()
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
