"""PAPER 실행 기록 분석 (T-017 준비, A-044 2단계) — 읽기 전용.

PAPER 저장소(var/paper/trading.db)를 읽기 전용 연결로 열어 PAPER-0 검토(docs/07-paper-review.md, pv-procedure §4)에
쓸 집계를 만든다. 쓰기·브로커 연결 없음. 판정(합격·불합격)은 하지 않고 사실만 모은다 — 판정은 사람과 검토 문서가 한다.

다루는 항목: 기간·거래일, 거래 왕복, 게이트 결과, 의도 종결 상태, 결과 불명, 중복 검사, 주문번호(PV-02),
전송→결과 지연(PV-05), 체결 금액·추정 비용(PV-07, 앱 내역과 비교용), 정지 이력, 대조, 시계 점검, 운영 명령, 알림,
일일 기준(DAILY_BASE), [T-023 CP-027 draft] PV-10 체결가 − 시세(전송 시점·체결 시점, 틱), PV-15 시세 조회 주기·지연.
시세 기록 표(quote_snapshot·quote_poll_stat)가 없는 저장소(이 기능 전)는 "시세 기록 표 없음"으로 표시한다.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_system.domain.clock import KST
from trading_system.domain.market import CostRates, estimate_cost

# 키움 kt00001 관찰 필드 (broker/kiwoom/adapter.CASH_OBSERVED_FIELDS와 같음 — reporting은 broker를 import하지 않는다)
CASH_FIELDS = ("entr", "d1_entra", "d2_entra", "ord_alow_amt", "pymn_alow_amt")
QUOTE_MAX_AGE_SECONDS = Decimal("60")   # 03 D-095 시세 신선도 — PV-15 기준 "주기 × 3 ≤ 60초"


@dataclass
class Review:
    db: str
    period: tuple[str, str] | None = None
    trade_dates: list[str] = field(default_factory=list)
    days: dict[str, list[dict]] = field(default_factory=dict)          # 거래일 → 의도 행(요약)
    decisions: Counter = field(default_factory=Counter)                 # (result, failed_check)
    intent_states: Counter = field(default_factory=Counter)
    unknown_attempts: int = 0
    escalated: int = 0
    duplicate_signals: list[str] = field(default_factory=list)
    order_nos: dict[str, list[str]] = field(default_factory=dict)       # 거래일 → 주문번호 (PV-02)
    reused_order_nos: list[str] = field(default_factory=list)
    latency_seconds: list[float] = field(default_factory=list)          # 전송 시작 → 결과 (PV-05)
    fills: list[dict] = field(default_factory=list)                     # PV-07
    halts: list[dict] = field(default_factory=list)
    reconcile: Counter = field(default_factory=Counter)
    open_recon_items: int = 0
    clock: dict = field(default_factory=dict)
    commands: Counter = field(default_factory=Counter)
    alerts: Counter = field(default_factory=Counter)
    emergencies: list[str] = field(default_factory=list)
    daily_bases: list[dict] = field(default_factory=list)
    cash_observations: list[dict] = field(default_factory=list)
    market_log: bool = False                                            # 시세 기록 표 있음 (T-023)
    slippage: list[dict] = field(default_factory=list)                  # PV-10 체결마다
    poll_stats: list[dict] = field(default_factory=list)                # PV-15 창마다


def _kst(text: str | None) -> str:
    if not text:
        return "-"
    return datetime.fromisoformat(text).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def review(db_path: Path, rates: CostRates | None = None) -> Review:
    """읽기 전용으로 열어 집계한다. 파일이 없으면 FileNotFoundError (빈 DB를 만들지 않는다)."""
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _collect(conn, str(db_path), rates)
    finally:
        conn.close()


def _collect(conn: sqlite3.Connection, db: str, rates: CostRates | None) -> Review:
    r = Review(db)
    intents = _rows(conn, "SELECT * FROM intent ORDER BY created_seq")
    if intents:
        r.period = (_kst(intents[0]["created_at"]), _kst(intents[-1]["created_at"]))
    for it in intents:
        day = datetime.fromisoformat(it["created_at"]).astimezone(KST).date().isoformat()
        r.days.setdefault(day, []).append({
            "at": _kst(it["created_at"]), "type": it["request_type"], "side": it["side"], "qty": it["quantity"],
            "instrument": it["instrument"], "state": it["state"], "filled": it["filled_quantity"],
            "reason": it["closed_reason"] or ""})
        r.intent_states[it["state"]] += 1
    r.trade_dates = sorted(r.days)
    per_signal = Counter(it["signal_id"] for it in intents if it["signal_id"])
    r.duplicate_signals = sorted(s for s, n in per_signal.items() if n > 1)

    for d in _rows(conn, "SELECT result, failed_check FROM gate_decision"):
        r.decisions[(d["result"], d["failed_check"] or "-")] += 1

    attempts = _rows(conn, "SELECT outcome, created_at, result_at FROM attempt")
    r.unknown_attempts = sum(1 for a in attempts if a["outcome"] == "UNKNOWN")
    for a in attempts:
        if a["result_at"]:
            sec = (datetime.fromisoformat(a["result_at"]) - datetime.fromisoformat(a["created_at"])).total_seconds()
            r.latency_seconds.append(sec)

    owners: dict[str, set[str]] = defaultdict(set)
    for o in _rows(conn, "SELECT trade_date, order_no FROM broker_order ORDER BY created_at"):
        r.order_nos.setdefault(o["trade_date"], []).append(o["order_no"])
        owners[o["order_no"]].add(o["trade_date"])
    r.reused_order_nos = sorted(n for n, ds in owners.items() if len(ds) > 1)

    for f in _rows(conn, "SELECT f.quantity, f.price, f.at, b.side, b.instrument, b.trade_date "
                         "FROM fill f JOIN broker_order b ON f.order_key=b.order_key ORDER BY f.at"):
        amount = Decimal(f["quantity"]) * Decimal(f["price"])
        cost = estimate_cost(amount, f["side"] == "SELL", rates) if rates is not None else None
        r.fills.append({"at": _kst(f["at"]), "side": f["side"], "instrument": f["instrument"],
                        "qty": f["quantity"], "price": f["price"], "amount": str(amount),
                        "est_cost": str(cost) if cost is not None else "-"})

    for h in _rows(conn, "SELECT halt_type, actor, reason, created_at, state, released_at, released_by "
                         "FROM halt ORDER BY created_at"):
        dur = "-"
        if h["released_at"]:
            dur = str(round((datetime.fromisoformat(h["released_at"])
                             - datetime.fromisoformat(h["created_at"])).total_seconds()))
        r.halts.append({"type": h["halt_type"], "at": _kst(h["created_at"]), "actor": h["actor"],
                        "reason": (h["reason"] or "")[:80], "state": h["state"],
                        "released_by": h["released_by"] or "-", "seconds": dur})

    for c in _rows(conn, "SELECT result, COUNT(*) n FROM reconciliation GROUP BY result"):
        r.reconcile[c["result"]] = c["n"]
    r.open_recon_items = _rows(conn, "SELECT COUNT(*) n FROM recon_item WHERE state='OPEN'")[0]["n"]

    checks = _rows(conn, "SELECT offset_seconds, result FROM clock_check")
    offsets = [abs(Decimal(c["offset_seconds"])) for c in checks if c["offset_seconds"] is not None]
    r.clock = {"checks": len(checks), "results": dict(Counter(c["result"] for c in checks)),
               "max_abs_offset": str(max(offsets)) if offsets else "-"}

    for c in _rows(conn, "SELECT command, status FROM ops_command"):
        r.commands[(c["command"], c["status"])] += 1

    for e in _rows(conn, "SELECT kind, at, summary FROM audit_event "
                         "WHERE kind IN ('ALERT', 'UNKNOWN_ESCALATED', 'DAILY_BASE', 'CASH_OBSERVED') ORDER BY seq"):
        s = json.loads(e["summary"])
        if e["kind"] == "UNKNOWN_ESCALATED":
            r.escalated += 1
        elif e["kind"] == "CASH_OBSERVED":
            obs = s.get("observed") or {}
            r.cash_observations.append({"at": _kst(e["at"]), "deposit": s.get("deposit"), "orderable": s.get("orderable"),
                                        **{k: obs.get(k, "-") for k in CASH_FIELDS}})
        elif e["kind"] == "DAILY_BASE":
            r.daily_bases.append({"trade_date": s.get("trade_date"), "cash": s.get("cash"), "value": s.get("value"),
                                  "at": _kst(s.get("at"))})
        else:
            r.alerts[s.get("level", "?")] += 1
            if s.get("level") == "EMERGENCY":
                r.emergencies.append(f"{_kst(e['at'])} {str(s.get('message', ''))[:100]}")
    _collect_market_log(conn, r)
    return r


def _dec(text: object) -> Decimal | None:
    try:
        return None if text is None else Decimal(str(text))
    except ArithmeticError:
        return None


def _slip(side: str, fill: Decimal | None, quote: Decimal | None) -> Decimal | None:
    """체결가 − 시세, 불리한 방향이 양수 (매수: 비싸게 산 만큼, 매도: 싸게 판 만큼)."""
    if fill is None or quote is None:
        return None
    d = fill - quote
    return d if side == "BUY" else -d


def _ticks(v: Decimal | None, tick: Decimal | None) -> str:
    if v is None or tick is None or tick <= 0:
        return "-"
    t = v / tick
    return str(t.to_integral_value()) if t == t.to_integral_value() else f"{t:.2f}"


def _collect_market_log(conn: sqlite3.Connection, r: Review) -> None:
    """PV-10·PV-15 (T-023). 표가 없으면(이 기능 전 저장소) 건너뛴다 — 읽기 전용이라 만들지 않는다."""
    names = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"quote_snapshot", "quote_poll_stat"} <= names:
        return
    r.market_log = True
    snaps = _rows(conn, "SELECT * FROM quote_snapshot ORDER BY seq")
    sends = {s["intent_id"]: s for s in snaps if s["event"] == "SEND" and s["intent_id"]}
    for f in (s for s in snaps if s["event"] == "FILL"):
        send = sends.get(f["intent_id"])
        price = _dec(f["event_price"])
        q_send = _dec(send["quote_price"]) if send is not None else None
        q_fill = _dec(f["quote_price"])
        tick = (_dec(send["tick"]) if send is not None else None) or _dec(f["tick"])
        s_send, s_fill = _slip(f["side"], price, q_send), _slip(f["side"], price, q_fill)
        r.slippage.append({
            "at": _kst(f["event_at"]), "instrument": f["instrument"], "side": f["side"], "qty": f["event_quantity"],
            "price": f["event_price"], "quote_send": str(q_send) if q_send is not None else "-",
            "age_send": (send["quote_age_seconds"] if send is not None else None) or "-",
            "slip_send": str(s_send) if s_send is not None else "-", "slip_send_ticks": _ticks(s_send, tick),
            "quote_fill": str(q_fill) if q_fill is not None else "-", "age_fill": f["quote_age_seconds"] or "-",
            "slip_fill": str(s_fill) if s_fill is not None else "-", "slip_fill_ticks": _ticks(s_fill, tick),
            "tick": str(tick) if tick is not None else "-"})
    for p in _rows(conn, "SELECT * FROM quote_poll_stat ORDER BY seq"):
        mx = _dec(p["interval_max_seconds"])
        r.poll_stats.append({
            "start": _kst(p["window_start"]), "end": _kst(p["window_end"]), "watched": p["watched"],
            "instruments": p["instruments"], "requests": p["requests"], "failures": p["failures"],
            "lat_p50": p["latency_ms_p50"], "lat_max": p["latency_ms_max"],
            "iv_avg": p["interval_avg_seconds"] or "-", "iv_max": p["interval_max_seconds"] or "-",
            "over_limit": mx is not None and mx * 3 > QUOTE_MAX_AGE_SECONDS,
            "fields": ", ".join(json.loads(p["response_fields"])) if p["response_fields"] else ""})


def _table(head: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def render_markdown(r: Review) -> str:
    """검토 문서(07-paper-review)에 붙일 수 있는 표. 계좌번호·토큰은 DB에 없으므로 나오지 않는다."""
    L = [f"# PAPER 실행 기록 집계 (읽기 전용)", "", f"- DB: `{r.db}`",
         f"- 기간: {r.period[0]} ~ {r.period[1]}" if r.period else "- 기간: 의도 없음",
         f"- 거래일: {', '.join(r.trade_dates) or '-'}", ""]
    L += ["## 거래 (의도)", ""]
    for day in r.trade_dates:
        L += [f"### {day}", ""]
        L += _table(["시각(KST)", "유형", "매매", "종목", "수량", "체결", "상태", "종결 사유"],
                    [[x["at"], x["type"], x["side"], x["instrument"], x["qty"], x["filled"], x["state"], x["reason"]]
                     for x in r.days[day]])
        L.append("")
    L += ["## 게이트 판정", ""] + _table(["결과", "실패 검사", "건수"],
                                         [[k[0], k[1], n] for k, n in sorted(r.decisions.items())]) + [""]
    L += ["## 의도 종결 상태", ""] + _table(["상태", "건수"], sorted(r.intent_states.items())) + [""]
    lat = sorted(r.latency_seconds)
    L += ["## 결과 불명·중복·지연", "",
          f"- 결과 불명 전송: {r.unknown_attempts}건, 사람 확인으로 넘김(K3): {r.escalated}건",
          f"- 같은 신호의 의도 2개 이상: {len(r.duplicate_signals)}건 {r.duplicate_signals[:5] or ''}",
          f"- 전송→결과 지연(PV-05): {len(lat)}건" + (f", 중앙값 {lat[len(lat) // 2]:.2f}초, 최대 {lat[-1]:.2f}초"
                                                    if lat else ""), ""]
    L += ["## 주문번호 (PV-02)", ""]
    L += [f"- {d}: {', '.join(nos)}" for d, nos in sorted(r.order_nos.items())] or ["- 없음"]
    L += [f"- 여러 거래일에 같은 번호: {', '.join(r.reused_order_nos) or '없음'}", ""]
    L += ["## 체결 (PV-07 — 키움 앱 체결 내역의 수수료·세금과 비교)", ""]
    L += _table(["시각(KST)", "매매", "종목", "수량", "가격", "금액", "추정 비용"],
                [[f["at"], f["side"], f["instrument"], f["qty"], f["price"], f["amount"], f["est_cost"]]
                 for f in r.fills]) + [""]
    L += ["## 정지 이력", ""] + _table(["유형", "발동(KST)", "주체", "사유", "상태", "해제", "지속(초)"],
                                     [[h["type"], h["at"], h["actor"], h["reason"], h["state"], h["released_by"],
                                       h["seconds"]] for h in r.halts]) + [""]
    L += ["## 대조·시계·명령·알림", "",
          f"- 대조: {dict(r.reconcile) or '-'}, 열린 불일치 항목 {r.open_recon_items}건",
          f"- 시계 점검: {r.clock.get('checks', 0)}회 {r.clock.get('results', {})}, 최대 |오차| {r.clock.get('max_abs_offset')}초",
          f"- 운영 명령: {dict((f'{k[0]}:{k[1]}', n) for k, n in sorted(r.commands.items())) or '-'}",
          f"- 알림: {dict(r.alerts) or '-'}", ""]
    if r.emergencies:
        L += ["### 긴급 알림 (최근 20)", ""] + [f"- {e}" for e in r.emergencies[-20:]] + [""]
    L += ["## 일일 기준 (DAILY_BASE, DP-31)", ""]
    L += _table(["거래일", "현금", "평가액", "기록 시각"],
                [[b["trade_date"], b["cash"], b["value"], b["at"]] for b in r.daily_bases]) + [""]
    L += ["## 예수금 관찰 (CASH_OBSERVED, DP-31)", "", "값이 바뀔 때만 기록. 관찰 전용(게이트 계산에 쓰지 않음)", ""]
    L += _table(["시각", "예수금(내부)", "주문가능", *CASH_FIELDS],
                [[c["at"], c["deposit"], c["orderable"], *(c[k] for k in CASH_FIELDS)] for c in r.cash_observations]) + [""]
    L += _render_market_log(r)
    L += ["## 이 기록으로 볼 수 없는 것", ""]
    if r.market_log:
        L += ["- PV-10 시세는 시스템이 받은 시각 기준의 마지막 조회값이다(체결 순간의 거래소 가격 아님). "
              "시세 나이(초)가 조회 주기만큼 클 수 있으니 나이와 함께 본다",
              "- 메모리 시세 이력은 재시작 때 비워진다 — 재시작 전 사건과 재시작 직후 첫 조회 전 사건은 시세 '-'"]
    else:
        L += ["- PV-10·PV-15: 이 저장소에는 시세 기록 표 없음(T-023 전) → 운영 로그·키움 앱으로"]
    L += ["- 키움 앱 화면의 예수금과 응답 필드의 일치: 필요하면 사용자가 앱과 대조", ""]
    return "\n".join(L)


def _render_market_log(r: Review) -> list[str]:
    if not r.market_log:
        return ["## 시세 기록 (PV-10·PV-15)", "", "- 시세 기록 표 없음 (이 기능 전 저장소)", ""]
    L = ["## 체결가 vs 시세 (PV-10)", "",
         "차이 = 체결가 − 시세, 불리한 방향이 양수(매수는 비싸게, 매도는 싸게). 틱 = 차이 ÷ 호가 단위(전송 시 시세 기준). "
         "나이 = 사건 시각 − 시세를 받은 시각(초)", ""]
    L += _table(["체결(KST)", "종목", "매매", "수량", "체결가", "전송 시 시세", "나이", "차이", "틱",
                 "체결 시 시세", "나이", "차이", "틱"],
                [[x["at"], x["instrument"], x["side"], x["qty"], x["price"], x["quote_send"], x["age_send"],
                  x["slip_send"], x["slip_send_ticks"], x["quote_fill"], x["age_fill"], x["slip_fill"],
                  x["slip_fill_ticks"]] for x in r.slippage]) + [""]
    ticks = [Decimal(x["slip_send_ticks"]) for x in r.slippage if x["slip_send_ticks"] != "-"]
    L += [f"- 전송 시 시세 대비 평균 {sum(ticks, Decimal(0)) / len(ticks):.2f}틱 ({len(ticks)}건)" if ticks
          else "- 전송 시 시세 대비 틱 계산 가능한 체결 없음", ""]
    L += ["## 시세 조회 주기·지연 (PV-15)", "",
          f"간격 = 같은 종목의 연속 성공 조회 사이(초). 기준: 최대 간격 × 3 ≤ {QUOTE_MAX_AGE_SECONDS}초 (03 D-095). "
          "지연 = 조회 요청~응답(호출 제한 대기 포함, ms)", ""]
    L += _table(["창 시작(KST)", "창 끝", "감시 종목", "조회 종목", "조회", "실패", "지연 중앙", "지연 최대",
                 "간격 평균", "간격 최대", "기준"],
                [[p["start"], p["end"], p["watched"], p["instruments"], p["requests"], p["failures"], p["lat_p50"],
                  p["lat_max"], p["iv_avg"], p["iv_max"], "초과" if p["over_limit"] else "이내"]
                 for p in r.poll_stats]) + [""]
    fields = [(p["start"], p["fields"]) for p in r.poll_stats if p["fields"]]
    L += ["### 시세 응답 필드 이름 (ka10001, 값은 기록하지 않음 — 체결 시각 필드 유무 확인용)", ""]
    L += [f"- {at}: {names}" for at, names in fields] or ["- 기록 없음"]
    return L + [""]
