"""T-014 키움 어댑터 (모의 전용, 네트워크 없이 루프백 합성 서버) — 05-plan T-014,
04 V-49·V-50·V-69·V-70·V-71·V-103·V-105, 03 D-041~D-050·D-059·D-094.

실제 키움 서버에 연결하지 않는다. 응답은 명세 필드 이름을 따른 합성 데이터(tests/support/kiwoom_sim.py).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.gate_fakes import TEST_POLICY, make_facts
from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET, Scripted, Sim
from trading_system.broker.base import BrokerQueryError
from trading_system.broker.fake.broker import FakeBehavior, FakeBroker
from trading_system.broker.kiwoom import parse
from trading_system.broker.kiwoom.adapter import (
    EndpointError, KiwoomAdapter, KiwoomConfig, MOCK_REST, build_fills, build_orders, check_endpoint,
)
from trading_system.broker.kiwoom.errors import ErrorClass, classify, specific_code
from trading_system.broker.kiwoom.http import KiwoomHttp
from trading_system.broker.kiwoom.realtime import KiwoomRealtime, WsState, order_handlers, parse_order_event
from trading_system.broker.ratelimit import Priority, RateLimiter, call_priority
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import IntentState, Mode, OrderType, RequestType, SubmitOutcome
from trading_system.domain.models import Account, GateRequest, OrderRequest, Signal
from trading_system.execution.executor import Executor
from trading_system.execution.resolver import ResolveOutcome, UnknownResolver
from trading_system.gate.gate import Gate
from trading_system.storage.repository import Repository

ALIAS = "KR-PAPER-01"
CODE = "005930"
T0 = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)  # 09:05 KST
SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def sim(clock: FixedClock) -> Iterator[Sim]:
    s = Sim(now=lambda: clock.now().astimezone(KST).replace(tzinfo=None))
    yield s
    s.close()


def adapter(sim: Sim, clock: FixedClock, **kw: object) -> KiwoomAdapter:
    cfg = KiwoomConfig(ALIAS, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET, rest_url=sim.url,
                       ws_url=sim.ws_url, per_tr_interval=0.0, order_wait=1.0, query_wait=2.0)
    return KiwoomAdapter(cfg, clock, http=KiwoomHttp(sim.url, 1.0, 0.5), **kw)  # type: ignore[arg-type]


def buy(qty: str = "10", order_type: OrderType = OrderType.MARKET, price: str = "0") -> OrderRequest:
    return OrderRequest("NEW", ALIAS, CODE, "BUY", Decimal(qty), order_type, Decimal(price))


def sent(sim: Sim, api: str) -> int:
    return sum(1 for a, _, _ in sim.log if a == api)


# --- V-49 전송 결과 4분류 ----------------------------------------------------------------------

CASES = [
    ("정상 접수", None, SubmitOutcome.ACCEPTED),
    ("업무 거부(주문가능금액 부족, 가상 코드 20)",
     Scripted("body", body={"return_code": 20, "return_msg": "주문가능금액 부족"}), SubmitOutcome.REJECTED),
    ("입력 오류 1517", Scripted("body", body={"return_code": 1517, "return_msg": "형식"}), SubmitOutcome.REJECTED),
    ("호출 제한 1700", Scripted("body", body={"return_code": 1700, "return_msg": "초과"}), SubmitOutcome.REJECTED),
    ("모의 미지원 8104", Scripted("body", body={"return_code": 8104, "return_msg": "x"}), SubmitOutcome.REJECTED),
    ("5xx", Scripted("status", status=503, body={"return_msg": "점검"}), SubmitOutcome.UNKNOWN),
    ("5xx + 업무 코드", Scripted("status", status=502, body={"return_code": 20, "return_msg": "x"}),
     SubmitOutcome.UNKNOWN),
    ("일반 인증 코드 3(본문 코드 없음)", Scripted("body", body={"return_code": "3", "return_msg": "인증에 실패했습니다"}),
     SubmitOutcome.UNKNOWN),
    ("응답 해석 불가", Scripted("nonjson"), SubmitOutcome.UNKNOWN),
    ("1999 예기치 못한 오류", Scripted("body", body={"return_code": 1999, "return_msg": "?"}), SubmitOutcome.UNKNOWN),
    ("성공인데 주문번호 없음", Scripted("body", body={"return_code": 0, "return_msg": "ok"}), SubmitOutcome.UNKNOWN),
    ("return_code 없음", Scripted("body", body={"ord_no": "0000001"}), SubmitOutcome.UNKNOWN),
    ("응답 타임아웃", Scripted("timeout"), SubmitOutcome.UNKNOWN),
    ("응답 전 연결 끊김", Scripted("close"), SubmitOutcome.UNKNOWN),
    ("전송 후 인증 오류(일반 코드+본문 코드)",
     Scripted("body", body={"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"}), SubmitOutcome.UNKNOWN),
    ("HTTP 401", Scripted("status", status=401, body={}), SubmitOutcome.UNKNOWN),
    ("4xx + 업무 코드", Scripted("status", status=400, body={"return_code": 1511, "return_msg": "필수"}),
     SubmitOutcome.REJECTED),
]


@pytest.mark.parametrize("name,script,expected", CASES, ids=[c[0] for c in CASES])
def test_v49_classification_and_v50_no_resend(sim: Sim, clock: FixedClock, name: str, script: Scripted | None,
                                              expected: SubmitOutcome) -> None:
    a = adapter(sim, clock)
    if script is not None:
        sim.script("kt10000", script)
    r = a.place_order(buy())
    assert r.outcome is expected, r.reason
    assert sent(sim, "kt10000") == 1  # 어떤 경우에도 같은 주문을 다시 보내지 않음 (V-50)
    if expected is SubmitOutcome.ACCEPTED:
        assert r.order_no == "0000001"   # 응답의 짧은 주문번호를 7자리로 맞춤
    assert "virtual-token" not in (r.reason or "") and APP_KEY not in (r.reason or "")


def test_pre_send_failures(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    assert a.place_order(buy("1.5")).outcome is SubmitOutcome.PRE_SEND_FAILURE          # 로컬 검증
    assert a.place_order(OrderRequest("NEW", "OTHER", CODE, "BUY", Decimal("1"), OrderType.MARKET,
                                      Decimal("0"))).outcome is SubmitOutcome.PRE_SEND_FAILURE
    assert a.place_order(buy("1", OrderType.LIMIT, "0")).outcome is SubmitOutcome.PRE_SEND_FAILURE
    assert sent(sim, "kt10000") == 0
    dead = KiwoomAdapter(KiwoomConfig(ALIAS, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET,
                                      rest_url="http://127.0.0.1:9", ws_url=sim.ws_url, per_tr_interval=0.0),
                         clock, http=KiwoomHttp("http://127.0.0.1:9", 0.5, 0.5))
    assert dead.place_order(buy()).outcome is SubmitOutcome.PRE_SEND_FAILURE            # 연결 수립 전 실패


# --- V-69 인증 만료: 전송 전/후/응답 없음 --------------------------------------------------------

def test_v69_auth_before_after_and_no_response(sim: Sim, clock: FixedClock) -> None:
    crit: list[tuple[str, str]] = []
    a = adapter(sim, clock, on_critical=lambda k, m: crit.append((k, m)))
    sim.token_fail = 3
    r = a.place_order(buy())
    assert r.outcome is SubmitOutcome.PRE_SEND_FAILURE and sent(sim, "kt10000") == 0      # 전송 전
    assert sent(sim, "au10001") == 3 and crit and crit[-1][0] == "AUTH"                     # 3회 실패 → 치명(K5)
    assert a.place_order(buy()).outcome is SubmitOutcome.ACCEPTED
    sim.valid_tokens.clear()                                                               # 서버에서 토큰 무효
    assert a.place_order(buy()).outcome is SubmitOutcome.UNKNOWN                           # 전송 후 인증 오류
    issued = sent(sim, "au10001")
    assert a.place_order(buy()).outcome is SubmitOutcome.ACCEPTED                          # 다음 요청부터 재발급
    assert sent(sim, "au10001") == issued + 1
    sim.script("kt10000", Scripted("timeout"))
    assert a.place_order(buy()).outcome is SubmitOutcome.UNKNOWN                           # 응답 없음


def test_token_refresh_before_expiry_and_revoke(sim: Sim, clock: FixedClock) -> None:
    sim.expires_in = timedelta(hours=1)
    a = adapter(sim, clock)
    a.place_order(buy())
    clock.advance(timedelta(minutes=45))
    a.place_order(buy())
    assert sent(sim, "au10001") == 1
    clock.advance(timedelta(minutes=6))  # 만료 9분 전 → 재발급
    a.place_order(buy())
    assert sent(sim, "au10001") == 2
    assert a.close() and sent(sim, "au10002") == 1 and not sim.valid_tokens - {"virtual-token-1"}


# --- V-103 모드 불일치, 단말기 --------------------------------------------------------------------

@pytest.mark.parametrize("code,kind", [(8030, "MODE_MISMATCH"), (8031, "MODE_MISMATCH"), (8010, "TERMINAL")])
def test_v103_mode_mismatch_and_terminal_are_critical(sim: Sim, clock: FixedClock, code: int, kind: str) -> None:
    crit: list[str] = []
    a = adapter(sim, clock, on_critical=lambda k, m: crit.append(k))
    sim.script("kt10000", Scripted("body", body={"return_code": code, "return_msg": "x"}))
    assert a.place_order(buy()).outcome is SubmitOutcome.UNKNOWN
    assert crit == [kind]
    sim.script("kt00001", Scripted("body", body={"return_code": code, "return_msg": "x"}))
    with pytest.raises(BrokerQueryError):
        a.query_cash(ALIAS)
    assert crit == [kind, kind]


def test_error_classes() -> None:
    assert specific_code(3, "[8005:Token]") == 8005 and specific_code(0, "[8005:x]") == 0
    assert specific_code(5, "CODE=8031") == 8031
    assert [classify(c) for c in (0, 8005, 8103, 8030, 8040, 1700, 1687, 1511, 1999, 8104, 20, None)] == [
        ErrorClass.OK, ErrorClass.AUTH, ErrorClass.AUTH, ErrorClass.MODE_MISMATCH, ErrorClass.TERMINAL,
        ErrorClass.RATE_LIMIT, ErrorClass.RATE_LIMIT, ErrorClass.INPUT, ErrorClass.UNEXPECTED,
        ErrorClass.MOCK_UNSUPPORTED, ErrorClass.BUSINESS, ErrorClass.UNEXPECTED]
    # 실측(2026-10-03, kt00005): 모의 미제공은 8104 말고 return_code 20 + RC9000 문구로도 온다 (kiwoom-kr K-U8)
    rc9000 = "[2000](RC9000:모의투자에서는 해당업무가 제공되지 않습니다.)"
    assert specific_code(20, rc9000) == 8104 and classify(specific_code(20, rc9000)) is ErrorClass.MOCK_UNSUPPORTED
    assert specific_code(20, "[2000](RC4007:가상 다른 업무 오류)") == 20


def test_cash_query_uses_kt00001_because_kt00005_is_not_in_mock(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    cash = a.query_cash(ALIAS)
    assert cash.deposit == cash.broker_orderable == Decimal("10000000")
    assert sent(sim, "kt00001") == 1 and sent(sim, "kt00005") == 0
    assert next(b for api, b, _ in sim.log if api == "kt00001") == {"qry_tp": "3"}


# --- V-70 호출 제한: 주문 재전송 0, 조회 제한된 재시도 ---------------------------------------------

def test_v70_rate_limit_query_retries_then_fails(sim: Sim, clock: FixedClock) -> None:
    lim = RateLimiter(0.0, max_penalty=0.05)
    a = adapter(sim, clock, limiter=lim)
    sim.script("kt00001", *[Scripted("body", body={"return_code": 1700, "return_msg": "초과"})] * 2)
    assert a.query_cash(ALIAS).deposit == Decimal("10000000")
    assert sent(sim, "kt00001") == 3
    sim.script("kt00001", *[Scripted("body", body={"return_code": 1700, "return_msg": "초과"})] * 3)
    with pytest.raises(BrokerQueryError, match="1700"):
        a.query_cash(ALIAS)
    sim.script("kt10000", Scripted("body", body={"return_code": 1700, "return_msg": "초과"}))
    assert a.place_order(buy()).outcome is SubmitOutcome.REJECTED and sent(sim, "kt10000") == 1


def test_query_auth_error_reissues_token_within_attempts(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    a.query_cash(ALIAS)
    sim.valid_tokens.clear()
    assert a.query_cash(ALIAS).broker_orderable == Decimal("10000000")
    assert sent(sim, "au10001") == 2


# --- V-71 호출 제한기 우선순위 ---------------------------------------------------------------------

def test_v71_resolve_query_goes_before_market_query() -> None:
    now = [0.0]
    lim = RateLimiter(1.0, monotonic=lambda: now[0])
    lim.acquire("ka10075", priority=Priority.OPS)  # 방금 사용 → 시각 1.0까지 대기
    order: list[str] = []

    def run(p: Priority, name: str) -> None:
        with call_priority(p):
            lim.acquire("ka10075", timeout=100)
        order.append(name)

    market = threading.Thread(target=run, args=(Priority.MARKET, "market"), daemon=True)
    market.start()
    assert wait_until(lambda: len(lim._waiting) == 1)  # noqa: SLF001 — 시세 조회가 먼저 줄 섬
    resolve = threading.Thread(target=run, args=(Priority.RESOLVE, "resolve"), daemon=True)
    resolve.start()
    assert wait_until(lambda: len(lim._waiting) == 2)  # noqa: SLF001
    now[0] = 1.0
    lim.wake()
    assert wait_until(lambda: order == ["resolve"])
    now[0] = 2.0
    lim.wake()
    market.join(5)
    resolve.join(5)
    assert order == ["resolve", "market"]
    assert lim.grants[-2:] == [("ka10075", int(Priority.RESOLVE)), ("ka10075", int(Priority.MARKET))]


def test_rate_limiter_interval_penalty_and_timeout() -> None:
    now = [0.0]
    lim = RateLimiter(1.0, monotonic=lambda: now[0])
    lim.acquire("kt10000")
    with pytest.raises(Exception, match="초과"):
        lim.acquire("kt10000", timeout=0.0)
    now[0] = 1.0
    lim.acquire("kt10000", timeout=0.0)
    assert lim.penalize("kt10000") == 1.0 and lim.penalize("kt10000") == 2.0
    lim.succeeded("kt10000")
    assert lim.penalize("kt10000") == 1.0


# --- 조회 정규화·계좌번호 제거 (V-105, D-094) -----------------------------------------------------

def test_queries_normalize_and_strip_account(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    no = a.place_order(buy("10", OrderType.LIMIT, "70000")).order_no
    assert no is not None
    sim.fill(no, Decimal("4"), Decimal("70000"))
    orders = a.query_orders(ALIAS)
    [o] = orders
    assert (o.order_no, o.kind, o.instrument, o.side, o.quantity, o.filled_quantity, o.state, o.order_type) == (
        no, "NEW", CODE, "BUY", Decimal("10"), Decimal("4"), "PARTIAL", OrderType.LIMIT)
    [f] = a.query_fills(ALIAS)
    assert (f.order_no, f.fill_id, f.quantity, f.price) == (no, "0000001", Decimal("4"), Decimal("70000"))
    assert [(p.instrument, p.quantity) for p in a.query_positions(ALIAS)] == [(CODE, Decimal("4"))]
    dump = repr(orders) + repr(a.query_fills(ALIAS)) + repr(a.query_positions(ALIAS)) + repr(a.query_cash(ALIAS))
    assert ACCOUNT_NO not in dump


def test_other_account_rows_dropped_and_critical(sim: Sim, clock: FixedClock) -> None:
    crit: list[str] = []
    a = adapter(sim, clock, on_critical=lambda k, m: crit.append(k))
    sim.extra_open.append({"acnt_no": "1111111111", "ord_no": "0000099", "stk_cd": CODE, "ord_qty": "1",
                           "ord_pric": "0", "oso_qty": "1", "orig_ord_no": "0000000", "io_tp_nm": "+매수",
                           "trde_tp": "시장가"})
    assert a.query_orders(ALIAS) == [] and crit == ["ACCOUNT_MISMATCH"]
    assert "1111111111" not in repr(a.critical)


def test_unparseable_row_fails_whole_query(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    sim.extra_rows.append({"ord_no": "0000077", "stk_cd": "A005930", "io_tp_nm": "현금매수", "mdfy_cncl_tp": "",
                           "ord_qty": "1,000", "ord_uv": "0", "trde_tp": "보통", "orig_ord_no": "0000000"})
    with pytest.raises(BrokerQueryError, match="해석 불가"):
        a.query_orders(ALIAS)


def test_paging_collects_all_and_refuses_runaway(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    for _ in range(5):
        a.place_order(buy("1", OrderType.LIMIT, "70000"))
    sim.page_size = 2
    assert len(a.query_orders(ALIAS)) == 5
    assert sum(1 for api, _, h in sim.log if api == "kt00009" and h.get("cont-yn") == "Y") == 2
    sim.always_cont = True
    with pytest.raises(BrokerQueryError, match="연속조회"):
        a.query_fills(ALIAS)


def test_build_orders_states() -> None:
    def row(no: str, io: str, mc: str = "", orig: str = "0000000", qty: str = "10", cntr: str = "",
            cq: str = "0", acpt: str = "접수") -> dict:
        return {"ord_no": no, "stk_cd": "A005930", "io_tp_nm": io, "mdfy_cncl_tp": mc, "ord_qty": qty,
                "ord_uv": "70000", "trde_tp": "보통", "orig_ord_no": orig, "cntr_no": cntr, "cntr_qty": cq,
                "acpt_tp": acpt}
    rows = [row("0000001", "현금매수"), row("0000002", "매수취소", "취소", "0000001"),
            row("0000003", "현금매도", cntr="0000011", cq="10"), row("0000004", "현금매수"),
            row("0000005", "매수정정", "정정", "0000004", qty="5"), row("0000006", "현금매수", acpt="거부"),
            row("0000007", "현금매수")]
    open_rows = [{"ord_no": "0000005", "ord_qty": "5", "oso_qty": "5", "stk_cd": CODE, "io_tp_nm": "매수정정",
                  "ord_pric": "69000", "trde_tp": "보통", "orig_ord_no": "0000004"}]
    st = {o.order_no: (o.kind, o.state) for o in build_orders(rows, open_rows)}
    assert st == {"0000001": ("NEW", "CANCELLED"), "0000002": ("CANCEL", "CONFIRMED"), "0000003": ("NEW", "FILLED"),
                  "0000004": ("NEW", "MODIFIED"), "0000005": ("MODIFY", "ACCEPTED"),
                  "0000006": ("NEW", "REJECTED"), "0000007": ("NEW", "CLOSED_UNKNOWN")}
    with pytest.raises(parse.KiwoomParseError):
        build_orders([row("0000009", "현금")], [])            # 매수·매도 구분 불가
    with pytest.raises(parse.KiwoomParseError):
        build_orders([row("0000009", "현금매수", cntr="0000001", cq="11")], [])   # 체결 > 주문
    day = T0.astimezone(KST).date()
    assert build_fills([row("0000003", "현금매도", cntr="0000000", cq="1")], day) == []   # 체결번호 없음 (D-094)


def test_strict_parsers() -> None:
    assert parse.dec("+000000004900", "x") == Decimal("4900") and parse.price("-60000", "x") == Decimal("60000")
    for bad in ("", " 1", "1,000", "1e3", "１"):
        with pytest.raises(parse.KiwoomParseError):
            parse.dec(bad, "x")
    with pytest.raises(parse.KiwoomParseError):
        parse.order_no("０００２４")
    assert parse.order_no("00024") == "0000024" and parse.optional_order_no("0000000", "o") is None
    assert parse.stock_code("A005930") == CODE
    for bad in ("005930_NX", "5930", "B005930"):
        with pytest.raises(parse.KiwoomParseError):
            parse.stock_code(bad)
    with pytest.raises(parse.KiwoomParseError):
        parse.qty("-1", "q")


# --- 실전 도메인 상수 없음·접속 대상 제한 ----------------------------------------------------------

def test_connect_failure_is_pre_send_but_other_connection_errors_are_after_send() -> None:
    import requests
    from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError
    from trading_system.broker.kiwoom.http import _is_connect_failure
    refused = requests.ConnectionError(MaxRetryError(None, "/x", reason=NewConnectionError(None, "refused")))  # type: ignore[arg-type]
    reset = requests.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError()))
    assert _is_connect_failure(refused) and not _is_connect_failure(reset)


def test_ws_url_must_be_mock(sim: Sim, clock: FixedClock) -> None:
    cfg = KiwoomConfig(ALIAS, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET, rest_url=sim.url,
                       ws_url="wss://api.kiwoom" + ".com:10000")
    with pytest.raises(EndpointError):
        KiwoomAdapter(cfg, clock, http=KiwoomHttp(sim.url))


def test_realtime_00_from_other_account_dropped(clock: FixedClock) -> None:
    got: list = []
    seen: list[object] = []
    hs = order_handlers(got.append, ALIAS, clock, lambda v: (seen.append(v), v == ACCOUNT_NO)[1])
    vals = {"9203": "0000001", "904": "0000000", "908": "090501", "905": "+매수", "913": "거부"}
    hs["00"]("", {**vals, "9201": "1111111111"})
    hs["00"]("", {**vals, "9201": ACCOUNT_NO})
    assert [e.kind for e in got] == ["REJECTED"] and seen == ["1111111111", ACCOUNT_NO]


def test_no_live_domain_and_endpoint_restricted() -> None:
    hits = [p for p in SRC.rglob("*.py") if re.search(r"(?<!mock)api\.kiwoom\.com", p.read_text(encoding="utf-8"))]
    assert hits == []
    assert check_endpoint(MOCK_REST, MOCK_REST) == MOCK_REST
    for bad in ("https://api.kiwoom.com", "https://mockapi.kiwoom.com.evil.example", "http://10.0.0.1"):
        with pytest.raises(EndpointError):
            check_endpoint(bad, MOCK_REST)


# --- 실시간 00 해석 ---------------------------------------------------------------------------------

def test_parse_order_event_kinds() -> None:
    day = T0.astimezone(KST).date()
    base = {"9203": "0000005", "904": "0000000", "908": "090501", "905": "+매수", "900": "10", "901": "70000"}
    assert parse_order_event({**base, "913": "접수"}, day, ALIAS) is None
    f = parse_order_event({**base, "913": "체결", "909": "0000007", "914": "70100", "915": "3", "911": "7",
                           "910": "70100"}, day, ALIAS)
    assert f is not None and (f.kind, f.quantity, f.price, f.fill_id, f.account, f.trade_date) == (
        "FILL", Decimal("3"), Decimal("70100"), "0000007", ALIAS, "2026-10-01")   # 단위체결량, 누계 아님
    c = parse_order_event({**base, "913": "확인", "905": "매수취소", "9203": "0000006", "904": "0000005"}, day, ALIAS)
    assert c is not None and (c.kind, c.order_no) == ("CANCELLED", "0000005")
    m = parse_order_event({**base, "913": "확인", "905": "매수정정", "9203": "0000007", "904": "0000005",
                           "900": "10", "901": "69000"}, day, ALIAS)
    assert m is not None and (m.kind, m.order_no, m.new_order_no, m.price) == ("MODIFIED", "0000005", "0000007",
                                                                               Decimal("69000"))
    r = parse_order_event({**base, "913": "거부"}, day, ALIAS)
    assert r is not None and r.kind == "REJECTED"
    for bad in ({**base, "913": "체결", "909": "", "915": "1", "914": "1"}, {**base, "913": "??"}):
        with pytest.raises(parse.KiwoomParseError):
            parse_order_event(bad, day, ALIAS)


# --- 실시간 세션 (LOGIN·REG·PING·재접속) -------------------------------------------------------------

def wait_until(cond, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def realtime(a: KiwoomAdapter, sim: Sim, clock: FixedClock, states: list, crit: list | None = None,
             **kw: object) -> KiwoomRealtime:
    return KiwoomRealtime(sim.ws_url, a.tokens, clock, registrations=[("00", []), ("04", [])],
                          handlers=order_handlers(a.push_event, ALIAS, clock,
                                                  lambda v: a._check_account(v, "00")),  # noqa: SLF001
                          on_state=lambda s, m: states.append(s),
                          on_critical=lambda k, m: (crit if crit is not None else []).append(k),
                          backoff_base=0.01, backoff_cap=0.05, **kw)  # type: ignore[arg-type]


def test_ws_login_register_ping_events_and_reconnect(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    states: list[WsState] = []
    rt = realtime(a, sim, clock, states)
    rt.start()
    try:
        assert wait_until(lambda: WsState.ONLINE in states)
        regs = [m for m in sim.ws_in if isinstance(m, dict) and m.get("trnm") == "REG"]
        assert [r["data"] for r in regs] == [[{"item": [], "type": ["00"]}], [{"item": [], "type": ["04"]}]]
        sim.ws_out.put('{"trnm":"PING"}')
        assert wait_until(lambda: {"trnm": "PING"} in sim.ws_in)
        no = a.place_order(buy("2", OrderType.LIMIT, "70000")).order_no
        sim.fill(no or "")
        assert wait_until(lambda: any(e.kind == "FILL" for e in a.poll_events() or []) or rt.parse_errors)
        assert rt.parse_errors == []
        sim.drop_ws()
        assert wait_until(lambda: WsState.RECONNECTED in states)
        assert WsState.OFFLINE in states and rt.reconnects == 1 and sim.ws_logins == 2
    finally:
        rt.stop()


def test_ws_gives_up_after_max_and_raises_critical(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    sim.ws_reject_reg = True
    states: list[WsState] = []
    crit: list[str] = []
    rt = realtime(a, sim, clock, states, crit, max_reconnects=3)
    rt.start()
    try:
        assert wait_until(lambda: WsState.FAILED in states)
        assert crit == ["WS_FAILED"] and states.count(WsState.OFFLINE) == 3
    finally:
        rt.stop()


def test_ws_login_rejected_invalidates_token(sim: Sim, clock: FixedClock) -> None:
    a = adapter(sim, clock)
    sim.ws_reject_login = True
    states: list[WsState] = []
    rt = realtime(a, sim, clock, states, max_reconnects=2)
    rt.start()
    try:
        assert wait_until(lambda: WsState.FAILED in states)
        assert sent(sim, "au10001") == 2  # 로그인 거부마다 토큰 무효 → 재발급
    finally:
        rt.stop()


# --- fake·키움 공통 계약 -------------------------------------------------------------------------

class FakeHarness:
    def __init__(self) -> None:
        self.broker = FakeBroker(FakeBehavior.ACCEPT_ONLY)

    def fill(self, no: str, qty: Decimal) -> None:
        self.broker.fill(no, qty)

    def events(self, n: int) -> list:
        return self.broker.poll_events()

    def close(self) -> None:
        return


class KiwoomHarness:
    def __init__(self, sim: Sim, clock: FixedClock) -> None:
        self.sim = sim
        self.broker = adapter(sim, clock)
        self.rt = realtime(self.broker, sim, clock, [])
        self.rt.start()
        wait_until(lambda: self.rt.state in (WsState.ONLINE,))

    def fill(self, no: str, qty: Decimal) -> None:
        self.sim.fill(no, qty)

    def events(self, n: int) -> list:
        got: list = []
        wait_until(lambda: (got.extend(self.broker.poll_events()) or len(got) >= n), 3.0)
        return got

    def close(self) -> None:
        self.rt.stop()


@pytest.fixture(params=["fake", "kiwoom"])
def harness(request: pytest.FixtureRequest, sim: Sim, clock: FixedClock) -> Iterator[object]:
    h = FakeHarness() if request.param == "fake" else KiwoomHarness(sim, clock)
    yield h
    h.close()


def acct(h: object) -> str:
    return ALIAS


def test_contract_place_fill_cancel(harness: FakeHarness | KiwoomHarness) -> None:
    b = harness.broker
    r = b.place_order(OrderRequest("NEW", ALIAS, CODE, "BUY", Decimal("10"), OrderType.LIMIT, Decimal("70000")))
    assert r.outcome is SubmitOutcome.ACCEPTED and r.order_no
    harness.fill(r.order_no, Decimal("4"))
    fills = [e for e in harness.events(1) if e.kind == "FILL"]
    assert [(e.order_no, e.quantity) for e in fills] == [(r.order_no, Decimal("4"))] and fills[0].fill_id
    c = b.cancel_order(OrderRequest("CANCEL", ALIAS, CODE, "BUY", Decimal("0"), OrderType.LIMIT, Decimal("0"),
                                    target_order_no=r.order_no))
    assert c.outcome is SubmitOutcome.ACCEPTED and c.order_no and c.order_no != r.order_no
    assert [(e.kind, e.order_no) for e in harness.events(1) if e.kind == "CANCELLED"] == [("CANCELLED", r.order_no)]
    by_no = {o.order_no: o for o in b.query_orders(ALIAS)}
    assert by_no[r.order_no].state == "CANCELLED" and by_no[r.order_no].filled_quantity == Decimal("4")
    assert (by_no[c.order_no].kind, by_no[c.order_no].state, by_no[c.order_no].original_order_no) == (
        "CANCEL", "CONFIRMED", r.order_no)
    assert [(f.order_no, f.quantity) for f in b.query_fills(ALIAS)] == [(r.order_no, Decimal("4"))]
    assert [(p.instrument, p.quantity) for p in b.query_positions(ALIAS)] == [(CODE, Decimal("4"))]


def test_contract_modify(harness: FakeHarness | KiwoomHarness) -> None:
    b = harness.broker
    r = b.place_order(OrderRequest("NEW", ALIAS, CODE, "BUY", Decimal("10"), OrderType.LIMIT, Decimal("70000")))
    m = b.modify_order(OrderRequest("MODIFY", ALIAS, CODE, "BUY", Decimal("10"), OrderType.LIMIT, Decimal("69000"),
                                    target_order_no=r.order_no))
    assert m.outcome is SubmitOutcome.ACCEPTED and m.order_no not in (None, r.order_no)
    [ev] = [e for e in harness.events(1) if e.kind == "MODIFIED"]
    assert (ev.order_no, ev.new_order_no, ev.price) == (r.order_no, m.order_no, Decimal("69000"))
    by_no = {o.order_no: o for o in b.query_orders(ALIAS)}
    assert by_no[r.order_no].state == "MODIFIED"
    assert (by_no[m.order_no].kind, by_no[m.order_no].state, by_no[m.order_no].original_order_no) == (
        "MODIFY", "ACCEPTED", r.order_no)
    bad = b.modify_order(OrderRequest("MODIFY", ALIAS, CODE, "BUY", Decimal("10"), OrderType.LIMIT,
                                      Decimal("68000"), target_order_no=r.order_no))   # 이미 정정된 원주문
    assert bad.outcome is SubmitOutcome.REJECTED


# --- 종단 간: 게이트 → 실행기 → 키움 어댑터 → 실시간 통보 / 결과 불명 해소 -------------------------

def paper_env(tmp_path: Path, sim: Sim, clock: FixedClock):  # type: ignore[no-untyped-def]
    repo = Repository(tmp_path / "paper.db", Mode.PAPER)
    repo.register_account(Account(alias=ALIAS, mode=Mode.PAPER, broker_env="mock"))
    facts = make_facts(ALIAS, clock.now)
    facts.market.add(CODE, "70000", upper="70000")
    a = adapter(sim, clock)
    gate = Gate(repo, clock, facts.inputs(), TEST_POLICY)
    ex = Executor(repo, a, clock, tmp_path / "KILL")
    return repo, a, gate, ex


def signal(ev: str) -> Signal:
    return Signal("t001-fixed", 1, ev, ALIAS, CODE, "R1", RequestType.ENTRY, Decimal("10"), OrderType.MARKET,
                  Decimal("70000"), T0 - timedelta(minutes=1), T0 + timedelta(minutes=30))


def test_e2e_entry_filled_via_realtime(tmp_path: Path, sim: Sim, clock: FixedClock) -> None:
    repo, a, gate, ex = paper_env(tmp_path, sim, clock)
    sim.auto_fill = True
    rt = realtime(a, sim, clock, [])
    rt.start()
    try:
        assert wait_until(lambda: rt.state is WsState.ONLINE)
        d = gate.decide(GateRequest.from_signal(signal("e1")))
        assert ex.submit(d.approved) is IntentState.ACCEPTED
        assert wait_until(lambda: (ex.process_events() or True) and
                          repo.intent(d.approved.intent_id).state is IntentState.FILLED)
        assert repo.position(ALIAS, CODE).quantity == Decimal("10")
    finally:
        rt.stop()
        repo.close()


def test_e2e_lost_response_resolved_by_queries(tmp_path: Path, sim: Sim, clock: FixedClock) -> None:
    repo, a, gate, ex = paper_env(tmp_path, sim, clock)
    sim.auto_fill = True
    sim.script("kt10000", Scripted("timeout", apply=True))   # 서버는 접수·체결, 응답은 유실
    d = gate.decide(GateRequest.from_signal(signal("e2")))
    assert ex.submit(d.approved) is IntentState.UNKNOWN
    res = UnknownResolver(repo, a, ex, clock, lambda s: None)
    assert res.resolve(d.approved.intent_id) is ResolveOutcome.FOUND
    assert repo.intent(d.approved.intent_id).state is IntentState.FILLED
    assert repo.position(ALIAS, CODE).quantity == Decimal("10")
    assert sent(sim, "kt10000") == 1
    repo.close()


def test_v71_group_priority_holds_lower_priority_even_when_its_tr_is_free() -> None:
    """같은 그룹(실전 조회 그룹 같은)에서 높은 순위가 기다리면, 자기 TR이 비어 있어도 낮은 순위는 기다린다."""
    now = [0.0]
    lim = RateLimiter(1.0, groups={"query": ({"ka10075", "ka10001"}, 0.0)}, monotonic=lambda: now[0])
    lim.acquire("ka10075")                         # ka10075는 시각 1.0까지 막힘, ka10001은 비어 있음
    order: list[str] = []

    def run(p: Priority, tr: str, name: str) -> None:
        with call_priority(p):
            lim.acquire(tr, timeout=100)
        order.append(name)

    resolve = threading.Thread(target=run, args=(Priority.RESOLVE, "ka10075", "resolve"), daemon=True)
    resolve.start()
    assert wait_until(lambda: len(lim._waiting) == 1)  # noqa: SLF001
    market = threading.Thread(target=run, args=(Priority.MARKET, "ka10001", "market"), daemon=True)
    market.start()
    time.sleep(0.3)
    assert order == []                             # 시세 조회는 결과 불명 조회보다 먼저 나가지 않음
    now[0] = 1.0
    lim.wake()
    resolve.join(5)
    market.join(5)
    assert order == ["resolve", "market"]
