"""CP-032 D-180 모의 조회·구독 실측 도구 (승인 A-085 Q-2) — 네트워크 없이 합성 응답으로만 시험한다.

소켓을 열지 않는다: HTTP는 GuardedHttp 안쪽에 가짜 객체, WebSocket은 connect 주입으로 가짜 세션을 쓴다.
- 안전 장치: --mode·TRADE_MODE가 PAPER가 아니면, 모의 도메인이 아니면, 거래일 장중(06:30~17:00 KST)이면,
  run.lock·autopilot.lock·하트비트가 살아 있으면 → 연결 없이 거부(종료 코드 2).
- 허용 목록: REST api-id {au10001, au10002, ka10080, ka10079}, WS trnm {LOGIN, REG, REMOVE, PING}, 실시간 항목 {0B, 0D}.
  밖의 것은 전송 전에 막는다. 도구 소스는 주문 모듈·주문 함수를 import·호출하지 않는다.
- 기록: 필드 이름·건수·시각 범위·반환 코드만. 가격 값·토큰·앱키·계좌번호는 결과 파일·출력에 없다.
"""

from __future__ import annotations

import ast
import json
import shutil
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.scenario.test_t015_preflight import ENV_OK, make_root
from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET
from trading_system.app import probe_tool
from trading_system.app.instance_lock import AUTOPILOT_LOCK, acquire_instance_lock
from trading_system.app.preflight import ORDER_API_IDS, GuardedHttp
from trading_system.app.probe_tool import (
    MIN_LIST_KEY, PROBE_REST_APIS, TICK_LIST_KEY, WS_ALLOWED_TRNM, ProbeError, ProbeWs, market_hours_refusal,
    probe_main, summarize_times)
from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS, KiwoomAdapter, KiwoomConfig
from trading_system.broker.kiwoom.http import HttpReply, PreSendError
from trading_system.domain.clock import KST, FixedClock
from trading_system.marketdata.calendar import KrxCalendar

REPO = Path(__file__).resolve().parents[2]
ALIAS = "KR-PAPER-01"
TOKEN = "virtual-token-probe-0001"
PRICE = "71234"                                    # 합성 가격 — 결과 파일·출력에 나오면 안 된다
EVENING = datetime(2026, 10, 8, 11, 0, tzinfo=UTC)  # 2026-10-08(목, 거래일) 20:00 KST
PROVIDE_FROM = date(2026, 7, 1)                   # 합성 서버: 이 날짜 이전 분봉은 주지 않는다
PAGE = 100


def kst(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=KST)


# --- 합성 서버 (HTTP 안쪽 객체, 소켓 없음) ------------------------------------------------------------

def minute_rows(base: date, scope: int) -> list[dict]:
    """base 이하 최근 평일 3일, 09:00~15:30, scope분 간격, 최신→과거 (키움 예시 순서)."""
    days: list[date] = []
    d = base
    while len(days) < 3 and d >= PROVIDE_FROM:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    rows = []
    for day in days:
        t, start = datetime(day.year, day.month, day.day, 15, 30), datetime(day.year, day.month, day.day, 9, 0)
        while t >= start:
            rows.append({"cur_prc": f"+{PRICE}", "open_pric": f"+{PRICE}", "high_pric": f"+{PRICE}",
                         "low_pric": f"-{PRICE}", "trde_qty": "4321", "acc_trde_qty": "99999",
                         "cntr_tm": t.strftime("%Y%m%d%H%M%S"), "pred_pre": "+34", "pred_pre_sig": "2"})
            t -= timedelta(minutes=scope)
    return rows


def tick_rows() -> list[dict]:
    t = datetime(2026, 10, 8, 15, 30)
    return [{"cur_prc": f"+{PRICE}", "trde_qty": "1", "cntr_tm": (t - timedelta(seconds=i)).strftime("%Y%m%d%H%M%S")}
            for i in range(150)]


