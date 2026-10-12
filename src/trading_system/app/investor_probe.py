"""키움 모의 서버 투자자별 일별 TR 실측 도구 — 읽기 전용 (T-034, K-U42, CAP-DAT-05 Q-2).

근거: 사용자 승인(대화 2026-10-11, 기록 A-088 예정) "모의 서버에서 장외에 2~3종목으로 ka10060·ka10045·ka10008을 조회해
몇 년 전까지 오는지·한 번에 몇 행인지·그날 값이 언제 확정되는지 확인, 주문 없음". 깊은 조회·값 저장(--deep)은
사용자 추가 지시(2026-10-11, A-088 Q-2 범위 — 조회만, 장외).
요청·응답 필드: S2 공식 명세 kiwoom_api_spec.json(2026-10-11 내려받음) — docs/broker-specs/kiwoom-kr.md "비시세 데이터 조사".
  - ka10060 종목별투자자기관별차트 POST /api/dostk/chart: dt·stk_cd·amt_qty_tp(1 금액)·trde_tp(0 순매수)·unit_tp(1000 천주)
    → 목록 stk_invsr_orgn_chart (dt, cur_prc, pred_pre, acc_trde_prica, ind_invsr … natfor, 금액 단위 백만원)
  - ka10045 종목별기관매매추이 POST /api/dostk/mrkcond: stk_cd·strt_dt·end_dt·orgn_prsm_unp_tp·for_prsm_unp_tp(명세 필수,
    1 매수단가) → 목록 stk_orgn_trde_trnsn
  - ka10008 주식외국인종목별매매동향 POST /api/dostk/frgnistt: stk_cd → 목록 stk_frgnr

사람이 직접 실행한다. 주문 API는 부르지 않는다(HTTP 경계 허용 목록 — 밖의 api-id는 전송 전에 막는다).

연결 전 검사 (하나라도 어긋나면 네트워크 객체를 만들지 않는다 — 종료 코드 2)
- `--mode PAPER`만. TRADE_MODE=PAPER·.env.paper·connection_approved = true·모의 도메인(connect_paper).
- 장외 전용: KRX 거래일 06:30~17:00 KST(늦게 끝나는 날은 마감 + 90분)이면 거부 (probe_tool.market_hours_refusal).
- PAPER run(run.lock)·자동 운영(autopilot.lock)·하트비트(30초 이내)가 있으면 거부.

기본 모드 (종목 최대 3, TR당 1.2초 간격)
- ka10060: dt = 마지막으로 장이 끝난 거래일(아래), 연속 조회 --max-pages쪽까지 → 쪽별 행 수·가장 오래된 일자·다음 쪽 여부.
  dt를 --back-days만큼 과거로 옮긴 1쪽씩 → 그 시점 자료가 오는지.
- ka10045: strt_dt=20100101 ~ end_dt=마지막 거래일, 연속 조회 --max-pages쪽까지 → 행 수·가장 오래된 일자·연속 여부.
- ka10008: 연속 조회 --max-pages쪽까지 → 행 수·가장 오래된 일자.
- 확정 시각 판단용(K-U42): 종목별 ka10060 최근 3개 일자의 투자자 값 해시(sha256 앞 16자)와 행 전체 해시 + 수집 시각.
  다른 시각에 다시 실행하며 `--compare <이전 결과 json>`을 주면 일자별 같음/다름을 출력한다.
- 투자자 합계 검사(최근 일자 1개): 개인+외국인+기관계+기타법인+국가+내외국인 합, 국가를 뺀 합, 기관계 − 세부 합,
  기관계 − (세부 + 국가)의 차이 크기 범주(0, ≤1, >1 — 단위 백만원)만 기록한다. 값은 기록하지 않는다.
- 기록: var/paper/probe/<YYYYMMDD-HHMMSS>-investor-probe.json·.md — 필드 이름·쪽별 행 수·일자 범위·반환 코드·해시·범주만.
  가격·금액 원값·토큰·앱키·계좌번호는 기록·출력하지 않는다.

깊은 조회 모드 (--deep, --codes가 정확히 1종목일 때만, 허용 목록 au10001·au10002·ka10060)
- ka10060을 위 조건으로 연속 조회를 응답이 더 없을 때까지(안전 상한 --deep-max-pages, 기본 400). 상한에 닿으면
  "상한 도달 — 더 과거가 있을 수 있음". 연속 실패 3회면 중단(받은 것까지 저장).
- 끝까지 받았으면 dt를 가장 오래된 일자 하루 전으로 옮겨 1쪽 더 조회 → 빈 목록이면 "이전 자료 없음 확인".
- 저장(이 모드만 원값 저장 — 공개 시장 데이터, 연구용): --save-dir(기본 var/backtest/data/kiwoom-investor-probe) 아래
  <종목>-ka10060-amt.csv(응답 필드 그대로, 부호 유지, 일자 오름차순)·meta-<YYYYMMDD-HHMMSS>.json. 같은 CSV가 있으면 거부.

"마지막 거래일": 거래일이고 정규장 마감이 지났으면 오늘, 아니면 그 전 거래일. 일봉 확정(20:00, daily_fetch)과 달리
장 마감 직후에도 그날 행을 받아 해시를 남기기 위해서다 — 그날 값이 언제 최종값이 되는지가 이 실측의 대상이다.

사용 (PowerShell, 저녁·주말·휴일에 — 거래일 06:30~17:00 KST에는 거부됨):
    cd C:\\AI\\trading_system\\trading_system
    $env:TRADE_MODE='PAPER'
    .venv\\Scripts\\python scripts\\investor-probe.py --mode PAPER

    선택: --codes "005930,000660,035420" (최대 3), --max-pages 30 (1~60), --back-days 365,1095,1825,3650,
          --compare var\\paper\\probe\\<이전 시각>-investor-probe.json
    확정 시각 확인 예: 거래일 17:10쯤 한 번 실행 → 다음 날 06:00쯤(또는 휴일) --compare로 다시 실행.

    깊은 조회·값 저장 (1종목):
    .venv\\Scripts\\python scripts\\investor-probe.py --mode PAPER --codes 005930 --deep

기대 출력 (기본 모드, 3종목, 기본값이면 조회 최대 282회 × 1.2초 ≈ 6분):
    투자자 실측 시작: 종목 005930, 000660, 035420 / 마지막 거래일 2026xxxx / 최대 282회 조회 (TR당 1.2초 간격 ...)
    토큰 발급 au10001: 정상
    REST 조회:
      ka10060 005930 paging: 30쪽 ...행 20xxxxxx~2026xxxx (다음 쪽 있음)
      ka10060 005930 dt-365d: 1쪽 ...행 ...
      ka10045 005930 range: ...쪽 ...행 ...
      ka10008 005930 paging: ...
    확정 시각 해시: 005930 2026xxxx·...·... (합계 범주 total=0)
    토큰 폐기 au10002: 완료
    결과: ...\\var\\paper\\probe\\<시각>-investor-probe.json
    요약: ...\\var\\paper\\probe\\<시각>-investor-probe.md
  --compare를 주면 끝에 "005930 2026xxxx: 투자자 값 같음 / 행 전체 같음" 줄이 일자마다 나온다.
기대 출력 (--deep):
    깊은 조회 시작: 005930 ka10060 dt=2026xxxx, 상한 400쪽 (TR당 1.2초 간격, 최대 약 8.0분)
    토큰 발급 au10001: 정상
      쪽 10: 누적 ...행, 가장 오래된 일자 ...
    연속 조회 끝: 더 없음 — ...쪽 ...행 20xxxxxx~2026xxxx
    이전 자료 확인(dt=...): 확인 완료 — 이전 자료 없음
    토큰 폐기 au10002: 완료
    저장: ...\\var\\backtest\\data\\kiwoom-investor-probe\\005930-ka10060-amt.csv (...행)
    메타: ...\\var\\backtest\\data\\kiwoom-investor-probe\\meta-<시각>.json
종료 코드: 0 = 모두 정상, 1 = 일부 실패(결과 파일의 '실패'), 2 = 실행 전 거부(연결 없음).
판정(K-U42 해소 여부)은 사람이 결과를 보고 kiwoom-kr.md에 "② 모의 실측, 일자"로 적는다.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom import parse
from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS
from trading_system.broker.kiwoom.token import TokenError
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.marketdata.calendar import KrxCalendar

from .preflight import connect_paper
from .probe_tool import (
    MAX_CODES, MAX_CONSECUTIVE_FAILURES, PROBE_INTERVAL, UNSUPPORTED_MARK, ProbeError, _api_summary, _load_calendar,
    _mask, _running_refusal, fatal_critical, market_hours_refusal, parse_back_days, parse_codes)

TOOL = "scripts/investor-probe.py"
BASIS = "T-034 K-U42, CAP-DAT-05 Q-2 사용자 승인 2026-10-11(A-088 예정) — 조회만, 주문 없음"

# --- 허용 목록·명세 값 -------------------------------------------------------------------------------------

INVESTOR_REST_APIS = frozenset({"au10001", "au10002", "ka10060", "ka10045", "ka10008"})   # HTTP 경계 허용 목록
DEEP_REST_APIS = frozenset({"au10001", "au10002", "ka10060"})                            # --deep (위의 부분집합)
CHART_PATH, MRKCOND_PATH, FRGNISTT_PATH = "/api/dostk/chart", "/api/dostk/mrkcond", "/api/dostk/frgnistt"
INVSR_LIST, ORGN_LIST, FRGN_LIST = "stk_invsr_orgn_chart", "stk_orgn_trde_trnsn", "stk_frgnr"
INVSR_BODY = {"amt_qty_tp": "1", "trde_tp": "0", "unit_tp": "1000"}       # 금액·순매수·천주 (명세 S2)
ORGN_BODY = {"orgn_prsm_unp_tp": "1", "for_prsm_unp_tp": "1"}             # 명세 필수 입력 (1 매수단가 — 기록 안 함)
KA10045_START = "20100101"
# ka10060 투자자 필드 (명세 S2, 단위 백만원)
TOTAL_FIELDS = ("ind_invsr", "frgnr_invsr", "orgn", "etc_corp", "natn", "natfor")
ORGN_PARTS = ("fnnc_invt", "insrnc", "invtrt", "etc_fnnc", "bank", "penfnd_etc", "samo_fund")
INVESTOR_FIELDS = ("ind_invsr", "frgnr_invsr", "orgn", *ORGN_PARTS, "natn", "etc_corp", "natfor")
FIELD_UNITS = {"dt": "YYYYMMDD", "cur_prc": "원, 부호 포함(등락 표시)", "pred_pre": "원, 부호 포함",
               "acc_trde_prica": "백만원", **{f: "백만원 (amt_qty_tp=1 금액, trde_tp=0 순매수)" for f in INVESTOR_FIELDS}}

DEFAULT_CODES = "005930,000660,035420"
PAGES_DEFAULT, PAGES_MAX = 30, 60
BACK_DAYS_DEFAULT = "365,1095,1825,3650"
DEEP_PAGES_DEFAULT, DEEP_PAGES_MAX = 400, 1000
DEEP_SAVE_DIR = "var/backtest/data/kiwoom-investor-probe"
# [A-090] --collect: 종목 목록 × ka10060 금액·순매수, since 이후만(그 이전 쪽은 받지 않음) → <out>/<코드>.csv
COLLECT_COLUMNS = ("dt", "acc_trde_prica", "ind_invsr", "frgnr_invsr", "orgn", "fnnc_invt", "insrnc", "invtrt",
                   "etc_fnnc", "bank", "penfnd_etc", "samo_fund", "natn", "etc_corp", "natfor")
COLLECT_SINCE_DEFAULT = "2016-01-01"
COLLECT_MAX_CODES = 300
COLLECT_PAGES_MAX = 200                     # 종목당 안전 상한 (2016~ 약 27쪽)
_LIST_CODE = re.compile(r"[0-9A-Z]{6}", re.ASCII)   # 영숫자 신형 코드 포함 (daily_fetch와 같은 규칙)
DEEP_PROGRESS_EVERY = 10
FINAL_DAYS = 3                 # 확정 시각 해시를 남길 최근 일자 수
HASH_LEN = 16
FALLBACK_CLOSE = dtime(15, 30)  # 달력이 없을 때 정규장 마감

_DT = re.compile(r"[0-9]{8}", re.ASCII)
_CODE_KEY = re.compile(r"[0-9]{6}", re.ASCII)


# --- 순수 함수 ---------------------------------------------------------------------------------------------

def last_closed_day(now: datetime, calendar: KrxCalendar | None) -> date:
    """정규장 마감이 지난 가장 최근 거래일. 달력이 없거나 모르는 날은 평일을 거래일로 본다."""
    local = now.astimezone(KST)
    d = local.date()
    for _ in range(31):
        session = calendar.session(d) if calendar is not None else None
        trading = session.is_open if session is not None else d.weekday() < 5
        if trading:
            if d < local.date():
                return d
            close = session.regular_close if session is not None and session.regular_close is not None else \
                datetime.combine(d, FALLBACK_CLOSE, tzinfo=KST)
            if local >= close:
                return d
        d -= timedelta(days=1)
    return d


def date_summary(rows: Sequence[Mapping[str, object]]) -> dict:
    """행의 dt → 형태 요약(행 수·유효·순서·최신·최고(最古)·중복). 값은 보지 않는다."""
    raw = [str(r.get("dt") or "") for r in rows]
    valid = [v for v in raw if _DT.fullmatch(v)]
    out: dict[str, Any] = {"rows": len(raw), "valid": len(valid), "invalid": len(raw) - len(valid),
                           "distinct": len(set(valid)), "duplicates": len(valid) - len(set(valid)),
                           "newest": max(valid) if valid else None, "oldest": min(valid) if valid else None}
    if all(a > b for a, b in zip(valid, valid[1:])):
        out["order"] = "최신→과거"
    elif all(a < b for a, b in zip(valid, valid[1:])):
        out["order"] = "과거→최신"
    else:
        out["order"] = "섞임"
    return out


def _norm(row: Mapping[str, object], field: str) -> str:
    if field not in row:
        return "\u2205"
    try:
        return str(parse.dec(row[field], field))   # "+1584"와 "1584"는 같은 값
    except parse.KiwoomParseError:
        return "?" + str(row[field])


def value_hash(row: Mapping[str, object], fields: Sequence[str]) -> str:
    """지정 필드 값의 sha256 앞 16자. 원값은 남기지 않고 같음/다름 비교에만 쓴다."""
    text = "\n".join(f"{f}={_norm(row, f)}" for f in fields)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LEN]


def finality_dates(rows: Sequence[Mapping[str, object]]) -> list[dict]:
    """최근 FINAL_DAYS개 일자의 투자자 값 해시·행 전체 해시. 같은 일자가 여럿이면 처음 것."""
    by_dt: dict[str, Mapping[str, object]] = {}
    for r in rows:
        dt = str(r.get("dt") or "")
        if _DT.fullmatch(dt):
            by_dt.setdefault(dt, r)
    out = []
    for dt in sorted(by_dt, reverse=True)[:FINAL_DAYS]:
        r = by_dt[dt]
        out.append({"dt": dt, "investor_hash": value_hash(r, INVESTOR_FIELDS),
                    "row_hash": value_hash(r, sorted(k for k in r if k != "dt")),
                    "missing_fields": [f for f in INVESTOR_FIELDS if f not in r]})
    return out


def diff_category(x: Decimal) -> str:
    a = abs(x)
    return "0" if a == 0 else "≤1" if a <= 1 else ">1"


def sum_check(row: Mapping[str, object]) -> dict:
    """투자자 합계 검사 — 차이 크기 범주만 (단위 백만원). 해석 못 한 필드는 이름만."""
    vals: dict[str, Decimal] = {}
    bad: list[str] = []
    for f in INVESTOR_FIELDS:
        try:
            vals[f] = parse.dec(row.get(f), f)
        except parse.KiwoomParseError:
            bad.append(f)

    def cat(plus: Sequence[str], minus: Sequence[str] = ()) -> str:
        if any(f in bad for f in (*plus, *minus)):
            return "해석 불가"
        return diff_category(sum((vals[f] for f in plus), Decimal(0)) - sum((vals[f] for f in minus), Decimal(0)))

    no_natn = tuple(f for f in TOTAL_FIELDS if f != "natn")
    return {"dt": str(row.get("dt") or ""), "total": cat(TOTAL_FIELDS), "total_without_natn": cat(no_natn),
            "orgn_vs_parts": cat(("orgn",), ORGN_PARTS), "orgn_vs_parts_with_natn": cat(("orgn",), (*ORGN_PARTS, "natn")),
            "unparsable": bad}


def compare_finality(prev: Mapping[str, Any], cur: Mapping[str, Any]) -> dict:
    """이전·이번 확정 시각 해시를 종목·일자별로 비교: 같음 / 다름 / 이전 결과에 없음 / 이번 결과에 없음."""
    def dates(fin: Mapping[str, Any], code: str) -> dict[str, dict]:
        entry = fin.get(code)
        items = entry.get("dates") if isinstance(entry, dict) else None
        return {str(d.get("dt")): d for d in items or [] if isinstance(d, dict)}

    codes = [k for k in cur if _CODE_KEY.fullmatch(str(k))]
    codes += [k for k in prev if _CODE_KEY.fullmatch(str(k)) and k not in codes]
    out: list[dict] = []
    for code in codes:
        p, c = dates(prev, code), dates(cur, code)
        for dt in [*c, *(d for d in sorted(p, reverse=True) if d not in c)]:
            if dt not in p:
                inv = row = "이전 결과에 없음"
            elif dt not in c:
                inv = row = "이번 결과에 없음"
            else:
                inv = "같음" if p[dt].get("investor_hash") == c[dt].get("investor_hash") else "다름"
                row = "같음" if p[dt].get("row_hash") == c[dt].get("row_hash") else "다름"
            out.append({"code": code, "dt": dt, "investor": inv, "row": row})
    return {"previous_collected_at": prev.get("collected_at"), "current_collected_at": cur.get("collected_at"),
            "dates": out}


def load_compare(path: Path) -> dict:
    """--compare 파일: 이 도구가 쓴 결과 json의 finality 절만 읽는다. 형식이 다르면 ProbeError."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProbeError(f"--compare 파일을 읽을 수 없음: {type(exc).__name__}") from None
    if not isinstance(data, dict) or data.get("tool") != TOOL:
        raise ProbeError(f"--compare 파일이 {TOOL} 결과가 아님")
    fin = data.get("finality")
    if not isinstance(fin, dict) or not all(isinstance(v, dict) for k, v in fin.items() if _CODE_KEY.fullmatch(k)):
        raise ProbeError("--compare 파일에 finality 절이 없거나 형식이 다름")
    return fin


