"""CP-018 반영 준비 작업: 정지·알림 단일 경로(03 D-093), 의존 규칙(03 §7 v0.5)."""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_rules import check_source
from tests.scenario.conftest import ACCOUNT, Env, make_signal
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import HaltType, Mode, RequestType
from trading_system.domain.models import GateRequest
from trading_system.execution.resolver import ResolveOutcome, UnknownResolver
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import Repository


def test_k3_from_resolver_goes_through_halt_manager_without_duplicates(env: Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, quantity="10")))
    env.executor.submit(d.approved)
    env.halts.activate(HaltType.K3, "test", "먼저 발동", ACCOUNT, instrument="TST-A")
    env.broker.query_failures = 1
    r = UnknownResolver(env.repo, env.broker, env.executor, env.clock, lambda s: None)
    assert r.resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS
    rows = [h for h in env.repo.halt_rows(ACCOUNT) if h["halt_type"] == "K3"]
    assert len(rows) == 1  # 관리자의 중복 확인을 거침 (직접 기록이면 2건)


def test_alert_falls_back_to_file_when_storage_fails(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "a.db", Mode.BACKTEST)
    clock = FixedClock(__import__("tests.scenario.conftest", fromlist=["T0"]).T0)
    fallback = tmp_path / "logs" / "alerts-fallback.log"
    sink = AuditAlertSink(repo, clock, fallback)
    repo.fail_next_transaction = True
    sink.alert("EMERGENCY", "정지 K7", "halt-1")
    assert "EMERGENCY\thalt-1\t정지 K7" in fallback.read_text(encoding="utf-8")
    sink.alert("INFO", "정상", None)
    assert [e["kind"] for e in repo.audit_events()] == ["ALERT"]
    repo.close()


def test_storage_failure_after_send_k7_goes_through_halt_manager(env: Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, quantity="10")))
    original = env.broker.place_order

    def place_then_break(req):  # type: ignore[no-untyped-def]
        result = original(req)
        env.repo.fail_next_transaction = True
        return result

    env.broker.place_order = place_then_break  # type: ignore[method-assign]
    try:
        env.executor.submit(d.approved)
    except Exception:  # noqa: BLE001 — StorageFailure 예상
        pass
    env.broker.place_order = original  # type: ignore[method-assign]
    assert env.repo.active_halts(ACCOUNT) == ["K7"]
    assert env.executor._halts is env.halts  # 실행기의 정지 발동이 관리자로 연결됨


def test_new_dependency_rules_detect_violations() -> None:
    bad = {
        "trading_system.storage.x": "from trading_system.gate.gate import Gate",
        "trading_system.marketdata.x": "from trading_system.execution.executor import Executor",
        "trading_system.reconciliation.x": "import trading_system.gate.gate",
        "trading_system.monitoring.x": "from trading_system.broker.base import BrokerAdapter",
        "trading_system.evaluation.x": "from trading_system.gate.ports import GateInputs",
    }
    for module, src in bad.items():
        assert check_source(src, module), module
    assert check_source("from trading_system.domain.enums import Mode", "trading_system.storage.x") == []
