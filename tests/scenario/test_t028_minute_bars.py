"""T-028 분봉 수집·저장·품질 (CP-032 D-177, V-241~V-243, 승인 A-095) — 합성 응답만, 네트워크 없음.

- V-241 분봉 파싱: ka10080 목록 `stk_min_pole_chart_qry` → 부호 제거·Decimal·시각 오름차순·연속 조회 쪽 이어 붙이기.
  필드 누락·형식 오류·빈 목록 → 조회 실패(누락 표시).
- 정규장 봉만(A-095, K-U43): 봉 [cntr_tm, cntr_tm + 길이)가 KRX 달력의 정규장 [개장, 마감) 안에 완전히 들어가는 봉만.
  마감 동시호가·장 마감 뒤 봉(15:30·15:35·16:00~19:59)·휴장일·달력 없는 날 봉은 제외하고 건수를 센다.
- V-242 봉 확정: 판정 시각 T에는 봉 끝(= cntr_tm + 길이) ≤ T − 확정 여유(기본 5초)인 봉만. 최신 봉 교란 시 결과 불변.
- V-243 품질: 중복 시각·역순·음수·0 이하 가격·고저 모순 → 그 종목 그 날 무효(K10 경로), 거래 없는 분 = 거래 없음(유효),
  연속 조회 쪽 실패 = 누락(무효).
- 저장: 기존 DB에 추가 전용 표 minute_bar(스키마 번호 유지, DB 열 때 생성 — A-086 ③ 방식), 키 (종목, 봉 길이, 봉 시작,
  수정주가 구분), 고치기·지우기 금지. CSV 내보내기 `<code>-<len>m.csv` (datetime,open,high,low,close,volume).
- 축적 도구 scripts/fetch-minute.py(A-095 ②): PAPER·모의 도메인만, 장중·run·autopilot 실행 중 거부, 허용 API
  au10001·au10002·ka10080, 같은 쪽 3회 실패 = 종목 실패, 3종목 연속 실패 = 나머지 미조회, 기존 CSV 이어 쓰기.
"""

from __future__ import annotations

import ast
import csv
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.scenario.test_cp032_probe import FORBIDDEN_NAMES, FORBIDDEN_PACKAGES, kst, repo_root
from tests.scenario.test_t034_investor_probe import make_conn
from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET
from trading_system.app import minute_fetch
from trading_system.app.instance_lock import AUTOPILOT_LOCK, acquire_instance_lock
from trading_system.app.minute_fetch import MINUTE_REST_APIS, export_minute_csv, minute_main
from trading_system.broker.kiwoom.adapter import KiwoomAdapter
from trading_system.broker.kiwoom.http import HttpReply
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import HaltType, Mode
from trading_system.domain.minute_bar import MinuteBar
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.minute import (
    CONFIRM_MARGIN, MIN_LIST_KEY, MINUTE_CSV_HEADER, MinuteDataMissing, confirmed_bars, merge_minute_csv,
    minute_csv_name, missing_supply, parse_minute_pages, prepare_minutes, read_minute_csv, report_k10, split_regular)
from trading_system.storage.repository import SCHEMA_VERSION, Repository

REPO = Path(__file__).resolve().parents[2]
CAL = KrxCalendar.load_dir(REPO / "config" / "calendar")
TOKEN = "virtual-token-investor-0001"     # test_t034_investor_probe.FakeInner와 같은 토큰 값 (make_conn이 쓰는 어댑터)
PRICE = 71234
D1 = date(2026, 10, 8)                     # 목, 거래일
D0 = date(2026, 10, 7)                     # 수, 거래일


# --- 합성 행 ----------------------------------------------------------------------------------------------

def row(at: datetime, *, o: int = PRICE, h: int | None = None, lo: int | None = None, c: int | None = None,
        v: str = "4321", sign: str = "+", **override: Any) -> dict:
    hi = o + 50 if h is None else h
    low = o - 50 if lo is None else lo
    close = o + 10 if c is None else c
    r = {"acc_trde_qty": "99999", "cntr_tm": at.strftime("%Y%m%d%H%M%S"), "cur_prc": f"{sign}{close}",
         "high_pric": f"{sign}{hi}", "low_pric": f"{sign}{low}", "open_pric": f"{sign}{o}", "pred_pre": "+34",
         "pred_pre_sig": "2", "trde_qty": v, "upd_rt": "+0.00", "upd_stkpc_tp": "1"}
    r.update(override)
    return {k: val for k, val in r.items() if val is not None}


