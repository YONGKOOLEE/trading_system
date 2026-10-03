"""휴장일 달력 초안 추출 도구 — 키움 모의 일봉(ka10081) 조회만 (A-037, 03 D-055, krx-market X-U3).

- 기준 종목 두 개 **모두** 봉이 없는 평일만 휴장일로 본다. 한 종목만 빠진 날은 거래정지 등으로 보고 따로 보고한다.
- 올해는 마지막 확정 거래일(일봉 확정 20:00, DP-5)까지만 추출한다. 남은 기간·개장 시간 변경일은 KRX 공지로 넣는다.
- 결과는 `var/paper/calendar-derive/`의 초안이다. config/calendar에는 쓰지 않는다(사람 확인 후 반영).
- 연결 전 검사·주문 API 차단은 시작 점검(preflight)과 같다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom import parse
from trading_system.broker.ratelimit import Priority, call_priority
from trading_system.domain.clock import KST, Clock, SystemClock
from trading_system.marketdata.calendar_derive import render_calendar_toml, weekday_holidays
from trading_system.marketdata.kiwoom_feed import CHART_PATH
from trading_system.marketdata.store import latest_complete_day

from .preflight import ORDER_API_IDS, connect_paper

DERIVE_CODES = ("005930", "000660")   # 삼성전자·SK하이닉스 (조회만)
SUSPICIOUS_COUNT = 25                 # 한 해 평일 휴장일이 이보다 많으면 데이터 의심


class DeriveError(RuntimeError):
    pass


def _dt(row: dict) -> date:
    s = str(row.get("dt") or "")
    if len(s) != 8 or not (s.isascii() and s.isdigit()):
        raise parse.KiwoomParseError("dt 형식 아님")
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def fetch_trading_days(adapter, code: str, since: date, until: date) -> set[date]:  # type: ignore[no-untyped-def]
    def enough(rows: list[dict]) -> bool:
        return any(_dt(r) < since for r in rows)

    with call_priority(Priority.MARKET):
        _, rows = adapter.query_raw(CHART_PATH, "ka10081", {
            "stk_cd": code, "base_dt": until.strftime("%Y%m%d"), "upd_stkpc_tp": "1"},
            "stk_dt_pole_chart_qry", enough)
    days = {_dt(r) for r in rows}
    if not days or min(days) >= since:
        raise DeriveError(f"{code}: {since} 이전 일봉까지 받지 못함 — 기간 부족")
    return {d for d in days if d <= until}


def derive(days_by_code: Mapping[str, set[date]], years: Sequence[int], until: date) -> dict[int, dict]:
    union = set().union(*days_by_code.values())
    out: dict[int, dict] = {}
    for y in years:
        start, end = date(y, 1, 1), min(date(y, 12, 31), until)
        hol = weekday_holidays(union, start, end)
        gaps = sorted(d for d in union if start <= d <= end and
                      any(d not in s for s in days_by_code.values()))
        out[y] = {"holidays": hol, "covered_until": end, "single_code_gaps": gaps,
                  "suspicious": len(hol) > SUSPICIOUS_COUNT}
    return out


def derive_main(root: Path, environ: Mapping[str, str] | None = None, *, years: Sequence[int] = (2024, 2025, 2026),
                rest_url: str | None = None, ws_url: str | None = None, clock: Clock | None = None,
                per_tr_interval: float = 1.1) -> int:
    clock = clock or SystemClock()
    kw = {k: v for k, v in (("rest_url", rest_url), ("ws_url", ws_url)) if v is not None}
    conn = connect_paper(root, environ, clock, per_tr_interval=per_tr_interval, **kw)  # type: ignore[arg-type]
    if isinstance(conn, str):
        print(f"달력 추출 거부(연결 없음): {conn}")
        return 2
    now = clock.now()
    until = latest_complete_day(now, None)
    since = date(min(years), 1, 1)
    try:
        days = {c: fetch_trading_days(conn.adapter, c, since, until) for c in DERIVE_CODES}
    except (BrokerQueryError, parse.KiwoomParseError, DeriveError) as exc:
        print(f"달력 추출 실패: {exc}")
        return 1
    finally:
        conn.adapter.close()
    if conn.guard.blocked or any(r.api_id in ORDER_API_IDS for r in conn.guard.records):
        print("달력 추출 중 주문 API 호출 시도 — 결과 버림")
        return 1

    result = derive(days, years, until)
    out_dir = root / "var" / "paper" / "calendar-derive"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = now.astimezone(KST).date().isoformat()
    summary: dict[str, dict] = {}
    for y, r in result.items():
        notes = ["초안(미확정) — 키움 모의 ka10081 일봉에서 자동 추출. 사람 확인 후 config/calendar에 반영",
                 f"기준 종목 {'·'.join(DERIVE_CODES)} 모두 봉이 없는 평일 = 휴장일. 추출 범위 {y}-01-01 ~ {r['covered_until']}",
                 "개장 시간 변경일(연초 개장·수능일)은 일봉으로 알 수 없어 포함하지 않음",
                 "LIVE 전 재확인·검증 데이터 요청 (A-037 조건)"]
        text = render_calendar_toml(y, r["holidays"], {}, "키움 모의 ka10081 일봉 추출(" + "·".join(DERIVE_CODES) + ")",
                                    today, notes)
        (out_dir / f"krx-{y}.draft.toml").write_text(text, encoding="utf-8")
        summary[str(y)] = {"holidays": [d.isoformat() for d in r["holidays"]],
                           "covered_until": r["covered_until"].isoformat(),
                           "single_code_gaps": [d.isoformat() for d in r["single_code_gaps"]],
                           "suspicious": r["suspicious"]}
        print(f"{y}: 평일 휴장 {len(r['holidays'])}일 (~{r['covered_until']}), 한 종목만 빠진 날 "
              f"{len(r['single_code_gaps'])}일{' — 의심: 너무 많음' if r['suspicious'] else ''}")
        print("  " + ", ".join(d.isoformat() for d in r["holidays"]))
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"초안: {out_dir} (config/calendar에는 쓰지 않음)")
    return 1 if any(r["suspicious"] for r in result.values()) else 0