# --- 쪽 단위 조회 --------------------------------------------------------------------------------------------

def _page(adapter: Any, api_id: str, path: str, body: dict, cont: tuple[str | None, str | None]) -> Any:
    with call_priority(Priority.MARKET):
        return adapter._query_page(api_id, body, cont, path)  # noqa: SLF001 — 쪽 경계(cont-yn·행 수) 실측


def fetch_pages(adapter: Any, api_id: str, path: str, body: dict, list_key: str,
                max_pages: int) -> tuple[dict, list[dict]]:
    """연속 조회 최대 max_pages쪽. (형태 기록, 행) — 행은 메모리에서 해시·합계 검사에만 쓰고 저장하지 않는다."""
    rec: dict[str, Any] = {"api": api_id, "path": path, "request": dict(body), "pages": 0, "rows_per_page": [],
                           "page_oldest": [], "more_pages": False, "ok": False, "error": None, "fields": [],
                           "body_keys": []}
    rows: list[dict] = []
    fields: set[str] = set()
    keys: set[str] = set()
    cont: tuple[str | None, str | None] = (None, None)
    for _ in range(max_pages):
        try:
            page = _page(adapter, api_id, path, body, cont)
        except BrokerQueryError as exc:
            rec["error"] = _mask(exc)
            break
        rec["pages"] += 1
        pb = page.body or {}
        keys.update(str(k) for k in pb)
        items = pb.get(list_key)
        if not isinstance(items, list):
            rec["error"] = f"목록 키 {list_key} 없음 또는 목록 아님"
            break
        good = [i for i in items if isinstance(i, dict)]
        rec["rows_per_page"].append(len(items))
        rec["page_oldest"].append(date_summary(good)["oldest"])
        for i in good:
            fields.update(str(k) for k in i)
        rows.extend(good)
        rec["more_pages"] = (page.cont_yn or "").upper() == "Y" and bool(page.next_key)
        if not rec["more_pages"]:
            break
        cont = ("Y", page.next_key)
    rec["ok"] = rec["error"] is None and rec["pages"] > 0
    rec["rows"] = len(rows)
    rec["fields"] = sorted(fields)
    rec["body_keys"] = sorted(keys)
    rec["dates"] = date_summary(rows)
    return rec, rows


