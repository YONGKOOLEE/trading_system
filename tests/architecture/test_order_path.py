"""주문 경로 단일성 (04 V-94, R-03, G-1).

- ApprovedIntent는 gate 패키지에서만 생성한다.
- 브로커 주문 함수(place_order, cancel_order)는 execution 패키지에서만 호출한다.
- Executor.submit은 ApprovedIntent가 아니면 거부한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "trading_system"
ORDER_CALLS = {"place_order", "cancel_order"}


def _calls(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name:
                out.append((name, node.lineno))
    return out


def _package(path: Path) -> str:
    return path.relative_to(SRC).parts[0]


def test_approved_intent_created_only_in_gate() -> None:
    bad = [
        f"{p.relative_to(SRC)}:{line}"
        for p in SRC.rglob("*.py")
        for name, line in _calls(p)
        if name == "ApprovedIntent" and _package(p) != "gate"
    ]
    assert bad == []


def test_broker_order_calls_only_in_execution() -> None:
    bad = [
        f"{p.relative_to(SRC)}:{line}"
        for p in SRC.rglob("*.py")
        for name, line in _calls(p)
        if name in ORDER_CALLS and _package(p) not in {"execution", "broker"}
    ]
    assert bad == []


def test_executor_rejects_non_approved_input(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from trading_system.broker.fake.broker import FakeBroker
    from trading_system.domain.clock import FixedClock
    from trading_system.domain.enums import Mode
    from trading_system.execution.executor import Executor
    from trading_system.storage.repository import Repository

    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    broker = FakeBroker()
    ex = Executor(repo, broker, FixedClock(datetime(2026, 10, 1, tzinfo=UTC)), tmp_path / "KILL")
    with pytest.raises(TypeError):
        ex.submit(object())  # type: ignore[arg-type]
    assert broker.sent == []
    repo.close()
