"""T-034 투자자별 일별 TR 모의 실측 도구 (K-U42, CAP-DAT-05 Q-2 — 사용자 승인 2026-10-11, 기록 A-088 예정).

네트워크 없이 합성 응답으로만 시험한다: HTTP는 GuardedHttp 안쪽에 가짜 객체.
- 안전 장치: --mode·TRADE_MODE가 PAPER가 아니면, 연결 스위치가 꺼져 있으면, 모의 도메인이 아니면, 거래일 장중
  (06:30~17:00 KST)이면, run.lock·autopilot.lock·하트비트가 살아 있으면 → 연결 없이 거부(종료 코드 2).
- 허용 목록: REST api-id {au10001, au10002, ka10060, ka10045, ka10008}. 밖의 것은 전송 전에 막는다.
  도구 소스는 주문 모듈·주문 함수를 import·호출하지 않는다(AST). 끝나면(실패해도) 토큰을 폐기한다.
- 기록: 필드 이름·쪽별 행 수·일자 범위·반환 코드·해시·합계 범주만. 가격·금액 원값·토큰·앱키·계좌번호는 없다.
- 명세 근거: S2 kiwoom_api_spec.json(2026-10-11 내려받음) ka10060·ka10045·ka10008 요청·응답 필드.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.scenario.test_cp032_probe import FORBIDDEN_NAMES, FORBIDDEN_PACKAGES, kst, repo_root
from tests.scenario.test_t015_preflight import ENV_OK, make_root
from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET
from trading_system.app import investor_probe
from trading_system.app.instance_lock import AUTOPILOT_LOCK, acquire_instance_lock
from trading_system.app.investor_probe import (
    DEEP_REST_APIS, INVESTOR_FIELDS, INVESTOR_REST_APIS, diff_category, investor_main, last_closed_day, sum_check, value_hash)
from trading_system.app.preflight import ORDER_API_IDS, GuardedHttp
from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS, KiwoomAdapter, KiwoomConfig
from trading_system.broker.kiwoom.http import HttpReply, PreSendError
from trading_system.domain.clock import KST, FixedClock
from trading_system.marketdata.calendar import KrxCalendar

REPO = Path(__file__).resolve().parents[2]
ALIAS = "KR-PAPER-01"
TOKEN = "virtual-token-investor-0001"
PRICE = "71234"                                     # 합성 가격 — 결과 파일·출력에 나오면 안 된다
TRADE_AMOUNT = "1105968"                            # 합성 누적거래대금 — 나오면 안 된다
EVENING = datetime(2026, 10, 8, 11, 0, tzinfo=UTC)  # 2026-10-08(목, 거래일) 20:00 KST
HOLIDAY_MORNING = kst(2026, 10, 9, 10, 0).astimezone(UTC)   # 한글날 휴장 10:00 KST
PROVIDE_FROM = date(2024, 1, 2)                     # 합성 서버: ka10060·ka10045는 이 날짜부터 준다
FRGN_DAYS = 250                                     # 합성 서버: ka10008은 최근 250 거래일만
PAGE = 100
CHART, MRKCOND, FRGNISTT = "/api/dostk/chart", "/api/dostk/mrkcond", "/api/dostk/frgnistt"
LAST = date(2026, 10, 8)


# --- 합성 서버 (HTTP 안쪽 객체, 소켓 없음) ------------------------------------------------------------

def weekdays_desc(upto: date, since: date = PROVIDE_FROM) -> list[date]:
    out: list[date] = []
    d = upto
    while d >= since:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def signed(v: int) -> str:
    return "0" if v == 0 else f"{v:+d}"


def investor_row(d: date, natfor_delta: int = 0) -> dict:
    """합: 개인+외국인+기관계+기타법인+국가+내외국인 = natfor_delta. 기관계 = 금융투자~사모펀드 합."""
    k = d.toordinal() % 1000
    ind, orgn = 55000 + k, 640 + k % 7
    return {"dt": d.strftime("%Y%m%d"), "cur_prc": f"+{PRICE}", "pred_pre": "+34", "acc_trde_prica": TRADE_AMOUNT,
            "ind_invsr": signed(ind), "frgnr_invsr": signed(-(ind + orgn)), "orgn": signed(orgn),
            "fnnc_invt": signed(orgn - 40), "insrnc": "+10", "invtrt": "+10", "etc_fnnc": "+5", "bank": "+5",
            "penfnd_etc": "+5", "samo_fund": "+5", "natn": "0", "etc_corp": "0", "natfor": signed(natfor_delta)}


def orgn_row(d: date) -> dict:
    return {"dt": d.strftime("%Y%m%d"), "close_pric": f"+{PRICE}", "pre_sig": "2", "pred_pre": "+34",
            "flu_rt": "+0.05", "trde_qty": "4321", "orgn_dt_acc": "158", "orgn_daly_nettrde_qty": "77",
            "for_dt_acc": "28315", "for_daly_nettrde_qty": "11243", "limit_exh_rt": "+26.14"}


def frgn_row(d: date) -> dict:
    return {"dt": d.strftime("%Y%m%d"), "close_pric": f"+{PRICE}", "pred_pre": "+34", "trde_qty": "4321",
            "chg_qty": "-3441", "poss_stkcnt": "6642402", "wght": "+26.02", "gain_pos_stkcnt": "18884304",
            "frgnr_limit": "25526706", "frgnr_limit_irds": "0", "limit_exh_rt": "+26.02"}


class FakeInner:
    """KiwoomHttp 자리. 실제 전송 대신 합성 응답. fail로 특정 api-id에 오류 응답을 줄 수 있다."""

    def __init__(self, *, revision: dict[date, int] | None = None, fail: dict[str, dict] | None = None,
                 token_reply: dict | None = None, mutate: Any = None, fail_offsets: set[int] | None = None) -> None:
        self.calls = 0
        self.log: list[tuple[str, str, dict, bool]] = []    # (경로, api-id, 본문, 토큰 있음)
        self.revision = revision or {}
        self.fail = fail or {}
        self.token_reply = token_reply
        self.mutate = mutate                    # ka10060 행 목록 변형 (깊은 조회 시험)
        self.fail_offsets = fail_offsets or set()   # ka10060 이 위치(next-key)의 쪽은 오류 응답

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
        if api_id in self.fail:
            return HttpReply(200, dict(self.fail[api_id]))
        off = int(next_key or 0)
        top: dict[str, Any] = {}
        if api_id == "ka10060":
            assert path == CHART
            dt = datetime.strptime(body["dt"], "%Y%m%d").date()
            rows = [investor_row(d, self.revision.get(d, 0)) for d in weekdays_desc(dt)]
            if self.mutate is not None and rows:
                rows = self.mutate(rows)
            if off in self.fail_offsets:
                return HttpReply(200, {"return_code": 5, "return_msg": "기타 업무 오류"})
            key = "stk_invsr_orgn_chart"
        elif api_id == "ka10045":
            assert path == MRKCOND
            start = max(datetime.strptime(body["strt_dt"], "%Y%m%d").date(), PROVIDE_FROM)
            rows = [orgn_row(d) for d in weekdays_desc(datetime.strptime(body["end_dt"], "%Y%m%d").date(), start)]
            key = "stk_orgn_trde_trnsn"
            top = {"orgn_prsm_avg_pric": "117052", "for_prsm_avg_pric": "0"}
        elif api_id == "ka10008":
            assert path == FRGNISTT
            rows, key = [frgn_row(d) for d in weekdays_desc(LAST)[:FRGN_DAYS]], "stk_frgnr"
        else:
            raise AssertionError(f"허용 목록 밖 API가 HTTP까지 옴: {api_id}")
        more = off + PAGE < len(rows)
        page = {"return_code": 0, "return_msg": "정상", **top, key: rows[off:off + PAGE]}
        return HttpReply(200, page, "Y" if more else "N", str(off + PAGE) if more else None)

    def apis(self) -> set[str]:
        return {a for _, a, _, _ in self.log}

    def count(self, api_id: str) -> int:
        return sum(1 for _, a, _, _ in self.log if a == api_id)


def make_conn(inner: FakeInner, clock: FixedClock, *, rest_url: str = MOCK_REST,
              allow: frozenset[str] = INVESTOR_REST_APIS) -> SimpleNamespace:
    guard = GuardedHttp(inner, allow)  # type: ignore[arg-type]
    cfg = KiwoomConfig(ALIAS, lambda: ACCOUNT_NO, lambda: APP_KEY, lambda: SECRET, rest_url=rest_url, ws_url=MOCK_WS,
                       per_tr_interval=0.0, query_wait=2.0)
    adapter = KiwoomAdapter(cfg, clock, http=guard)  # type: ignore[arg-type]
    return SimpleNamespace(adapter=adapter, guard=guard, app=SimpleNamespace(instruments={}))


class Harness:
    def __init__(self, tmp_path: Path, *, inner: FakeInner | None = None, when: datetime = EVENING,
                 rest_url: str = MOCK_REST, allow: frozenset[str] = INVESTOR_REST_APIS) -> None:
        self.root = repo_root(tmp_path)
        self.clock = FixedClock(when)
        self.inner = inner or FakeInner()
        self.connects = 0
        self.rest_url = rest_url
        self.allow = allow
        self.adapters: list[KiwoomAdapter] = []

    def connect(self, root: Path, environ: Any, clock: Any) -> SimpleNamespace:
        self.connects += 1
        conn = make_conn(self.inner, self.clock, rest_url=self.rest_url, allow=self.allow)
        self.adapters.append(conn.adapter)
        return conn

    def run(self, argv: list[str]) -> int:
        return investor_main(self.root, argv, {"TRADE_MODE": "PAPER"}, clock=self.clock, connect=self.connect)

    def files(self) -> list[Path]:
        return sorted((self.root / "var" / "paper" / "probe").glob("*-investor-probe.*"))

    def report(self, index: int = -1) -> tuple[dict, str, str, Path]:
        js = [p for p in self.files() if p.suffix == ".json"]
        md = [p for p in self.files() if p.suffix == ".md"]
        assert len(js) == len(md) >= 1
        text = js[index].read_text(encoding="utf-8")
        return json.loads(text), text, md[index].read_text(encoding="utf-8"), js[index]


ARGS = ["--mode", "PAPER", "--codes", "005930,000660"]


# --- 1. 모드·접속 대상 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["--mode", "LIVE"], ["--mode", "BACKTEST"], ["--mode", "paper"], []])
def test_refuses_non_paper_mode_argument_without_connecting(tmp_path: Path, argv: list[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(argv) == 2
    assert h.connects == 0 and h.inner.calls == 0 and h.files() == []


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
    code = investor_main(root, ["--mode", "PAPER"], environ, clock=FixedClock(EVENING))
    out = capsys.readouterr().out
    assert code == 2
    assert "연결 없음" in out and reason in out
    assert no_network == []
    for secret in (ACCOUNT_NO, APP_KEY, SECRET):
        assert secret not in out


def test_refuses_when_connection_switch_off(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                           no_network: list[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=False)
    assert investor_main(root, ["--mode", "PAPER"], {"TRADE_MODE": "PAPER"}, clock=FixedClock(EVENING)) == 2
    assert "connection_approved" in capsys.readouterr().out
    assert no_network == []


def test_default_connect_uses_mock_target_allowlist_and_conservative_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(investor_probe, "connect_paper", lambda root, env, clock, **kw: seen.update(kw) or "x")
    investor_probe._default_connect(Path("."), {}, FixedClock(EVENING))  # noqa: SLF001
    assert seen["allowed_apis"] == INVESTOR_REST_APIS
    assert seen["per_tr_interval"] >= 1.2          # 모의 TR당 1초 1회(E4)보다 보수적
    assert "rest_url" not in seen and "ws_url" not in seen   # 기본값 = 모의 도메인(MOCK_REST·MOCK_WS)


def test_refuses_non_mock_endpoint_and_revokes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path, rest_url="http://127.0.0.1:9")
    assert h.run(ARGS) == 2
    assert "모의 도메인" in capsys.readouterr().out
    assert h.inner.calls == 0


# --- 2. 장외 전용 ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("when", [kst(2026, 10, 8, 6, 30), kst(2026, 10, 8, 10, 0), kst(2026, 10, 8, 16, 59),
                                  kst(2026, 11, 19, 17, 30)])   # 수능일 마감 16:30 → 18:00까지 거부
def test_refuses_during_market_hours_without_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                        when: datetime) -> None:
    h = Harness(tmp_path, when=when.astimezone(UTC))
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
    ["--codes", "005930,000660,069500,035420"], ["--codes", "5930"], ["--max-pages", "0"], ["--max-pages", "61"],
    ["--back-days", "0"], ["--back-days", "abc"], ["--back-days", "3651"], ["--compare", "var/none.json"],
])
def test_refuses_bad_arguments(tmp_path: Path, extra: list[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(["--mode", "PAPER", *extra]) == 2
    assert h.connects == 0 and h.files() == []


@pytest.mark.parametrize("content", ["not json", json.dumps([1, 2]), json.dumps({"tool": "scripts/kiwoom-probe.py"}),
                                     json.dumps({"tool": "scripts/investor-probe.py", "finality": "x"})])
def test_refuses_compare_file_not_from_this_tool(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                 content: str) -> None:
    h = Harness(tmp_path)
    prev = tmp_path / "prev.json"
    prev.write_text(content, encoding="utf-8")
    assert h.run([*ARGS, "--compare", str(prev)]) == 2
    assert "--compare" in capsys.readouterr().out
    assert h.connects == 0


# --- 4. 정상 실측: 명세 요청 본문, 형태만 기록, 허용 목록 안에서만 호출 ------------------------------------------

def test_probe_queries_spec_bodies_and_records_shape_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(ARGS) == 0
    out = capsys.readouterr().out
    rep, text, md, _ = h.report()

    # 허용 목록 안에서만, 주문 API 0회, 토큰 폐기
    assert h.inner.apis() == INVESTOR_REST_APIS
    assert not h.inner.apis() & ORDER_API_IDS
    assert {p for p, *_ in h.inner.log} == {"/oauth2/token", "/oauth2/revoke", CHART, MRKCOND, FRGNISTT}
    assert rep["blocked_before_send"] == [] and rep["failures"] == []
    assert rep["token"] == {"issued": True, "expires_at": "2026-10-09T20:00:00+09:00", "revoked": True}
    assert h.inner.log[-1][1] == "au10002"
    assert rep["last_day"] == "20261008"

    # 요청 본문 (S2 명세 필수 입력)
    bodies = {(a, b.get("stk_cd"), b.get("dt")): b for _, a, b, _ in h.inner.log}
    assert bodies[("ka10060", "005930", "20261008")] == {"dt": "20261008", "stk_cd": "005930", "amt_qty_tp": "1",
                                                         "trde_tp": "0", "unit_tp": "1000"}
    k45 = next(b for _, a, b, _ in h.inner.log if a == "ka10045")
    assert k45 == {"stk_cd": "005930", "strt_dt": "20100101", "end_dt": "20261008", "orgn_prsm_unp_tp": "1",
                   "for_prsm_unp_tp": "1"}
    assert next(b for _, a, b, _ in h.inner.log if a == "ka10008") == {"stk_cd": "005930"}

    total = len(weekdays_desc(LAST))
    pages = -(-total // PAGE)
    recs = {(r["api"], r["code"], r["kind"]): r for r in rep["rest"]}
    p60 = recs[("ka10060", "005930", "paging")]
    assert p60["pages"] == pages and p60["more_pages"] is False and p60["rows"] == total
    assert p60["rows_per_page"] == [PAGE] * (pages - 1) + [total - PAGE * (pages - 1)]
    assert p60["dates"]["newest"] == "20261008" and p60["dates"]["oldest"] == "20240102"
    assert p60["dates"]["order"] == "최신→과거" and p60["dates"]["duplicates"] == 0
    assert p60["page_oldest"][0] == weekdays_desc(LAST)[PAGE - 1].strftime("%Y%m%d")
    assert set(INVESTOR_FIELDS) <= set(p60["fields"]) and "acc_trde_prica" in p60["fields"]
    assert p60["requested_dt_row_present"] is True

    # dt를 과거로 옮긴 1쪽
    b365 = recs[("ka10060", "005930", "dt-365d")]
    assert b365["request"]["dt"] == "20251008" and b365["rows"] == PAGE and b365["more_pages"] is True
    assert b365["dates"]["newest"] == "20251008" and b365["dt_honored"] is True
    b1095 = recs[("ka10060", "005930", "dt-1095d")]
    assert b1095["ok"] is True and b1095["rows"] == 0 and b1095["dt_honored"] is None   # 제공 범위 밖 = 빈 목록
    assert {k for (a, c, k) in recs if a == "ka10060" and c == "000660"} == \
        {"paging", "dt-365d", "dt-1095d", "dt-1825d", "dt-3650d"}

    p45 = recs[("ka10045", "005930", "range")]
    assert p45["rows"] == total and p45["dates"]["oldest"] == "20240102" and p45["more_pages"] is False
    assert "orgn_prsm_avg_pric" in p45["body_keys"] and "orgn_daly_nettrde_qty" in p45["fields"]
    p08 = recs[("ka10008", "005930", "paging")]
    assert p08["rows"] == FRGN_DAYS and p08["pages"] == 3 and p08["rows_per_page"] == [100, 100, 50]
    assert p08["dates"]["oldest"] == weekdays_desc(LAST)[FRGN_DAYS - 1].strftime("%Y%m%d")

    # 확정 시각 판단용 해시: 최근 3일자, 16자 hex, 원값 없음
    fin = rep["finality"]["005930"]
    assert [d["dt"] for d in fin["dates"]] == ["20261008", "20261007", "20261006"]
    assert all(re.fullmatch(r"[0-9a-f]{16}", d["investor_hash"]) for d in fin["dates"])
    assert all(re.fullmatch(r"[0-9a-f]{16}", d["row_hash"]) for d in fin["dates"])
    assert rep["finality"]["collected_at"] == "2026-10-08T20:00:00+09:00"
    assert rep["sum_check"]["005930"]["dt"] == "20261008"
    assert rep["sum_check"]["005930"]["total"] == "0" and rep["sum_check"]["005930"]["orgn_vs_parts"] == "0"

    # 기록·출력에 값 원문·비밀 없음
    latest = investor_row(LAST)
    raw_values = {v.lstrip("+-") for k, v in latest.items() if k != "dt" and len(v.lstrip("+-")) >= 4}
    for blob in (text, md, out):
        for secret in (TOKEN, APP_KEY, SECRET, ACCOUNT_NO, PRICE, TRADE_AMOUNT, "117052", "6642402", "11243",
                       *raw_values):
            assert secret not in blob
    assert "ka10060" in md and "| 005930 |" in md and "확정 시각" in md


def test_max_pages_caps_continuation(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.run([*ARGS, "--max-pages", "3", "--back-days", "30"]) == 0
    rep, *_ = h.report()
    p60 = next(r for r in rep["rest"] if r["api"] == "ka10060" and r["kind"] == "paging")
    assert p60["pages"] == 3 and p60["more_pages"] is True and p60["rows"] == 300
    assert h.inner.count("ka10060") == 2 * (3 + 1)
    assert h.inner.count("ka10045") == 2 * 3 and h.inner.count("ka10008") == 2 * 3


def test_default_codes_are_three(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.run(["--mode", "PAPER", "--max-pages", "1", "--back-days", "365"]) == 0
    rep, *_ = h.report()
    assert rep["codes"] == ["005930", "000660", "035420"]


# --- 5. 확정 시각 비교 (--compare) ----------------------------------------------------------------------------

def test_compare_reports_same_and_different_per_date(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(ARGS) == 0
    first, _, _, first_path = h.report()
    capsys.readouterr()

    # 다음 날(휴장일) 아침 — 2026-10-08 값만 바뀐 서버
    h.clock = FixedClock(HOLIDAY_MORNING)
    h.inner = FakeInner(revision={LAST: 5})
    assert h.run([*ARGS, "--compare", str(first_path)]) == 0
    out = capsys.readouterr().out
    second, text, md, _ = h.report()
    assert second["last_day"] == "20261008"
    cmp = {(c["code"], c["dt"]): c for c in second["compare"]["dates"]}
    assert cmp[("005930", "20261008")]["investor"] == "다름"
    assert cmp[("005930", "20261007")]["investor"] == "같음"
    assert cmp[("005930", "20261006")]["investor"] == "같음"
    assert cmp[("000660", "20261008")]["row"] == "다름"
    assert second["compare"]["previous_collected_at"] == first["finality"]["collected_at"]
    assert "005930 20261008: 투자자 값 다름" in out and "005930 20261007: 투자자 값 같음" in out
    assert "## 확정 시각 비교" in md
    assert second["sum_check"]["005930"]["total"] == ">1"
    for blob in (text, md, out):
        assert TOKEN not in blob and PRICE not in blob


def test_compare_marks_dates_missing_on_either_side(tmp_path: Path) -> None:
    prev = {"tool": "scripts/investor-probe.py", "finality": {
        "collected_at": "2026-10-07T20:00:00+09:00",
        "005930": {"dates": [{"dt": "20261007", "investor_hash": "0" * 16, "row_hash": "0" * 16},
                             {"dt": "20261005", "investor_hash": "1" * 16, "row_hash": "1" * 16}]}}}
    cur = {"collected_at": "x", "005930": {"dates": [{"dt": "20261008", "investor_hash": "2" * 16,
                                                      "row_hash": "2" * 16},
                                                     {"dt": "20261007", "investor_hash": "0" * 16,
                                                      "row_hash": "3" * 16}]}}
    res = {(c["dt"]): c for c in investor_probe.compare_finality(prev["finality"], cur)["dates"]}
    assert res["20261008"]["investor"] == "이전 결과에 없음"
    assert res["20261007"]["investor"] == "같음" and res["20261007"]["row"] == "다름"
    assert res["20261005"]["investor"] == "이번 결과에 없음"


# --- 6. 실패에도 토큰 폐기·중단 -----------------------------------------------------------------------------

def test_consecutive_failures_stop_and_token_still_revoked(tmp_path: Path) -> None:
    inner = FakeInner(fail={"ka10060": {"return_code": 5, "return_msg": "기타 업무 오류"}})
    h = Harness(tmp_path, inner=inner)
    assert h.run(ARGS) == 1
    rep, _, md, _ = h.report()
    assert [r["ok"] for r in rep["rest"]] == [False, False, False]
    assert inner.count("ka10060") == 3 and "ka10045" not in inner.apis()
    assert rep["rest_skipped"] and rep["token"]["revoked"] is True and inner.log[-1][1] == "au10002"
    assert rep["finality"]["005930"]["dates"] == [] and "## 실패" in md


def test_mock_unsupported_api_is_recorded_and_only_that_api_skipped(tmp_path: Path) -> None:
    inner = FakeInner(fail={"ka10045": {"return_code": 20,
                                        "return_msg": "[2000](RC9000:모의투자에서는 해당업무가 제공되지 않습니다.)"}})
    h = Harness(tmp_path, inner=inner)
    assert h.run(ARGS) == 1
    rep, *_ = h.report()
    assert inner.count("ka10045") == 1
    assert rep["api_calls"]["ka10045"]["codes"] == {"8104": 1}
    assert any("모의 미지원" in s for s in rep["rest_skipped"])
    assert inner.count("ka10008") == 2 * 3 and rep["token"]["revoked"] is True


def test_token_failure_stops_before_any_query(tmp_path: Path) -> None:
    inner = FakeInner(token_reply={"return_code": 8030, "return_msg": "[8030:투자구분 불일치]"})
    h = Harness(tmp_path, inner=inner)
    assert h.run(ARGS) == 1
    rep, *_ = h.report()
    assert inner.apis() == {"au10001"}
    assert rep["rest"] == [] and any("토큰 발급 실패" in f for f in rep["failures"])


def test_unexpected_exception_still_revokes_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("합성 예외")
    monkeypatch.setattr(investor_probe, "run_queries", boom)
    with pytest.raises(RuntimeError):
        h.run(ARGS)
    assert h.inner.log[-1][1] == "au10002"


# --- 7. 허용 목록·주문 코드 부재 ------------------------------------------------------------------------------

@pytest.mark.parametrize("path, api_id", [
    ("/api/dostk/ordr", "kt10000"), ("/api/dostk/ordr", "kt10001"), ("/api/dostk/ordr", "kt10003"),
    (CHART, "ka10080"), (CHART, "ka10081"), ("/api/dostk/stkinfo", "ka10059"), ("/api/dostk/stkinfo", "ka10061"),
    ("/api/dostk/acnt", "kt00018"),
])
def test_guard_allowlist_blocks_before_send(path: str, api_id: str) -> None:
    inner = FakeInner()
    g = GuardedHttp(inner, INVESTOR_REST_APIS)  # type: ignore[arg-type]
    with pytest.raises(PreSendError):
        g.post(path, api_id, {}, TOKEN)
    assert inner.calls == 0 and g.blocked == [api_id]


def test_rest_allowlist_is_exactly_token_and_three_investor_trs() -> None:
    assert INVESTOR_REST_APIS == {"au10001", "au10002", "ka10060", "ka10045", "ka10008"}
    assert not INVESTOR_REST_APIS & ORDER_API_IDS


@pytest.mark.parametrize("path", [REPO / "src" / "trading_system" / "app" / "investor_probe.py",
                                  REPO / "scripts" / "investor-probe.py"])
def test_sources_have_no_order_imports_or_calls(path: Path) -> None:
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


# --- 8. 순수 함수 ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def calendar() -> KrxCalendar:
    return KrxCalendar.load_dir(REPO / "config" / "calendar")


@pytest.mark.parametrize("when, expected", [
    (kst(2026, 10, 8, 17, 0), date(2026, 10, 8)),     # 거래일 장 마감 뒤 = 그날
    (kst(2026, 10, 8, 20, 0), date(2026, 10, 8)),
    (kst(2026, 10, 8, 6, 0), date(2026, 10, 7)),      # 거래일 장 전 = 전 거래일
    (kst(2026, 10, 9, 10, 0), date(2026, 10, 8)),     # 한글날 휴장
    (kst(2026, 10, 12, 5, 0), date(2026, 10, 8)),     # 월요일 새벽 (금 휴장·주말 건너뜀)
])
def test_last_closed_day(calendar: KrxCalendar, when: datetime, expected: date) -> None:
    assert last_closed_day(when, calendar) == expected


def test_last_closed_day_without_calendar_uses_weekdays() -> None:
    assert last_closed_day(kst(2026, 10, 10, 10, 0), None) == date(2026, 10, 9)   # 달력 없음: 금요일 = 거래일


def test_value_hash_normalizes_sign_and_is_16_hex() -> None:
    a = {"ind_invsr": "+1584", "frgnr_invsr": "-61779", "orgn": "60195"}
    b = {"ind_invsr": "1584", "frgnr_invsr": "-61779", "orgn": "+60195", "cur_prc": "+1"}
    fields = ("ind_invsr", "frgnr_invsr", "orgn")
    assert value_hash(a, fields) == value_hash(b, fields)
    assert re.fullmatch(r"[0-9a-f]{16}", value_hash(a, fields))
    assert value_hash(a, fields) != value_hash({**a, "orgn": "60196"}, fields)
    assert value_hash(a, fields) != value_hash({k: v for k, v in a.items() if k != "orgn"}, fields)


def test_sum_check_categories() -> None:
    base = investor_row(LAST)
    assert sum_check(base) == {"dt": "20261008", "total": "0", "total_without_natn": "0", "orgn_vs_parts": "0",
                               "orgn_vs_parts_with_natn": "0", "unparsable": []}
    assert sum_check({**base, "natfor": "+1"})["total"] == "≤1"
    assert sum_check({**base, "natfor": "-3"})["total"] == ">1"
    # 국가가 기관계에 포함된 경우: 국가를 빼고 합하면 0, 기관계 = 세부 + 국가
    natn_in = {**base, "natn": "+7", "orgn": signed(int(base["orgn"]) + 7),
               "frgnr_invsr": signed(int(base["frgnr_invsr"]) - 7)}
    r = sum_check(natn_in)
    assert r["total_without_natn"] == "0" and r["total"] == ">1"
    assert r["orgn_vs_parts_with_natn"] == "0" and r["orgn_vs_parts"] == ">1"
    bad = sum_check({k: v for k, v in base.items() if k != "bank"} | {"etc_corp": "x"})
    assert bad["unparsable"] == ["bank", "etc_corp"] and bad["orgn_vs_parts"] == "해석 불가"
    assert bad["total"] == "해석 불가"


def test_diff_category() -> None:
    from decimal import Decimal
    assert diff_category(Decimal("0")) == "0"
    assert diff_category(Decimal("-1")) == "≤1" and diff_category(Decimal("0.5")) == "≤1"
    assert diff_category(Decimal("1.01")) == ">1" and diff_category(Decimal("-250")) == ">1"


def test_kst_helper_is_aware() -> None:
    assert kst(2026, 10, 8, 20, 0).tzinfo is KST


# --- 9. 깊은 조회·값 저장 (--deep, 사용자 추가 지시 2026-10-11) ----------------------------------------------------

DEEP = ["--mode", "PAPER", "--codes", "005930", "--deep"]
SAVE = Path("var") / "backtest" / "data" / "kiwoom-investor-probe"


def deep_harness(tmp_path: Path, **kw: Any) -> Harness:
    return Harness(tmp_path, allow=DEEP_REST_APIS, **kw)


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    import csv
    with path.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def deep_meta(h: Harness) -> dict:
    metas = sorted((h.root / SAVE).glob("meta-*.json"))
    assert len(metas) == 1
    return json.loads(metas[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize("argv", [
    ["--mode", "PAPER", "--deep"],                                   # 기본 3종목 — 명시 1종목 아님
    ["--mode", "PAPER", "--codes", "005930,000660", "--deep"],
    [*DEEP, "--deep-max-pages", "0"], [*DEEP, "--deep-max-pages", "1001"],
    [*DEEP, "--compare", "x.json"],
])
def test_deep_refuses_unless_exactly_one_code_and_valid_args(tmp_path: Path, argv: list[str]) -> None:
    h = deep_harness(tmp_path)
    assert h.run(argv) == 2
    assert h.connects == 0 and h.inner.calls == 0


def test_deep_refuses_existing_csv_without_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = deep_harness(tmp_path)
    target = h.root / SAVE / "005930-ka10060-amt.csv"
    target.parent.mkdir(parents=True)
    target.write_text("dt\n20200101\n", encoding="utf-8")
    assert h.run(DEEP) == 2
    assert "덮어쓰지 않는다" in capsys.readouterr().out
    assert h.connects == 0 and target.read_text(encoding="utf-8") == "dt\n20200101\n"


@pytest.mark.parametrize("when", [kst(2026, 10, 8, 10, 0)])
def test_deep_refuses_during_market_hours(tmp_path: Path, when: datetime) -> None:
    h = deep_harness(tmp_path, when=when.astimezone(UTC))
    assert h.run(DEEP) == 2
    assert h.connects == 0


def test_deep_refuses_while_run_lock_held(tmp_path: Path) -> None:
    h = deep_harness(tmp_path)
    lock = acquire_instance_lock(h.root / "var" / "paper", "run.lock")
    try:
        assert h.run(DEEP) == 2
    finally:
        lock.release()
    assert h.connects == 0


def test_deep_connect_uses_subset_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(investor_probe, "connect_paper", lambda root, env, clock, **kw: seen.update(kw) or "x")
    investor_probe._deep_connect(Path("."), {}, FixedClock(EVENING))  # noqa: SLF001
    assert seen["allowed_apis"] == DEEP_REST_APIS == {"au10001", "au10002", "ka10060"}
    assert DEEP_REST_APIS < INVESTOR_REST_APIS
    assert seen["per_tr_interval"] >= 1.2 and "rest_url" not in seen


@pytest.mark.parametrize("path, api_id", [(MRKCOND, "ka10045"), (FRGNISTT, "ka10008"), ("/api/dostk/ordr", "kt10000"),
                                          ("/api/dostk/stkinfo", "ka10059")])
def test_deep_guard_blocks_other_apis_before_send(path: str, api_id: str) -> None:
    inner = FakeInner()
    g = GuardedHttp(inner, DEEP_REST_APIS)  # type: ignore[arg-type]
    with pytest.raises(PreSendError):
        g.post(path, api_id, {}, TOKEN)
    assert inner.calls == 0 and g.blocked == [api_id]


def test_deep_fetches_until_no_more_then_checks_before_and_saves(tmp_path: Path,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    h = deep_harness(tmp_path)
    assert h.run(DEEP) == 0
    out = capsys.readouterr().out
    total = len(weekdays_desc(LAST))
    pages = -(-total // PAGE)
    assert h.inner.apis() == DEEP_REST_APIS
    k60 = [b for _, a, b, _ in h.inner.log if a == "ka10060"]
    assert len(k60) == pages + 1                                   # 끝까지 + 이전 자료 확인 1쪽
    assert all(b["dt"] == "20261008" and b["amt_qty_tp"] == "1" and b["trde_tp"] == "0" and b["unit_tp"] == "1000"
               for b in k60[:-1])
    assert k60[-1]["dt"] == "20240101"                            # 가장 오래된 일자(20240102) 하루 전
    assert h.inner.log[-1][1] == "au10002"

    meta = deep_meta(h)
    assert meta["end"] == "더 없음" and meta["cap_reached"] is False
    assert meta["pages"] == pages and meta["rows_per_page"][:-1] == [PAGE] * (pages - 1)
    assert meta["rows_per_call"]["without_last_page"] == {"min": PAGE, "max": PAGE, "mode": PAGE}
    assert meta["start_date"] == "20240102" and meta["end_date"] == "20261008"
    assert meta["before_check"]["done"] is True and meta["before_check"]["rows"] == 0
    assert meta["before_check"]["result"].startswith("확인 완료")
    assert meta["field_units"]["ind_invsr"].startswith("백만원") and meta["interval_seconds"] >= 1.2
    assert meta["calls"] == 1 + pages + 1 + 1 and meta["api_calls"]["ka10060"]["codes"] == {"0": pages + 1}
    assert meta["failures"] == [] and meta["token"]["revoked"] is True

    cols, rows = read_csv(h.root / SAVE / "005930-ka10060-amt.csv")
    assert cols == list(investor_row(LAST).keys())
    assert len(rows) == total and [r["dt"] for r in rows] == sorted(r["dt"] for r in rows)
    expect = investor_row(LAST)
    assert rows[-1] == expect                                      # 응답 문자열 그대로(부호 유지)
    assert rows[-1]["ind_invsr"].startswith("+") and rows[-1]["frgnr_invsr"].startswith("-") \
        and rows[-1]["natn"] == "0"
    meta_text = json.dumps(meta, ensure_ascii=False)
    for blob in (out, meta_text):
        for secret in (TOKEN, APP_KEY, SECRET, ACCOUNT_NO):
            assert secret not in blob
    for blob in (out, meta_text):
        assert PRICE not in blob and expect["ind_invsr"].lstrip("+") not in blob   # 값은 CSV에만


def test_deep_cap_reached_records_possible_older_data(tmp_path: Path) -> None:
    h = deep_harness(tmp_path)
    assert h.run([*DEEP, "--deep-max-pages", "3"]) == 0
    meta = deep_meta(h)
    assert meta["cap_reached"] is True and meta["end"].startswith("상한 도달") and "더 과거가 있을 수 있음" in meta["end"]
    assert h.inner.count("ka10060") == 3                           # 상한 도달이면 이전 자료 확인 쪽 없음
    assert meta["before_check"]["done"] is False
    _, rows = read_csv(h.root / SAVE / "005930-ka10060-amt.csv")
    assert len(rows) == 3 * PAGE


def test_deep_csv_field_union_missing_and_duplicate_dates(tmp_path: Path) -> None:
    def mutate(rows: list[dict]) -> list[dict]:
        rows = [dict(r) for r in rows]
        rows[120]["xtra_fld"] = "7"                     # 나중 쪽에만 있는 필드 → 열 끝에 추가
        del rows[5]["natfor"]                           # 빠진 필드 → 빈 칸
        rows.insert(150, {**rows[10], "ind_invsr": "+1"})   # 다른 쪽에 같은 일자 → 첫 값 유지 + 실패 기록
        return rows

    h = deep_harness(tmp_path, inner=FakeInner(mutate=mutate))
    assert h.run(DEEP) == 1
    meta = deep_meta(h)
    cols, rows = read_csv(h.root / SAVE / "005930-ka10060-amt.csv")
    assert cols == [*investor_row(LAST).keys(), "xtra_fld"]
    by_dt = {r["dt"]: r for r in rows}
    days = weekdays_desc(LAST)
    assert len(rows) == len(days)
    assert by_dt[days[5].strftime("%Y%m%d")]["natfor"] == ""
    assert by_dt[days[10].strftime("%Y%m%d")]["ind_invsr"] == investor_row(days[10])["ind_invsr"]
    assert by_dt[days[120].strftime("%Y%m%d")]["xtra_fld"] == "7" and by_dt[days[0].strftime("%Y%m%d")]["xtra_fld"] == ""
    assert any("중복 일자 1건" in f for f in meta["failures"])
    assert meta["field_units"]["xtra_fld"].startswith("명세에 없음")


def test_deep_consecutive_failures_stop_and_save_received(tmp_path: Path) -> None:
    h = deep_harness(tmp_path, inner=FakeInner(fail_offsets={2 * PAGE}))
    assert h.run(DEEP) == 1
    meta = deep_meta(h)
    assert h.inner.count("ka10060") == 2 + 3                      # 2쪽 받고 3쪽째 3회 연속 실패
    assert meta["end"] == "중단: 연속 실패 3회" and meta["pages"] == 2
    assert meta["before_check"]["done"] is False and meta["token"]["revoked"] is True
    _, rows = read_csv(h.root / SAVE / "005930-ka10060-amt.csv")
    assert len(rows) == 2 * PAGE