def day_rows(day: date, *, minutes: int = 1, start: tuple[int, int] = (9, 0), regular_end: tuple[int, int] = (15, 20),
             extra: tuple[tuple[int, int], ...] = ((15, 30), (15, 35)), after: tuple[int, int] | None = (16, 0),
             after_end: tuple[int, int] = (20, 0)) -> list[dict]:
    """하루치 최신→과거. 기본 1분: 09:00~15:19(380) + 15:30·15:35 + 16:00~19:59(240) = 622봉 (K-U43 실측 형태)."""
    times: list[datetime] = []
    t = datetime(day.year, day.month, day.day, *start, tzinfo=KST)
    end = datetime(day.year, day.month, day.day, *regular_end, tzinfo=KST)
    while t < end:
        times.append(t)
        t += timedelta(minutes=minutes)
    times += [datetime(day.year, day.month, day.day, hh, mm, tzinfo=KST) for hh, mm in extra]
    if after is not None:
        t = datetime(day.year, day.month, day.day, *after, tzinfo=KST)
        stop = datetime(day.year, day.month, day.day, *after_end, tzinfo=KST)
        while t < stop:
            times.append(t)
            t += timedelta(minutes=minutes)
    return [row(x, o=PRICE + i % 7) for i, x in enumerate(sorted(times, reverse=True))]


def bars_at(*hm: tuple[int, int], day: date = D1, minutes: int = 1) -> list[MinuteBar]:
    return [MinuteBar("005930", minutes, datetime(day.year, day.month, day.day, h, m, tzinfo=KST), Decimal(PRICE),
                      Decimal(PRICE + 50), Decimal(PRICE - 50), Decimal(PRICE + 10), Decimal(1)) for h, m in hm]


# --- V-241 분봉 파싱 --------------------------------------------------------------------------------------

def test_v241_parse_strips_sign_decimal_ascending_and_joins_pages() -> None:
    rows = day_rows(D1, regular_end=(9, 4), extra=(), after=None)          # 09:00~09:03, 최신→과거
    rows[0]["low_pric"] = f"-{PRICE - 50}"                                # 부호는 등락 표시 → 뗀다
    page1, page2 = rows[:2], rows[2:]                                     # 연속 조회 2쪽
    bars = parse_minute_pages([page1, page2], "005930", 1)
    assert [b.start for b in bars] == [kst(2026, 10, 8, 9, m) for m in range(4)]   # 오름차순 정렬
    assert all(isinstance(x, Decimal) for b in bars for x in (b.open, b.high, b.low, b.close, b.volume))
    last = bars[-1]
    assert last.low == Decimal(PRICE - 50) and last.close == Decimal(PRICE + 10) and last.volume == Decimal(4321)
    assert bars[0].open == Decimal(PRICE + 3)                                # 09:00 = 받은 순서 마지막 행
    assert last.end == kst(2026, 10, 8, 9, 4) and last.adjusted == "1" and last.minutes == 1
    assert last.start.tzinfo is not None


@pytest.mark.parametrize("field", ["cntr_tm", "open_pric", "high_pric", "low_pric", "cur_prc", "trde_qty"])
def test_v241_missing_field_is_query_failure(field: str) -> None:
    rows = day_rows(D1, regular_end=(9, 3), extra=(), after=None)
    del rows[1][field]
    with pytest.raises(MinuteDataMissing, match=field):
        parse_minute_pages([rows], "005930", 1)
    sup = prepare_minutes(rows, "005930", 1, CAL, kst(2026, 10, 8, 20, 0))
    assert sup.missing and not sup.data_ok and sup.bars == () and field in sup.reason


@pytest.mark.parametrize("bad", [{"cntr_tm": "2026100809000"}, {"cntr_tm": "20261008090000x"},
                                 {"cur_prc": ""}, {"trde_qty": "1.5"}, {"high_pric": "+7１"}])
def test_v241_bad_format_is_query_failure(bad: dict) -> None:
    rows = [row(kst(2026, 10, 8, 9, 0), **bad)]
    with pytest.raises(MinuteDataMissing):
        parse_minute_pages([rows], "005930", 1)


def test_v241_empty_list_is_missing_not_no_trade() -> None:
    sup = prepare_minutes([], "005930", 1, CAL, kst(2026, 10, 8, 20, 0))
    assert sup.missing and not sup.data_ok and "빈" in sup.reason


# --- 정규장 봉만 (A-095, K-U43) -----------------------------------------------------------------------------

def test_regular_session_only_excludes_closing_auction_and_after_market() -> None:
    bars = bars_at((8, 59), (9, 0), (15, 19), (15, 29), (15, 30), (15, 35), (16, 0), (19, 59))
    kept, excluded = split_regular(bars, CAL)
    assert [b.start.strftime("%H%M") for b in kept] == ["0900", "1519", "1529"]
    assert excluded == {"정규장 밖": 5}


