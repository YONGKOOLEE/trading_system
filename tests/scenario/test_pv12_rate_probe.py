"""PV-12 키움 모의 호출 제한(1700) 유도 실측 도구 (04 §4.1 PV-12, K-U10, 승인 A-097 ③) — 네트워크 없이 합성 응답으로만.

소켓을 열지 않는다: HTTP는 GuardedHttp 안쪽에 가짜 객체, 시간은 가짜 시계(monotonic·sleep 주입)로 진행한다.
- 안전 장치: --mode·TRADE_MODE가 PAPER가 아니면, 모의 도메인이 아니면, 거래일 장중(probe_tool 규칙)이면,
  run.lock·autopilot.lock·하트비트가 살아 있으면 → 연결 없이 거부(종료 코드 2).
- 허용 목록: REST api-id {au10001, au10002, ka10001}. 밖의 것은 전송 전에 막는다. 소스는 주문 모듈을 import하지 않는다.
- 절차: 간격 단계별(기본 1.0 → 0.5 → 0.25 → 0초, 단계마다 최대 10회) 같은 조회 → 1700(1701·1702)이면 그 단계 즉시 중단 →
  대기 1·2·4·8·16초로 회복 확인 → 종료. 전체 호출·시간 상한. 차단(418)·인증·알 수 없는 오류는 즉시 중단. 끝나면 토큰 폐기.
- 기록: 단계별 간격·호출 수·코드 분포·첫 1700 시각·직전 간격·회복 대기·return_msg(가림)만. 가격·토큰·키·계좌 없음.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.scenario.test_cp032_probe import FORBIDDEN_NAMES, FORBIDDEN_PACKAGES, kst, repo_root
from tests.scenario.test_t015_preflight import ENV_OK, make_root
from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET
from trading_system.app import probe_tool, rate_probe
from trading_system.app.instance_lock import AUTOPILOT_LOCK, acquire_instance_lock
from trading_system.app.preflight import ORDER_API_IDS, GuardedHttp
from trading_system.app.rate_probe import (
    DEFAULT_INTERVALS, RATE_REST_APIS, RECOVERY_WAITS, RateProbeError, parse_intervals, rate_probe_main)
from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS, KiwoomAdapter, KiwoomConfig
from trading_system.broker.kiwoom.http import AfterSendError, HttpReply, PreSendError
from trading_system.domain.clock import KST

REPO = Path(__file__).resolve().parents[2]
ALIAS = "KR-PAPER-01"
TOKEN = "virtual-token-rate-0001"
PRICE = "71234"                                    # 합성 가격 — 결과 파일·출력에 나오면 안 된다
EVENING = datetime(2026, 10, 8, 11, 0, tzinfo=UTC)  # 2026-10-08(목, 거래일) 20:00 KST
LATENCY = 0.05                                     # 합성 응답 지연(초)
LIMIT_MSG = "[1700:허용된 요청 개수를 초과하였습니다. API ID=ka10001] 계좌 1234567890"


class FakeTime:
    """monotonic·sleep·시계를 한 축으로 묶는다. sleep만 시간을 진행하고, 응답마다 LATENCY만큼 진행한다."""

    def __init__(self, start: datetime = EVENING) -> None:
        self.t = 100.0
        self.start = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.slept.append(round(seconds, 6))
        self.t += seconds

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.t - 100.0)


class FakeInner:
    """KiwoomHttp 자리. ka10001은 직전 ka10001 요청과의 간격이 min_gap 미만이면 limit_code로 거부한다.

    회복: 마지막 요청(거부 포함) 뒤 quiet초 이상 조용해야 정상 응답한다. script로 응답을 직접 줄 수도 있다."""

    def __init__(self, time: FakeTime, *, min_gap: float = 0.4, quiet: float = 2.0, limit_code: int = 1700,
                 script: list[Any] | None = None, token_reply: dict | None = None) -> None:
        self.time = time
        self.min_gap = min_gap
        self.quiet = quiet
        self.limit_code = limit_code
        self.script = list(script) if script is not None else None
        self.token_reply = token_reply
        self.calls = 0
        self.log: list[tuple[str, str, dict, float]] = []   # (경로, api-id, 본문, 보낸 시각)
        self._last: float | None = None
        self._limited = False

    def post(self, path: str, api_id: str, body: dict, token: str | None,
             cont_yn: str | None = None, next_key: str | None = None) -> HttpReply:
        self.calls += 1
        sent = self.time.t
        self.log.append((path, api_id, dict(body), sent))
        self.time.t += LATENCY
        if api_id == "au10001":
            return HttpReply(200, self.token_reply or {"return_code": 0, "return_msg": "정상", "token": TOKEN,
                                                       "token_type": "bearer", "expires_dt": "20261231235959"})
        if api_id == "au10002":
            return HttpReply(200, {"return_code": 0, "return_msg": "정상"})
        assert api_id == "ka10001", f"허용 목록 밖 API가 HTTP까지 옴: {api_id}"
        assert token == TOKEN
        if self.script is not None:
            item = self.script.pop(0) if self.script else "ok"
            if isinstance(item, Exception):
                raise item
            if item != "ok":
                return item
            return self._ok(body)
        gap = None if self._last is None else sent - self._last
        prev, self._last = self._last, sent
        if self._limited:
            if prev is not None and sent - prev < self.quiet:
                return self._limit()
            self._limited = False
            return self._ok(body)
        if gap is not None and gap < self.min_gap:
            self._limited = True
            return self._limit()
        return self._ok(body)

    def _limit(self) -> HttpReply:
        msg = LIMIT_MSG.replace("1700", str(self.limit_code))
        return HttpReply(200, {"return_code": 5, "return_msg": msg})

    @staticmethod
    def _ok(body: dict) -> HttpReply:
        return HttpReply(200, {"return_code": 0, "return_msg": "정상적으로 처리되었습니다", "stk_cd": body["stk_cd"],
                               "cur_prc": f"+{PRICE}", "upl_pric": "+92600", "lst_pric": "-49800",
                               "trde_qty": "4321"})

    def apis(self) -> list[str]:
        return [a for _, a, _, _ in self.log]

    def sends(self) -> list[float]:
        return [t for _, a, _, t in self.log if a == "ka10001"]


def make_conn(inner: FakeInner, clock: Any, *, rest_url: str = MOCK_REST, ws_url: str = MOCK_WS) -> SimpleNamespace:
    guard = GuardedHttp(inner, RATE_REST_APIS)  # type: ignore[arg-type]
    cfg = KiwoomConfig(ALIAS, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET, rest_url=rest_url, ws_url=ws_url,
                       per_tr_interval=0.0, query_wait=2.0)
    adapter = KiwoomAdapter(cfg, clock, http=guard)  # type: ignore[arg-type]
    return SimpleNamespace(adapter=adapter, guard=guard, app=SimpleNamespace(instruments={"005930": None}))


class Harness:
    def __init__(self, tmp_path: Path, *, when: datetime = EVENING, rest_url: str = MOCK_REST,
                 ws_url: str = MOCK_WS, **inner_kw: Any) -> None:
        self.root = repo_root(tmp_path)
        self.time = FakeTime(when)
        self.inner = FakeInner(self.time, **inner_kw)
        self.connects = 0
        self.rest_url, self.ws_url = rest_url, ws_url

    def connect(self, root: Path, environ: Any, clock: Any) -> SimpleNamespace:
        self.connects += 1
        return make_conn(self.inner, clock, rest_url=self.rest_url, ws_url=self.ws_url)

    def run(self, argv: list[str]) -> int:
        return rate_probe_main(self.root, argv, {"TRADE_MODE": "PAPER"}, clock=self.time, connect=self.connect,
                               monotonic=self.time.monotonic, sleep=self.time.sleep)

    def report(self) -> tuple[dict, str, str]:
        files = sorted((self.root / "var" / "paper" / "probe").glob("*-rate-probe.*"))
        assert [p.suffix for p in files] == [".json", ".md"]
        text = files[0].read_text(encoding="utf-8")
        return json.loads(text), text, files[1].read_text(encoding="utf-8")


ARGS = ["--mode", "PAPER"]


def assert_no_secrets(*blobs: str) -> None:
    for blob in blobs:
        for secret in (TOKEN, APP_KEY, SECRET, ACCOUNT_NO, PRICE, "92600", "49800", "4321", "1234567890"):
            assert secret not in blob


# --- 1. 모드·접속 대상 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["--mode", "LIVE"], ["--mode", "BACKTEST"], ["--mode", "paper"], []])
def test_refuses_non_paper_mode_argument_without_connecting(tmp_path: Path, argv: list[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(argv) == 2
    assert h.connects == 0 and h.inner.calls == 0


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
    t = FakeTime()
    code = rate_probe_main(root, ARGS, environ, clock=t, monotonic=t.monotonic, sleep=t.sleep)
    out = capsys.readouterr().out
    assert code == 2
    assert "연결 없음" in out and reason in out
    assert no_network == []
    assert_no_secrets(out)


def test_refuses_when_connection_switch_off(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                           no_network: list[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=False)
    t = FakeTime()
    assert rate_probe_main(root, ARGS, {"TRADE_MODE": "PAPER"}, clock=t, monotonic=t.monotonic, sleep=t.sleep) == 2
    assert "connection_approved" in capsys.readouterr().out
    assert no_network == []


def test_default_connect_uses_mock_target_allowlist_and_unchanged_adapter_interval(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(rate_probe, "connect_paper", lambda root, env, clock, **kw: seen.update(kw) or "x")
    rate_probe._default_connect(Path("."), {}, FixedClockLike())  # noqa: SLF001
    assert seen["allowed_apis"] == RATE_REST_APIS
    assert seen["per_tr_interval"] >= 1.1           # 어댑터(토큰 발급 au10001)는 보수 간격 그대로
    assert "rest_url" not in seen and "ws_url" not in seen   # 기본값 = 모의 도메인(MOCK_REST·MOCK_WS)


class FixedClockLike:
    def now(self) -> datetime:
        return EVENING


def test_adapter_default_interval_is_unchanged() -> None:
    """M1 PAPER run 동작 불변: 어댑터·호출 제한기 기본 간격은 1.1초 그대로 (이 도구는 자기 안에서만 간격을 줄인다)."""
    from trading_system.broker.ratelimit import RateLimiter
    assert KiwoomConfig("A", str, str, str).per_tr_interval == 1.1
    assert RateLimiter()._interval == 1.1  # noqa: SLF001


@pytest.mark.parametrize("rest_url, ws_url", [("http://127.0.0.1:9", MOCK_WS), (MOCK_REST, "ws://127.0.0.1:9")])
def test_refuses_non_mock_endpoint(tmp_path: Path, capsys: pytest.CaptureFixture[str], rest_url: str,
                                   ws_url: str) -> None:
    h = Harness(tmp_path, rest_url=rest_url, ws_url=ws_url)
    assert h.run(ARGS) == 2
    assert "모의 도메인" in capsys.readouterr().out
    assert h.inner.calls == 0


# --- 2. 장외 전용 (probe_tool 규칙 그대로) -----------------------------------------------------------------

def test_market_hours_rule_is_probe_tool_rule() -> None:
    assert rate_probe.market_hours_refusal is probe_tool.market_hours_refusal
    assert rate_probe._running_refusal is probe_tool._running_refusal  # noqa: SLF001


@pytest.mark.parametrize("when", [kst(2026, 10, 8, 6, 30), kst(2026, 10, 8, 10, 0), kst(2026, 10, 8, 16, 59),
                                  kst(2026, 11, 19, 17, 30)])
def test_refuses_during_market_hours_without_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                        when: datetime) -> None:
    h = Harness(tmp_path, when=when.astimezone(UTC))
    assert h.run(ARGS) == 2
    assert "장외 전용" in capsys.readouterr().out
    assert h.connects == 0 and h.inner.calls == 0


@pytest.mark.parametrize("when", [kst(2026, 10, 8, 20, 0), kst(2026, 10, 9, 10, 0), kst(2026, 10, 10, 10, 0)])
def test_runs_outside_market_hours(tmp_path: Path, when: datetime) -> None:
    h = Harness(tmp_path, when=when.astimezone(UTC))
    assert h.run(ARGS) == 0
    assert h.connects == 1


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
    ["--code", "5930"], ["--code", "005930,000660"], ["--per-step", "0"], ["--per-step", "21"],
    ["--max-calls", "0"], ["--max-calls", "81"], ["--max-seconds", "0"], ["--max-seconds", "181"],
    ["--intervals", "abc"], ["--intervals", "0.5,1.0"], ["--intervals", "1.0,1.0"], ["--intervals", "-1"],
    ["--intervals", "1.5"], ["--intervals", ""], ["--intervals", "1,0.8,0.6,0.4,0.3,0.2,0.1,0"],
])
def test_refuses_bad_arguments(tmp_path: Path, extra: list[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(["--mode", "PAPER", *extra]) == 2
    assert h.connects == 0


def test_parse_intervals() -> None:
    assert parse_intervals("1.0, 0.5,0.25,0") == [1.0, 0.5, 0.25, 0.0]
    assert DEFAULT_INTERVALS == (1.0, 0.5, 0.25, 0.0)
    with pytest.raises(RateProbeError):
        parse_intervals("0.25,0.5")


# --- 4. 정상 실측: 단계별 간격 축소 → 1700 → 그 단계 즉시 중단 → 회복 대기 ---------------------------------------

def test_descends_until_1700_stops_step_and_measures_recovery(tmp_path: Path,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    """합성 서버: 간격 0.4초 미만이면 1700, 1700 뒤에는 2초 이상 조용해야 정상."""
    h = Harness(tmp_path)
    assert h.run(ARGS) == 0
    out = capsys.readouterr().out
    rep, text, md = h.report()

    # 허용 목록 안에서만, 같은 TR·같은 종목만, 주문 API 0회
    assert set(h.inner.apis()) == {"au10001", "ka10001", "au10002"}
    assert not set(h.inner.apis()) & ORDER_API_IDS
    assert {p for p, a, _, _ in h.inner.log if a == "ka10001"} == {"/api/dostk/stkinfo"}
    assert all(b == {"stk_cd": "005930"} for _, a, b, _ in h.inner.log if a == "ka10001")
    assert rep["blocked_before_send"] == []

    steps = rep["steps"]
    assert [s["interval"] for s in steps] == [1.0, 0.5, 0.25]          # 0초 단계는 가지 않는다
    assert [s["calls"] for s in steps] == [10, 10, 1]                  # 1700에서 그 단계 즉시 중단
    assert steps[0]["codes"] == {"0": 10} and steps[1]["codes"] == {"0": 10} and steps[2]["codes"] == {"1700": 1}
    assert steps[0]["gap_min"] >= 1.0 and steps[1]["gap_min"] >= 0.5   # 단계 간격을 지켰다
    assert "1700" in steps[2]["ended"]

    first = rep["first_limit"]
    assert first["code"] == 1700 and first["step_interval"] == 0.25 and first["call_in_step"] == 1
    assert first["total_call"] == 21
    assert first["gap_before"] == pytest.approx(0.25)                # 직전 요청 시작부터의 실제 간격
    assert first["at"].startswith("2026-10-08T20:") and first["at"].endswith("+09:00")
    assert "1700" in first["return_msg"] and "1234567890" not in first["return_msg"]   # 가림 처리

    rec = rep["recovery"]
    assert [(a["wait"], a["code"]) for a in rec["attempts"]] == [(1, 1700), (2, 0)]
    assert rec["recovered_after"] == 2
    assert rep["result"] == "1700 유도·회복 측정 완료" and rep["failures"] == []
    assert rep["calls_total"] == 23 and h.inner.apis().count("ka10001") == 23

    # 회복 대기는 1700 응답 수신 뒤 실제로 잠든 시간 — 1초, (재차 1700) 2초
    assert h.time.slept[-2:] == [1.0, 2.0]
    # 토큰 폐기가 마지막 호출
    assert h.inner.apis()[-1] == "au10002" and rep["token"]["revoked"] is True

    assert_no_secrets(text, md, out)
    assert "## 단계별" in md and "| 0.25 | 1 |" in md and "회복에 필요한 대기: 2초" in md
    assert "K-U10" in md


@pytest.mark.parametrize("limit_code", [1701, 1702])
def test_1701_and_1702_also_end_step_and_start_recovery(tmp_path: Path, limit_code: int) -> None:
    h = Harness(tmp_path, limit_code=limit_code)
    assert h.run(ARGS) == 0
    rep, _, _ = h.report()
    assert rep["first_limit"]["code"] == limit_code
    assert rep["recovery"]["recovered_after"] == 2


def test_no_limit_runs_every_step_and_reports_not_reached(tmp_path: Path) -> None:
    h = Harness(tmp_path, min_gap=0.0)
    assert h.run(ARGS) == 0
    rep, _, md = h.report()
    assert [s["interval"] for s in rep["steps"]] == [1.0, 0.5, 0.25, 0.0]
    assert [s["calls"] for s in rep["steps"]] == [10, 10, 10, 10]
    assert rep["first_limit"] is None and rep["recovery"] is None
    assert rep["result"] == "1700 미발생"
    assert h.inner.apis().count("ka10001") == 40 and h.inner.apis()[-1] == "au10002"
    assert "1700 미발생" in md


def test_custom_steps_and_code(tmp_path: Path) -> None:
    h = Harness(tmp_path, min_gap=0.0)
    assert h.run(ARGS + ["--intervals", "0.8,0.3", "--per-step", "3", "--code", "000660"]) == 0
    rep, _, _ = h.report()
    assert [(s["interval"], s["calls"]) for s in rep["steps"]] == [(0.8, 3), (0.3, 3)]
    assert all(b == {"stk_cd": "000660"} for _, a, b, _ in h.inner.log if a == "ka10001")


def test_recovery_not_reached_within_16_seconds(tmp_path: Path) -> None:
    h = Harness(tmp_path, quiet=60.0)
    assert h.run(ARGS) == 1
    rep, _, md = h.report()
    assert [a["wait"] for a in rep["recovery"]["attempts"]] == list(RECOVERY_WAITS) == [1, 2, 4, 8, 16]
    assert all(a["code"] == 1700 for a in rep["recovery"]["attempts"])
    assert rep["recovery"]["recovered_after"] is None
    assert any("회복" in f for f in rep["failures"])
    assert h.inner.apis()[-1] == "au10002" and rep["token"]["revoked"] is True


# --- 5. 즉시 중단: 차단·인증·알 수 없는 오류·전송 오류 ----------------------------------------------------------

@pytest.mark.parametrize("reply, word", [
    (HttpReply(418, None), "418"),
    (HttpReply(418, {"return_code": 1700, "return_msg": "blocked"}), "418"),
    (HttpReply(429, {"return_code": 9, "return_msg": "too many"}), "429"),
    (HttpReply(401, None), "인증"),
    (HttpReply(200, {"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"}), "AUTH"),
    (HttpReply(200, {"return_code": 1999, "return_msg": "예기치 못한 오류"}), "UNEXPECTED"),
    (HttpReply(200, {"return_code": 5, "return_msg": "기타 업무 오류"}), "BUSINESS"),
    (HttpReply(200, {"return_code": 1687, "return_msg": "재귀 호출"}), "1687"),
    (HttpReply(200, {"return_code": 8030, "return_msg": "[8030:투자구분 불일치]"}), "MODE_MISMATCH"),
    (HttpReply(500, None), "500"),
    (HttpReply(200, {"return_msg": "코드 없음"}), "return_code"),
])
def test_aborts_immediately_on_block_auth_or_unknown(tmp_path: Path, reply: HttpReply, word: str) -> None:
    h = Harness(tmp_path, script=["ok", "ok", reply, "ok", "ok"])
    assert h.run(ARGS) == 1
    rep, text, md = h.report()
    assert h.inner.apis().count("ka10001") == 3                         # 그 응답 뒤로 더 보내지 않는다
    assert rep["first_limit"] is None and rep["recovery"] is None
    assert rep["result"].startswith("중단") and word in rep["result"]
    assert h.inner.apis()[-1] == "au10002" and rep["token"]["revoked"] is True
    assert_no_secrets(text, md)


@pytest.mark.parametrize("exc", [PreSendError("ka10001: 연결 실패"), AfterSendError("ka10001: 읽기 타임아웃")])
def test_aborts_on_transport_error(tmp_path: Path, exc: Exception) -> None:
    h = Harness(tmp_path, script=["ok", exc, "ok"])
    assert h.run(ARGS) == 1
    rep, _, _ = h.report()
    assert h.inner.apis().count("ka10001") == 2
    assert "전송" in rep["result"]
    assert rep["token"]["revoked"] is True


def test_abort_during_recovery_stops_immediately(tmp_path: Path) -> None:
    limit = HttpReply(200, {"return_code": 5, "return_msg": "[1700:허용된 요청 개수를 초과]"})
    h = Harness(tmp_path, script=["ok", limit, HttpReply(418, None), "ok"])
    assert h.run(ARGS) == 1
    rep, _, _ = h.report()
    assert h.inner.apis().count("ka10001") == 3
    assert rep["first_limit"]["code"] == 1700
    assert [a["wait"] for a in rep["recovery"]["attempts"]] == [1]
    assert rep["recovery"]["recovered_after"] is None and "418" in rep["result"]


def test_token_failure_sends_no_query(tmp_path: Path) -> None:
    h = Harness(tmp_path, token_reply={"return_code": 8030, "return_msg": "[8030:투자구분 불일치]"})
    assert h.run(ARGS) == 1
    rep, _, _ = h.report()
    assert "ka10001" not in h.inner.apis()
    assert rep["steps"] == [] and any("토큰 발급 실패" in f for f in rep["failures"])


# --- 6. 상한: 전체 호출 수·전체 시간 --------------------------------------------------------------------------

def test_total_call_cap(tmp_path: Path) -> None:
    h = Harness(tmp_path, min_gap=0.0)
    assert h.run(ARGS + ["--max-calls", "15"]) == 1
    rep, _, _ = h.report()
    assert h.inner.apis().count("ka10001") == 15
    assert [s["calls"] for s in rep["steps"]] == [10, 5]
    assert "호출 상한" in rep["result"] and h.inner.apis()[-1] == "au10002"


def test_total_call_cap_also_bounds_recovery(tmp_path: Path) -> None:
    h = Harness(tmp_path, quiet=60.0)
    assert h.run(ARGS + ["--max-calls", "23"]) == 1
    rep, _, _ = h.report()
    assert h.inner.apis().count("ka10001") == 23
    assert [a["wait"] for a in rep["recovery"]["attempts"]] == [1, 2]
    assert "호출 상한" in rep["result"]


def test_total_time_cap(tmp_path: Path) -> None:
    h = Harness(tmp_path, min_gap=0.0)
    assert h.run(ARGS + ["--max-seconds", "5"]) == 1
    rep, _, _ = h.report()
    assert h.time.t - 100.0 < 5 + 1.5                                   # 상한을 넘겨 기다리지 않는다
    assert h.inner.apis().count("ka10001") < 10
    assert "시간 상한" in rep["result"] and rep["token"]["revoked"] is True


def test_default_caps() -> None:
    ns = rate_probe._args(["--mode", "PAPER"])  # noqa: SLF001
    assert ns is not None
    assert (ns.per_step, ns.max_calls, ns.max_seconds, ns.code) == (10, 80, 180, "005930")


# --- 7. 허용 목록·주문 코드 부재 ------------------------------------------------------------------------------

def test_rate_allowlist_is_exactly_token_and_one_query() -> None:
    assert RATE_REST_APIS == {"au10001", "au10002", "ka10001"}
    assert not RATE_REST_APIS & ORDER_API_IDS


@pytest.mark.parametrize("path, api_id", [
    ("/api/dostk/ordr", "kt10000"), ("/api/dostk/ordr", "kt10003"), ("/api/dostk/chart", "ka10080"),
    ("/api/dostk/acnt", "kt00018"), ("/api/dostk/stkinfo", "ka10099"),
])
def test_guard_allowlist_blocks_before_send(path: str, api_id: str) -> None:
    inner = FakeInner(FakeTime())
    g = GuardedHttp(inner, RATE_REST_APIS)  # type: ignore[arg-type]
    with pytest.raises(PreSendError):
        g.post(path, api_id, {}, TOKEN)
    assert inner.calls == 0 and g.blocked == [api_id]


@pytest.mark.parametrize("path", [REPO / "src" / "trading_system" / "app" / "rate_probe.py",
                                  REPO / "scripts" / "rate-probe.py"])
def test_rate_probe_sources_have_no_order_imports_or_calls(path: Path) -> None:
    import ast
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


def test_report_written_even_without_limit_has_kst_names(tmp_path: Path) -> None:
    h = Harness(tmp_path, min_gap=0.0)
    h.run(ARGS)
    files = sorted((h.root / "var" / "paper" / "probe").glob("*-rate-probe.*"))
    assert [p.name for p in files] == [f"{EVENING.astimezone(KST):%Y%m%d-%H%M%S}-rate-probe.json",
                                       f"{EVENING.astimezone(KST):%Y%m%d-%H%M%S}-rate-probe.md"]