class FakeInner:
    """KiwoomHttp 자리. 실제 전송 대신 합성 응답. chart_reply로 ka10080 응답을 바꿀 수 있다."""

    def __init__(self, *, chart_reply: dict | None = None, token_reply: dict | None = None) -> None:
        self.calls = 0
        self.log: list[tuple[str, str, dict, bool]] = []    # (경로, api-id, 본문, 토큰 있음)
        self.chart_reply = chart_reply
        self.token_reply = token_reply

    def post(self, path: str, api_id: str, body: dict, token: str | None,
             cont_yn: str | None = None, next_key: str | None = None) -> HttpReply:
        self.calls += 1
        self.log.append((path, api_id, dict(body), token is not None))
        if api_id == "au10001":
            return HttpReply(200, self.token_reply or {"return_code": 0, "return_msg": "정상", "token": TOKEN,
                                                       "token_type": "bearer", "expires_dt": "20261009200000"})
        if api_id == "au10002":
            return HttpReply(200, {"return_code": 0, "return_msg": "정상"})
        assert token == TOKEN
        off = int(next_key or 0)
        if api_id == "ka10080":
            if self.chart_reply is not None:
                return HttpReply(200, dict(self.chart_reply))
            rows = minute_rows(datetime.strptime(body["base_dt"], "%Y%m%d").date(), int(body["tic_scope"]))
            key = MIN_LIST_KEY
        elif api_id == "ka10079":
            rows, key = tick_rows(), TICK_LIST_KEY
        else:
            raise AssertionError(f"허용 목록 밖 API가 HTTP까지 옴: {api_id}")
        more = off + PAGE < len(rows)
        page = {"return_code": 0, "return_msg": "정상", "stk_cd": body["stk_cd"], key: rows[off:off + PAGE]}
        if api_id == "ka10079":
            page["last_tic_cnt"] = "7"
        return HttpReply(200, page, "Y" if more else "N", str(off + PAGE) if more else None)

    def apis(self) -> set[str]:
        return {a for _, a, _, _ in self.log}


class FakeWs:
    """WebSocket 세션 자리. 보낸 메시지에 합성 응답을 줄 세운다."""

    def __init__(self, reg_code: int = 0) -> None:
        self.sent: list[Any] = []
        self.q: deque[dict] = deque()
        self.reg_code = reg_code
        self.closed = False

    def send(self, text: str) -> None:
        msg = json.loads(text) if text.startswith("{") else text
        self.sent.append(msg)
        if not isinstance(msg, dict):
            return
        t = msg["trnm"]
        if t == "LOGIN":
            self.q.append({"trnm": "LOGIN", "return_code": 0, "return_msg": ""})
        elif t == "REG":
            rtype, item = msg["data"][0]["type"][0], msg["data"][0]["item"][0]
            self.q.append({"trnm": "REG", "return_code": self.reg_code,
                           "return_msg": "" if self.reg_code == 0 else "등록 실패"})
            if self.reg_code == 0:
                values = {"20": "153000", "10": f"+{PRICE}"} if rtype == "0B" else {"21": "153000", "41": f"+{PRICE}"}
                for _ in range(2):
                    self.q.append({"trnm": "REAL", "data": [{"type": rtype, "name": "x", "item": item,
                                                             "values": values}]})
                self.q.append({"trnm": "PING"})
        elif t == "REMOVE":
            self.q.append({"trnm": "REMOVE", "return_code": 0, "return_msg": ""})

    def recv(self, timeout: float | None = None) -> str:
        if not self.q:
            raise TimeoutError
        return json.dumps(self.q.popleft(), ensure_ascii=False)

    def close(self) -> None:
        self.closed = True


def ticker() -> Any:
    t = [0.0]

    def mono() -> float:
        t[0] += 0.5
        return t[0]
    return mono


