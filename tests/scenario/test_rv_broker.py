"""PAPER-0 전 검토 결함 수정 — 결과 불명 해소·키움 어댑터 (docs/changes/review-2026-10-03-pre-paper0.md).

RV-08 AMBIGUOUS 후 무한 재조회·알림 / RV-09 전일 의도 미접수 오확정 / RV-10 return_code·목록 키 누락 /
RV-11 HTTP 401 비JSON / RV-12 계좌번호 형식 / RV-13 시장가 빈 가격 / RV-14 체결번호 정규화·조회 체결 자동 반영 /
RV-19 토큰 발급 호출 제한·대기·시간 상한. 모든 값은 가상이다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET, Scripted, Sim
from trading_system.broker.base import BrokerQueryError
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.broker.kiwoom import parse
from trading_system.broker.kiwoom.adapter import KiwoomAdapter, KiwoomConfig, build_fills, build_orders
from trading_system.broker.kiwoom.http import KiwoomHttp
from trading_system.broker.kiwoom.realtime import parse_order_event
from trading_system.broker.kiwoom.token import TokenError, TokenManager
from trading_system.broker.ratelimit import RateLimiter
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import IntentState, OrderType, RequestType, SubmitOutcome
from trading_system.domain.models import GateRequest, OrderEvent, OrderRequest
from trading_system.execution.resolver import ResolveOutcome, ResolveProgress, UnknownResolver

ALIAS = "KR-PAPER-01"
CODE = "005930"
T0 = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)  # 09:05 KST


# --- 공통 -------------------------------------------------------------------------------------------

def emergencies(env: Env) -> list[str]:
    return [json.loads(e["summary"])["message"] for e in env.repo.audit_events()
            if e["kind"] == "ALERT" and json.loads(e["summary"])["level"] == "EMERGENCY"
            and "결과 불명 미해소" in json.loads(e["summary"])["message"]]


def unknown_entry(env: Env, ev: str = "e1") -> str:
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation=ev)))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    assert env.repo.intent(d.approved.intent_id).state is IntentState.UNKNOWN
    return d.approved.intent_id


def engine_ticks(env: Env, r: UnknownResolver, progress: dict[str, ResolveProgress], ticks: int,
                 step: timedelta = timedelta(seconds=10)) -> None:
    """app/engine.py `_resolve_step`과 같은 반복 (엔진 코드 변경 없이 동작해야 한다)."""
    for _ in range(ticks):
        env.clock.advance(step)
        live = {i.intent_id for i in env.repo.intents_in_state(IntentState.UNKNOWN) if i.account == ACCOUNT}
        for gone in set(progress) - live:
            progress.pop(gone)
        for iid in sorted(live - set(progress)):
            progress[iid] = r.begin(iid)
        for iid, p in sorted(progress.items(), key=lambda kv: kv[1].next_at):
            if env.clock.now() < p.next_at:
                continue
            if r.step(p) is not None:
                progress.pop(iid, None)


def resolver(env: Env) -> UnknownResolver:
    return UnknownResolver(env.repo, env.broker, env.executor, env.clock, lambda s: env.clock.advance(
        timedelta(seconds=s)))


# --- RV-08 AMBIGUOUS 후 재시작 반복 금지 ------------------------------------------------------------

def test_rv08_ambiguous_is_not_restarted_every_tick(env: Env) -> None:
    iid = unknown_entry(env)
    env.broker.query_failures = 1          # 조회 실패 섞임 → AMBIGUOUS
    r = resolver(env)
    progress: dict[str, ResolveProgress] = {}
    engine_ticks(env, r, progress, 30)     # 5분: 1차 해소(62초) 후에도 계속 돈다
    after_first = env.broker.queries
    assert len(emergencies(env)) == 1
    engine_ticks(env, r, progress, 200)    # 이후 30여 분
    assert env.broker.queries == after_first           # 조회 반복 없음
    assert len(emergencies(env)) == 1                  # 긴급 알림 1회
    assert env.repo.intent(iid).state is IntentState.UNKNOWN
    assert "K3" in env.repo.active_halts(ACCOUNT)


def test_rv08_awaiting_state_survives_restart(env: Env) -> None:
    unknown_entry(env)
    env.broker.query_failures = 1
    engine_ticks(env, resolver(env), {}, 30)
    q = env.broker.queries
    engine_ticks(env, resolver(env), {}, 60)           # 재시작: 새 해소기·새 진행 표
    assert env.broker.queries == q and len(emergencies(env)) == 1


def test_rv08_operator_confirm_ends_awaiting(env: Env) -> None:
    iid = unknown_entry(env)
    env.broker.query_failures = 1
    r = resolver(env)
    progress: dict[str, ResolveProgress] = {}
    engine_ticks(env, r, progress, 30)
    env.halts.confirm_unknown(iid, False, "operator")
    engine_ticks(env, r, progress, 3)
    assert progress == {} and env.repo.intent(iid).state is IntentState.NOT_ACCEPTED


def test_rv08_new_unmatched_event_reopens_evaluation_once(env: Env) -> None:
    unknown_entry(env)
    env.broker.query_failures = 1
    r = resolver(env)
    progress: dict[str, ResolveProgress] = {}
    engine_ticks(env, r, progress, 30)
    q = env.broker.queries
    env.broker.inject(OrderEvent(kind="FILL", order_no="9999999", fill_id="X1", quantity=Decimal("1"),
                                 price=Decimal("10000")))   # 새 증거 (짝 없는 통보)
    engine_ticks(env, r, progress, 30)
    assert env.broker.queries > q                      # 다시 조회
    assert len(emergencies(env)) == 2                  # 새 증거로 다시 판정한 1회만 추가
    q2 = env.broker.queries
    engine_ticks(env, r, progress, 60)
    assert env.broker.queries == q2 and len(emergencies(env)) == 2


def test_rv08_sync_resolve_still_reevaluates(env: Env) -> None:
    """운영 명령의 동기 해소는 사람 대기 중이어도 다시 조회한다 (무한 대기 금지)."""
    iid = unknown_entry(env)
    env.broker.query_failures = 1
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    q = env.broker.queries
    # 요구사항 변경 A-084/CP-031: 조회 모두 성공·후보 없음이어도 자동 미접수 종결 없음 → 다시 조회하되 AMBIGUOUS(K3 유지)
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert env.broker.queries > q and env.repo.intent(iid).state is IntentState.UNKNOWN


def test_rv08_sync_resolve_finds_late_order_on_second_call(env: Env) -> None:
    """사람 대기(K3) 중 동기 재해소가 실제로 다시 조회한다: 1차 5회(10번 조회) 동안 안 보이던 접수 주문이
    조회 반영 지연 뒤 보이면 2차 동기 해소가 찾아 채택한다 (FOUND, 재전송 없음)."""
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    env.broker.query_delay = 10                         # 1차 해소의 조회 10번 동안 안 보임 (PV-05 반영 지연)
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation="e1")))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    iid = d.approved.intent_id
    assert env.repo.intent(iid).state is IntentState.UNKNOWN
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert env.broker.queries == 10 and env.repo.intent(iid).state is IntentState.UNKNOWN
    [order] = env.broker.query_orders_unchecked()
    assert resolver(env).resolve(iid) is ResolveOutcome.FOUND
    assert env.broker.queries == 12                     # 2차 1회차(주문+체결)에서 발견
    intent = env.repo.intent(iid)
    assert intent.state is not IntentState.UNKNOWN and intent.broker_order_key is not None
    assert intent.broker_order_key.endswith(f"|{order.order_no}")
    assert len(env.broker.sent) == 1                    # 재전송 없음


# --- RV-09 전일 전송 의도 ---------------------------------------------------------------------------

def _k3_reasons(env: Env) -> list[str]:
    return [h["reason"] for h in env.repo.halt_rows(None) if h["halt_type"] == "K3"]


def test_rv09_previous_trade_date_never_closed_not_accepted(env: Env) -> None:
    iid = unknown_entry(env)
    env.clock.advance(timedelta(days=1))
    q = env.broker.queries
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert env.repo.intent(iid).state is IntentState.UNKNOWN
    assert "K3" in env.repo.active_halts(ACCOUNT)
    # 날짜 방어선이 판정했음: 당일 조회로는 전일 주문을 볼 수 없으므로 조회하지 않고 바로 K3 (CP-031의 5회 소진 K3가 아님)
    assert env.broker.queries == q
    [reason] = _k3_reasons(env)
    assert "전송 거래일" in reason and "≠ 오늘" in reason


def test_rv09_date_rolls_over_during_rounds(env: Env) -> None:
    iid = unknown_entry(env)
    env.clock.advance(timedelta(hours=14, minutes=54, seconds=50))   # 23:59:55 KST — 해소 중 날짜 경과
    q = env.broker.queries
    assert resolver(env).resolve(iid) is ResolveOutcome.AMBIGUOUS
    assert env.repo.intent(iid).state is IntentState.UNKNOWN
    # 시작 23:59:50 KST: 1회차(:52)·2회차(:56)만 조회(주문+체결 ×2), 3회차(00:00:04)는 조회 전 날짜 방어선 → K3.
    # 5회 소진 경로(10번 조회)가 아님
    assert env.broker.queries - q == 4
    [reason] = _k3_reasons(env)
    assert "전송 거래일" in reason and "≠ 오늘" in reason


# --- 키움 시험 준비 ---------------------------------------------------------------------------------

@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def sim(clock: FixedClock) -> Iterator[Sim]:
    s = Sim(now=lambda: clock.now().astimezone(KST).replace(tzinfo=None))
    yield s
    s.close()


def adapter(sim: Sim, clock: FixedClock, account: str = ACCOUNT_NO, **kw: object) -> KiwoomAdapter:
    cfg = KiwoomConfig(ALIAS, lambda: account, lambda: APP_KEY, lambda: SECRET, rest_url=sim.url,
                       ws_url=sim.ws_url, per_tr_interval=0.0, order_wait=1.0, query_wait=2.0)
    return KiwoomAdapter(cfg, clock, http=KiwoomHttp(sim.url, 1.0, 0.5), **kw)  # type: ignore[arg-type]


def buy(qty: str = "10", order_type: OrderType = OrderType.MARKET, price: str = "0") -> OrderRequest:
    return OrderRequest("NEW", ALIAS, CODE, "BUY", Decimal(qty), order_type, Decimal(price))


def sent(sim: Sim, api: str) -> int:
    return sum(1 for a, _, _ in sim.log if a == api)


# --- RV-10 return_code·목록 키 누락 -----------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"acnt_ord_cntr_prst_array": []},                       # return_code 없음
    {"return_code": 0, "return_msg": "조회 완료"},           # 목록 키 없음
    {"return_code": 0, "acnt_ord_cntr_prst_array": None},   # 목록 null
], ids=["no-return-code", "no-list-key", "list-null"])
def test_rv10_missing_code_or_list_is_failure(sim: Sim, clock: FixedClock, body: dict) -> None:
    a = adapter(sim, clock)
    sim.script("kt00009", Scripted("body", body=body))
    with pytest.raises(BrokerQueryError):
        a.query_orders(ALIAS)


def test_rv10_explicit_empty_list_is_ok(sim: Sim, clock: FixedClock) -> None:
    assert adapter(sim, clock).query_orders(ALIAS) == []


def test_rv10_cash_without_return_code_is_failure(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    sim.script("kt00001", Scripted("body", body={"entr": "1", "ord_alow_amt": "1"}))
    with pytest.raises(BrokerQueryError):
        a.query_cash(ALIAS)


# --- RV-11 HTTP 401 비JSON ---------------------------------------------------------------------------

def test_rv11_order_401_nonjson_invalidates_token(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    assert a.place_order(buy()).outcome is SubmitOutcome.ACCEPTED
    issued = sent(sim, "au10001")
    sim.script("kt10000", Scripted("nonjson", status=401))
    assert a.place_order(buy()).outcome is SubmitOutcome.UNKNOWN       # 주문은 결과 불명 유지
    assert a.place_order(buy()).outcome is SubmitOutcome.ACCEPTED
    assert sent(sim, "au10001") == issued + 1                          # 다음 요청에서 재발급


def test_rv11_query_401_nonjson_takes_auth_retry_path(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    a.query_cash(ALIAS)
    issued = sent(sim, "au10001")
    sim.script("kt00001", Scripted("nonjson", status=401))
    assert a.query_cash(ALIAS).deposit > 0                                # 재발급 후 같은 3회 안에서 성공
    assert sent(sim, "au10001") == issued + 1


# --- RV-12 계좌번호 형식 -----------------------------------------------------------------------------

@pytest.mark.parametrize("configured,received,ok", [
    ("9999999999", "99999999", True),        # 응답 8자리, 설정 10자리
    ("99999999", "9999999999", True),
    ("9999-9999-99", "9999999999", True),    # 구분자
    ("9999999999", " 99999999-99 ", True),
    ("9999999999", "1111111111", False),
    ("9999999999", "99999998", False),
    ("9999999999", "9999", False),           # 8자리 미만은 비교 불가 → 불일치(보수적)
    ("9999999999", "abc", False),
])
def test_rv12_account_number_normalized(sim: Sim, clock: FixedClock, configured: str, received: str,
                                        ok: bool) -> None:
    crit: list[tuple[str, str]] = []
    a = adapter(sim, clock, account=configured, on_critical=lambda k, m: crit.append((k, m)))
    assert a._check_account(received, "00") is ok  # noqa: SLF001
    assert (crit == []) is ok
    assert all(received.strip() not in m and configured not in m for _, m in crit)   # 계좌번호를 남기지 않는다


# --- RV-13 시장가 빈 가격 ----------------------------------------------------------------------------

def _row(no: str = "0000001", **kw: str) -> dict:
    r = {"ord_no": no, "stk_cd": "A005930", "io_tp_nm": "현금매수", "mdfy_cncl_tp": "", "ord_qty": "10",
         "ord_uv": "", "trde_tp": "시장가", "orig_ord_no": "0000000", "cntr_no": "", "cntr_qty": "", "cntr_uv": "",
         "cntr_tm": "", "acpt_tp": "접수"}
    r.update(kw)
    return r


def test_rv13_market_order_empty_price_parses_as_zero() -> None:
    [o] = build_orders([_row()], [{"ord_no": "0000001", "stk_cd": "005930", "ord_qty": "10", "oso_qty": "10",
                                   "ord_pric": "", "io_tp_nm": "+매수", "trde_tp": "시장가",
                                   "orig_ord_no": "0000000"}])
    assert (o.price, o.order_type, o.state) == (Decimal("0"), OrderType.MARKET, "ACCEPTED")


def test_rv13_limit_order_empty_price_still_fails() -> None:
    with pytest.raises(parse.KiwoomParseError):
        build_orders([_row(trde_tp="보통")], [])


def test_rv13_query_with_market_order_row(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    sim.extra_rows.append(_row("0000088"))
    [o] = a.query_orders(ALIAS)
    assert (o.order_no, o.price) == ("0000088", Decimal("0"))


# --- RV-14 체결번호 정규화, 조회 체결 자동 반영 없음 ---------------------------------------------------

def test_rv14_fill_numbers_normalized_identically() -> None:
    day = T0.astimezone(KST).date()
    base = {"9203": "0000005", "904": "0000000", "908": "090501", "905": "+매수", "900": "10", "901": "70000",
            "913": "체결", "914": "70100", "915": "3"}
    rt = {parse_order_event({**base, "909": v}, day, ALIAS).fill_id for v in ("7", "0000007", " 00000007 ")}
    q = {f.fill_id for v in ("7", "0000007", "000000000007")
         for f in build_fills([_row("0000005", cntr_no=v, cntr_qty="3", cntr_uv="70100", cntr_tm="09:05:01")], day)}
    assert len(rt) == 1 and rt == q


def test_rv14_build_fills_dedups_differently_padded_same_fill() -> None:
    day = T0.astimezone(KST).date()
    rows = [_row(cntr_no="0000007", cntr_qty="3", cntr_uv="70100", cntr_tm="09:05:01"),
            _row(cntr_no="7", cntr_qty="3", cntr_uv="70100", cntr_tm="09:05:01")]
    assert len(build_fills(rows, day)) == 1
    [o] = build_orders(rows, [])
    assert o.filled_quantity == Decimal("3")


def test_rv14_resolver_does_not_adopt_kiwoom_query_fills(tmp_path: Path, sim: Sim, clock: FixedClock) -> None:
    from tests.scenario.test_t014_kiwoom import paper_env, signal
    repo, a, gate, ex = paper_env(tmp_path, sim, clock)
    sim.auto_fill = True
    sim.script("kt10000", Scripted("timeout", apply=True))
    d = gate.decide(GateRequest.from_signal(signal("rv14")))
    assert ex.submit(d.approved) is IntentState.UNKNOWN
    assert UnknownResolver(repo, a, ex, clock, lambda s: None).resolve(d.approved.intent_id) is ResolveOutcome.FOUND
    assert repo.intent(d.approved.intent_id).state is IntentState.ACCEPTED   # 주문만 채택 (K-U14)
    assert repo.position(ALIAS, CODE).quantity == Decimal("0")             # 체결은 실시간·대조·사람
    assert sent(sim, "kt10000") == 1
    repo.close()


# --- RV-19 토큰 발급 호출 제한·대기·시간 상한 --------------------------------------------------------

class _Mono:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_rv19_issue_goes_through_limiter_with_backoff(sim: Sim, clock: FixedClock) -> None:
    mono = _Mono()
    limiter = RateLimiter(0.0, monotonic=mono)
    tm = TokenManager(KiwoomHttp(sim.url, 1.0, 0.5), lambda: APP_KEY, lambda: SECRET, clock,
                      limiter=limiter, sleep=mono.sleep, monotonic=mono)
    sim.token_fail = 3
    with pytest.raises(TokenError):
        tm.token()
    assert sent(sim, "au10001") == 3
    assert [tr for tr, _ in limiter.grants] == ["au10001"] * 3            # 호출 제한기 통과
    assert len(mono.sleeps) == 2 and mono.sleeps[0] > 0 and mono.sleeps[1] > mono.sleeps[0]   # 시도 사이 대기


def test_rv19_issue_total_time_bounded(sim: Sim, clock: FixedClock) -> None:
    mono = _Mono()
    tm = TokenManager(KiwoomHttp(sim.url, 1.0, 0.5), lambda: APP_KEY, lambda: SECRET, clock,
                      limiter=RateLimiter(0.0, monotonic=mono), sleep=mono.sleep, monotonic=mono, issue_budget=1.5)
    sim.token_fail = 3
    with pytest.raises(TokenError):
        tm.token()
    assert sent(sim, "au10001") < 3                                       # 시간 상한을 넘기는 시도는 하지 않음
    assert mono.t <= 1.5


def test_rv19_adapter_shares_limiter_with_token_issue(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    a.place_order(buy())
    assert "au10001" in [tr for tr, _ in a.limiter.grants]