Plan = list[tuple[str, str, str, dict, str, int, str]]   # (종류, api-id, 경로, 본문, 목록 키, 최대 쪽, 종목)


def investor_plan(codes: Sequence[str], last_day: date, max_pages: int, back_days: Sequence[int]) -> Plan:
    end = last_day.strftime("%Y%m%d")
    plan: Plan = []
    for code in codes:
        plan.append(("paging", "ka10060", CHART_PATH, {"dt": end, "stk_cd": code, **INVSR_BODY}, INVSR_LIST,
                     max_pages, code))
        for days in back_days:
            dt = (last_day - timedelta(days=days)).strftime("%Y%m%d")
            plan.append((f"dt-{days}d", "ka10060", CHART_PATH, {"dt": dt, "stk_cd": code, **INVSR_BODY}, INVSR_LIST,
                         1, code))
        plan.append(("range", "ka10045", MRKCOND_PATH,
                     {"stk_cd": code, "strt_dt": KA10045_START, "end_dt": end, **ORGN_BODY}, ORGN_LIST, max_pages, code))
        plan.append(("paging", "ka10008", FRGNISTT_PATH, {"stk_cd": code}, FRGN_LIST, max_pages, code))
    return plan


def run_queries(adapter: Any, guard: Any, plan: Plan,
                log: Callable[[str], None]) -> tuple[list[dict], dict[str, list[dict]], list[str]]:
    """계획대로 조회. (기록, 종목별 ka10060 연속 조회 행, 조회 안 한 항목). 연속 실패·허용 밖 시도·환경 치명이면 멈춘다."""
    out: list[dict] = []
    latest: dict[str, list[dict]] = {}
    skipped: list[str] = []
    unsupported: set[str] = set()
    streak = 0
    for i, (kind, api_id, path, body, key, pages, code) in enumerate(plan):
        if guard.blocked or fatal_critical(adapter) or streak >= MAX_CONSECUTIVE_FAILURES:
            skipped += [f"{p[1]} {p[6]} {p[0]}" for p in plan[i:]]
            why = "허용 밖 API 시도" if guard.blocked else "브로커 치명 오류" if fatal_critical(adapter) else "연속 실패"
            log(f"  중단: {why} — 나머지 {len(plan) - i}건 조회 안 함")
            break
        if api_id in unsupported:
            skipped.append(f"{api_id} {code} {kind} (모의 미지원)")
            continue
        rec, rows = fetch_pages(adapter, api_id, path, body, key, pages)
        rec.update(kind=kind, code=code)
        if api_id == "ka10060":
            newest = rec["dates"]["newest"]
            if kind == "paging":
                rec["requested_dt_row_present"] = any(str(r.get("dt")) == body["dt"] for r in rows)
                latest[code] = rows
            else:
                rec["dt_honored"] = None if newest is None else newest <= body["dt"]
        out.append(rec)
        if not rec["ok"] and UNSUPPORTED_MARK in str(rec["error"]):
            unsupported.add(api_id)
            log(f"  {api_id}: 모의 미지원 응답 — 이 API는 더 부르지 않음")
            continue
        streak = 0 if rec["ok"] else streak + 1
        d = rec["dates"]
        log(f"  {api_id} {code} {kind}: {rec['pages']}쪽 {rec['rows']}행 {d['oldest'] or '-'}~{d['newest'] or '-'}"
            f"{' (다음 쪽 있음)' if rec['more_pages'] else ''}{'' if rec['ok'] else ' 실패: ' + str(rec['error'])}")
    return out, latest, skipped