def test_regular_session_uses_calendar_special_session_day() -> None:
    """수능일 2025-11-13 10:00~16:30 (krx-2025.toml) — 개장 시간 변경도 달력대로."""
    bars = bars_at((9, 30), (9, 59), (10, 0), (15, 30), (16, 29), (16, 30), day=date(2025, 11, 13))
    kept, excluded = split_regular(bars, CAL)
    assert [b.start.strftime("%H%M") for b in kept] == ["1000", "1530", "1629"]
    assert excluded == {"정규장 밖": 3}


def test_regular_session_bar_must_fit_completely() -> None:
    """60분봉 15:00 [15:00, 16:00)은 마감 15:30을 넘으므로 제외. 5분봉 15:25 [15:25, 15:30)은 포함."""
    kept, excluded = split_regular(bars_at((14, 0), (15, 0), minutes=60), CAL)
    assert [b.start.strftime("%H%M") for b in kept] == ["1400"] and excluded == {"정규장 밖": 1}
    kept, _ = split_regular(bars_at((15, 25), (15, 30), minutes=5), CAL)
    assert [b.start.strftime("%H%M") for b in kept] == ["1525"]


def test_regular_session_holiday_and_unknown_year_bars_excluded() -> None:
    holiday = bars_at((10, 0), day=date(2026, 10, 9))        # 한글날 휴장
    unknown = bars_at((10, 0), day=date(2030, 3, 4))         # 달력 파일 없는 연도 = 모름
    kept, excluded = split_regular(holiday + unknown, CAL)
    assert kept == [] and excluded == {"휴장일": 1, "달력 없음": 1}


# --- V-242 봉 확정 ----------------------------------------------------------------------------------------

def test_v242_default_margin_is_five_seconds() -> None:
    assert CONFIRM_MARGIN == timedelta(seconds=5)


def test_v242_only_bars_ending_before_t_minus_margin() -> None:
    bars = bars_at(*((9, m) for m in range(5)))                          # 09:00~09:04, 끝 09:01~09:05
    t = kst(2026, 10, 8, 9, 5) + timedelta(seconds=4)
    assert [b.start.minute for b in confirmed_bars(bars, t)] == [0, 1, 2, 3]   # 09:04 봉 끝 09:05 > T − 5초
    t = kst(2026, 10, 8, 9, 5) + timedelta(seconds=5)
    assert [b.start.minute for b in confirmed_bars(bars, t)] == [0, 1, 2, 3, 4]
    assert [b.start.minute for b in confirmed_bars(bars, t, timedelta(seconds=30))] == [0, 1, 2, 3]


def test_v242_latest_unconfirmed_bar_perturbation_does_not_change_supply() -> None:
    """응답의 가장 최신 봉(진행 중)이 무엇이든 — 값이 튀거나 고저 모순이어도 — 공급 결과는 같다."""
    t = kst(2026, 10, 8, 9, 5, ) + timedelta(seconds=2)
    base = day_rows(D1, regular_end=(9, 5), extra=(), after=None)        # 09:00~09:04, 09:04는 미확정
    wild = [dict(r) for r in base]
    wild[0].update(high_pric="+1", low_pric="+999999", cur_prc="+500000", trde_qty="-7")
    a = prepare_minutes(base, "005930", 1, CAL, t)
    b = prepare_minutes(wild, "005930", 1, CAL, t)
    assert a.data_ok and b.data_ok
    assert a.bars == b.bars and [x.start.minute for x in a.bars] == [0, 1, 2, 3]
    assert a.excluded == b.excluded == {"미확정": 1}


# --- V-243 품질 -------------------------------------------------------------------------------------------

def two_days(mutate_d1: Any = None) -> list[dict]:
    d1 = day_rows(D1, regular_end=(9, 10), extra=(), after=None)
    d0 = day_rows(D0, regular_end=(9, 10), extra=(), after=None)
    if mutate_d1 is not None:
        d1 = mutate_d1(d1)
    return d1 + d0                                                          # 최신→과거