def make_conn(inner: FakeInner, clock: FixedClock, *, ws_url: str = MOCK_WS,
              codes: tuple[str, ...] = ("005930",)) -> SimpleNamespace:
    guard = GuardedHttp(inner, PROBE_REST_APIS)  # type: ignore[arg-type]
    cfg = KiwoomConfig(ALIAS, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET, rest_url=MOCK_REST, ws_url=ws_url,
                       per_tr_interval=0.0, query_wait=2.0)
    adapter = KiwoomAdapter(cfg, clock, http=guard)  # type: ignore[arg-type]
    return SimpleNamespace(adapter=adapter, guard=guard, app=SimpleNamespace(instruments={c: None for c in codes}))


def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "config" / "calendar").mkdir(parents=True)
    shutil.copy(REPO / "config" / "calendar" / "krx-2026.toml", root / "config" / "calendar" / "krx-2026.toml")
    return root


class Harness:
    def __init__(self, tmp_path: Path, *, inner: FakeInner | None = None, ws: FakeWs | None = None,
                 when: datetime = EVENING, ws_url: str = MOCK_WS) -> None:
        self.root = repo_root(tmp_path)
        self.clock = FixedClock(when)
        self.inner = inner or FakeInner()
        self.ws = ws or FakeWs()
        self.ws_urls: list[str] = []
        self.connects = 0
        self.ws_url = ws_url

    def connect(self, root: Path, environ: Any, clock: Any) -> SimpleNamespace:
        self.connects += 1
        return make_conn(self.inner, self.clock, ws_url=self.ws_url, codes=("005930",))

    def ws_connect(self, url: str, **kw: Any) -> FakeWs:
        self.ws_urls.append(url)
        return self.ws

    def run(self, argv: list[str]) -> int:
        return probe_main(self.root, argv, {"TRADE_MODE": "PAPER"}, clock=self.clock, connect=self.connect,
                          ws_connect=self.ws_connect, monotonic=ticker())

    def report(self) -> tuple[dict, str, str]:
        files = sorted((self.root / "var" / "paper" / "probe").glob("*-probe.*"))
        assert [p.suffix for p in files] == [".json", ".md"]
        text = files[0].read_text(encoding="utf-8")
        return json.loads(text), text, files[1].read_text(encoding="utf-8")


ARGS = ["--mode", "PAPER", "--codes", "005930,000660"]


