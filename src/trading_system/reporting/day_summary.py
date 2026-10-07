"""일일 요약 문구 (PAPER 무인 자동 운영의 15:45 종료 뒤 텔레그램 1건, CP-029 D-156) — 읽기 전용 집계.

내용: 오늘 체결(종목·방향·수량), 실현 손익(일일 기준 금액 대비 %), 슬리피지(전송 시점 시세 대비 틱, 시세 기록이 있을 때),
오늘 발동한 정지와 남은 정지, 마지막 대조·열린 불일치, 결과 불명·미체결 건수, 자동 운영 진행 기록(호출자가 준 줄).
민감값 정책은 텔레그램 /review와 같다(A-069 ⑤): **계좌 별칭·계좌번호·금액 원문을 넣지 않는다**. 손익은 비율로만 쓴다.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from datetime import date, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.domain.clock import KST, from_iso
from trading_system.domain.enums import IntentState, Mode
from trading_system.domain.market import CostRates
from trading_system.storage.repository import Repository

from .daily import build_report
from .paper_review import review

SIDE_KO = {"BUY": "매수", "SELL": "매도"}


def build_day_summary(db: Path, mode: Mode, account: str, day: date, reference_time: time,
                      rates: CostRates | None, notes: Sequence[str]) -> str:
    """`day`(KST 거래일)의 요약. 저장소가 없으면 그 사실만. 집계 일부가 실패해도 나머지는 쓴다(실패는 유형만 표시)."""
    head = f"[{day} 일일 요약 — {mode.value} 자동 운영]"
    if not db.is_file():
        return "\n".join([head, "저장소 없음 — 집계 생략", *notes])
    lines = [head]
    repo = Repository(db, mode)
    try:
        for part in (_fills, _pnl, _halts, _recon):
            try:
                lines += part(repo, account, day, reference_time, rates)
            except Exception as exc:  # noqa: BLE001 — 한 부분의 실패로 요약 전체를 잃지 않는다
                lines.append(f"(집계 실패: {part.__name__.strip('_')} {type(exc).__name__})")
    finally:
        repo.close()
    try:
        lines += _slippage(db, day)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(집계 실패: 슬리피지 {type(exc).__name__})")
    if notes:
        lines.append("자동 운영 기록:")
        lines += [f"- {n}" for n in notes]
    return "\n".join(lines)


def _fills(repo: Repository, account: str, day: date, *_: object) -> list[str]:
    rows = repo.select(
        "SELECT bo.instrument AS instrument, bo.side AS side, f.quantity AS quantity FROM fill f "
        "JOIN broker_order bo ON bo.order_key = f.order_key WHERE bo.trade_date = ? AND bo.account = ? ORDER BY f.at",
        (day.isoformat(), account))
    totals: dict[tuple[str, str], Decimal] = {}
    for f in rows:
        key = (f["instrument"], f["side"])
        totals[key] = totals.get(key, Decimal(0)) + Decimal(f["quantity"])
    out = [f"체결 {len(rows)}건" + (": " if totals else "") +
           ", ".join(f"{inst} {SIDE_KO.get(side, side)} {qty.normalize():f}" for (inst, side), qty in totals.items())]
    open_orders = repo.select("SELECT COUNT(*) AS n FROM broker_order WHERE account=? AND state IN ('ACCEPTED','PARTIAL') "
                              "AND origin <> 'INTERNAL_CANCEL'", (account,))[0]["n"]
    unknown = len([i for i in repo.intents_in_state(IntentState.UNKNOWN) if i.account == account])
    out.append(f"결과 불명 {unknown}건, 미체결 {open_orders}건")
    return out


def _pnl(repo: Repository, account: str, day: date, reference_time: time, rates: CostRates | None) -> list[str]:
    content = build_report(repo, account, day, reference_time, rates or CostRates(Decimal("0"), Decimal("0")),
                           lambda inst: None, None)
    note = "" if rates is not None else " (비용 미반영)"
    if not content["fills_today"]:
        return [f"실현 손익: 체결 없음{note}"]
    realized = Decimal(content["realized_total"])
    base = _base_value(repo, account, day)
    if base is None or base <= 0:
        sign = "+" if realized > 0 else "-" if realized < 0 else "±"
        return [f"실현 손익: 방향 {sign} (일일 기준 금액 기록 없음 — 비율 생략){note}"]
    return [f"실현 손익: 일일 기준 대비 {realized / base * 100:+.3f}%{note}"]


def _base_value(repo: Repository, account: str, day: date) -> Decimal | None:
    for e in repo.select("SELECT summary FROM audit_event WHERE kind='DAILY_BASE' AND ref=? ORDER BY seq DESC",
                         (account,)):
        s = json.loads(e["summary"])
        if s.get("trade_date") == day.isoformat():
            try:
                return Decimal(str(s.get("value")))
            except (InvalidOperation, ValueError):
                return None
    return None


def _halts(repo: Repository, account: str, day: date, *_: object) -> list[str]:
    today: Counter[str] = Counter()
    for h in repo.select("SELECT halt_type, account, created_at FROM halt ORDER BY created_at"):
        if h["account"] in (None, account) and from_iso(h["created_at"]).astimezone(KST).date() == day:
            today[h["halt_type"]] += 1
    remaining = [f"{h['halt_type']}({h['state']})" for h in repo.halt_rows(account)]
    return ["오늘 발동한 정지: " + (", ".join(f"{k} {n}건" for k, n in sorted(today.items())) or "없음"),
            "남은 정지: " + (", ".join(remaining) or "없음")]


def _recon(repo: Repository, account: str, day: date, *_: object) -> list[str]:
    last = repo.latest_reconciliation(account)
    items = repo.open_recon_items(account)
    kinds = sorted({i["kind"] for i in items})
    text = "없음" if last is None else (
        f"{last['trigger']} {last['result']} ({from_iso(last['created_at']).astimezone(KST):%H:%M} KST)")
    return [f"마지막 대조: {text}, 열린 불일치 {len(items)}건" + (f" ({', '.join(kinds)})" if kinds else "")]


def _slippage(db: Path, day: date) -> list[str]:
    r = review(db)
    if not r.market_log:
        return ["슬리피지: 시세 기록 표 없음"]
    rows = [x for x in r.slippage if str(x["at"]).startswith(day.isoformat())]
    if not rows:
        return ["슬리피지: 오늘 체결 없음"]
    parts = [f"{x['instrument']} {SIDE_KO.get(x['side'], x['side'])} {x['slip_send_ticks']}틱" for x in rows]
    return ["슬리피지(전송 시점 시세 대비, 불리한 쪽 +): " + ", ".join(parts)]