@pytest.mark.parametrize("mutate, word", [
    (lambda rs: rs[:3] + [dict(rs[3])] + rs[3:], "중복"),
    (lambda rs: [rs[1], rs[0]] + rs[2:], "역순"),
    (lambda rs: [{**rs[0], "trde_qty": "-5"}] + rs[1:], "음수"),
    (lambda rs: [{**rs[2], "open_pric": "0"}] + rs[:2] + rs[3:], "0 이하"),
    (lambda rs: rs[:4] + [{**rs[4], "high_pric": f"+{PRICE - 100}"}] + rs[5:], "고가·저가 모순"),
])
def test_v243_bad_day_invalidates_that_instrument_day(mutate: Any, word: str) -> None:
    sup = prepare_minutes(two_days(mutate), "005930", 1, CAL, kst(2026, 10, 8, 20, 0))
    assert not sup.data_ok and not sup.missing
    assert [d for d, _ in sup.invalid_days] == [D1]
    assert any(word in issue for issue in sup.invalid_days[0][1])
    assert {b.start.date() for b in sup.bars} == {D0}                     # 무효인 날의 봉은 내지 않음


def test_v243_no_trade_minutes_are_valid() -> None:
    rows = [r for r in two_days() if r["cntr_tm"][:12] not in ("202610080903", "202610080904", "202610080905")]
    sup = prepare_minutes(rows, "005930", 1, CAL, kst(2026, 10, 8, 20, 0))
    assert sup.data_ok and sup.invalid_days == ()
    assert len(sup.bars) == 20 - 3
    assert sup.no_trade_slots[D1] == 390 - (10 - 3) and sup.no_trade_slots[D0] == 390 - 10


def test_v243_page_failure_is_missing_and_k10_scope_is_instrument() -> None:
    sup = missing_supply("005930", 1, kst(2026, 10, 8, 9, 30), "ka10080 2쪽 3회 실패")
    assert sup.missing and not sup.data_ok and sup.bars == ()
    halts = RecordingHalts()
    assert report_k10(halts, "KR-PAPER-01", sup) is True
    assert halts.calls == [(HaltType.K10, "marketdata", "KR-PAPER-01", "005930")]
    assert "누락" in halts.reasons[0]


def test_v243_quality_failure_goes_to_k10_like_daily_path_and_ok_does_not() -> None:
    halts = RecordingHalts()
    bad = prepare_minutes(two_days(lambda rs: [rs[1], rs[0]] + rs[2:]), "005930", 1, CAL, kst(2026, 10, 8, 20, 0))
    ok = prepare_minutes(two_days(), "000660", 1, CAL, kst(2026, 10, 8, 20, 0))
    assert report_k10(halts, "KR-PAPER-01", bad) is True
    assert report_k10(halts, "KR-PAPER-01", ok) is False
    assert halts.calls == [(HaltType.K10, "marketdata", "KR-PAPER-01", "005930")]
    assert "분봉" in halts.reasons[0] and "역순" in halts.reasons[0]


class RecordingHalts:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.reasons: list[str] = []

    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None, *,
                 instrument: str | None = None, owner: str | None = None, variant: str | None = None) -> str:
        self.calls.append((halt_type, actor, account, instrument))
        self.reasons.append(reason)
        return "H-1"


# --- 저장: 추가 전용 표 ---------------------------------------------------------------------------------------

