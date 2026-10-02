"""검토(2026-10-02)에서 찾은 변경·누락·중복·권한 오류 사례 (docs/changes/CP-016-review-2026-10-02.md 표 B).

정상 경로는 test_t001_flow.py가 다룬다. 여기서는 비정상 입력·순서·실패에서 위험이 커지지 않는지 본다.
모든 값은 가상이다.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, PRICES, Env, make_signal
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import Check, GateResult, Mode, OrderType, RequestSource, RequestType, Side
from trading_system.domain.models import GateRequest, OrderEvent
from trading_system.execution.executor import ExecutionBlocked
from trading_system.storage.repository import ModeMismatch, Repository, StorageFailure


def _req(**kw: object) -> GateRequest:
    base = dict(source=RequestSource.SIGNAL, request_type=RequestType.ENTRY, account=ACCOUNT, instrument="TST-A",
                side=Side.BUY, quantity=Decimal("10"), order_type=OrderType.MARKET, price=Decimal("10000"),
                idem_key="k-1", owner="t001-fixed:1")
    base.update(kw)
    return GateRequest(**base)  # type: ignore[arg-type]


def _hold(env: Env, qty: str = "100") -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, quantity=qty)))
    env.executor.submit(d.approved)
    env.executor.process_events()


# --- B-1 저장소 모드: 다른 모드로 기존 DB를 열면 거부 (R-01, INV-12) ------------------

def test_b1_reopen_db_with_other_mode_refused(tmp_path: Path) -> None:
    p = tmp_path / "t.db"
    Repository(p, Mode.BACKTEST).close()
    with pytest.raises(ModeMismatch):
        Repository(p, Mode.PAPER)
    Repository(p, Mode.BACKTEST).close()  # 같은 모드는 다시 열린다


# --- B-2 요청 형식·권한 (01 G3·G10, R-21) ---------------------------------------

@pytest.mark.parametrize(("kw", "check"), [
    ({"quantity": Decimal("0")}, Check.G10),
    ({"quantity": Decimal("-5")}, Check.G10),
    ({"price": Decimal("0")}, Check.G10),
    ({"side": Side.SELL}, Check.G3),                                   # ENTRY인데 매도
    ({"source": RequestSource.OPERATOR, "owner": None}, Check.G3),     # 운영 요청으로 신규 진입 (R-21)
    ({"owner": None}, Check.G3),                                       # 소유자 없는 진입
])
def test_b2_malformed_or_unauthorized_requests_rejected(env: Env, kw: dict, check: Check) -> None:
    d = env.gate.decide(_req(**kw))
    assert d.result is GateResult.REJECTED and d.failed_check is check
    assert env.repo.intents() == [] and env.broker.sent == []


def test_b2_negative_quantity_sell_rejected(env: Env) -> None:
    _hold(env)
    d = env.gate.decide(_req(request_type=RequestType.REDUCE, side=Side.SELL, quantity=Decimal("-5"), idem_key="k-2"))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G10


# --- B-3 시세 없음: 예외가 아니라 거부 (CLAUDE.md §8-10) ------------------------------

def test_b3_missing_price_rejects_instead_of_crashing(env: Env) -> None:
    d = env.gate.decide(_req(instrument="TST-Z", idem_key="k-z"))  # 가격표에 없는 종목
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G11
    assert env.repo.decision(d.decision_id) is not None  # 판정은 기록된다


# --- B-4 전송 후 저장 실패: 이후 전송을 막는다 (R-25, 불확실하면 주문하지 않음) --------------

def test_b4_storage_failure_after_send_blocks_further_sends(env: Env) -> None:
    d1 = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-A")))
    d2 = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-B", evaluation="e2")))
    original = env.broker.place_order

    def place_then_break_storage(req):
        result = original(req)
        env.repo.fail_next_transaction = True  # 브로커 전송 직후 저장소 장애
        return result

    env.broker.place_order = place_then_break_storage  # type: ignore[method-assign]
    with pytest.raises(StorageFailure):
        env.executor.submit(d1.approved)
    env.broker.place_order = original  # type: ignore[method-assign]
    sent = len(env.broker.sent)
    with pytest.raises(ExecutionBlocked):
        env.executor.submit(d2.approved)
    assert len(env.broker.sent) == sent


# --- B-5 비정상 체결 통보는 반영하지 않고 기록 (R-08, INV-04) -----------------------------

@pytest.mark.parametrize("bad", ["missing_id", "zero_qty", "over_fill", "bad_price"])
def test_b5_anomalous_fill_not_applied(env: Env, bad: str) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, quantity="10")))
    env.executor.submit(d.approved)
    order_no = env.repo.intent(d.approved.intent_id).broker_order_key.split("|")[-1]
    ev = {
        "missing_id": OrderEvent("FILL", order_no, None, Decimal("10"), Decimal("10000")),
        "zero_qty": OrderEvent("FILL", order_no, "F1", Decimal("0"), Decimal("10000")),
        "over_fill": OrderEvent("FILL", order_no, "F1", Decimal("11"), Decimal("10000")),
        "bad_price": OrderEvent("FILL", order_no, "F1", Decimal("10"), Decimal("0")),
    }[bad]
    env.broker.inject(ev)
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("0")
    assert any(e["kind"] == "EVENT_ANOMALY" for e in env.repo.audit_events())


def test_b5_sell_fill_beyond_position_not_applied_and_batch_continues(env: Env) -> None:
    _hold(env, "100")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.REDUCE, quantity="50", evaluation="e2")))
    env.executor.submit(d.approved)
    sell_no = env.repo.intent(d.approved.intent_id).broker_order_key.split("|")[-1]
    with env.repo.tx():  # 외부 수동 매도로 내부 원장보다 실제 보유가 줄어든 상황을 흉내
        env.repo.set_position(ACCOUNT, "TST-A", Decimal("30"), Decimal("10000"))
    env.broker.inject(OrderEvent("FILL", sell_no, "S1", Decimal("50"), Decimal("10000")))
    # 같은 배치의 다음 정상 통보는 처리되어야 한다
    env.broker.behavior = FakeBehavior.FILL
    d2 = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-B", evaluation="e3")))
    env.executor.submit(d2.approved)
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("30")
    assert env.repo.position(ACCOUNT, "TST-B").quantity == Decimal("100")
    assert any(e["kind"] == "EVENT_ANOMALY" for e in env.repo.audit_events())
    # 테스트가 만든 원장 불일치(30주 < 활성 매도 50주)는 INV-06이 잡는다. 확인 후 되돌린다 (실제로는 K4 대조, T-008)
    from tests.support.invariants import check_invariants
    assert any(v.startswith("INV-06") for v in check_invariants(env.repo.connection, "BACKTEST"))
    with env.repo.tx():
        env.repo.set_position(ACCOUNT, "TST-A", Decimal("100"), Decimal("10000"))


# --- B-6 통보 처리 중 저장 실패: 남은 통보를 잃지 않는다 ------------------------------------

def test_b6_events_kept_when_storage_fails_midway(env: Env) -> None:
    env.broker.behavior = FakeBehavior.FILL
    d1 = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-A")))
    d2 = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-B", evaluation="e2")))
    env.executor.submit(d1.approved)
    env.executor.submit(d2.approved)  # 체결 통보 2건 대기
    env.repo.fail_next_transaction = True
    with pytest.raises(StorageFailure):
        env.executor.process_events()
    env.executor.process_events()  # 복구 후 다시 처리
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("100")
    assert env.repo.position(ACCOUNT, "TST-B").quantity == Decimal("100")


# --- B-7 같은 승인 의도를 동시에 두 번 전송하지 않는다 (INV-08, R-05) -----------------------

def test_b7_concurrent_double_submit_sends_once(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
    errors: list[BaseException] = []

    def run() -> None:
        try:
            env.executor.submit(d.approved)
        except (ValueError, StorageFailure) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(env.broker.sent) == 1
    assert len(errors) == 3


# --- B-8 다른 스레드가 커밋 전 데이터를 읽지 않는다 (공유 연결) ------------------------------

def test_b8_reader_never_sees_uncommitted_rows(env: Env) -> None:
    started, seen = threading.Event(), []
    original = env.repo.insert_intent

    def insert_then_wait_and_fail(row):
        original(row)
        started.set()
        time.sleep(0.3)  # 다른 스레드가 읽기를 시도할 시간
        raise RuntimeError("판정 도중 실패 → 롤백")

    env.repo.insert_intent = insert_then_wait_and_fail  # type: ignore[method-assign]

    def decide() -> None:
        with pytest.raises(RuntimeError):
            env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))

    t = threading.Thread(target=decide)
    t.start()
    assert started.wait(5)
    seen.extend(env.repo.intents())  # 잠금이 없으면 롤백될 의도를 보게 된다
    t.join()
    assert seen == []
    assert PRICES  # conftest 가격표 사용 표시