# --- 깊은 조회 (--deep) ---------------------------------------------------------------------------------------

def deep_fetch(adapter: Any, guard: Any, code: str, last_day: date, max_pages: int,
               log: Callable[[str], None]) -> tuple[dict, list[dict]]:
    """ka10060 연속 조회를 응답이 더 없을 때까지(상한 max_pages). 끝까지 받았으면 가장 오래된 일자 전날로 1쪽 확인."""
    body = {"dt": last_day.strftime("%Y%m%d"), "stk_cd": code, **INVSR_BODY}
    res: dict[str, Any] = {"request": dict(body), "pages": [], "end": None, "cap_reached": False, "failures": [],
                           "fields": [], "body_keys": [], "before_check": None}
    rows: list[dict] = []
    fields: list[str] = []
    keys: set[str] = set()
    cont: tuple[str | None, str | None] = (None, None)
    streak = 0
    while len(res["pages"]) < max_pages:
        if guard.blocked or fatal_critical(adapter):
            res["end"] = "중단: 허용 밖 API 시도 또는 브로커 치명 오류"
            break
        n = len(res["pages"]) + 1
        try:
            page = _page(adapter, "ka10060", CHART_PATH, body, cont)
            items = (page.body or {}).get(INVSR_LIST)
            if not isinstance(items, list):
                raise BrokerQueryError(f"목록 키 {INVSR_LIST} 없음 또는 목록 아님")
        except BrokerQueryError as exc:
            streak += 1
            res["failures"].append(f"쪽 {n} 시도 실패: {_mask(exc)}")
            if UNSUPPORTED_MARK in str(exc):
                res["end"] = "중단: 모의 미지원"
                break
            if streak >= MAX_CONSECUTIVE_FAILURES:
                res["end"] = f"중단: 연속 실패 {streak}회"
                break
            continue
        streak = 0
        keys.update(str(k) for k in page.body or {})
        good = [i for i in items if isinstance(i, dict)]
        for i in good:
            fields.extend(str(k) for k in i if str(k) not in fields)
        s = date_summary(good)
        res["pages"].append({"rows": len(items), "newest": s["newest"], "oldest": s["oldest"]})
        rows.extend(good)
        if n % DEEP_PROGRESS_EVERY == 0:
            log(f"  쪽 {n}: 누적 {len(rows)}행, 가장 오래된 일자 {date_summary(rows)['oldest'] or '-'}")
        if not ((page.cont_yn or "").upper() == "Y" and page.next_key):
            res["end"] = "더 없음"
            break
        cont = ("Y", page.next_key)
    else:
        res["cap_reached"] = True
        res["end"] = "상한 도달 — 더 과거가 있을 수 있음"
    res["fields"] = fields
    res["body_keys"] = sorted(keys)
    res["dates"] = date_summary(rows)
    res["before_check"] = _before_check(adapter, guard, code, res, rows)
    return res, rows


def _before_check(adapter: Any, guard: Any, code: str, res: dict, rows: list[dict]) -> dict:
    oldest = res["dates"]["oldest"] if rows else None
    if res["end"] != "더 없음" or oldest is None:
        return {"done": False, "result": f"확인 안 함 ({res['end'] if res['end'] != '더 없음' else '행 없음'})"}
    if guard.blocked or fatal_critical(adapter):
        return {"done": False, "result": "확인 안 함 (허용 밖 API 시도 또는 브로커 치명 오류)"}
    prev = (datetime.strptime(oldest, "%Y%m%d").date() - timedelta(days=1)).strftime("%Y%m%d")
    body = {"dt": prev, "stk_cd": code, **INVSR_BODY}
    try:
        page = _page(adapter, "ka10060", CHART_PATH, body, (None, None))
    except BrokerQueryError as exc:
        return {"done": False, "dt": prev, "result": f"확인 실패: {_mask(exc)}"}
    items = (page.body or {}).get(INVSR_LIST)
    if not isinstance(items, list):
        return {"done": False, "dt": prev, "result": f"확인 실패: 목록 키 {INVSR_LIST} 없음"}
    s = date_summary([i for i in items if isinstance(i, dict)])
    if not items:
        return {"done": True, "dt": prev, "rows": 0, "result": "확인 완료 — 이전 자료 없음"}
    return {"done": True, "dt": prev, "rows": len(items), "newest": s["newest"], "oldest": s["oldest"],
            "result": "이전 자료 있음 — 연속 조회가 끝까지 오지 않았을 수 있음(확인 필요)"}