def test_minute_table_created_on_open_of_existing_db_schema_version_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "trading.db"
    Repository(db, Mode.PAPER).close()
    c = sqlite3.connect(db)
    c.execute("DROP TABLE minute_bar")                                     # 이 기능 전 DB 흉내
    c.commit()
    c.close()
    repo = Repository(db, Mode.PAPER)
    names = {n for (n,) in repo.connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
    assert {"minute_bar", "minute_bar_no_update", "minute_bar_no_delete"} <= names
    assert repo.select("SELECT value FROM meta WHERE key='schema_version'")[0]["value"] == SCHEMA_VERSION == "2"
    repo.close()


def test_minute_insert_idempotent_keyed_and_append_only(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    at = datetime(2026, 10, 8, 11, 0, tzinfo=UTC)
    bars = bars_at((9, 0), (9, 1), (9, 2))
    with repo.tx():
        assert repo.insert_minute_bars(bars, "ka10080", at) == (3, 0, 0)
    changed = [MinuteBar(b.instrument, b.minutes, b.start, b.open, b.high, b.low, b.close + 1, b.volume)
               for b in bars[:1]]
    other_adj = [MinuteBar(b.instrument, b.minutes, b.start, b.open, b.high, b.low, b.close, b.volume, "0")
                 for b in bars[:1]]
    with repo.tx():
        assert repo.insert_minute_bars(bars, "ka10080", at) == (0, 3, 0)          # 같은 키·같은 값
        assert repo.insert_minute_bars(changed, "ka10080", at) == (0, 0, 1)       # 같은 키·다른 값 → 기존 유지
        assert repo.insert_minute_bars(other_adj, "ka10080", at) == (1, 0, 0)     # 수정주가 구분이 키의 일부
    got = repo.minute_bars("005930", 1)
    assert got == bars                                                             # 기존 값 유지, 오름차순
    assert repo.minute_bars("005930", 1, start=kst(2026, 10, 8, 9, 1), end=kst(2026, 10, 8, 9, 2)) == bars[1:2]
    assert repo.minute_bars("005930", 1, "0") == other_adj
    for sql in ("UPDATE minute_bar SET close='1'", "DELETE FROM minute_bar"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            repo.connection.execute(sql)
    repo.close()


def test_minute_insert_requires_transaction(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    with pytest.raises(ValueError):
        repo.insert_minute_bars(bars_at((9, 0)), "ka10080", datetime(2026, 10, 8, tzinfo=UTC))
    repo.close()


def test_export_minute_csv_from_db(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    with repo.tx():
        repo.insert_minute_bars(bars_at((9, 1), (9, 0)), "ka10080", datetime(2026, 10, 8, 11, 0, tzinfo=UTC))
    path, res = export_minute_csv(repo, "005930", 1, tmp_path / "out")
    assert path.name == "005930-1m.csv" == minute_csv_name("005930", 1) and res.added == 2
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(MINUTE_CSV_HEADER) == "datetime,open,high,low,close,volume"
    assert lines[1] == f"2026-10-08T09:00:00+09:00,{PRICE},{PRICE + 50},{PRICE - 50},{PRICE + 10},1"
    _, again = export_minute_csv(repo, "005930", 1, tmp_path / "out")
    assert again.added == 0 and again.skipped == 2
    repo.close()


def test_merge_csv_appends_only_new_bars_and_keeps_existing_values(tmp_path: Path) -> None:
    p = tmp_path / "005930-1m.csv"
    first = bars_at((9, 0), (9, 1))
    assert merge_minute_csv(p, first).added == 2
    newer = bars_at((9, 1), (9, 2))
    changed = MinuteBar("005930", 1, newer[0].start, Decimal(1), Decimal(2), Decimal(1), Decimal(1), Decimal(1))
    res = merge_minute_csv(p, [changed, newer[1]])
    assert (res.added, res.skipped, res.differing) == (1, 1, 1)
    back = read_minute_csv(p, "005930", 1)
    assert [b.start.minute for b in back] == [0, 1, 2] and back[1] == first[1]       # 기존 값 유지


def test_merge_csv_refuses_foreign_header(tmp_path: Path) -> None:
    p = tmp_path / "005930-1m.csv"
    p.write_text("date,open\n2026-10-08,1\n", encoding="utf-8")
    with pytest.raises(MinuteDataMissing):
        merge_minute_csv(p, bars_at((9, 0)))
    assert p.read_text(encoding="utf-8") == "date,open\n2026-10-08,1\n"


# --- 축적 도구 scripts/fetch-minute.py --------------------------------------------------------------------

EVENING = datetime(2026, 10, 8, 11, 0, tzinfo=UTC)             # 2026-10-08(목) 20:00 KST
NEXT_EVENING = datetime(2026, 10, 12, 11, 0, tzinfo=UTC)       # 2026-10-12(월) 20:00 KST (10-09 휴장, 주말)
PROVIDE_START = kst(2026, 10, 1, 13, 6)                         # 합성 서버 제공 시작(부분일 — K-U30 실측 형태)
PAGE = 300
OUT = Path("var") / "backtest" / "data" / "minute-test"


class FakeMinuteServer:
    """KiwoomHttp 자리. ka10080: base_dt 이하 거래일(달력)의 하루 622봉(1분), 최신→과거, PAGE행씩 연속 조회."""

    def __init__(self, *, upto: date = D1, fail_pages: dict[str, set[int]] | None = None,
                 fail_codes: set[str] | None = None, mutate: Any = None) -> None:
        self.calls = 0
        self.log: list[tuple[str, str, dict, str | None]] = []
        self.upto = upto
        self.fail_pages = fail_pages or {}
        self.fail_codes = fail_codes or set()
        self.mutate = mutate

    def rows(self, code: str, base: date, minutes: int) -> list[dict]:
        out: list[dict] = []
        d = min(base, self.upto)
        while d >= PROVIDE_START.date():
            if CAL.is_trading_day(d):
                out += [r for r in day_rows(d, minutes=minutes)
                        if datetime.strptime(r["cntr_tm"], "%Y%m%d%H%M%S").replace(tzinfo=KST) >= PROVIDE_START]
            d -= timedelta(days=1)
        return self.mutate(code, out) if self.mutate is not None else out

    def post(self, path: str, api_id: str, body: dict, token: str | None,
             cont_yn: str | None = None, next_key: str | None = None) -> HttpReply:
        self.calls += 1
        self.log.append((path, api_id, dict(body), next_key))
        if api_id == "au10001":
            return HttpReply(200, {"return_code": 0, "return_msg": "정상", "token": TOKEN, "token_type": "bearer",
                                   "expires_dt": "20261013200000"})
        if api_id == "au10002":
            return HttpReply(200, {"return_code": 0, "return_msg": "정상"})
        assert token == TOKEN and api_id == "ka10080" and path == "/api/dostk/chart"
        off = int(next_key or 0)
        code = body["stk_cd"]
        if code in self.fail_codes or off in self.fail_pages.get(code, set()):
            return HttpReply(200, {"return_code": 5, "return_msg": "기타 업무 오류"})
        rows = self.rows(code, datetime.strptime(body["base_dt"], "%Y%m%d").date(), int(body["tic_scope"]))
        more = off + PAGE < len(rows)
        page = {"return_code": 0, "return_msg": "정상", "stk_cd": code, MIN_LIST_KEY: rows[off:off + PAGE]}
        return HttpReply(200, page, "Y" if more else "N", str(off + PAGE) if more else None)

    def ka10080(self, code: str | None = None) -> list[tuple[dict, str | None]]:
        return [(b, k) for _, a, b, k in self.log if a == "ka10080" and (code is None or b["stk_cd"] == code)]


class Harness:
    def __init__(self, tmp_path: Path, *, server: FakeMinuteServer | None = None, when: datetime = EVENING) -> None:
        self.root = repo_root(tmp_path)
        self.clock = FixedClock(when)
        self.server = server or FakeMinuteServer()
        self.connects = 0
        self.adapters: list[KiwoomAdapter] = []

    def connect(self, root: Path, environ: Any, clock: Any) -> Any:
        self.connects += 1
        conn = make_conn(self.server, self.clock, allow=MINUTE_REST_APIS)  # type: ignore[arg-type]
        self.adapters.append(conn.adapter)
        return conn

    def run(self, argv: list[str]) -> int:
        return minute_main(self.root, argv, {"TRADE_MODE": "PAPER"}, clock=self.clock, connect=self.connect)

    def codes(self, codes: list[str]) -> Path:
        p = self.root / "codes.txt"
        p.write_text("\n".join(codes) + "\n", encoding="utf-8")
        return p

    def metas(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.root / OUT / "meta").glob("*.json"))]


def args(codes_file: Path, *extra: str, since: str = "2026-10-01") -> list[str]:
    return ["--mode", "PAPER", "--codes-file", str(codes_file), "--minutes", "1", "--since", since,
            "--out", str(OUT), *extra]


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_tool_saves_regular_bars_only_drops_partial_oldest_day(tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    h = Harness(tmp_path)
    assert h.run(args(h.codes(["005930", "069500"]))) == 0
    out = capsys.readouterr().out
    reqs = h.server.ka10080("005930")
    assert all(b == {"stk_cd": "005930", "tic_scope": "1", "upd_stkpc_tp": "1", "base_dt": "20261008"}
               for b, _ in reqs)
    assert {a for _, a, _, _ in h.server.log} == {"au10001", "ka10080", "au10002"}
    assert h.server.log[-1][1] == "au10002"                                   # 토큰 폐기
    rows = read_rows(h.root / OUT / "005930-1m.csv")
    assert list(rows[0]) == list(MINUTE_CSV_HEADER)
    days = sorted({r["datetime"][:10] for r in rows})
    assert days == ["2026-10-02", "2026-10-06", "2026-10-07", "2026-10-08"]   # 10-01은 13:06부터라 부분일 → 제외
    assert len(rows) == 4 * 380
    times = {r["datetime"][11:16] for r in rows}
    assert min(times) == "09:00" and max(times) == "15:19"                     # 15:30·15:35·16:00~ 제외
    assert [r["datetime"] for r in rows] == sorted(r["datetime"] for r in rows)
    assert rows[0]["datetime"] == "2026-10-02T09:00:00+09:00"
    meta = h.metas()[0]
    pc = meta["per_code"]["005930"]
    assert pc["added"] == 4 * 380 and pc["skipped_existing"] == 0 and pc["end"] == "더 없음"
    assert pc["excluded"]["정규장 밖"] == 4 * 242 + (2 + 240)
    assert pc["partial_days_dropped"] == ["2026-10-01"]
    assert pc["invalid_days"] == [] and pc["no_trade_slots"] == 4 * 10       # 15:20~15:29 동시호가 = 거래 없음
    assert meta["allowed_rest_apis"] == sorted(MINUTE_REST_APIS) and meta["interval_seconds"] >= 1.2
    assert meta["rules"]["regular_session"] and meta["rules"]["confirm_margin_seconds"] == 5
    assert meta["ok"] == 2 and meta["failed"] == [] and meta["not_queried"] == []
    text = out + json.dumps(meta, ensure_ascii=False)
    for secret in (TOKEN, APP_KEY, SECRET, ACCOUNT_NO):
        assert secret not in text
    assert "성공 2" in out


def test_tool_since_stops_paging_and_rerun_appends_only_new_bars(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.run(args(h.codes(["005930"]), since="2026-10-07")) == 0
    assert len(h.server.ka10080()) == 5                                        # 1244행 → 300행씩 5쪽에서 since 도달
    rows = read_rows(h.root / OUT / "005930-1m.csv")
    assert sorted({r["datetime"][:10] for r in rows}) == ["2026-10-07", "2026-10-08"] and len(rows) == 760
    assert h.metas()[0]["per_code"]["005930"]["end"] == "since 도달"

    h2 = Harness(tmp_path / "next", server=FakeMinuteServer(upto=date(2026, 10, 12)), when=NEXT_EVENING)
    h2.root = h.root
    assert h2.run(args(h.codes(["005930"]), since="2026-10-07")) == 0
    assert len(h2.server.ka10080()) == 5            # 기존 마지막 날(10-08)까지만: 10-12 622 + 10-08 622 → 5쪽
    assert all(b["base_dt"] == "20261012" for b, _ in h2.server.ka10080())
    rows = read_rows(h.root / OUT / "005930-1m.csv")
    assert sorted({r["datetime"][:10] for r in rows}) == ["2026-10-07", "2026-10-08", "2026-10-12"]
    assert len(rows) == 3 * 380 and [r["datetime"] for r in rows] == sorted(r["datetime"] for r in rows)
    meta = sorted(h2.metas(), key=lambda m: m["started_at"])[-1]
    assert meta["per_code"]["005930"]["added"] == 380 and meta["per_code"]["005930"]["skipped_existing"] == 380


def test_tool_same_page_three_failures_fail_that_code_only(tmp_path: Path) -> None:
    h = Harness(tmp_path, server=FakeMinuteServer(fail_pages={"000660": {600}}))
    assert h.run(args(h.codes(["005930", "000660", "069500"]))) == 1
    assert [k for _, k in h.server.ka10080("000660")].count("600") == 3      # 같은 쪽 3회 실패
    assert not (h.root / OUT / "000660-1m.csv").exists()                     # 누락 = 무효 → 아무것도 쓰지 않음
    assert (h.root / OUT / "069500-1m.csv").exists()
    meta = h.metas()[0]
    assert [f["code"] for f in meta["failed"]] == ["000660"] and "누락" in meta["failed"][0]["reason"]
    assert meta["ok"] == 2


def test_tool_three_failed_codes_in_a_row_stop_rest(tmp_path: Path) -> None:
    h = Harness(tmp_path, server=FakeMinuteServer(fail_codes={"005930", "000660", "035420"}))
    assert h.run(args(h.codes(["005930", "000660", "035420", "005380", "373220"]))) == 1
    meta = h.metas()[0]
    assert len(meta["failed"]) == 3 and meta["not_queried"] == ["005380", "373220"]
    assert h.server.ka10080("005380") == [] and h.server.log[-1][1] == "au10002"


def test_tool_invalid_day_not_saved_and_recorded(tmp_path: Path) -> None:
    def dup(code: str, rows: list[dict]) -> list[dict]:
        i = next(i for i, r in enumerate(rows) if r["cntr_tm"] == "20261007100000")
        return rows[:i] + [dict(rows[i])] + rows[i:]

    h = Harness(tmp_path, server=FakeMinuteServer(mutate=dup))
    assert h.run(args(h.codes(["005930"]))) == 0
    rows = read_rows(h.root / OUT / "005930-1m.csv")
    assert "2026-10-07" not in {r["datetime"][:10] for r in rows} and len(rows) == 3 * 380
    pc = h.metas()[0]["per_code"]["005930"]
    assert [d["day"] for d in pc["invalid_days"]] == ["2026-10-07"] and "중복" in pc["invalid_days"][0]["issues"][0]


def test_tool_existing_differing_bar_kept_and_counted(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    p = h.root / OUT / "005930-1m.csv"
    p.parent.mkdir(parents=True)
    p.write_text("datetime,open,high,low,close,volume\n2026-10-08T09:00:00+09:00,1,2,1,1,1\n", encoding="utf-8")
    assert h.run(args(h.codes(["005930"]))) == 0
    rows = read_rows(p)
    assert rows[[r["datetime"] for r in rows].index("2026-10-08T09:00:00+09:00")]["open"] == "1"
    pc = h.metas()[0]["per_code"]["005930"]
    assert pc["differing_existing"] == 1 and pc["skipped_existing"] == 1


def test_tool_foreign_csv_fails_code_without_touching_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    p = h.root / OUT / "005930-1m.csv"
    p.parent.mkdir(parents=True)
    p.write_text("date,open\n", encoding="utf-8")
    assert h.run(args(h.codes(["005930"]))) == 1
    assert p.read_text(encoding="utf-8") == "date,open\n"


@pytest.mark.parametrize("argv_extra, when", [
    (["--mode", "LIVE"], EVENING), (["--mode", "BACKTEST"], EVENING),
    (["--minutes", "2"], EVENING), (["--since", "2026-13-01"], EVENING),
    ([], datetime(2026, 10, 8, 1, 0, tzinfo=UTC)),                       # 거래일 10:00 KST — 장중
    ([], datetime(2026, 10, 8, 7, 30, tzinfo=UTC)),                      # 거래일 16:30 KST — 장외 전용 구간
])
def test_tool_refuses_before_connecting(tmp_path: Path, argv_extra: list[str], when: datetime) -> None:
    h = Harness(tmp_path, when=when)
    argv = args(h.codes(["005930"]))
    for i in range(0, len(argv_extra), 2):
        argv[argv.index(argv_extra[i]) + 1] = argv_extra[i + 1]
    assert h.run(argv) == 2 and h.connects == 0 and h.server.calls == 0


def test_tool_runs_on_holiday_daytime(tmp_path: Path) -> None:
    h = Harness(tmp_path, when=kst(2026, 10, 9, 11, 0).astimezone(UTC))   # 한글날 휴장 11:00
    assert h.run(args(h.codes(["005930"]))) == 0
    assert all(b["base_dt"] == "20261008" for b, _ in h.server.ka10080())


def test_tool_requires_codes_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.run(["--mode", "PAPER", "--minutes", "1"]) == 2 and h.connects == 0


@pytest.mark.parametrize("lock", ["run", "autopilot"])
def test_tool_refuses_while_run_or_autopilot_holds_lock(tmp_path: Path, lock: str) -> None:
    h = Harness(tmp_path)
    base = h.root / "var" / "paper"
    held = acquire_instance_lock(base) if lock == "run" else acquire_instance_lock(base, AUTOPILOT_LOCK)
    try:
        assert h.run(args(h.codes(["005930"]))) == 2 and h.connects == 0
    finally:
        held.release()


def test_tool_refuses_non_mock_target(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def connect(root: Path, environ: Any, clock: Any) -> Any:
        h.connects += 1
        return make_conn(h.server, h.clock, rest_url="https://api.kiwoom.com", allow=MINUTE_REST_APIS)  # type: ignore[arg-type]

    code = minute_main(h.root, args(h.codes(["005930"])), {"TRADE_MODE": "PAPER"}, clock=h.clock, connect=connect)
    assert code == 2 and h.server.ka10080() == []


def test_default_connect_uses_minute_allowlist_and_conservative_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(minute_fetch, "connect_paper", lambda root, env, clock, **kw: seen.update(kw) or "x")
    minute_fetch._default_connect(Path("."), {}, FixedClock(EVENING))  # noqa: SLF001
    assert seen["allowed_apis"] == MINUTE_REST_APIS == {"au10001", "au10002", "ka10080"}
    assert seen["per_tr_interval"] >= 1.2


@pytest.mark.parametrize("path", [REPO / "src" / "trading_system" / "app" / "minute_fetch.py",
                                  REPO / "src" / "trading_system" / "marketdata" / "minute.py",
                                  REPO / "scripts" / "fetch-minute.py"])
def test_minute_sources_have_no_order_imports_or_calls(path: Path) -> None:
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


def test_engine_does_not_use_minute_bars_yet() -> None:
    """M1 PAPER 동작 불변: 분봉은 아직 판정 입력에 연결하지 않는다 (주기 판정 배선은 T-029)."""
    for name in ("engine.py", "main.py", "wiring.py", "backtest.py"):
        text = (REPO / "src" / "trading_system" / "app" / name).read_text(encoding="utf-8")
        for word in ("marketdata.minute", "minute_bar", "MinuteBar", "minute_fetch", "prepare_minutes"):
            assert word not in text
