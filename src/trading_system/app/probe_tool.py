"""키움 모의 서버 조회·구독 실측 도구 — 읽기 전용 (CP-032 D-180, 승인 A-085 Q-2).

목적: K-U30(ka10080 분봉 과거 제공 기간)·K-U31(1회 건수·`cntr_tm` 형태)·K-U32(ka10080·ka10079·0B·0D 모의 지원)·
K-U33(ka10079 제공 범위)·K-U34/K-U35 일부(0B·0D 등록·해지 응답, 등록 직후 수신 여부). 장중 수신 빈도는 장외 실측으로
알 수 없다(D-180) → PV-18.

사람이 직접 실행한다(또는 사용자가 대화로 지시할 때 에이전트, D-159 방식). 주문 API는 부르지 않는다.

순서
1. 연결 전 검사 — 하나라도 어긋나면 네트워크 객체를 만들지 않는다.
   - `--mode PAPER`만 허용.
   - 장외 전용: KRX 거래일(달력, 달력이 없으면 평일을 거래일로 본다)의 06:30~17:00 KST(늦게 끝나는 날은 마감 + 90분)이면 거부.
   - PAPER 엔진 실행 잠금(run.lock)·자동 운영 잠금(autopilot.lock)·하트비트(30초 이내)가 있으면 거부.
   - 연결 조건(connect_paper): TRADE_MODE=PAPER, .env.paper, connection_approved = true, 모의 도메인.
2. REST 조회 (TR당 1.2초 간격 — 모의 TR당 1초 1회(E4)보다 보수적). HTTP 경계에서 허용 목록
   {au10001 토큰 발급, au10002 토큰 폐기, ka10080, ka10079} 밖의 api-id는 전송 전에 막는다.
   - ka10080 종목 × `tic_scope` 1·5, `upd_stkpc_tp`=1:
     (a) `base_dt`=오늘에서 `cont-yn`/`next-key` 연속 조회 최대 --max-pages쪽,
     (b) `base_dt`를 --back-days만큼 과거로 옮겨 각 1쪽 (그 날짜가 아직 제공되는지 — 마지막 반환일).
   - ka10079 첫 종목, 1틱, 최대 2쪽 (제공 범위만).
3. WebSocket (기존 세션 코드 KiwoomRealtime을 그대로 쓰는 1회용 세션, 계좌당 1개): 로그인 → 0B·0D 각 첫 종목 REG →
   응답 → --ws-seconds 동안 수신 건수 → REMOVE 응답 → 종료. 보내는 메시지는 LOGIN·REG·REMOVE·PING 응답뿐이다.
4. 끝나면 토큰 폐기(au10002).

기록: `var/paper/probe/<YYYYMMDD-HHMMSS>-probe.json`·`.md`. 응답 원문·가격·수량 값은 저장하지 않는다. 필드 이름·건수·
시각(`cntr_tm`) 범위와 앞뒤 몇 개·반환 코드·서버 문구(가림 처리)만 남긴다. 토큰·앱키·계좌번호는 기록·출력하지 않는다.
판정(K-U30 등 해소 여부)은 사람이 결과를 보고 kiwoom-kr에 "② 모의 실측, 일자"로 적는다.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom.adapter import MOCK_REST, MOCK_WS
from trading_system.broker.kiwoom.errors import normalize_code
from trading_system.broker.kiwoom.realtime import KiwoomRealtime
from trading_system.broker.kiwoom.token import TokenError, TokenManager
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.errors import MarketDataError
from trading_system.marketdata.kiwoom_feed import CHART_PATH
from trading_system.storage.jsonlog import Masker

from .instance_lock import AUTOPILOT_LOCK, instance_running
from .preflight import connect_paper, engine_alive

# --- 허용 목록·설정값 -----------------------------------------------------------------------------------

PROBE_REST_APIS = frozenset({"au10001", "au10002", "ka10080", "ka10079"})   # HTTP 경계 허용 목록 (A-085 Q-2)
WS_ALLOWED_TRNM = frozenset({"LOGIN", "REG", "REMOVE", "PING"})             # PING은 서버 PING을 그대로 돌려줄 때만
WS_TYPES = ("0B", "0D")
WS_GROUP = "1"
PROBE_INTERVAL = 1.2            # TR당 호출 간격(초) — 모의 TR당 1초 1회(E4)보다 보수적
MIN_SCOPES = ("1", "5")         # ka10080 tic_scope (분)
TICK_SCOPE = "1"                # ka10079 tic_scope (틱)
TICK_PAGES = 2                  # ka10079는 제공 범위만 (1~2회)
UPD_STKPC_TP = "1"              # 수정주가 (D-177: 백테스트·PAPER 판정 같은 값)
MIN_LIST_KEY, TICK_LIST_KEY = "stk_min_pole_chart_qry", "stk_tic_chart_qry"
MAX_CODES = 3
PAGES_DEFAULT, PAGES_MAX = 5, 20
BACK_DAYS_DEFAULT = "30,90,180,365,730"
BACK_DAYS_MAX_ITEMS, BACK_DAYS_LIMIT = 8, 3650
WS_SECONDS_DEFAULT, WS_SECONDS_MAX = 10, 60
ACK_TIMEOUT = 10.0
MAX_CONSECUTIVE_FAILURES = 3
SAMPLE = 3                      # cntr_tm 앞·뒤 표본 개수
DAYS_SHOWN = 5                  # 날짜별 첫·끝 시각을 보여줄 최근 날짜 수
MESSAGE_MAX = 160
# 장외 판정 (KST). 시작: 예약 작업(06:40 시계 점검, 06:45 자동 운영 — scripts/schedule-register.ps1) 전.
# 끝: 자동 운영 종료 절차(Schedule.stop_at 15:45) + 마감 대기(close_wait 30분) + 검토·백업 여유.
BLOCK_START, BLOCK_END = dtime(6, 30), dtime(17, 0)
LATE_CLOSE_MARGIN = timedelta(minutes=90)   # 마감이 늦은 날(특별 개장 시간)은 마감 + 90분까지 거부

_CODE = re.compile(r"[0-9]{6}", re.ASCII)
_TS = re.compile(r"[0-9]{14}", re.ASCII)


class ProbeError(RuntimeError):
    pass


def _mask(value: object) -> str:
    return Masker().text(str(value or ""))[:MESSAGE_MAX]


# --- 장외 판정 (순수 함수) -------------------------------------------------------------------------------

def market_hours_refusal(now: datetime, calendar: KrxCalendar | None) -> str | None:
    """KRX 거래일 장중(전후 여유 포함)이면 거부 사유. 달력이 없거나 모르는 날은 평일을 거래일로 본다(보수적)."""
    local = now.astimezone(KST)
    day = local.date()
    session = calendar.session(day) if calendar is not None else None
    if session is None:
        trading = day.weekday() < 5
        basis = "달력 없음 — 평일을 거래일로 봄"
    else:
        trading = session.is_open
        basis = "KRX 달력"
    if not trading:
        return None
    end = datetime.combine(day, BLOCK_END, tzinfo=KST)
    if session is not None and session.regular_close is not None:
        end = max(end, session.regular_close.astimezone(KST) + LATE_CLOSE_MARGIN)
    start = datetime.combine(day, BLOCK_START, tzinfo=KST)
    if start <= local < end:
        return (f"거래일({basis}) {local:%H:%M} KST — 장외 전용 도구: {BLOCK_START:%H:%M}~{end:%H:%M} KST에는 실행하지 "
                f"않는다(PAPER run·자동 운영과 토큰·WS 세션 충돌 방지, D-180)")
    return None


# --- cntr_tm 요약 (순수 함수, 값 원문 대신 형태만) --------------------------------------------------------

def summarize_times(rows: Sequence[Mapping[str, object]], key: str = "cntr_tm") -> dict:
    """응답 행의 시각 필드 → 형태 요약. 가격·수량 값은 보지 않는다."""
    raw = [str(r.get(key) or "") for r in rows]
    valid = [v for v in raw if _TS.fullmatch(v)]
    out: dict[str, Any] = {"field": key, "rows": len(raw), "valid": len(valid), "invalid": len(raw) - len(valid),
                           "first": raw[:SAMPLE], "last": raw[-SAMPLE:] if len(raw) > SAMPLE else []}
    if not valid:
        return out
    if all(a >= b for a, b in zip(valid, valid[1:])):
        order = "최신→과거"
    elif all(a <= b for a, b in zip(valid, valid[1:])):
        order = "과거→최신"
    else:
        order = "섞임"
    asc = sorted(valid)
    by_day: dict[str, list[str]] = {}
    for v in asc:
        by_day.setdefault(v[:8], []).append(v[8:])
    steps: Counter[int] = Counter()
    for a, b in zip(asc, asc[1:]):
        if a[:8] == b[:8]:
            steps[_seconds(b[8:]) - _seconds(a[8:])] += 1
    days = sorted(by_day)
    out.update({
        "order": order, "min": asc[0], "max": asc[-1], "duplicates": len(valid) - len(set(valid)),
        "seconds_not_00": sum(1 for v in valid if v[12:] != "00"),
        "days": len(days),
        "per_day": [{"date": d, "rows": len(by_day[d]), "first_hms": by_day[d][0], "last_hms": by_day[d][-1]}
                    for d in days[-DAYS_SHOWN:]],
        "step_seconds": {str(k): n for k, n in steps.most_common(5)},
    })
    return out


def _seconds(hms: str) -> int:
    return int(hms[:2]) * 3600 + int(hms[2:4]) * 60 + int(hms[4:6])


# --- REST 조회 ------------------------------------------------------------------------------------------

def chart_pages(adapter: Any, api_id: str, body: dict, list_key: str, max_pages: int) -> dict:
    """한 요청을 연속 조회로 최대 max_pages쪽 받고 형태만 기록한다. 실패는 기록하고 예외를 올리지 않는다.

    adapter._query_page를 쓴다: 쪽마다 `cont-yn`·건수를 남기기 위해서다(query_raw는 쪽 경계를 감춘다).
    재시도(호출 제한·인증)·토큰·호출 간격은 어댑터 그대로."""
    rec: dict[str, Any] = {"api": api_id, "request": dict(body), "pages": 0, "rows_per_page": [], "more_pages": False,
                           "ok": False, "error": None, "fields": [], "body_keys": []}
    rows: list[dict] = []
    fields: set[str] = set()
    body_keys: set[str] = set()
    cont: tuple[str | None, str | None] = (None, None)
    for _ in range(max_pages):
        try:
            with call_priority(Priority.MARKET):
                page = adapter._query_page(api_id, body, cont, CHART_PATH)  # noqa: SLF001 — 쪽 단위 실측
        except BrokerQueryError as exc:
            rec["error"] = _mask(exc)
            break
        rec["pages"] += 1
        page_body = page.body or {}
        body_keys.update(str(k) for k in page_body)
        items = page_body.get(list_key)
        if not isinstance(items, list):
            rec["error"] = f"목록 키 {list_key} 없음 또는 목록 아님"
            break
        good = [i for i in items if isinstance(i, dict)]
        rec["rows_per_page"].append(len(items))
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
    rec["body_keys"] = sorted(body_keys)
    rec["times"] = summarize_times(rows)
    return rec


def _base_dt_honored(rec: dict) -> bool | None:
    """반환된 가장 최신 날짜가 요청 base_dt 이하인지 (기준일자가 지켜지는지). 행이 없으면 None."""
    top = rec.get("times", {}).get("max")
    base = rec["request"].get("base_dt")
    if not top or not base:
        return None
    return top[:8] <= base


def rest_plan(codes: Sequence[str], today: date, max_pages: int, back_days: Sequence[int]) -> list[tuple[str, str, dict, str, int]]:
    """(종류, api-id, 요청 본문, 목록 키, 최대 쪽) 목록. 순서대로 부른다."""
    plan: list[tuple[str, str, dict, str, int]] = []
    for code in codes:
        for scope in MIN_SCOPES:
            base = {"stk_cd": code, "tic_scope": scope, "upd_stkpc_tp": UPD_STKPC_TP}
            plan.append(("paging", "ka10080", {**base, "base_dt": today.strftime("%Y%m%d")}, MIN_LIST_KEY, max_pages))
            for days in back_days:
                d = today - timedelta(days=days)
                plan.append((f"base_dt-{days}d", "ka10080", {**base, "base_dt": d.strftime("%Y%m%d")}, MIN_LIST_KEY, 1))
    plan.append(("tick", "ka10079", {"stk_cd": codes[0], "tic_scope": TICK_SCOPE, "upd_stkpc_tp": UPD_STKPC_TP},
                 TICK_LIST_KEY, TICK_PAGES))
    return plan


FATAL_CRITICAL = frozenset({"TERMINAL", "MODE_MISMATCH", "CORPORATE"})   # 환경 문제 — 모든 실측 중단
UNSUPPORTED_MARK = "MOCK_UNSUPPORTED"   # 모의 미지원(8104·RC9000)은 실측 결과 — 그 API만 더 부르지 않는다


def fatal_critical(adapter: Any) -> list[str]:
    return [kind for kind, _ in adapter.critical if kind in FATAL_CRITICAL]


def _label(kind: str, api_id: str, body: Mapping[str, object]) -> str:
    return f"{api_id} {body.get('stk_cd')} 범위{body.get('tic_scope')} {kind}"


def run_rest(adapter: Any, guard: Any, plan: Sequence[tuple[str, str, dict, str, int]],
             log: Callable[[str], None]) -> tuple[list[dict], list[str]]:
    """계획대로 조회. 연속 실패·허용 밖 API 시도·환경 치명 오류면 나머지는 부르지 않는다. 모의 미지원 API는 그 API만 건너뛴다."""
    out: list[dict] = []
    skipped: list[str] = []
    unsupported: set[str] = set()
    streak = 0
    for i, (kind, api_id, body, key, pages) in enumerate(plan):
        if guard.blocked or fatal_critical(adapter) or streak >= MAX_CONSECUTIVE_FAILURES:
            skipped += [_label(k, a, b) for k, a, b, _, _ in plan[i:]]
            why = "허용 밖 API 시도" if guard.blocked else "브로커 치명 오류" if fatal_critical(adapter) else "연속 실패"
            log(f"  중단: {why} — 나머지 {len(plan) - i}건 조회 안 함")
            break
        if api_id in unsupported:
            skipped.append(_label(kind, api_id, body) + " (모의 미지원)")
            continue
        rec = chart_pages(adapter, api_id, body, key, pages)
        rec["kind"] = kind
        if api_id == "ka10080":
            rec["base_dt_honored"] = _base_dt_honored(rec)
        out.append(rec)
        if not rec["ok"] and UNSUPPORTED_MARK in str(rec["error"]):
            unsupported.add(api_id)
            log(f"  {api_id}: 모의 미지원 응답 — 이 API는 더 부르지 않음")
            continue
        streak = 0 if rec["ok"] else streak + 1
        t = rec["times"]
        span = f"{t.get('min', '-')}~{t.get('max', '-')}"
        log(f"  {api_id} {body['stk_cd']} 범위{body['tic_scope']} {kind}: {rec['pages']}쪽 {rec['rows']}행 {span}"
            f"{' (다음 쪽 있음)' if rec['more_pages'] else ''}{'' if rec['ok'] else ' 실패: ' + str(rec['error'])}")
    return out, skipped


# --- WebSocket (기존 KiwoomRealtime 위의 1회용 세션) ---------------------------------------------------------

class ProbeWs(KiwoomRealtime):
    """스레드·재접속 없이 한 번 접속해 LOGIN → REG → 수신 대기 → REMOVE만 한다. 값은 저장하지 않는다(필드 이름·건수만).

    전송은 KiwoomRealtime._send를 거치며, 이 클래스가 trnm 허용 목록(WS_ALLOWED_TRNM)과 항목(0B·0D)을 먼저 검사한다."""

    def __init__(self, ws_url: str, tokens: TokenManager, clock: Clock, *, connect: Callable[..., Any] | None = None,
                 ack_timeout: float = ACK_TIMEOUT, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.stats: dict[str, dict[str, Any]] = {t: {"count": 0, "fields": set(), "items": set(), "first_at": None,
                                                     "last_at": None} for t in WS_TYPES}
        self.other_real: Counter[str] = Counter()
        self.other_control: list[dict] = []
        self.sent_trnm: list[str] = []
        self._mono = monotonic
        super().__init__(ws_url, tokens, clock, registrations=[],
                         handlers={t: self._counter(t) for t in WS_TYPES}, connect=connect,
                         login_timeout=ack_timeout)

    def _counter(self, rtype: str) -> Callable[[str, dict], None]:
        def h(item: str, values: dict) -> None:
            st = self.stats[rtype]
            at = self._clock.now().astimezone(KST).isoformat(timespec="seconds")
            st["count"] += 1
            st["fields"].update(str(k) for k in values)
            st["items"].add(str(item)[:12])
            st["first_at"] = st["first_at"] or at
            st["last_at"] = at
        return h

    def _send(self, obj: object) -> None:
        trnm = str(obj.get("trnm", "")).upper() if isinstance(obj, dict) else str(obj).strip().upper()
        if trnm not in WS_ALLOWED_TRNM:
            raise ProbeError(f"WS 전송 거부: 허용 목록 밖 trnm '{trnm[:20]}'")
        if trnm in ("REG", "REMOVE") and isinstance(obj, dict):
            types = {t for d in obj.get("data") or [] for t in d.get("type") or []}
            if not types <= set(WS_TYPES):
                raise ProbeError(f"WS 전송 거부: 허용 목록 밖 실시간 항목 {sorted(types - set(WS_TYPES))}")
        self.sent_trnm.append(trnm)
        super()._send(obj)

    def _note(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        name = str(msg.get("trnm", "")).upper()
        if name == "REAL":
            for d in msg.get("data") or []:
                if isinstance(d, dict) and str(d.get("type")) not in WS_TYPES:
                    self.other_real[str(d.get("type"))[:8]] += 1
        elif name != "PING" and len(self.other_control) < 20:
            self.other_control.append({"trnm": name[:20], **ack_record(msg)})

    def _await(self, trnm: str, timeout: float) -> dict:
        deadline = self._mono() + timeout
        while True:
            left = deadline - self._mono()
            if left <= 0:
                raise TimeoutError(f"{trnm} 응답 없음({timeout:.0f}초)")
            msg = self._recv(left)
            if isinstance(msg, dict) and str(msg.get("trnm", "")).upper() == trnm:
                return msg
            self._note(msg)
            self._handle(msg)

    def login(self) -> dict:
        try:
            token = self._tokens.token()
        except TokenError as exc:
            raise ProbeError(f"토큰 없음: {exc}") from None
        self._ws = self._connect(self._url, open_timeout=5, ping_interval=None, proxy=None, close_timeout=2)
        self._send({"trnm": "LOGIN", "token": token})
        return self._await("LOGIN", self._login_timeout)

    def request(self, trnm: str, rtype: str, code: str) -> dict:
        msg: dict[str, Any] = {"trnm": trnm, "grp_no": WS_GROUP}
        if trnm == "REG":
            msg["refresh"] = "1"   # 기존 등록 유지 (새 세션이라 다른 등록 없음)
        msg["data"] = [{"item": [code], "type": [rtype]}]
        self._send(msg)
        return self._await(trnm, self._login_timeout)

    def listen(self, seconds: float) -> None:
        deadline = self._mono() + seconds
        while (left := deadline - self._mono()) > 0:
            try:
                msg = self._recv(min(1.0, left))
            except TimeoutError:
                continue
            self._note(msg)
            self._handle(msg)

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 — 종료 중 오류는 기록하지 않는다
                pass

    def received(self) -> dict:
        return {t: {"count": s["count"], "fields": sorted(s["fields"]), "items": sorted(s["items"]),
                    "first_at": s["first_at"], "last_at": s["last_at"]} for t, s in self.stats.items()}


def ack_record(msg: Mapping[str, object]) -> dict:
    """등록·해지·로그인 응답 → 반환 코드·문구(가림)·키 이름. 값 원문은 남기지 않는다."""
    return {"return_code": normalize_code(msg.get("return_code")), "return_msg": _mask(msg.get("return_msg")),
            "keys": sorted(str(k) for k in msg)}


def run_ws(session: ProbeWs, code: str, seconds: float, log: Callable[[str], None]) -> dict:
    rep: dict[str, Any] = {"code": code, "login": None, "reg": [], "remove": [], "listen_seconds": seconds,
                           "error": None}
    registered: list[str] = []
    try:
        rep["login"] = ack_record(session.login())
        log(f"  WS 로그인 응답 return_code={rep['login']['return_code']}")
        if rep["login"]["return_code"] not in (0, None):
            raise ProbeError("WS 로그인 거부")
        for rtype in WS_TYPES:
            ack = ack_record(session.request("REG", rtype, code))
            rep["reg"].append({"type": rtype, **ack})
            log(f"  WS REG {rtype} {code}: return_code={ack['return_code']} {ack['return_msg']}")
            if ack["return_code"] == 0:
                registered.append(rtype)
        if registered:
            session.listen(seconds)
        for rtype in registered:
            ack = ack_record(session.request("REMOVE", rtype, code))
            rep["remove"].append({"type": rtype, **ack})
            log(f"  WS REMOVE {rtype} {code}: return_code={ack['return_code']} {ack['return_msg']}")
    except Exception as exc:  # noqa: BLE001 — 실측 기록: 어떤 실패든 종류·문구(가림)만 남기고 끝낸다
        rep["error"] = f"{type(exc).__name__}: {_mask(exc)}"
        log(f"  WS 실패: {rep['error']}")
    finally:
        session.close()
    rep["received"] = session.received()
    rep["other_real_types"] = dict(session.other_real)
    rep["other_control"] = session.other_control
    rep["sent_trnm"] = list(session.sent_trnm)
    return rep


# --- 진입점 ----------------------------------------------------------------------------------------------

Connect = Callable[[Path, Mapping[str, str] | None, Clock], object]


def _default_connect(root: Path, environ: Mapping[str, str] | None, clock: Clock) -> object:
    return connect_paper(root, environ, clock, per_tr_interval=PROBE_INTERVAL, allowed_apis=PROBE_REST_APIS)


def _refuse(msg: str) -> int:
    print(f"모의 실측 거부(연결 없음): {msg}")
    return 2


def _args(argv: Sequence[str]) -> argparse.Namespace | None:
    p = argparse.ArgumentParser(prog="scripts/kiwoom-probe.py",
                                description="키움 모의 조회·구독 실측 (읽기 전용, CP-032 D-180, A-085 Q-2)")
    p.add_argument("--mode", required=True, help="PAPER만 허용")
    p.add_argument("--codes", default=None, help=f"종목코드 쉼표 목록(최대 {MAX_CODES}). 없으면 config/app.paper.toml 종목")
    p.add_argument("--max-pages", type=int, default=PAGES_DEFAULT, help=f"ka10080 연속 조회 최대 쪽 (1~{PAGES_MAX})")
    p.add_argument("--back-days", default=BACK_DAYS_DEFAULT, help="ka10080 base_dt를 오늘에서 며칠 전으로 옮길지 쉼표 목록")
    p.add_argument("--ws-seconds", type=int, default=WS_SECONDS_DEFAULT, help=f"등록 뒤 수신 대기 초 (0~{WS_SECONDS_MAX})")
    try:
        return p.parse_args(list(argv))
    except SystemExit:
        return None


def parse_codes(text: str) -> list[str]:
    codes: list[str] = []
    for part in text.split(","):
        c = part.strip()
        if not c:
            continue
        if not _CODE.fullmatch(c):
            raise ProbeError(f"종목코드 형식 아님: {c[:12]!r} (6자리 숫자)")
        if c not in codes:
            codes.append(c)
    if not codes:
        raise ProbeError("종목코드 없음")
    if len(codes) > MAX_CODES:
        raise ProbeError(f"종목은 최대 {MAX_CODES}개")
    return codes


def parse_back_days(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        s = part.strip()
        if not s:
            continue
        if not (s.isascii() and s.isdigit()) or not 1 <= int(s) <= BACK_DAYS_LIMIT:
            raise ProbeError(f"--back-days 값 {s[:8]!r} — 1~{BACK_DAYS_LIMIT} 정수")
        if int(s) not in out:
            out.append(int(s))
    if len(out) > BACK_DAYS_MAX_ITEMS:
        raise ProbeError(f"--back-days는 최대 {BACK_DAYS_MAX_ITEMS}개")
    return sorted(out)


def _load_calendar(root: Path) -> KrxCalendar | None:
    try:
        return KrxCalendar.load_dir(root / "config" / "calendar")
    except (MarketDataError, OSError, ValueError):
        return None   # 모르면 평일 = 거래일 (market_hours_refusal)


def _running_refusal(base: Path, now: datetime) -> str | None:
    if instance_running(base):
        return "PAPER 엔진이 실행 중(run.lock 보유) — run을 끈 뒤 다시 실행"
    if instance_running(base, AUTOPILOT_LOCK):
        return "PAPER 자동 운영이 실행 중(autopilot.lock 보유) — 자동 운영이 끝난 뒤 다시 실행"
    return engine_alive(base / "heartbeat", now)


def _api_summary(guard: Any) -> dict:
    out: dict[str, dict] = {}
    for r in guard.records:
        s = out.setdefault(r.api_id, {"calls": 0, "ok": 0, "codes": {}, "last_error": ""})
        s["calls"] += 1
        if r.error_class == "OK":
            s["ok"] += 1
        else:
            s["last_error"] = f"{r.error_class} 코드 {r.code} {r.message}".strip()
        key = "응답 없음" if r.code is None else str(r.code)
        s["codes"][key] = s["codes"].get(key, 0) + 1
    return out


def probe_main(root: Path, argv: Sequence[str], environ: Mapping[str, str] | None = None, *,
               clock: Clock | None = None, connect: Connect | None = None,
               ws_connect: Callable[..., Any] | None = None,
               monotonic: Callable[[], float] = time.monotonic) -> int:
    ns = _args(argv)
    if ns is None:
        return 2
    if ns.mode != "PAPER":
        return _refuse(f"--mode {ns.mode} — 이 도구는 PAPER(키움 모의 조회·구독)에서만 실행한다. LIVE·BACKTEST는 연결하지 않는다")
    if not 1 <= ns.max_pages <= PAGES_MAX:
        return _refuse(f"--max-pages {ns.max_pages} — 1~{PAGES_MAX}")
    if not 0 <= ns.ws_seconds <= WS_SECONDS_MAX:
        return _refuse(f"--ws-seconds {ns.ws_seconds} — 0~{WS_SECONDS_MAX}")
    try:
        back_days = parse_back_days(ns.back_days)
        codes = parse_codes(ns.codes) if ns.codes else None
    except ProbeError as exc:
        return _refuse(str(exc))
    clock = clock or SystemClock()
    now = clock.now()
    hours = market_hours_refusal(now, _load_calendar(root))
    if hours:
        return _refuse(hours)
    base = root / "var" / "paper"
    running = _running_refusal(base, now)
    if running:
        return _refuse(running)

    conn = (connect or _default_connect)(root, environ, clock)
    if isinstance(conn, str):
        return _refuse(conn)
    adapter, guard = conn.adapter, conn.guard  # type: ignore[attr-defined]
    if adapter.cfg.rest_url != MOCK_REST or adapter.cfg.ws_url != MOCK_WS:
        adapter.close()
        return _refuse("접속 대상이 키움 모의 도메인이 아님 — 실측하지 않는다")
    if codes is None:
        codes = list(getattr(conn, "app").instruments)[:MAX_CODES]
        if not codes:
            adapter.close()
            return _refuse("config/app.paper.toml에 종목이 없음 — --codes로 지정")
    plan = rest_plan(codes, now.astimezone(KST).date(), ns.max_pages, back_days)
    calls_max = sum(p for *_, p in plan)
    print(f"모의 실측 시작: 종목 {', '.join(codes)} / ka10080 범위 {'·'.join(MIN_SCOPES)}분 / 최대 {calls_max}회 조회 "
          f"(TR당 {PROBE_INTERVAL}초 간격, 예상 최대 약 {calls_max * PROBE_INTERVAL / 60:.1f}분) / WS {ns.ws_seconds}초")
    report: dict[str, Any] = {
        "tool": "scripts/kiwoom-probe.py", "basis": "CP-032 D-180, 승인 A-085 Q-2 (조회·구독만, 주문 없음)",
        "started_at": now.astimezone(KST).isoformat(timespec="seconds"), "account_alias": adapter.cfg.account_alias,
        "codes": codes, "settings": {"min_scopes": list(MIN_SCOPES), "upd_stkpc_tp": UPD_STKPC_TP,
                                     "max_pages": ns.max_pages, "back_days": back_days, "tick_scope": TICK_SCOPE,
                                     "tick_pages": TICK_PAGES, "interval_seconds": PROBE_INTERVAL,
                                     "ws_seconds": ns.ws_seconds, "allowed_rest_apis": sorted(PROBE_REST_APIS)},
        "token": {"issued": False, "expires_at": None, "revoked": None},
        "rest": [], "rest_skipped": [], "ws": None, "failures": [],
    }
    try:
        try:
            adapter.tokens.token()
            exp = adapter.tokens.expires_at
            report["token"].update(issued=True, expires_at=exp.astimezone(KST).isoformat() if exp else None)
            print("토큰 발급 au10001: 정상")
        except TokenError as exc:
            report["failures"].append(f"토큰 발급 실패: {_mask(exc)}")
            print(f"토큰 발급 실패 — 조회·구독 안 함: {_mask(exc)}")
        if report["token"]["issued"]:
            print("REST 조회:")
            report["rest"], report["rest_skipped"] = run_rest(adapter, guard, plan, print)
            if guard.blocked or fatal_critical(adapter):
                print("WS 생략: 허용 밖 API 시도 또는 브로커 치명 오류(인증 단말·모드 불일치 등)")
            else:
                print("WebSocket:")
                session = ProbeWs(adapter.cfg.ws_url, adapter.tokens, clock, connect=ws_connect, monotonic=monotonic)
                report["ws"] = run_ws(session, codes[0], float(ns.ws_seconds), print)
    finally:
        revoked = adapter.close()   # 토큰 폐기 au10002
        if report["token"]["issued"]:
            report["token"]["revoked"] = revoked
            print(f"토큰 폐기 au10002: {'완료' if revoked else '실패 — 만료 시각에 정리됨'}")
    report["finished_at"] = clock.now().astimezone(KST).isoformat(timespec="seconds")
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


def _collect_failures(report: dict) -> None:
    f = report["failures"]
    for r in report["rest"]:
        if not r["ok"]:
            f.append(f"{r['api']} {r['request'].get('stk_cd')} 범위{r['request'].get('tic_scope')} {r['kind']}: {r['error']}")
    if report["rest_skipped"]:
        f.append(f"조회 안 함 {len(report['rest_skipped'])}건 (중단 또는 모의 미지원 API)")
    ws = report["ws"]
    if ws is not None:
        if ws["error"]:
            f.append(f"WS: {ws['error']}")
        f.extend(f"WS REG {r['type']}: return_code={r['return_code']} {r['return_msg']}"
                 for r in ws["reg"] if r["return_code"] != 0)
        f.extend(f"WS REMOVE {r['type']}: return_code={r['return_code']} {r['return_msg']}"
                 for r in ws["remove"] if r["return_code"] != 0)
    if report["blocked_before_send"]:
        f.append(f"허용 밖 API 시도(전송 전 차단): {', '.join(report['blocked_before_send'])}")
    f.extend(f"브로커 치명: {c}" for c in report["critical"])


# --- 기록 ------------------------------------------------------------------------------------------------

def write_report(directory: Path, report: dict, now: datetime) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{now.astimezone(KST):%Y%m%d-%H%M%S}-probe"
    jpath, mpath = directory / f"{stem}.json", directory / f"{stem}.md"
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    mpath.write_text(render_md(report), encoding="utf-8")
    return jpath, mpath


def _oldest(recs: Sequence[dict]) -> str:
    mins = [r["times"]["min"] for r in recs if r.get("times", {}).get("min")]
    return min(mins) if mins else "-"


def render_md(report: dict) -> str:
    L: list[str] = [
        f"# 키움 모의 조회·구독 실측 {report['started_at'][:10]}", "",
        f"- 근거: {report['basis']}", f"- 실행: {report['started_at']} ~ {report.get('finished_at', '-')} KST, "
        f"계좌 별칭 {report['account_alias']}, 종목 {', '.join(report['codes'])}",
        f"- 설정: {json.dumps(report['settings'], ensure_ascii=False)}",
        f"- 토큰: 발급 {report['token']['issued']}, 폐기 {report['token']['revoked']}",
        "- 이 파일은 형태만 담는다(필드 이름·건수·시각 범위·반환 코드). 가격·수량 값과 응답 원문은 저장하지 않았다.",
        "- 판정(K-U30·K-U31·K-U32·K-U33·K-U34·K-U35)은 사람이 이 결과로 하고 kiwoom-kr.md에 \"② 모의 실측\"으로 적는다.", "",
        "## ka10080 분봉 (K-U30·K-U31·K-U32)", "",
        "| 종목 | 범위(분) | 종류 | base_dt | 쪽 | 쪽별 건수 | 다음 쪽 | 시각 범위(cntr_tm) | 순서 | 기준일 지켜짐 | 결과 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    mins = [r for r in report["rest"] if r["api"] == "ka10080"]
    for r in mins:
        t, q = r["times"], r["request"]
        L.append(f"| {q.get('stk_cd')} | {q.get('tic_scope')} | {r['kind']} | {q.get('base_dt')} | {r['pages']} | "
                 f"{', '.join(map(str, r['rows_per_page'])) or '-'} | {'Y' if r['more_pages'] else 'N'} | "
                 f"{t.get('min', '-')} ~ {t.get('max', '-')} | {t.get('order', '-')} | {r.get('base_dt_honored')} | "
                 f"{'정상' if r['ok'] else '실패: ' + str(r['error'])} |")
    for scope in MIN_SCOPES:
        sub = [r for r in mins if r["request"].get("tic_scope") == scope]
        if sub:
            L.append(f"\n- 범위 {scope}분: 반환된 가장 오래된 cntr_tm = {_oldest(sub)}")
    paging = [r for r in mins if r["kind"] == "paging" and r["ok"]]
    if paging:
        r = paging[0]
        t = r["times"]
        L += ["", f"### cntr_tm 형태 — {r['request'].get('stk_cd')} 범위 {r['request'].get('tic_scope')}분 (연속 조회)", "",
              f"- 앞 {SAMPLE}개: {', '.join(t['first'])} / 끝 {SAMPLE}개: {', '.join(t['last']) or '-'}",
              f"- 날짜 수 {t.get('days', 0)}, 중복 {t.get('duplicates', 0)}, 초가 00이 아닌 값 {t.get('seconds_not_00', 0)}, "
              f"간격(초) 분포 {t.get('step_seconds', {})}",
              f"- 응답 필드: {', '.join(r['fields'])}", f"- 응답 최상위 키: {', '.join(r['body_keys'])}", "",
              "| 날짜 | 건수 | 첫 시각 | 끝 시각 |", "|---|---|---|---|"]
        L += [f"| {d['date']} | {d['rows']} | {d['first_hms']} | {d['last_hms']} |" for d in t.get("per_day", [])]
    L += ["", "## ka10079 틱 (K-U33)", ""]
    for r in (r for r in report["rest"] if r["api"] == "ka10079"):
        t = r["times"]
        L += [f"- {r['request'].get('stk_cd')} {r['request'].get('tic_scope')}틱: {r['pages']}쪽, 쪽별 "
              f"{', '.join(map(str, r['rows_per_page'])) or '-'}, 다음 쪽 {'Y' if r['more_pages'] else 'N'}, "
              f"시각 {t.get('min', '-')} ~ {t.get('max', '-')}, 날짜 수 {t.get('days', 0)}, "
              f"{'정상' if r['ok'] else '실패: ' + str(r['error'])}",
              f"- 응답 필드: {', '.join(r['fields']) or '-'} / 최상위 키: {', '.join(r['body_keys']) or '-'}"]
    L += ["", "## WebSocket 0B·0D (K-U32·K-U34·K-U35 일부)", ""]
    ws = report["ws"]
    if ws is None:
        L.append("- 실행 안 함")
    else:
        login = ws["login"] or {}
        L.append(f"- 로그인: return_code={login.get('return_code')} {login.get('return_msg', '')}")
        L += [f"- REG {r['type']} {ws['code']}: return_code={r['return_code']} {r['return_msg']} (응답 키 "
              f"{', '.join(r['keys'])})" for r in ws["reg"]]
        L += [f"- 수신 {t} ({ws['listen_seconds']:.0f}초): {s['count']}건, 시각 {s['first_at'] or '-'} ~ {s['last_at'] or '-'}, "
              f"FID {', '.join(s['fields']) or '-'}" for t, s in ws["received"].items()]
        L += [f"- REMOVE {r['type']}: return_code={r['return_code']} {r['return_msg']} (응답 키 {', '.join(r['keys'])})"
              for r in ws["remove"]]
        if ws["other_real_types"]:
            L.append(f"- 다른 실시간 항목 수신: {ws['other_real_types']}")
        if ws["other_control"]:
            L.append(f"- 기타 제어 응답: {json.dumps(ws['other_control'], ensure_ascii=False)}")
        L.append(f"- 보낸 메시지 종류: {', '.join(ws['sent_trnm'])}")
        if ws["error"]:
            L.append(f"- 오류: {ws['error']}")
        L.append("- 장외 실측이라 장중 수신 빈도(K-U35)는 알 수 없다 → PV-18 (D-180).")
    L += ["", "## API 호출 (HTTP 경계 기록)", "", "| api-id | 호출 | 정상 | 코드별 | 마지막 실패 |", "|---|---|---|---|---|"]
    L += [f"| {a} | {s['calls']} | {s['ok']} | {s['codes']} | {s['last_error'] or '-'} |"
          for a, s in sorted(report["api_calls"].items())]
    L += ["", f"- 전송 전 차단(허용 밖·주문 API): {', '.join(report['blocked_before_send']) or '없음'}", "", "## 실패", ""]
    L += [f"- {f}" for f in report["failures"]] or ["- 없음"]
    return "\n".join(L) + "\n"