def build_csv(rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> tuple[list[dict], list[str]]:
    """일자 오름차순 행과 문제 목록. 중복 일자는 첫 값 유지 + 문제로 기록, 일자 형식이 아닌 행은 빼고 기록."""
    by_dt: dict[str, Mapping[str, object]] = {}
    dups: list[str] = []
    invalid = 0
    for r in rows:
        dt = str(r.get("dt") or "")
        if not _DT.fullmatch(dt):
            invalid += 1
            continue
        if dt in by_dt:
            dups.append(dt)
            continue
        by_dt[dt] = r
    problems: list[str] = []
    if dups:
        problems.append(f"중복 일자 {len(dups)}건(첫 값 유지): {', '.join(sorted(set(dups))[:5])}")
    if invalid:
        problems.append(f"일자 형식이 아닌 행 {invalid}건(저장 안 함)")
    out = [{f: "" if by_dt[d].get(f) is None else str(by_dt[d].get(f)) for f in fields} for d in sorted(by_dt)]
    return out, problems


def write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def page_stats(counts: Sequence[int]) -> dict:
    def stats(xs: Sequence[int]) -> dict:
        if not xs:
            return {"min": None, "max": None, "mode": None}
        return {"min": min(xs), "max": max(xs), "mode": Counter(xs).most_common(1)[0][0]}
    return {"all_pages": stats(counts), "without_last_page": stats(counts[:-1])}


def csv_name(code: str) -> str:
    return f"{code}-ka10060-amt.csv"


def deep_main(root: Path, ns: argparse.Namespace, code: str, save_dir: Path, environ: Mapping[str, str] | None,
              clock: Clock, now: datetime, connect: Connect) -> int:
    conn = connect(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    if adapter.cfg.rest_url != MOCK_REST or adapter.cfg.ws_url != MOCK_WS:
        adapter.close()
        return _refuse("접속 대상이 키움 모의 도메인이 아님 — 실측하지 않는다")
    last_day = last_closed_day(now, _load_calendar(root))
    print(f"깊은 조회 시작: {code} ka10060 dt={last_day:%Y%m%d}, 상한 {ns.deep_max_pages}쪽 (TR당 {PROBE_INTERVAL}초 "
          f"간격, 최대 약 {ns.deep_max_pages * PROBE_INTERVAL / 60:.1f}분)")
    meta: dict[str, Any] = {"tool": TOOL, "mode": "deep", "basis": BASIS + " / --deep 사용자 추가 지시 2026-10-11",
                            "started_at": now.astimezone(KST).isoformat(timespec="seconds"),
                            "account_alias": adapter.cfg.account_alias, "code": code, "last_day": f"{last_day:%Y%m%d}",
                            "allowed_rest_apis": sorted(DEEP_REST_APIS), "interval_seconds": PROBE_INTERVAL,
                            "max_pages": ns.deep_max_pages, "token": {"issued": False, "revoked": None},
                            "failures": []}
    res: dict[str, Any] | None = None
    rows: list[dict] = []
    try:
        try:
            adapter.tokens.token()
            meta["token"]["issued"] = True
            print("토큰 발급 au10001: 정상")
        except TokenError as exc:
            meta["failures"].append(f"토큰 발급 실패: {_mask(exc)}")
            print(f"토큰 발급 실패 — 조회 안 함: {_mask(exc)}")
        if meta["token"]["issued"]:
            res, rows = deep_fetch(adapter, guard, code, last_day, ns.deep_max_pages, print)
            d = res["dates"]
            print(f"연속 조회 끝: {res['end']} — {len(res['pages'])}쪽 {len(rows)}행 {d['oldest'] or '-'}~"
                  f"{d['newest'] or '-'}")
            print(f"이전 자료 확인(dt={res['before_check'].get('dt', '-')}): {res['before_check']['result']}")
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002
        if meta["token"]["issued"]:
            meta["token"]["revoked"] = revoked
            print(f"토큰 폐기 au10002: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    meta["finished_at"] = clock.now().astimezone(KST).isoformat(timespec="seconds")
    if res is not None:
        counts = [p["rows"] for p in res["pages"]]
        meta.update(request=res["request"], end=res["end"], cap_reached=res["cap_reached"], pages=len(counts),
                    rows_per_page=counts, page_newest_oldest=[(p["newest"], p["oldest"]) for p in res["pages"]],
                    rows_per_call=page_stats(counts), start_date=res["dates"]["oldest"],
                    end_date=res["dates"]["newest"], dates=res["dates"], before_check=res["before_check"],
                    fields=res["fields"], body_keys=res["body_keys"],
                    field_units={f: FIELD_UNITS.get(f, "명세에 없음 — 미확인") for f in res["fields"]})
        meta["failures"] += res["failures"]
        if res["end"] != "더 없음" and not res["cap_reached"]:
            meta["failures"].append(f"연속 조회 {res['end']}")
        if res["before_check"]["result"].startswith(("확인 실패", "이전 자료 있음")):
            meta["failures"].append(f"이전 자료 확인: {res['before_check']['result']}")
    meta["api_calls"] = _api_summary(guard)
    meta["calls"] = sum(s["calls"] for s in meta["api_calls"].values())
    meta["blocked_before_send"] = list(guard.blocked)
    meta["critical"] = [f"{k}: {_mask(m)}" for k, m in adapter.critical]
    meta["failures"] += [f"허용 밖 API 시도(전송 전 차단): {a}" for a in guard.blocked]
    meta["failures"] += [f"브로커 치명: {c}" for c in meta["critical"]]
    save_dir.mkdir(parents=True, exist_ok=True)
    if rows and res is not None:
        out_rows, problems = build_csv(rows, res["fields"])
        meta["failures"] += problems
        target = save_dir / csv_name(code)
        if target.exists():   # 시작 전에 확인했지만 실행 중 생긴 경우도 덮어쓰지 않는다
            meta["failures"].append(f"{target.name}이 이미 있음 — 저장 안 함")
        else:
            write_csv(target, res["fields"], out_rows)
            meta["csv"] = {"file": target.name, "rows": len(out_rows), "columns": list(res["fields"]),
                           "order": "일자 오름차순", "values": "응답 문자열 그대로(부호 유지)"}
            print(f"저장: {target} ({len(out_rows)}행)")
    mpath = save_dir / f"meta-{now.astimezone(KST):%Y%m%d-%H%M%S}.json"
    mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"메타: {mpath}")
    if res is not None and res["cap_reached"]:
        print(f"상한 {ns.deep_max_pages}쪽 도달 — 더 과거가 있을 수 있음 (--deep-max-pages를 늘려 다시 실행)")
    if meta["failures"]:
        print(f"실패·차단 {len(meta['failures'])}건 — 메타 파일의 failures 확인")
        return 1
    return 0


# --- 수집 (--collect, A-090) --------------------------------------------------------------------------------

def read_code_list(path: Path) -> list[str]:
    """종목 목록: `code,...` 머리 줄이 있는 CSV(kospi200-constituents.csv) 또는 한 줄에 코드 하나. 형식이 아니면 ProbeError."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if lines and lines[0].split(",")[0].strip().lower() == "code":
        lines = lines[1:]
    codes: list[str] = []
    for i, line in enumerate(lines, start=1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        code = text.split(",")[0].strip()
        if not _LIST_CODE.fullmatch(code):
            raise ProbeError(f"{path.name}:{i}: 종목코드 형식 아님")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ProbeError(f"{path.name}: 종목코드 없음")
    if len(codes) > COLLECT_MAX_CODES:
        raise ProbeError(f"{path.name}: 종목 {len(codes)}개 — 최대 {COLLECT_MAX_CODES}")
    return codes


def collect_fetch(adapter: Any, guard: Any, code: str, last_day: date, since: date) -> tuple[dict, list[dict]]:
    """한 종목 ka10060 연속 조회. since보다 오래된 일자가 나온 쪽에서 멈춘다. 같은 쪽 3회 연속 실패면 그 종목 실패."""
    body = {"dt": last_day.strftime("%Y%m%d"), "stk_cd": code, **INVSR_BODY}
    floor = since.strftime("%Y%m%d")
    res: dict[str, Any] = {"pages": 0, "error": None, "end": None}
    rows: list[dict] = []
    cont: tuple[str | None, str | None] = (None, None)
    streak = 0
    while res["pages"] < COLLECT_PAGES_MAX:
        if guard.blocked or fatal_critical(adapter):
            res["error"] = "허용 밖 API 시도 또는 브로커 치명 오류"
            return res, rows
        try:
            page = _page(adapter, "ka10060", CHART_PATH, body, cont)
            items = (page.body or {}).get(INVSR_LIST)
            if not isinstance(items, list):
                raise BrokerQueryError(f"목록 키 {INVSR_LIST} 없음 또는 목록 아님")
        except BrokerQueryError as exc:
            streak += 1
            if UNSUPPORTED_MARK in str(exc) or streak >= MAX_CONSECUTIVE_FAILURES:
                res["error"] = _mask(exc)
                return res, rows
            continue
        streak = 0
        res["pages"] += 1
        good = [i for i in items if isinstance(i, dict)]
        rows.extend(good)
        if any(str(i.get("dt") or "") < floor for i in good):
            res["end"] = "since 도달"
            break
        if not ((page.cont_yn or "").upper() == "Y" and page.next_key):
            res["end"] = "더 없음"
            break
        cont = ("Y", page.next_key)
    else:
        res["end"] = f"종목당 상한 {COLLECT_PAGES_MAX}쪽 도달"
    return res, [r for r in rows if str(r.get("dt") or "") >= floor]


def collect_main(root: Path, codes: list[str], since: date, out: Path, codes_file: Path, environ: Mapping[str, str] | None,
                 clock: Clock, now: datetime, last_day: date, connect: Connect) -> int:
    conn = connect(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    if adapter.cfg.rest_url != MOCK_REST or adapter.cfg.ws_url != MOCK_WS:
        adapter.close()
        return _refuse("접속 대상이 키움 모의 도메인이 아님 — 수집하지 않는다")
    print(f"투자자 수집 시작: {len(codes)}종목 ka10060 금액·순매수 {since:%Y%m%d}~{last_day:%Y%m%d} "
          f"(TR당 {PROBE_INTERVAL}초 간격) → {out}")
    meta: dict[str, Any] = {"tool": TOOL, "mode": "collect", "basis": "A-090 (2026-10-11) — 조회만, 주문 없음",
                            "started_at": now.astimezone(KST).isoformat(timespec="seconds"),
                            "account_alias": adapter.cfg.account_alias, "codes_file": str(codes_file),
                            "codes_requested": len(codes), "since": f"{since:%Y%m%d}", "until": f"{last_day:%Y%m%d}",
                            "request": {"dt": f"{last_day:%Y%m%d}", **INVSR_BODY},
                            "allowed_rest_apis": sorted(DEEP_REST_APIS), "interval_seconds": PROBE_INTERVAL,
                            "columns": list(COLLECT_COLUMNS),
                            "field_units": {f: FIELD_UNITS.get(f, "명세에 없음 — 미확인") for f in COLLECT_COLUMNS},
                            "token": {"issued": False, "revoked": None}, "ok": 0, "per_code": {}, "failed": [],
                            "not_queried": []}
    out.mkdir(parents=True, exist_ok=True)
    try:
        try:
            adapter.tokens.token()
            meta["token"]["issued"] = True
        except TokenError as exc:
            meta["failed"].append({"code": "-", "reason": f"토큰 발급 실패: {_mask(exc)}"})
            meta["not_queried"] = list(codes)
        streak = 0
        for idx, code in enumerate(codes if meta["token"]["issued"] else []):
            if guard.blocked or fatal_critical(adapter):
                meta["not_queried"] = codes[idx:]
                print("중단: 허용 밖 API 시도 또는 브로커 치명 오류 — 나머지 조회 안 함")
                break
            res, rows = collect_fetch(adapter, guard, code, last_day, since)
            if res["error"] is None and rows:
                out_rows, problems = build_csv(rows, COLLECT_COLUMNS)
                write_csv(out / f"{code}.csv", COLLECT_COLUMNS, out_rows)
                meta["per_code"][code] = {"rows": len(out_rows), "pages": res["pages"], "first": out_rows[0]["dt"],
                                          "last": out_rows[-1]["dt"], "end": res["end"], "problems": problems}
                meta["ok"] += 1
                streak = 0
                print(f"  {idx + 1:>3}/{len(codes)} {code}: {out_rows[0]['dt']}~{out_rows[-1]['dt']} {len(out_rows)}행 "
                      f"{res['pages']}쪽", flush=True)
                continue
            reason = res["error"] or f"{since:%Y%m%d} 이후 행 없음"
            meta["failed"].append({"code": code, "reason": reason, "pages": res["pages"]})
            print(f"  {idx + 1:>3}/{len(codes)} {code}: 실패 — {reason}", flush=True)
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES or UNSUPPORTED_MARK in reason:
                meta["not_queried"] = codes[idx + 1:]
                print(f"중단: 연속 {streak}종목 실패 — 나머지 {len(meta['not_queried'])}종목 조회 안 함")
                break
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002
        if meta["token"]["issued"]:
            meta["token"]["revoked"] = revoked
            print(f"토큰 폐기 au10002: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    meta["finished_at"] = clock.now().astimezone(KST).isoformat(timespec="seconds")
    meta["api_calls"] = _api_summary(guard)
    meta["calls"] = sum(s["calls"] for s in meta["api_calls"].values())
    meta["blocked_before_send"] = list(guard.blocked)
    meta["critical"] = [f"{k}: {_mask(m)}" for k, m in adapter.critical]
    firsts = sorted(v["first"] for v in meta["per_code"].values())
    meta["range"] = {"earliest_first": firsts[0] if firsts else None, "latest_first": firsts[-1] if firsts else None,
                     "late_start_codes": sorted(c for c, v in meta["per_code"].items() if v["first"] > meta["since"][:6]
                                                + "31")}
    mpath = out / f"meta-{now.astimezone(KST):%Y%m%d-%H%M%S}.json"
    mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"요약: 종목 {len(codes)} 성공 {meta['ok']} 실패 {len(meta['failed'])} 미조회 {len(meta['not_queried'])}, "
          f"호출 {meta['calls']}회, 저장 {out}, 메타 {mpath.name}")
    bad = meta["failed"] or meta["not_queried"] or meta["blocked_before_send"] or meta["critical"]
    return 1 if bad else 0


# --- 진입점 ----------------------------------------------------------------------------------------------

Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=PROBE_INTERVAL, allowed_apis=INVESTOR_REST_APIS)


def _deep_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=PROBE_INTERVAL, allowed_apis=DEEP_REST_APIS)


def _refuse(msg: str) -> int:
    print(f"투자자 실측 거부(연결 없음): {msg}")
    return 2


def _args(argv: Sequence[str]) -> argparse.Namespace | None:
    p = argparse.ArgumentParser(prog=TOOL, description="키움 모의 투자자별 일별 TR 실측 (읽기 전용, T-034, K-U42)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--codes", default=None, help=f"종목코드 쉼표 목록(최대 {MAX_CODES}, 기본 {DEFAULT_CODES})")
    p.add_argument("--max-pages", type=int, default=PAGES_DEFAULT, help=f"연속 조회 최대 쪽 (1~{PAGES_MAX})")
    p.add_argument("--back-days", default=BACK_DAYS_DEFAULT, help="ka10060 dt를 며칠 전으로 옮길지 쉼표 목록")
    p.add_argument("--compare", default=None, help="이전 결과 json — 확정 시각 해시를 일자별로 비교")
    p.add_argument("--deep", action="store_true", help="1종목 ka10060 끝까지 연속 조회 + 값 저장")
    p.add_argument("--deep-max-pages", type=int, default=DEEP_PAGES_DEFAULT,
                   help=f"--deep 연속 조회 안전 상한 (1~{DEEP_PAGES_MAX})")
    p.add_argument("--save-dir", default=DEEP_SAVE_DIR, help="--deep 저장 폴더 (저장소 기준 상대 경로 가능)")
    p.add_argument("--collect", action="store_true", help="[A-090] 종목 목록 × ka10060 since 이후 수집 + 종목별 CSV")
    p.add_argument("--codes-file", default=None, help="--collect 종목 목록 (code,... CSV 또는 한 줄에 하나)")
    p.add_argument("--since", default=COLLECT_SINCE_DEFAULT, help="--collect 첫 날짜 YYYY-MM-DD (그 이전 쪽은 받지 않음)")
    p.add_argument("--out", default=None, help="--collect 저장 폴더 (기본 var/backtest/data/kiwoom-investor-<마지막 거래일>)")
    try:
        return p.parse_args(list(argv))
    except SystemExit:
        return None


def _resolve(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def investor_main(root: Path, argv: Sequence[str], environ: Mapping[str, str] | None = None, *,
                  clock: Clock | None = None, connect: Connect | None = None) -> int:
    ns = _args(argv)
    if ns is None:
        return 2
    if ns.mode != "PAPER":
        return _refuse(f"--mode {ns.mode} — 이 도구는 PAPER(키움 모의 조회)에서만 실행한다. LIVE·BACKTEST는 연결하지 않는다")
    if not 1 <= ns.max_pages <= PAGES_MAX:
        return _refuse(f"--max-pages {ns.max_pages} — 1~{PAGES_MAX}")
    if not 1 <= ns.deep_max_pages <= DEEP_PAGES_MAX:
        return _refuse(f"--deep-max-pages {ns.deep_max_pages} — 1~{DEEP_PAGES_MAX}")
    try:
        back_days = parse_back_days(ns.back_days)
        codes = parse_codes(ns.codes or DEFAULT_CODES)
        previous = load_compare(_resolve(root, ns.compare)) if ns.compare and not ns.deep else None
    except ProbeError as exc:
        return _refuse(str(exc))
    save_dir = _resolve(root, ns.save_dir)
    collect_codes: list[str] = []
    since = date.min
    if ns.collect:
        if ns.deep or ns.codes or ns.compare:
            return _refuse("--collect는 --deep·--codes·--compare와 함께 쓸 수 없음 (종목은 --codes-file)")
        if not ns.codes_file:
            return _refuse("--collect에는 --codes-file이 필요")
        try:
            since = date.fromisoformat(ns.since)
        except ValueError:
            return _refuse(f"--since {ns.since} — YYYY-MM-DD 형식 아님")
        try:
            collect_codes = read_code_list(_resolve(root, ns.codes_file))
        except (OSError, ProbeError) as exc:
            return _refuse(f"--codes-file: {exc}")
    if ns.deep:
        if ns.codes is None or len(codes) != 1:
            return _refuse("--deep는 --codes로 정확히 1종목을 지정할 때만 실행한다")
        if ns.compare:
            return _refuse("--deep와 --compare는 함께 쓸 수 없음")
        if (save_dir / csv_name(codes[0])).exists():
            return _refuse(f"{save_dir / csv_name(codes[0])}가 이미 있음 — 덮어쓰지 않는다. 다른 --save-dir을 지정")
    clock = clock or SystemClock()
    now = clock.now()
    hours = market_hours_refusal(now, _load_calendar(root))
    if hours:
        return _refuse(hours)
    base = root / "var" / "paper"
    running = _running_refusal(base, now)
    if running:
        return _refuse(running)
    if ns.deep:
        return deep_main(root, ns, codes[0], save_dir, environ, clock, now, connect or _deep_connect)
    if ns.collect:
        last_day = last_closed_day(now, _load_calendar(root))
        if since > last_day:
            return _refuse(f"--since {since}가 마지막 거래일 {last_day}보다 뒤")
        out = _resolve(root, ns.out) if ns.out else \
            root / "var" / "backtest" / "data" / f"kiwoom-investor-{last_day:%Y%m%d}"
        clash = [c for c in collect_codes if (out / f"{c}.csv").exists()]
        if clash:
            return _refuse(f"{out}에 이미 CSV가 있음({', '.join(clash[:5])}) — 덮어쓰지 않는다. 다른 --out 지정")
        return collect_main(root, collect_codes, since, out, _resolve(root, ns.codes_file), environ, clock, now,
                            last_day, connect or _deep_connect)

    conn = (connect or _default_connect)(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    if adapter.cfg.rest_url != MOCK_REST or adapter.cfg.ws_url != MOCK_WS:
        adapter.close()
        return _refuse("접속 대상이 키움 모의 도메인이 아님 — 실측하지 않는다")
    last_day = last_closed_day(now, _load_calendar(root))
    plan = investor_plan(codes, last_day, ns.max_pages, back_days)
    calls_max = sum(p[5] for p in plan)
    print(f"투자자 실측 시작: 종목 {', '.join(codes)} / 마지막 거래일 {last_day:%Y%m%d} / 최대 {calls_max}회 조회 "
          f"(TR당 {PROBE_INTERVAL}초 간격, 예상 최대 약 {calls_max * PROBE_INTERVAL / 60:.1f}분)")
    report: dict[str, Any] = {
        "tool": TOOL, "basis": BASIS, "started_at": now.astimezone(KST).isoformat(timespec="seconds"),
        "account_alias": adapter.cfg.account_alias, "codes": codes, "last_day": f"{last_day:%Y%m%d}",
        "settings": {"max_pages": ns.max_pages, "back_days": back_days, "interval_seconds": PROBE_INTERVAL,
                     "ka10060": dict(INVSR_BODY), "ka10045": {"strt_dt": KA10045_START, **ORGN_BODY},
                     "allowed_rest_apis": sorted(INVESTOR_REST_APIS)},
        "token": {"issued": False, "expires_at": None, "revoked": None},
        "rest": [], "rest_skipped": [], "failures": [],
    }
    latest: dict[str, list[dict]] = {}
    try:
        try:
            adapter.tokens.token()
            exp = adapter.tokens.expires_at
            report["token"].update(issued=True, expires_at=exp.astimezone(KST).isoformat() if exp else None)
            print("토큰 발급 au10001: 정상")
        except TokenError as exc:
            report["failures"].append(f"토큰 발급 실패: {_mask(exc)}")
            print(f"토큰 발급 실패 — 조회 안 함: {_mask(exc)}")
        if report["token"]["issued"]:
            print("REST 조회:")
            report["rest"], latest, report["rest_skipped"] = run_queries(adapter, guard, plan, print)
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002
        if report["token"]["issued"]:
            report["token"]["revoked"] = revoked
            print(f"토큰 폐기 au10002: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    report["finished_at"] = clock.now().astimezone(KST).isoformat(timespec="seconds")
    _finality(report, codes, latest, now)
    if previous is not None:
        report["compare"] = compare_finality(previous, report["finality"])
        print(f"확정 시각 비교 (이전 {report['compare']['previous_collected_at']} → 이번 "
              f"{report['finality']['collected_at']}):")
        for c in report["compare"]["dates"]:
            print(f"  {c['code']} {c['dt']}: 투자자 값 {c['investor']} / 행 전체 {c['row']}")
    report["api_calls"] = _api_summary(guard)
    report["blocked_before_send"] = list(guard.blocked)
    report["critical"] = [f"{k}: {_mask(m)}" for k, m in adapter.critical]
    _collect_failures(report)
    paths = write_report(base / "probe", report, now)
    print(f"결과: {paths[0]}")
    print(f"요약: {paths[1]}")
    if report["failures"]:
        print(f"실패·차단 {len(report['failures'])}건 — 요약 파일의 '실패' 절 확인")
        return 1
    return 0


def _finality(report: dict, codes: Sequence[str], latest: Mapping[str, list[dict]], now: datetime) -> None:
    fin: dict[str, Any] = {"collected_at": now.astimezone(KST).isoformat(timespec="seconds"),
                           "requested_dt": report["last_day"]}
    checks: dict[str, Any] = {}
    for code in codes:
        rows = latest.get(code, [])
        fin[code] = {"dates": finality_dates(rows),
                     "requested_dt_row_present": any(str(r.get("dt")) == report["last_day"] for r in rows)}
        newest = max((r for r in rows if _DT.fullmatch(str(r.get("dt") or ""))), key=lambda r: str(r["dt"]),
                     default=None)
        checks[code] = sum_check(newest) if newest is not None else {"dt": None, "note": "행 없음"}
        if fin[code]["dates"]:
            print(f"확정 시각 해시: {code} {'·'.join(d['dt'] for d in fin[code]['dates'])} "
                  f"(합계 범주 total={checks[code].get('total')})")
    report["finality"] = fin
    report["sum_check"] = checks


def _collect_failures(report: dict) -> None:
    f = report["failures"]
    f.extend(f"{r['api']} {r['code']} {r['kind']}: {r['error']}" for r in report["rest"] if not r["ok"])
    if report["rest_skipped"]:
        f.append(f"조회 안 함 {len(report['rest_skipped'])}건 (중단 또는 모의 미지원 API)")
    if report["blocked_before_send"]:
        f.append(f"허용 밖 API 시도(전송 전 차단): {', '.join(report['blocked_before_send'])}")
    f.extend(f"브로커 치명: {c}" for c in report["critical"])


# --- 기록 ------------------------------------------------------------------------------------------------

def write_report(directory: Path, report: dict, now: datetime) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(KST):%Y%m%d-%H%M%S}-investor-probe"
    jpath, mpath = directory / f"{stem}.json", directory / f"{stem}.md"
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    mpath.write_text(render_md(report), encoding="utf-8")
    return jpath, mpath


def render_md(report: dict) -> str:
    L: list[str] = [
        f"# 키움 모의 투자자별 일별 TR 실측 {report['started_at'][:10]}", "",
        f"- 근거: {report['basis']}",
        f"- 실행: {report['started_at']} ~ {report.get('finished_at', '-')} KST, 계좌 별칭 {report['account_alias']}, "
        f"종목 {', '.join(report['codes'])}, 마지막 거래일 {report['last_day']}",
        f"- 설정: {json.dumps(report['settings'], ensure_ascii=False)}",
        f"- 토큰: 발급 {report['token']['issued']}, 폐기 {report['token']['revoked']}",
        "- 이 파일은 형태만 담는다(필드 이름·쪽별 행 수·일자 범위·반환 코드·해시·범주). 가격·금액 값은 저장하지 않았다.",
        "- 판정(K-U42)은 사람이 이 결과로 하고 kiwoom-kr.md에 \"② 모의 실측\"으로 적는다.", "",
        "## 조회 (과거 제공 범위·1회 행 수)", "",
        "| 종목 | api | 종류 | 요청 일자 | 쪽 | 쪽별 행 수 | 다음 쪽 | 일자 범위 | 순서 | 결과 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["rest"]:
        q, d = r["request"], r["dates"]
        when = q.get("dt") or (f"{q.get('strt_dt')}~{q.get('end_dt')}" if "strt_dt" in q else "-")
        L.append(f"| {r['code']} | {r['api']} | {r['kind']} | {when} | {r['pages']} | "
                 f"{', '.join(map(str, r['rows_per_page'])) or '-'} | {'Y' if r['more_pages'] else 'N'} | "
                 f"{d['oldest'] or '-'} ~ {d['newest'] or '-'} | {d['order']} | "
                 f"{'정상' if r['ok'] else '실패: ' + str(r['error'])} |")
    seen: set[str] = set()
    for r in report["rest"]:
        if r["ok"] and r["api"] not in seen:
            seen.add(r["api"])
            L.append(f"\n- {r['api']} 응답 필드: {', '.join(r['fields']) or '-'} / 최상위 키: {', '.join(r['body_keys'])}")
    L += ["", "## 확정 시각 판단용 해시 (K-U42)", "",
          f"- 수집 시각 {report['finality']['collected_at']}, 요청 dt {report['finality']['requested_dt']}. "
          "투자자 값 해시 = ka10060 투자자 13필드, 행 전체 해시 = dt 외 모든 필드 (sha256 앞 16자).", "",
          "| 종목 | 일자 | 투자자 값 해시 | 행 전체 해시 | 빠진 투자자 필드 |", "|---|---|---|---|---|"]
    for code in report["codes"]:
        for d in report["finality"][code]["dates"]:
            L.append(f"| {code} | {d['dt']} | {d['investor_hash']} | {d['row_hash']} | "
                     f"{', '.join(d['missing_fields']) or '-'} |")
    L += ["", "## 투자자 합계 검사 (최근 일자, 차이 크기 범주 — 단위 백만원)", "",
          "| 종목 | 일자 | 전체 합 | 국가 제외 합 | 기관계−세부 | 기관계−(세부+국가) | 해석 불가 필드 |",
          "|---|---|---|---|---|---|---|"]
    for code in report["codes"]:
        c = report["sum_check"][code]
        L.append(f"| {code} | {c.get('dt') or '-'} | {c.get('total', '-')} | {c.get('total_without_natn', '-')} | "
                 f"{c.get('orgn_vs_parts', '-')} | {c.get('orgn_vs_parts_with_natn', '-')} | "
                 f"{', '.join(c.get('unparsable', [])) or c.get('note', '-')} |")
    if "compare" in report:
        cmp = report["compare"]
        L += ["", "## 확정 시각 비교 (--compare)", "",
              f"- 이전 수집 {cmp['previous_collected_at']} → 이번 수집 {cmp['current_collected_at']}", "",
              "| 종목 | 일자 | 투자자 값 | 행 전체 |", "|---|---|---|---|"]
        L += [f"| {c['code']} | {c['dt']} | {c['investor']} | {c['row']} |" for c in cmp["dates"]]
    L += ["", "## API 호출 (HTTP 경계 기록)", "", "| api-id | 호출 | 정상 | 코드별 | 마지막 실패 |", "|---|---|---|---|---|"]
    L += [f"| {a} | {s['calls']} | {s['ok']} | {s['codes']} | {s['last_error'] or '-'} |"
          for a, s in sorted(report["api_calls"].items())]
    L += ["", f"- 전송 전 차단(허용 밖 API): {', '.join(report['blocked_before_send']) or '없음'}", "", "## 실패", ""]
    L += [f"- {f}" for f in report["failures"]] or ["- 없음"]
    return "\n".join(L) + "\n"
