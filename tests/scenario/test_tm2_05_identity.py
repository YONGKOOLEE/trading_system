"""T-M2-05 고객 주문 ID·식별 범위·결과 불명 해소 (CP-024 D-110·D-111·D-112, 승인 A-066·A-071).

검증: V-139(고객 주문 ID 의도별 유일·형식·재시작 후 같은 ID·재사용 금지), V-138(M2 해소: 고객 주문 ID로 조회 → 채택 /
-2013이면 allOrders·myTrades 교차 / 5회 모두 없음 → K3, 자동 미접수 종결 없음 / 전송 90일 이내 -2026 → K3, 재전송 0),
V-155(해소기 부분: UTC 자정 통과만으로 K3 아님, 전송 후 24시간 초과 → K3), 식별 범위 시장별(M1 (계좌, 거래일, 주문번호)
유지 / M2 (계좌, 심볼, orderId)), V-152(M1 해소 규칙 유지 — 기존 M1 시험이 담당, 여기서는 M1 키 형식 불변만).
B-U3(접수 직후 조회 반영 지연)·B-U21(체결 0 EXPIRED 주문의 고객 주문 ID 조회)은 미확인 — 합성(fake) 동작으로만 고정한다.
모든 값은 가상이다. 네트워크 없음. 매 시험 끝에 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, RULES, T0, M2Env, build_m2_env, ioc_buy_price
from trading_system.broker.binance.orders import client_order_id as binance_client_order_id
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import GateResult, HaltType, IntentState, Mode, OrderType, RequestType
from trading_system.domain.models import GateRequest, Signal, client_order_id_of
from trading_system.execution.executor import Executor
from trading_system.execution.resolver import ResolveOutcome
from trading_system.execution.spot_resolver import SpotUnknownResolver
from trading_system.storage.repository import OrderIdentity, Repository

STRATEGY = "tm205-fixed"  # 테스트 전용 고정 전략 (실제 매매전략 아님)
D = Decimal
CID = re.compile(r"[A-Za-z0-9_-]{1,36}")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def signal(env: M2Env, kind: RequestType = RequestType.ENTRY, instrument: str = "TSTA-USDT",
           quantity: str = "0.1000", evaluation: str = "eval-1") -> Signal:
    rule = RULES[instrument]
    buying = kind in (RequestType.ENTRY, RequestType.INCREASE)
    px = ioc_buy_price(rule.last, rule.tick_size) if buying else rule.last
    now = env.clock.now()
    return Signal(strategy_id=STRATEGY, strategy_version=1, evaluation_id=evaluation, account=ACCOUNT,
                  instrument=instrument, rule_id="R1", kind=kind, quantity=D(quantity),
                  order_type=OrderType.LIMIT if buying else OrderType.MARKET, price=px,
                  as_of=now - timedelta(minutes=1), valid_until=now + timedelta(hours=30))


def submit(env: M2Env, sig: Signal):  # type: ignore[no-untyped-def]
    d = env.gate.decide(GateRequest.from_signal(sig))
    assert d.result is GateResult.APPROVED, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    return d.approved.intent_id


def resolver(env: M2Env) -> SpotUnknownResolver:
    return SpotUnknownResolver(env.repo, env.broker, env.executor, env.clock,
                               lambda s: env.clock.advance(timedelta(seconds=s)))


def k3(env: M2Env) -> bool:
    return HaltType.K3.value in env.repo.active_halts(ACCOUNT)


# --- V-139 고객 주문 ID ------------------------------------------------------------------------------------

def test_v139_client_order_id_format_and_determinism() -> None:
    a = client_order_id_of("sig-abc")
    assert CID.fullmatch(a) and len(a) <= 36
    assert a == client_order_id_of("sig-abc")                  # 같은 멱등키 → 같은 ID (재시작 후 같은 의도)
    assert a != client_order_id_of("sig-abd")                  # 다른 의도 → 다른 ID
    assert a == binance_client_order_id("sig-abc")             # 어댑터 규칙(T-M2-10)과 같은 값
    with pytest.raises(ValueError):
        client_order_id_of("")


def test_v139_sent_order_carries_client_id_and_mapping_is_stored(env: M2Env) -> None:
    i1 = submit(env, signal(env))
    i2 = submit(env, signal(env, instrument="TSTB-USDT", quantity="100.0", evaluation="e2"))
    sent = [r.client_order_id for r in env.broker.sent]
    assert sent == [client_order_id_of(idem(env, i)) for i in (i1, i2)]
    assert len(set(sent)) == 2 and all(CID.fullmatch(c or "") for c in sent)
    assert env.repo.client_order_of(i1) == sent[0] and env.repo.intent_of_client_order(sent[1]) == i2


def idem(env: M2Env, intent_id: str) -> str:
    return env.repo.select("SELECT idem_key FROM intent WHERE intent_id=?", (intent_id,))[0]["idem_key"]


def test_v139_same_id_after_restart(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    iid = submit(env, signal(env))
    before = env.repo.client_order_of(iid)
    env.executor = Executor(env.repo, env.broker, env.clock, env.kill_file, client_ids=True)   # 재시작
    env.executor.recover_on_start()
    assert env.repo.client_order_of(iid) == before == client_order_id_of(idem(env, iid))
    assert resolver(env).resolve(iid) is ResolveOutcome.FOUND   # 재시작 뒤에도 같은 ID로 조회해 채택 (재전송 0)
    assert len(env.broker.sent) == 1


def test_v139_reused_client_id_is_never_sent(env: M2Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(signal(env)))
    with env.repo.tx():   # 가상: 같은 ID가 이미 다른 의도에 쓰였음 (해시 충돌·재사용 상황 재현)
        env.repo.insert_client_order(client_order_id_of(idem(env, d.approved.intent_id)), "int-other", ACCOUNT,
                                     "TSTA-USDT", env.clock.now())
    assert env.executor.submit(d.approved) is IntentState.NOT_SENT
    assert env.broker.sent == []
    assert "재사용" in (env.repo.select("SELECT closed_reason FROM intent WHERE intent_id=?",
                                       (d.approved.intent_id,))[0]["closed_reason"] or "")


def test_m1_executor_sends_no_client_id(tmp_path: Path) -> None:
    from tests.scenario.conftest import build_env, make_signal
    e = build_env(tmp_path)
    try:
        d = e.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
        e.executor.submit(d.approved)
        assert e.broker.sent[0].client_order_id is None
        assert e.repo.select("SELECT * FROM client_order") == []
    finally:
        e.repo.close()


# --- 식별 범위: M1 (계좌, 거래일, 주문번호) / M2 (계좌, 심볼, orderId) ---------------------------------------------

def test_m1_order_key_format_unchanged(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "m1.db", Mode.BACKTEST)
    try:
        assert repo.order_identity is OrderIdentity.TRADE_DATE
        at = datetime(2026, 10, 7, 15, 30, tzinfo=UTC)   # KST 10-08 00:30
        assert repo.broker_order_key("KR-BT-01", at, "0000001", "TST-A") == "KR-BT-01|2026-10-08|0000001"
        assert Repository.order_key("KR-BT-01", at, "0000001") == "KR-BT-01|2026-10-08|0000001"
    finally:
        repo.close()


def test_m2_order_identity_is_symbol_scoped(env: M2Env) -> None:
    repo = env.repo
    assert repo.order_identity is OrderIdentity.SYMBOL
    at = datetime(2026, 10, 7, 23, 59, tzinfo=UTC)
    with repo.tx():
        ka = repo.insert_broker_order(account=ACCOUNT, order_no="77", at=at, intent_id=None, instrument="TSTA-USDT",
                                      side="BUY", quantity=D("1"), origin="EXTERNAL")
        kb = repo.insert_broker_order(account=ACCOUNT, order_no="77", at=at, intent_id=None, instrument="TSTB-USDT",
                                      side="BUY", quantity=D("1"), origin="EXTERNAL")
    assert ka != kb   # orderId는 심볼 안에서만 유일 (B-U17)
    assert repo.broker_order_by_no("77", "아무 날짜", ACCOUNT, instrument="TSTB-USDT").order_key == kb
    assert repo.broker_order_by_no("77", "2026-10-09", ACCOUNT, instrument="TSTA-USDT").order_key == ka  # 날짜 무관
    assert repo.broker_order_by_no("77", "2026-10-07", ACCOUNT) is None   # 심볼 없이 둘이면 특정하지 않음
    with pytest.raises(ValueError), repo.tx():   # 같은 (계좌, 심볼, orderId)는 다시 만들지 않음
        repo.insert_broker_order(account=ACCOUNT, order_no="77", at=at + timedelta(days=1), intent_id=None,
                                 instrument="TSTA-USDT", side="BUY", quantity=D("1"), origin="EXTERNAL")


def test_m2_events_after_kst_midnight_still_match_order(env: M2Env) -> None:
    """M2 체결 통보는 거래일이 아니라 (계좌, 심볼, orderId)로 찾는다 — KST 날짜가 바뀐 뒤 통보도 반영 (D-110).
    M1 키(KST 거래일)였다면 짝을 찾지 못해 보류된다."""
    env.clock.advance(datetime(2026, 10, 7, 14, 59, 50, tzinfo=UTC) - env.clock.now())   # KST 23:59:50
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    iid = submit(env, signal(env))
    env.clock.advance(timedelta(seconds=30))   # KST 10-08 00:00:20
    [no] = env.broker.open_order_nos()
    env.broker.fill(no)
    env.executor.process_events()
    assert env.repo.intent(iid).state is IntentState.FILLED
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0999")


# --- V-138 결과 불명 해소 (M2) -------------------------------------------------------------------------------

def test_v138_lost_accept_found_by_client_id_and_fills_applied(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_FILL   # 거래소는 접수·전량 체결, 응답 유실
    env.broker.drop_events()
    d = env.gate.decide(GateRequest.from_signal(signal(env)))
    env.executor.submit(d.approved)
    env.broker.drop_events()                        # 실시간 통보도 유실 — 조회로만 알게 됨
    iid = d.approved.intent_id
    assert env.repo.intent(iid).state is IntentState.UNKNOWN
    assert resolver(env).resolve(iid) is ResolveOutcome.FOUND
    assert env.repo.intent(iid).state is IntentState.FILLED
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0999")   # 조회 체결의 수수료로 순 수령
    assert env.broker.lookups[0][1] == {"client_order_id": env.repo.client_order_of(iid)}   # 고객 주문 ID로 조회
    assert len(env.broker.sent) == 1 and not k3(env)                          # 재전송 0


def test_v138_not_found_then_all_orders_cross_check_adopts(env: M2Env) -> None:
    """-2013(접수 직후 반영 지연 가능, B-U3 미확인 — 합성) → 같은 회차 allOrders에서 고객 주문 ID로 발견 → 채택."""
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    env.broker.lookup_not_found = 99               # 단건 조회는 계속 -2013
    iid = submit(env, signal(env))
    assert resolver(env).resolve(iid) is ResolveOutcome.FOUND
    assert env.repo.intent(iid).state is IntentState.ACCEPTED
    assert env.broker.window_queries and len(env.broker.sent) == 1


def test_v138_five_rounds_nothing_goes_k3_without_auto_close(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN     # 응답 없음, 거래소도 받지 않음 (가상)
    iid = submit(env, signal(env))
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert env.repo.intent(iid).state is IntentState.UNKNOWN   # 미접수 자동 종결 없음
    assert k3(env) and len(env.broker.sent) == 1
    assert len(env.broker.lookups) == 5
    assert env.repo.active_reservations(ACCOUNT)                # 예약 유지 (재진입 차단)


def test_v138_archived_within_90_days_goes_k3(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    env.broker.archived = True                      # -2026 (가상)
    iid = submit(env, signal(env))
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert k3(env) and len(env.broker.lookups) == 1 and env.repo.intent(iid).state is IntentState.UNKNOWN


def test_v138_query_failures_go_k3(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    iid = submit(env, signal(env))
    env.broker.query_failures = 99
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert k3(env) and env.repo.intent(iid).state is IntentState.UNKNOWN


def test_v138_unattributed_trades_go_k3(env: M2Env) -> None:
    """myTrades에 우리가 모르는 orderId의 체결이 있으면 이 의도의 것인지 알 수 없다 → K3 (자동 채택 안 함)."""
    env.broker.behavior = FakeBehavior.UNKNOWN
    iid = submit(env, signal(env))
    ext = env.broker.add_external_order(ACCOUNT, "TSTA-USDT", "BUY", D("0.0500"), D("10050.00"))
    env.broker.fill(ext)
    env.broker.drop_events()
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert k3(env) and env.repo.intent(iid).state is IntentState.UNKNOWN


# --- V-155(해소기): UTC 자정 통과만으로 K3 아님, 전송 후 24시간 초과 → K3 ---------------------------------------------

def test_v155_unknown_across_utc_midnight_resolves(env: M2Env) -> None:
    env.clock.advance(datetime(2026, 10, 7, 23, 49, 50, tzinfo=UTC) - env.clock.now())   # 주문 창(~23:50) 안
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    iid = submit(env, signal(env))
    env.clock.advance(timedelta(minutes=11))        # UTC 자정 통과 뒤 해소 시작
    assert resolver(env).resolve(iid) is ResolveOutcome.FOUND
    assert not k3(env)


def test_v155_more_than_24h_after_send_goes_k3(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    iid = submit(env, signal(env))
    env.clock.advance(timedelta(hours=24, seconds=1))
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert k3(env) and env.broker.lookups == []      # 조회 창(24시간) 밖 — 조회하지 않고 사람 확인


def test_spot_resolver_requires_unknown_intent(env: M2Env) -> None:
    iid = submit(env, signal(env))
    with pytest.raises(ValueError):
        resolver(env).resolve(iid)