# --- 1. 모드·접속 대상 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["--mode", "LIVE"], ["--mode", "BACKTEST"], ["--mode", "paper"], []])
def test_refuses_non_paper_mode_argument_without_connecting(tmp_path: Path, argv: list[str],
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(argv) == 2
    assert h.connects == 0 and h.inner.calls == 0 and h.ws_urls == []


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []
    monkeypatch.setattr("trading_system.app.preflight.KiwoomHttp", lambda *a, **k: made.append("http"))
    return made


@pytest.mark.parametrize("environ, reason", [
    ({"TRADE_MODE": "LIVE"}, "LIVE"), ({"TRADE_MODE": "BACKTEST"}, "PAPER"), ({}, "TRADE_MODE"),
])
def test_refuses_trade_mode_not_paper_before_any_connection(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                            no_network: list[str], environ: dict,
                                                            reason: str) -> None:
    """실제 연결 함수(connect_paper)를 거친다 — TRADE_MODE가 PAPER가 아니거나 없으면 HTTP 객체도 만들지 않는다."""
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    code = probe_main(root, ["--mode", "PAPER"], environ, clock=FixedClock(EVENING))
    out = capsys.readouterr().out
    assert code == 2
    assert "연결 없음" in out and reason in out
    assert no_network == []
    for secret in (ACCOUNT_NO, APP_KEY, SECRET):
        assert secret not in out


def test_refuses_when_connection_switch_off(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                           no_network: list[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=False)
    assert probe_main(root, ["--mode", "PAPER"], {"TRADE_MODE": "PAPER"}, clock=FixedClock(EVENING)) == 2
    assert "connection_approved" in capsys.readouterr().out
    assert no_network == []


def test_default_connect_uses_mock_target_allowlist_and_conservative_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(probe_tool, "connect_paper", lambda root, env, clock, **kw: seen.update(kw) or "x")
    probe_tool._default_connect(Path("."), {}, FixedClock(EVENING))  # noqa: SLF001
    assert seen["allowed_apis"] == PROBE_REST_APIS
    assert seen["per_tr_interval"] >= 1.2          # 모의 TR당 1초 1회(E4)보다 보수적
    assert "rest_url" not in seen and "ws_url" not in seen   # 기본값 = 모의 도메인(MOCK_REST·MOCK_WS)


def test_refuses_non_mock_endpoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path, ws_url="ws://127.0.0.1:9")
    assert h.run(ARGS) == 2
    assert "모의 도메인" in capsys.readouterr().out
    assert h.inner.calls == 0 and h.ws_urls == []


# --- 2. 장외 전용 ------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def calendar() -> KrxCalendar:
    return KrxCalendar.load_dir(REPO / "config" / "calendar")


@pytest.mark.parametrize("when, refused", [
    (kst(2026, 10, 8, 6, 29), False),     # 거래일 예약 작업 전
    (kst(2026, 10, 8, 6, 30), True),
    (kst(2026, 10, 8, 8, 0), True),
    (kst(2026, 10, 8, 12, 0), True),
    (kst(2026, 10, 8, 16, 59), True),
    (kst(2026, 10, 8, 17, 0), False),
    (kst(2026, 10, 8, 20, 0), False),
    (kst(2026, 10, 9, 10, 0), False),     # 한글날 휴장
    (kst(2026, 10, 10, 10, 0), False),    # 토요일
    (kst(2026, 11, 19, 17, 30), True),    # 수능일 마감 16:30 → 18:00까지 거부
    (kst(2026, 11, 19, 18, 0), False),
])
def test_market_hours_window(calendar: KrxCalendar, when: datetime, refused: bool) -> None:
    assert (market_hours_refusal(when, calendar) is not None) is refused


def test_unknown_calendar_treats_weekday_as_trading_day() -> None:
    assert market_hours_refusal(kst(2027, 3, 3, 10, 0), KrxCalendar({})) is not None   # 달력 없는 해의 평일
    assert market_hours_refusal(kst(2027, 3, 3, 10, 0), None) is not None
    assert market_hours_refusal(kst(2027, 3, 6, 10, 0), None) is None                  # 토요일


def test_refuses_during_market_hours_without_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path, when=kst(2026, 10, 8, 10, 0).astimezone(UTC))
    assert h.run(ARGS) == 2
    assert "장외 전용" in capsys.readouterr().out
    assert h.connects == 0 and h.inner.calls == 0


# --- 3. PAPER run·자동 운영 실행 중 거부 ---------------------------------------------------------------------

@pytest.mark.parametrize("lock_name, word", [("run.lock", "엔진"), (AUTOPILOT_LOCK, "자동 운영")])
def test_refuses_while_lock_held(tmp_path: Path, capsys: pytest.CaptureFixture[str], lock_name: str,
                                 word: str) -> None:
    h = Harness(tmp_path)
    lock = acquire_instance_lock(h.root / "var" / "paper", lock_name)
    try:
        assert h.run(ARGS) == 2
    finally:
        lock.release()
    assert word in capsys.readouterr().out
    assert h.connects == 0 and h.inner.calls == 0


def test_refuses_while_heartbeat_fresh(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path)
    hb = h.root / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((EVENING - timedelta(seconds=10)).isoformat(), encoding="utf-8")
    assert h.run(ARGS) == 2
    assert "하트비트" in capsys.readouterr().out
    assert h.connects == 0


