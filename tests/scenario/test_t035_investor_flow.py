"""T-035 CAP-DAT-05a 투자자별 일별 순매수 수집·저장·품질 (CP-033 D-188·D-190·D-191·D-192, 승인 A-100).

네트워크 없이 합성 응답만 쓴다(HTTP 안쪽 가짜 객체 — 소켓 없음). 모든 값은 가상이다.
- V-257 ka10060 파싱: 일자·투자자 구분·부호 포함 금액(백만원) Decimal, 연속 조회 이어 붙이기, 필드 누락 → 그 일자 무효.
- V-260 정정 감지: 같은 키 재수집 값이 다르면 새 회차 행 + 감사, 이전 행 불변(추가 전용 — 고치기·지우기 거부).
- V-261 품질: 역순·중복·해석 불가·휴장일 행·누락 → 그 일자 무효(저장 안 함), 투자자 합계 ≠ 0 → 경고만(저장).
- V-262 수집 시간: PAPER run 실행 잠금·하트비트 살아 있음·거래일 15:45(늦은 마감 + 15분) 전·마감 시한 뒤 → 연결 없이
  거부. 허용 API 목록(au10001·au10002·ka10060) 밖은 전송 전 차단, 주문 경로 0.
- 자동 운영 연결(A-100 Q-6 (a)): 15:45 종료 절차(검토·백업·요약) **뒤** 한 단계. 스위치(config/collect.paper.toml) 꺼짐·
  파일 없음이면 아무것도 하지 않는다(기록도 없음). 수집 실패·예외는 경고 알림·기록만 — 자동 운영 종료 코드 불변.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.scenario.test_cp032_probe import FORBIDDEN_NAMES, FORBIDDEN_PACKAGES, repo_root
from tests.scenario.test_t034_investor_probe import ACCOUNT_NO, APP_KEY, SECRET, TOKEN, make_conn
from tests.scenario.test_t025_autopilot import Rig, kill_now, kst as ap_kst
from trading_system.app import investor_collect
from trading_system.app.instance_lock import acquire_instance_lock
from trading_system.app.investor_collect import (
    EVENING_REST_APIS, CollectSettings, collect_investor_flows, evening_collect, evening_from_autopilot,
    evening_refusal, load_collect_settings, window_start)
from trading_system.app.preflight import ORDER_API_IDS
from trading_system.broker.kiwoom.http import HttpReply, PreSendError
from trading_system.domain.clock import KST, FixedClock, to_iso
from trading_system.domain.enums import Mode
from trading_system.domain.investor_flow import INVESTOR_KEYS, UNIT_AMOUNT, InvestorFlow
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.investor_flow import parse_ka10060_rows
from trading_system.storage.repository import Repository

REPO = Path(__file__).resolve().parents[2]
CAL = KrxCalendar.load_dir(REPO / "config" / "calendar")
LAST = date(2026, 10, 8)                                   # 목요일 거래일 (10-05·10-09 휴장)
EVENING = datetime(2026, 10, 8, 16, 0, tzinfo=KST)         # 15:45 종료 절차 뒤
PAGE = 100
CHART = "/api/dostk/chart"


# --- 합성 서버 ----------------------------------------------------------------------------------------------

def trading_days_desc(upto: date, n: int = 400) -> list[date]:
    out: list[date] = []
    d = upto
    while len(out) < n:
        if CAL.is_trading_day(d) or (CAL.is_trading_day(d) is None and d.weekday() < 5):
            out.append(d)
        d -= timedelta(days=1)
    return out


def signed(v: int) -> str:
    return "0" if v == 0 else f"{v:+d}"


def flow_row(d: date, code: str = "005930", delta: int = 0) -> dict:
    """투자자 합(개인+외국인+기관계+기타법인+국가+내외국인) = delta. 기관계 = 세부 합."""
    k = (d.toordinal() + int(code[:3], 16 if not code[:3].isdigit() else 10)) % 1000
    ind, orgn = 5000 + k, 64 + k % 7
    return {"dt": d.strftime("%Y%m%d"), "cur_prc": "+71234", "pred_pre": "+34", "acc_trde_prica": "1105968",
            "ind_invsr": signed(ind), "frgnr_invsr": signed(-(ind + orgn)), "orgn": signed(orgn),
            "fnnc_invt": signed(orgn - 40), "insrnc": "+10", "invtrt": "+10", "etc_fnnc": "+5", "bank": "+5",
            "penfnd_etc": "+5", "samo_fund": "+0", "natn": "0", "etc_corp": "0", "natfor": signed(delta)}


class FlowInner:
    """KiwoomHttp 자리 — 거래일만 행을 준다(최신→과거, 쪽당 100행). mutate(code, rows)로 응답을 바꿀 수 있다."""

    def __init__(self, *, mutate: Callable[[str, list[dict]], list[dict]] | None = None,
                 fail_codes: frozenset[str] = frozenset(), fin: dict[str, dict] | None = None) -> None:
        self.log: list[tuple[str, str, dict]] = []
        self.mutate = mutate
        self.fail_codes = fail_codes
        self.fin = fin or {}
        self.on_call: Callable[[str, dict], None] | None = None

    def post(self, path: str, api_id: str, body: dict, token: str | None,
             cont_yn: str | None = None, next_key: str | None = None) -> HttpReply:
        self.log.append((path, api_id, dict(body)))
        if self.on_call is not None:
            self.on_call(api_id, body)
        if api_id == "au10001":
            return HttpReply(200, {"return_code": 0, "return_msg": "정상", "token": TOKEN, "token_type": "bearer",
                                   "expires_dt": "20261231200000"})
        if api_id == "au10002":
            return HttpReply(200, {"return_code": 0, "return_msg": "정상"})
        assert token == TOKEN
        code = str(body.get("stk_cd"))
        if code in self.fail_codes:
            return HttpReply(200, {"return_code": 5, "return_msg": "기타 업무 오류"})
        if api_id == "ka10001":
            assert path == "/api/dostk/stkinfo"
            return HttpReply(200, {"return_code": 0, "return_msg": "정상", "stk_cd": code, **self.fin.get(code, {})})
        assert api_id == "ka10060" and path == CHART, api_id
        dt = datetime.strptime(body["dt"], "%Y%m%d").date()
        rows = [flow_row(d, code) for d in trading_days_desc(dt)]
        if self.mutate is not None:
            rows = self.mutate(code, rows)
        off = int(next_key or 0)
        more = off + PAGE < len(rows)
        page = {"return_code": 0, "return_msg": "정상", "stk_invsr_orgn_chart": rows[off:off + PAGE]}
        return HttpReply(200, page, "Y" if more else "N", str(off + PAGE) if more else None)

    def apis(self) -> set[str]:
        return {a for _, a, _ in self.log}


class World:
    def __init__(self, tmp_path: Path, *, inner: FlowInner | None = None, when: datetime = EVENING,
                 window_days: int = 5, codes: tuple[str, ...] = ("005930", "000660", "035420"),
                 deadline: dtime = dtime(17, 30)) -> None:
        self.root = repo_root(tmp_path)
        self.base = self.root / "var" / "paper"
        self.base.mkdir(parents=True)
        self.clock = FixedClock(when.astimezone(UTC))
        self.inner = inner or FlowInner()
        self.connects = 0
        self.codes_file = self.root / "list.csv"
        self.codes_file.write_text("code,name,basis\n" + "".join(f"{c},이름,가상\n" for c in codes), encoding="utf-8")
        self.settings = CollectSettings(enabled=True, codes_file=self.codes_file, window_days=window_days,
                                        deadline=deadline, financials=False)
        self.out: list[str] = []
        self.on_sleep: list[Callable[[], None]] = []

    def connect(self, root: Path, environ: Any, clock: Any) -> Any:
        self.connects += 1
        return make_conn(self.inner, self.clock, allow=EVENING_REST_APIS)  # type: ignore[arg-type]

    def sleep(self, seconds: float) -> None:     # settle_heartbeat 대기 — 가상 시계만 진행
        self.clock.advance(timedelta(seconds=seconds))
        for fn in self.on_sleep:
            fn()

    def run(self) -> int:
        return evening_collect(self.root, {"TRADE_MODE": "PAPER"}, self.clock, self.settings, connect=self.connect,
                               log=self.out.append, sleep=self.sleep)

    def repo(self) -> Repository:
        return Repository(self.base / "trading.db", Mode.PAPER)

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        repo = self.repo()
        try:
            return repo.select(sql, params)
        finally:
            repo.close()


# --- V-257 파싱 -----------------------------------------------------------------------------------------------

def test_v257_parse_signed_decimal_values_newest_first() -> None:
    days = trading_days_desc(LAST, 3)
    p = parse_ka10060_rows([flow_row(d) for d in days], "005930", CAL)
    assert [f.day for f in p.flows] == sorted(days) and p.invalid == {} and p.warnings == []
    f = p.flows[-1]
    assert isinstance(f, InvestorFlow) and f.instrument == "005930" and f.unit == UNIT_AMOUNT
    assert tuple(k for k, _ in f.values) == INVESTOR_KEYS
    assert all(isinstance(v, Decimal) for _, v in f.values)
    src = flow_row(LAST)
    assert f.value("frgnr_invsr") == Decimal(src["frgnr_invsr"]) < 0      # 부호 유지 (순매도 = 음수)
    assert f.value("ind_invsr") == Decimal(src["ind_invsr"]) > 0
    assert f.value("acc_trde_prica") is None and f.value("cur_prc") is None   # 투자자 구분만 담는다


def test_v257_missing_field_invalidates_only_that_day() -> None:
    days = trading_days_desc(LAST, 3)
    rows = [flow_row(d) for d in days]
    del rows[1]["orgn"]
    p = parse_ka10060_rows(rows, "005930", CAL)
    assert [f.day for f in p.flows] == [days[2], days[0]]
    assert "필드 누락" in p.invalid[days[1].strftime("%Y%m%d")]


def test_v257_collect_concatenates_continuation_pages(tmp_path: Path) -> None:
    w = World(tmp_path, window_days=120, codes=("005930",))
    assert w.run() == 0, w.out
    calls = [b for _, a, b in w.inner.log if a == "ka10060"]
    assert len(calls) == 2 and calls[0]["dt"] == "20261008"                 # 100행 쪽 + 이어 받은 쪽
    assert calls[0] == {"dt": "20261008", "stk_cd": "005930", "amt_qty_tp": "1", "trde_tp": "0", "unit_tp": "1000"}
    days = w.rows("SELECT DISTINCT trade_date FROM investor_flow WHERE instrument='005930' ORDER BY trade_date")
    want = sorted(trading_days_desc(LAST, 120))
    assert [r["trade_date"] for r in days] == [d.isoformat() for d in want]
    n = w.rows("SELECT COUNT(*) AS n FROM investor_flow")[0]["n"]
    assert n == 120 * len(INVESTOR_KEYS)


# --- V-261 품질 -----------------------------------------------------------------------------------------------

def _reversed(rows: list[dict]) -> list[dict]:
    rows = list(rows)
    rows[1], rows[2] = rows[2], rows[1]
    return rows


def _duplicate(rows: list[dict]) -> list[dict]:
    return [rows[0], rows[1], dict(rows[1]), *rows[2:]]


def _unparsable(rows: list[dict]) -> list[dict]:
    rows = [dict(r) for r in rows]
    rows[1]["natn"] = "1,234"
    return rows


def _holiday(rows: list[dict]) -> list[dict]:   # 10-05(개천절 대체) 행이 끼어 있음
    i = next(k for k, r in enumerate(rows) if r["dt"] < "20261005")
    return [*rows[:i], flow_row(date(2026, 10, 5)), *rows[i:]]


def _missing(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["dt"] != "20261006"]


@pytest.mark.parametrize("mutate, bad_day, word", [
    (_reversed, None, "역순"), (_duplicate, None, "중복"), (_unparsable, None, "해석 불가"),
    (_holiday, "20261005", "휴장일"), (_missing, "20261006", "누락"),
])
def test_v261_quality_invalid_day_not_stored(mutate: Callable[[list[dict]], list[dict]], bad_day: str | None,
                                             word: str) -> None:
    rows = mutate([flow_row(d) for d in trading_days_desc(LAST, 5)])
    p = parse_ka10060_rows(rows, "005930", CAL, first=trading_days_desc(LAST, 5)[-1], last=LAST)
    assert p.invalid, p
    if bad_day is not None:
        assert word in p.invalid[bad_day], p.invalid
    else:
        assert any(word in why for why in p.invalid.values()), p.invalid
    stored = {f.day.strftime("%Y%m%d") for f in p.flows}
    assert not stored & set(p.invalid)                                        # 무효 일자는 공급하지 않음
    assert stored and len(stored) + len(p.invalid) >= 5


def test_v261_sum_not_zero_warns_only_value_kept() -> None:
    days = trading_days_desc(LAST, 3)
    rows = [flow_row(d, delta=(7 if d == days[0] else 0)) for d in days]
    p = parse_ka10060_rows(rows, "005930", CAL)
    assert p.invalid == {} and len(p.flows) == 3
    assert p.warnings == [days[0].strftime("%Y%m%d")]


def test_v261_collect_records_quality_audit_and_skips_invalid(tmp_path: Path) -> None:
    def bad(code: str, rows: list[dict]) -> list[dict]:
        if code != "000660":
            return rows
        rows = [dict(r) for r in rows]
        rows[0]["orgn"] = "abc"
        return rows

    w = World(tmp_path, inner=FlowInner(mutate=bad))
    assert w.run() == 0, w.out                         # 품질 무효는 수집 실패가 아니다(그 일자만 무효)
    stored = {r["trade_date"] for r in w.rows("SELECT trade_date FROM investor_flow WHERE instrument='000660'")}
    assert "2026-10-08" not in stored and "2026-10-07" in stored
    [ev] = [e for e in w.rows("SELECT * FROM audit_event WHERE kind='INVESTOR_FLOW_QUALITY'")]
    assert ev["ref"] == "000660" and "20261008" in ev["summary"] and "해석 불가" in ev["summary"]


# --- V-260 정정 감지·추가 전용 --------------------------------------------------------------------------------

def _flow(day: date, frgn: str = "-100") -> InvestorFlow:
    vals = {k: Decimal("0") for k in INVESTOR_KEYS}
    vals["frgnr_invsr"], vals["ind_invsr"] = Decimal(frgn), -Decimal(frgn)
    return InvestorFlow("005930", day, UNIT_AMOUNT, tuple((k, vals[k]) for k in INVESTOR_KEYS))


def test_v260_same_value_adds_observation_different_value_new_revision(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    d = date(2026, 10, 7)
    t1, t2, t3 = (datetime(2026, 10, 7, 7 + i, 0, tzinfo=UTC) for i in range(3))
    with repo.tx():
        r1 = repo.record_investor_flows([_flow(d)], "ka10060", t1, "run-1")
    with repo.tx():
        r2 = repo.record_investor_flows([_flow(d)], "ka10060", t2, "run-2")
    with repo.tx():
        r3 = repo.record_investor_flows([_flow(d, "-250")], "ka10060", t3, "run-3")
    assert (r1.new, r1.same, r1.corrected) == (1, 0, 0)
    assert (r2.new, r2.same, r2.corrected) == (0, 1, 0)
    assert (r3.new, r3.same, r3.corrected) == (0, 0, 1) and r3.corrections == (("005930", d),)
    revs = repo.investor_flow_revisions("005930")
    assert [(x.revision, x.flow.value("frgnr_invsr")) for x in revs] == [(1, Decimal("-100")), (2, Decimal("-250"))]
    assert revs[0].collected_at == t1 and revs[0].seen_at == (t1, t2)            # 같은 값 재수집 = 관측만 추가
    assert revs[1].collected_at == t3 and revs[1].seen_at == (t3,)
    rows = repo.select("SELECT revision, value, collected_at FROM investor_flow WHERE investor='frgnr_invsr' "
                       "ORDER BY revision")
    assert [(r["revision"], r["value"]) for r in rows] == [(1, "-100"), (2, "-250")]   # 이전 행 불변
    [ev] = [e for e in repo.audit_events() if e["kind"] == "INVESTOR_FLOW_CORRECTED"]
    s = json.loads(ev["summary"])
    assert ev["ref"] == "005930" and s["day"] == "2026-10-07" and s["previous_revision"] == 1 and s["revision"] == 2
    assert s["changed"] == ["frgnr_invsr", "ind_invsr"]
    for sql in ("UPDATE investor_flow SET value='1'", "DELETE FROM investor_flow",
                "UPDATE investor_flow_seen SET seen_at='x'", "DELETE FROM investor_flow_seen"):
        with pytest.raises(sqlite3.Error):
            repo.connection.execute(sql)
    repo.close()


def test_v260_record_requires_transaction(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    with pytest.raises(ValueError):
        repo.record_investor_flows([_flow(date(2026, 10, 7))], "ka10060", datetime(2026, 10, 7, tzinfo=UTC), "r")
    repo.close()


def test_v260_tables_created_on_open_without_schema_bump(tmp_path: Path) -> None:
    from trading_system.storage.repository import SCHEMA_VERSION
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    names = {r["name"] for r in repo.select("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"investor_flow", "investor_flow_seen"} <= names and SCHEMA_VERSION == "2"
    repo.close()


def test_v260_evening_recollection_marks_seen_and_detects_correction(tmp_path: Path) -> None:
    w = World(tmp_path, codes=("005930",))
    assert w.run() == 0
    w.clock._now = datetime(2026, 10, 12, 16, 0, tzinfo=KST).astimezone(UTC)   # noqa: SLF001 — 다음 거래일 저녁

    def fix(code: str, rows: list[dict]) -> list[dict]:
        rows = [dict(r) for r in rows]
        for r in rows:
            if r["dt"] == "20261007":
                r["ind_invsr"], r["frgnr_invsr"] = "+1", "-1"
        return rows

    w.inner.mutate = fix
    assert w.run() == 0, w.out
    seen =w.rows("SELECT trade_date, revision, seen_at FROM investor_flow_seen WHERE trade_date='2026-10-08'")
    assert len(seen) == 2 and {r["revision"] for r in seen} == {1}
    revs = w.rows("SELECT DISTINCT revision FROM investor_flow WHERE trade_date='2026-10-07'")
    assert {r["revision"] for r in revs} == {1, 2}
    assert [e for e in w.rows("SELECT ref FROM audit_event WHERE kind='INVESTOR_FLOW_CORRECTED'")]


# --- V-262 수집 시간·허용 목록·주문 경로 ---------------------------------------------------------------------

@pytest.mark.parametrize("when, word", [
    (datetime(2026, 10, 8, 15, 30, tzinfo=KST), "15:45"),     # 종료 절차 전
    (datetime(2026, 10, 8, 10, 0, tzinfo=KST), "15:45"),      # 장중
    (datetime(2026, 11, 19, 16, 40, tzinfo=KST), "16:45"),    # 수능일 마감 16:30 → 16:45 전 거부
    (datetime(2026, 10, 8, 17, 31, tzinfo=KST), "시한"),      # 마감 시한(17:30) 뒤
])
def test_v262_time_refusal_without_connecting(tmp_path: Path, when: datetime, word: str) -> None:
    w = World(tmp_path, when=when)
    assert w.run() == 2
    assert w.connects == 0 and w.inner.log == []
    assert any(word in line for line in w.out), w.out


def test_v262_refuses_while_run_lock_or_heartbeat(tmp_path: Path) -> None:
    w = World(tmp_path)
    lock = acquire_instance_lock(w.base)
    try:
        assert w.run() == 2
    finally:
        lock.release()
    assert w.connects == 0 and any("run.lock" in line for line in w.out)
    # 하트비트가 계속 새로 쓰이면(잠금 없이 살아 있는 엔진 — 비정상) 최대 대기 뒤에도 거부
    w2 = World(tmp_path / "b")
    hb = w2.base / "heartbeat"

    def beat() -> None:
        hb.write_text(w2.clock.now().isoformat(), encoding="utf-8")

    beat()
    w2.on_sleep.append(beat)
    assert w2.run() == 2 and w2.connects == 0


def test_heartbeat_left_by_just_finished_run_is_waited_out(tmp_path: Path) -> None:
    """재검토 2026-10-15 #1: 자동 운영은 run 종료 직후(하트비트 0~10초 전) 이 단계를 부른다. run.lock이 풀렸으면 하트비트가
    오래될 때까지 기다린 뒤 수집한다 — 기다리지 않으면 매일 거부된다."""
    w = World(tmp_path)
    (w.base / "heartbeat").write_text((EVENING - timedelta(seconds=6)).astimezone(UTC).isoformat(), encoding="utf-8")
    assert w.run() == 0 and w.connects == 1
    assert w.clock.now() - EVENING.astimezone(UTC) <= timedelta(seconds=investor_collect.HEARTBEAT_SETTLE_MAX)


def test_v262_refusal_function_ignores_autopilot_lock_but_not_run_lock(tmp_path: Path) -> None:
    from trading_system.app.instance_lock import AUTOPILOT_LOCK
    base = tmp_path / "var" / "paper"
    base.mkdir(parents=True)
    ap = acquire_instance_lock(base, AUTOPILOT_LOCK)     # 호출자(자동 운영) 자신의 잠금
    try:
        assert evening_refusal(base, EVENING.astimezone(UTC), CAL, dtime(17, 30)) is None
        run = acquire_instance_lock(base)
        try:
            assert "run.lock" in str(evening_refusal(base, EVENING.astimezone(UTC), CAL, dtime(17, 30)))
        finally:
            run.release()
    finally:
        ap.release()


def test_v262_uses_allowlist_and_blocks_others_before_send(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}
    monkeypatch.setattr(investor_collect, "connect_paper", lambda root, env, clock, **kw: seen.update(kw) or "거부")
    w = World(tmp_path)
    assert evening_collect(w.root, {"TRADE_MODE": "PAPER"}, w.clock, w.settings, log=w.out.append) == 2
    assert seen["allowed_apis"] == EVENING_REST_APIS and seen["per_tr_interval"] >= 1.2
    assert EVENING_REST_APIS == frozenset({"au10001", "au10002", "ka10060"}) and not EVENING_REST_APIS & ORDER_API_IDS
    conn = make_conn(FlowInner(), w.clock, allow=EVENING_REST_APIS)  # type: ignore[arg-type]
    with pytest.raises(PreSendError):
        conn.guard.post("/api/dostk/ordr", "kt10000", {}, TOKEN)
    assert conn.guard.blocked == ["kt10000"]


def test_v262_collector_source_has_no_order_path() -> None:
    for path in (REPO / "src" / "trading_system" / "app" / "investor_collect.py",
                 REPO / "src" / "trading_system" / "marketdata" / "investor_flow.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                full = ("." * node.level) + (node.module or "")
                if full.startswith(FORBIDDEN_PACKAGES) or "execution" in full or "gate" in full:
                    bad.append(f"import {full}")
                bad += [a.name for a in node.names if a.name in FORBIDDEN_NAMES]
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                bad.append(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                bad.append(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and \
                    ("kt1000" in node.value or "/ordr" in node.value):
                bad.append(node.value)
        assert bad == [], (path, bad)


# --- 수집 전체 흐름 -----------------------------------------------------------------------------------------

def test_evening_collect_stores_window_and_writes_meta(tmp_path: Path) -> None:
    w = World(tmp_path)
    assert w.run() == 0, w.out
    assert w.inner.apis() == EVENING_REST_APIS and w.inner.log[-1][1] == "au10002"
    want = sorted(trading_days_desc(LAST, 5))
    assert window_start(LAST, 5, CAL) == want[0] == date(2026, 10, 1)
    for code in ("005930", "000660", "035420"):
        days = w.rows("SELECT DISTINCT trade_date FROM investor_flow WHERE instrument=? ORDER BY trade_date", (code,))
        assert [r["trade_date"] for r in days] == [d.isoformat() for d in want]
    r = w.rows("SELECT * FROM investor_flow WHERE instrument='005930' AND trade_date='2026-10-08' "
               "AND investor='frgnr_invsr'")[0]
    assert r["unit"] == UNIT_AMOUNT and r["source"].startswith("ka10060") and r["revision"] == 1
    assert r["collected_at"] == to_iso(EVENING)
    metas = sorted((w.base / "collect").glob("evening-*.json"))
    assert len(metas) == 1
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["investor"]["ok"] == 3 and meta["investor"]["failed"] == [] and meta["investor"]["new"] == 15
    assert meta["window"] == {"first": "2026-10-01", "last": "2026-10-08", "trading_days": 5}
    text = "\n".join(w.out) + json.dumps(meta, ensure_ascii=False)
    for secret in (TOKEN, APP_KEY, SECRET, ACCOUNT_NO, "71234", "1105968"):
        assert secret not in text
    assert any(line.startswith("저녁 수집") and "성공 3" in line for line in w.out), w.out


def test_evening_collect_failed_code_others_stored(tmp_path: Path) -> None:
    w = World(tmp_path, inner=FlowInner(fail_codes=frozenset({"000660"})))
    assert w.run() == 1
    assert not w.rows("SELECT 1 FROM investor_flow WHERE instrument='000660'")
    assert w.rows("SELECT 1 FROM investor_flow WHERE instrument='035420'")
    meta = json.loads(sorted((w.base / "collect").glob("evening-*.json"))[0].read_text(encoding="utf-8"))
    assert [f["code"] for f in meta["investor"]["failed"]] == ["000660"]


def test_evening_collect_stops_when_run_starts_midway(tmp_path: Path) -> None:
    w = World(tmp_path)
    held: list[Any] = []

    def on_call(api_id: str, body: dict) -> None:
        if api_id == "ka10060" and body.get("stk_cd") == "005930" and not held:
            held.append(acquire_instance_lock(w.base))     # 수동 run이 켜짐 → 다음 종목부터 조회 안 함

    w.inner.on_call = on_call
    try:
        assert w.run() == 1
    finally:
        held[0].release()
    codes = [b["stk_cd"] for _, a, b in w.inner.log if a == "ka10060"]
    # 재검토 2026-10-15 #4: 수집 중 run이 시작되면 토큰을 폐기하지 않는다(엔진이 같은 토큰을 받았을 수 있음 — preflight M-2)
    assert codes == ["005930"] and not any(a == "au10002" for _, a, _ in w.inner.log)
    meta = json.loads(sorted((w.base / "collect").glob("evening-*.json"))[0].read_text(encoding="utf-8"))
    assert meta["investor"]["not_queried"] == ["000660", "035420"] and "run.lock" in meta["stopped"]
    assert meta["token"]["revoked"] is None and any("토큰 폐기 생략" in line for line in w.out)


def test_collect_function_requires_known_window(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    conn = make_conn(FlowInner(), FixedClock(EVENING.astimezone(UTC)), allow=EVENING_REST_APIS)  # type: ignore[arg-type]
    conn.adapter.tokens.token()
    out = collect_investor_flows(conn.adapter, conn.guard, repo, ["005930"], LAST, CAL,
                                 FixedClock(EVENING.astimezone(UTC)), window_days=2, should_stop=lambda: None,
                                 log=lambda m: None, run_id="r1")
    assert out["ok"] == 1 and out["new"] == 2
    repo.close()


# --- 설정 (스위치 기본 꺼짐) -----------------------------------------------------------------------------------

def test_settings_missing_file_means_off(tmp_path: Path) -> None:
    s = load_collect_settings(tmp_path / "none.toml", tmp_path)
    assert s.enabled is False


def test_repo_config_switch_is_off_by_default() -> None:
    s = load_collect_settings(REPO / "config" / "collect.paper.toml", REPO)
    assert s.enabled is False and s.financials is False
    assert s.codes_file is not None and s.codes_file.name == "kospi200-constituents.csv"   # A-100 Q-8 (b)
    assert s.window_days == 5 and s.deadline == dtime(17, 30)


@pytest.mark.parametrize("text, word", [
    ("enabled = true\nwindow_days = 5\ndeadline = \"17:30\"\nfinancials = false\n", "codes_file"),
    ("enabled = \"yes\"\n", "enabled"),
    ("enabled = false\nbogus = 1\n", "알 수 없는 키"),
    ("enabled = true\ncodes_file = \"x.csv\"\nwindow_days = 0\ndeadline = \"17:30\"\nfinancials = false\n", "window_days"),
    ("enabled = true\ncodes_file = \"x.csv\"\nwindow_days = 5\ndeadline = \"18:30\"\nfinancials = false\n", "deadline"),
    ("enabled = true\ncodes_file = \"x.csv\"\nwindow_days = 5\ndeadline = \"17:30\"\n", "financials"),
])
def test_settings_schema_rejects(tmp_path: Path, text: str, word: str) -> None:
    from trading_system.config.errors import ConfigError
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=word):
        load_collect_settings(p, tmp_path)


def test_autopilot_entry_switch_off_ignores_other_keys(tmp_path: Path) -> None:
    """재검토 2026-10-15 #3: 자동 운영 진입점은 꺼진 스위치면 다른 키가 틀려도 아무것도 안 함 — 매일 경고를 만들지 않는다.
    (설정 읽기 자체는 엄격 — test_settings_schema_rejects. 스위치를 읽을 수 없는 형식 오류는 경고 유지.)"""
    root = repo_root(tmp_path)
    clock = FixedClock(EVENING.astimezone(UTC))
    cfg = root / "config" / "collect.paper.toml"
    cfg.write_text('enabled = false\nbogus = 1\ndeadline = "99:99"\n', encoding="utf-8")
    assert evening_from_autopilot(root, {"TRADE_MODE": "PAPER"}, clock) is None
    cfg.write_text("enabled = false\n[[[", encoding="utf-8")
    result = evening_from_autopilot(root, {"TRADE_MODE": "PAPER"}, clock)
    assert result is not None and result[0] == 2


# --- 자동 운영 연결 (A-100 Q-6 (a)) ----------------------------------------------------------------------------

def test_autopilot_evening_off_by_default_changes_nothing(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    assert r.ap.d.evening is None                    # 기본값: 단계 없음 (기존 시험 그대로)
    assert r.ap.run() == 0
    assert not any("저녁 수집" in t for _, t in r.notes) and not any("저녁 수집" in x for x in r.logs)


def test_autopilot_evening_runs_after_summary_without_changing_exit(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    calls: list[str] = []

    def evening() -> tuple[int, str]:
        calls.append(f"evening run_alive={r.runs[-1].alive()} t={r.clock.now() >= ap_kst(15, 45)}")
        return 0, "저녁 수집(05a): 종목 3 성공 3 실패 0 미조회 0\n세부"

    r.ap.d.evening = evening
    assert r.ap.run() == 0
    assert calls == ["evening run_alive=False t=True"]
    i_summary = next(i for i, (_, t) in enumerate(r.notes) if "일일 요약" in t)
    i_even = next(i for i, (_, t) in enumerate(r.notes) if "저녁 수집" in t)
    assert i_summary < i_even and r.notes[i_even] == ("INFO", "저녁 수집(05a): 종목 3 성공 3 실패 0 미조회 0")
    assert "저녁 수집" not in r.notes[i_summary][1]                 # 요약 내용 불변
    assert r.events == ["preflight", "launch", "review alive=False", "backup"]


@pytest.mark.parametrize("result", [(1, "저녁 수집(05a): 종목 3 성공 2 실패 1"), (2, "저녁 수집 거부: 가상"), "raise"])
def test_autopilot_evening_failure_only_warns(tmp_path: Path, result: Any) -> None:
    r = Rig(tmp_path)

    def evening() -> tuple[int, str]:
        if result == "raise":
            raise RuntimeError("가상 결함")
        return result

    r.ap.d.evening = evening
    assert r.ap.run() == 0                                        # 종료 코드·요약 불변
    warn = [t for lv, t in r.notes if lv == "WARNING" and "저녁 수집" in t]
    assert len(warn) == 1, r.notes
    assert "확인 필요" not in next(t for _, t in r.notes if "일일 요약" in t)


def test_autopilot_evening_none_result_is_silent(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.ap.d.evening = lambda: None                                 # 스위치 꺼짐
    assert r.ap.run() == 0
    assert not any("저녁 수집" in t for _, t in r.notes) and not any("저녁 수집" in x for x in r.logs)


def test_autopilot_evening_skipped_when_run_died(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    calls: list[int] = []
    r.ap.d.evening = lambda: calls.append(1) or (0, "x")  # type: ignore[func-returns-value]
    r.hooks.append((ap_kst(11, 0), lambda: kill_now(r)))
    assert r.ap.run() == 3
    assert calls == []


def test_autopilot_evening_silent_when_run_does_not_stop(tmp_path: Path) -> None:
    """재검토 2026-10-15 #2: run이 정지 요청을 따르지 않은 날(긴급 알림은 기존대로) 저녁 단계는 스위치를 읽지 않고 조용히
    건너뛴다 — 꺼진 스위치로 새 알림·기록이 생기지 않게."""
    r = Rig(tmp_path)
    calls: list[int] = []
    r.ap.d.evening = lambda: calls.append(1) or (0, "x")  # type: ignore[func-returns-value]
    r.hooks.append((ap_kst(15, 40), lambda: setattr(r.runs[-1], "frozen", True)))
    r.ap.run()
    assert calls == []
    assert not any("저녁 수집" in t for _, t in r.notes) and not any("저녁 수집" in x for x in r.logs)


def test_evening_from_autopilot_respects_switch(tmp_path: Path) -> None:
    root = repo_root(tmp_path)
    (root / "var" / "paper").mkdir(parents=True)
    connects: list[int] = []
    conn = lambda *a: connects.append(1) or "x"  # noqa: E731
    clock = FixedClock(EVENING.astimezone(UTC))
    assert evening_from_autopilot(root, {"TRADE_MODE": "PAPER"}, clock, connect=conn) is None   # 파일 없음
    (root / "config" / "collect.paper.toml").write_text("enabled = false\n", encoding="utf-8")
    assert evening_from_autopilot(root, {"TRADE_MODE": "PAPER"}, clock, connect=conn) is None
    (root / "config" / "collect.paper.toml").write_text("enabled = 1\n", encoding="utf-8")
    rc, text = evening_from_autopilot(root, {"TRADE_MODE": "PAPER"}, clock, connect=conn)  # type: ignore[misc]
    assert rc == 2 and "설정" in text
    (root / "config" / "collect.paper.toml").write_text(
        "enabled = true\ncodes_file = \"missing.csv\"\nwindow_days = 5\ndeadline = \"17:30\"\nfinancials = false\n",
        encoding="utf-8")
    rc, text = evening_from_autopilot(root, {"TRADE_MODE": "PAPER"}, clock, connect=conn)  # type: ignore[misc]
    assert rc == 2 and "missing.csv" in text
    assert connects == []


def test_evening_constants_match_autopilot_schedule() -> None:
    from trading_system.app.autopilot import Schedule
    assert investor_collect.EVENING_EARLIEST == Schedule().stop_at
    assert investor_collect.AFTER_CLOSE == Schedule().stop_after_close