@pytest.mark.parametrize("extra", [
    ["--codes", "005930,000660,069500,035420"], ["--codes", "5930"], ["--max-pages", "0"], ["--max-pages", "21"],
    ["--ws-seconds", "61"], ["--back-days", "0"], ["--back-days", "abc"],
])
def test_refuses_bad_arguments(tmp_path: Path, extra: list[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(["--mode", "PAPER", *extra]) == 2
    assert h.connects == 0


# --- 4. 정상 실측: 형태만 기록, 허용 목록 안에서만 호출 ------------------------------------------------------

def test_probe_records_shape_only_within_allowlist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(ARGS) == 0
    out = capsys.readouterr().out
    rep, text, md = h.report()

    # REST: 허용 목록 안에서만, 주문 API 0회
    assert h.inner.apis() <= PROBE_REST_APIS
    assert not h.inner.apis() & ORDER_API_IDS
    assert all(path != "/api/dostk/ordr" for path, *_ in h.inner.log)
    assert rep["blocked_before_send"] == [] and rep["failures"] == []
    assert rep["token"] == {"issued": True, "expires_at": "2026-10-09T20:00:00+09:00", "revoked": True}
    assert set(rep["api_calls"]) == {"au10001", "au10002", "ka10080", "ka10079"}

    mins = [r for r in rep["rest"] if r["api"] == "ka10080"]
    assert len(mins) == 2 * 2 * (1 + 5)      # 종목 2 × 범위 2 × (연속 조회 1 + base_dt 5)
    assert all(r["request"]["upd_stkpc_tp"] == "1" for r in mins)
    p1 = next(r for r in mins if r["kind"] == "paging" and r["request"]["tic_scope"] == "1")
    assert p1["pages"] == 5 and p1["rows_per_page"] == [100] * 5 and p1["more_pages"] is True   # --max-pages 상한
    assert p1["times"]["order"] == "최신→과거" and p1["times"]["max"] == "20261008153000"
    assert p1["times"]["first"] == ["20261008153000", "20261008152900", "20261008152800"]
    assert p1["times"]["step_seconds"] == {"60": 498}
    assert "acc_trde_qty" in p1["fields"] and "cntr_tm" in p1["fields"]
    p5 = next(r for r in mins if r["kind"] == "paging" and r["request"]["tic_scope"] == "5")
    assert p5["pages"] == 3 and p5["more_pages"] is False and p5["rows"] == 79 * 3
    assert p5["times"]["per_day"][-1] == {"date": "20261008", "rows": 79, "first_hms": "090000", "last_hms": "153000"}
    back = {r["kind"]: r for r in mins if r["request"]["stk_cd"] == "005930" and r["request"]["tic_scope"] == "1"}
    assert back["base_dt-30d"]["base_dt_honored"] is True and back["base_dt-30d"]["rows"] == 100
    assert back["base_dt-180d"]["rows"] == 0 and back["base_dt-180d"]["ok"] is True   # 제공 범위 밖 = 빈 목록
    tick = next(r for r in rep["rest"] if r["api"] == "ka10079")
    assert tick["pages"] == 2 and tick["rows"] == 150 and "last_tic_cnt" in tick["body_keys"]

    # WS: 0B·0D 각 1종목 REG → 수신 → REMOVE, 보낸 것은 허용 목록뿐
    ws = rep["ws"]
    assert h.ws_urls == [MOCK_WS + "/api/dostk/websocket"]
    assert ws["login"]["return_code"] == 0
    assert [(r["type"], r["return_code"]) for r in ws["reg"]] == [("0B", 0), ("0D", 0)]
    assert [(r["type"], r["return_code"]) for r in ws["remove"]] == [("0B", 0), ("0D", 0)]
    assert ws["received"]["0B"]["count"] == 2 and ws["received"]["0B"]["fields"] == ["10", "20"]
    assert ws["received"]["0D"]["count"] == 2
    assert set(ws["sent_trnm"]) <= WS_ALLOWED_TRNM
    sent = [m for m in h.ws.sent if isinstance(m, dict)]
    assert [m["trnm"] for m in sent if m["trnm"] != "PING"] == ["LOGIN", "REG", "REG", "REMOVE", "REMOVE"]
    assert {t for m in sent for d in m.get("data", []) for t in d["type"]} == {"0B", "0D"}
    assert h.ws.closed

    # 기록·출력에 값 원문·비밀 없음
    for blob in (text, md, out):
        for secret in (TOKEN, APP_KEY, SECRET, ACCOUNT_NO, PRICE, "4321", "99999"):
            assert secret not in blob
    assert "ka10080 분봉" in md and "| 005930 | 1 | paging |" in md


# --- 5. 실패 기록·중단 ------------------------------------------------------------------------------------

def test_mock_unsupported_chart_is_recorded_and_that_api_skipped(tmp_path: Path) -> None:
    """모의 미지원(K-U32)은 실측 결과다: ka10080은 1회만 부르고 건너뛰되, ka10079·WS 실측과 토큰 폐기는 한다."""
    inner = FakeInner(chart_reply={"return_code": 20,
                                   "return_msg": "[2000](RC9000:모의투자에서는 해당업무가 제공되지 않습니다.)"})
    h = Harness(tmp_path, inner=inner)
    assert h.run(ARGS) == 1
    rep, _, md = h.report()
    assert sum(1 for _, a, _, _ in inner.log if a == "ka10080") == 1
    assert [(r["api"], r["ok"]) for r in rep["rest"]] == [("ka10080", False), ("ka10079", True)]
    assert len(rep["rest_skipped"]) == 2 * 2 * 6 - 1 and all("모의 미지원" in s for s in rep["rest_skipped"])
    assert rep["api_calls"]["ka10080"]["codes"] == {"8104": 1}   # RC9000 → 모의 미지원(8104)으로 분류
    assert "RC9000" in rep["api_calls"]["ka10080"]["last_error"]
    assert rep["ws"] is not None and rep["ws"]["reg"][0]["return_code"] == 0
    assert rep["token"]["revoked"] is True
    assert "## 실패" in md


def test_consecutive_failures_stop_rest_but_ws_still_measured(tmp_path: Path) -> None:
    inner = FakeInner(chart_reply={"return_code": 5, "return_msg": "기타 업무 오류"})
    h = Harness(tmp_path, inner=inner)
    assert h.run(ARGS) == 1
    rep, _, _ = h.report()
    assert [r["ok"] for r in rep["rest"]] == [False, False, False]
    assert sum(1 for _, a, _, _ in inner.log if a == "ka10080") == 3
    assert "ka10079" not in inner.apis() and len(rep["rest_skipped"]) == 2 * 2 * 6 + 1 - 3
    assert rep["ws"] is not None and rep["token"]["revoked"] is True


def test_token_failure_stops_before_any_query_or_ws(tmp_path: Path) -> None:
    inner = FakeInner(token_reply={"return_code": 8030, "return_msg": "[8030:투자구분 불일치]"})
    h = Harness(tmp_path, inner=inner)
    assert h.run(ARGS) == 1
    rep, _, _ = h.report()
    assert inner.apis() == {"au10001"}
    assert rep["rest"] == [] and rep["ws"] is None and h.ws_urls == []
    assert any("토큰 발급 실패" in f for f in rep["failures"])


def test_rejected_registration_is_not_removed_and_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path, ws=FakeWs(reg_code=1))
    assert h.run(ARGS) == 1
    rep, _, _ = h.report()
    assert [r["return_code"] for r in rep["ws"]["reg"]] == [1, 1]
    assert rep["ws"]["remove"] == []
    assert not any(isinstance(m, dict) and m["trnm"] == "REMOVE" for m in h.ws.sent)
    assert any("WS REG 0B" in f for f in rep["failures"])


# --- 6. 허용 목록·주문 코드 부재 ------------------------------------------------------------------------------

@pytest.mark.parametrize("path, api_id", [
    ("/api/dostk/ordr", "kt10000"), ("/api/dostk/ordr", "kt10003"), ("/api/dostk/chart", "ka10081"),
    ("/api/dostk/acnt", "kt00018"), ("/api/dostk/stkinfo", "ka10001"),
])
def test_guard_allowlist_blocks_before_send(path: str, api_id: str) -> None:
    inner = FakeInner()
    g = GuardedHttp(inner, PROBE_REST_APIS)  # type: ignore[arg-type]
    with pytest.raises(PreSendError):
        g.post(path, api_id, {}, TOKEN)
    assert inner.calls == 0 and g.blocked == [api_id]


def test_guard_without_allowlist_is_unchanged() -> None:
    inner = FakeInner()
    g = GuardedHttp(inner)  # type: ignore[arg-type]
    g.post("/api/dostk/stkinfo", "au10002", {}, None)    # 허용 목록 없음 = 기존 동작(주문 API만 차단)
    assert inner.calls == 1 and g.blocked == []


@pytest.mark.parametrize("msg", [
    {"trnm": "CNSRREQ"},
    {"trnm": "REG", "grp_no": "1", "refresh": "1", "data": [{"item": [""], "type": ["00"]}]},
    {"trnm": "REG", "grp_no": "1", "refresh": "1", "data": [{"item": ["005930"], "type": ["0B", "0C"]}]},
    "SOMETHING",
])
def test_probe_ws_refuses_non_allowed_messages(msg: Any) -> None:
    ws = FakeWs()
    session = ProbeWs(MOCK_WS, None, FixedClock(EVENING), connect=lambda url, **kw: ws)  # type: ignore[arg-type]
    session._ws = ws  # noqa: SLF001
    with pytest.raises(ProbeError):
        session._send(msg)  # noqa: SLF001
    assert ws.sent == []


FORBIDDEN_PACKAGES = ("trading_system.execution", "trading_system.gate", "trading_system.halt", "trading_system.ops",
                      "trading_system.broker.fake")
FORBIDDEN_NAMES = {"place_order", "cancel_order", "modify_order", "ORDER_PATH", "ORDER_API_IDS", "Executor",
                   "ApprovedIntent", "OrderRequest"}


@pytest.mark.parametrize("path", [REPO / "src" / "trading_system" / "app" / "probe_tool.py",
                                  REPO / "scripts" / "kiwoom-probe.py"])
def test_probe_sources_have_no_order_imports_or_calls(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            full = f"trading_system.app.{mod}" if node.level and mod else mod
            if full.startswith(FORBIDDEN_PACKAGES):
                bad.append(f"import {full}")
            bad += [f"import {a.name}" for a in node.names if a.name in FORBIDDEN_NAMES]
        elif isinstance(node, ast.Import):
            bad += [f"import {a.name}" for a in node.names if a.name.startswith(FORBIDDEN_PACKAGES)]
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            bad.append(f"{node.id}:{node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            bad.append(f".{node.attr}:{node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and \
                ("kt1000" in node.value or "/ordr" in node.value):
            bad.append(f"문자열 {node.value[:20]!r}:{node.lineno}")
    assert bad == []


def test_probe_rest_allowlist_is_exactly_token_and_two_charts() -> None:
    assert PROBE_REST_APIS == {"au10001", "au10002", "ka10080", "ka10079"}
    assert not PROBE_REST_APIS & ORDER_API_IDS
    assert WS_ALLOWED_TRNM == {"LOGIN", "REG", "REMOVE", "PING"}


# --- 7. 시각 요약 (순수 함수) --------------------------------------------------------------------------------

def test_summarize_times_shape() -> None:
    rows = [{"cntr_tm": v} for v in ("20261008090200", "20261008090100", "20261008090000", "20261007153000", "bad")]
    s = summarize_times(rows)
    assert s["rows"] == 5 and s["valid"] == 4 and s["invalid"] == 1
    assert s["order"] == "최신→과거" and s["min"] == "20261007153000" and s["max"] == "20261008090200"
    assert s["days"] == 2 and s["step_seconds"] == {"60": 2}
    assert s["per_day"][-1] == {"date": "20261008", "rows": 3, "first_hms": "090000", "last_hms": "090200"}
    assert summarize_times([])["rows"] == 0
