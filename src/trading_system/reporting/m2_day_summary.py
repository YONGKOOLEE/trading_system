"""M2 일일 요약 문구 (CP-037 D-227, T-054) — 감독 프로세스의 UTC 일과 뒤 텔레그램 1건. 읽기 전용 집계.

D-156(M1 일일 요약) 형식의 M2판: 마감한 **UTC 거래일**(D-108)의 체결(심볼·방향·수량), 결과 불명·미체결 건수, 그날 발동한
정지와 남은 정지, 마지막 대조·열린 불일치, 감독 기록(호출자가 준 줄 — 자동 재시작·K8 자동 재개·일과 결과).
민감값 정책(A-069 ⑤, D-227): **계좌 별칭·계좌번호·금액 원문을 넣지 않는다**. 실현 손익 비율은 이 요약에 넣지 않는다
(M2 일일 기준 금액 계산은 paper-review·/review로 확인 — DP, 보수: 잘못된 비율을 무인 알림으로 내보내지 않음).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path

from trading_system.domain.clock import from_iso
from trading_system.domain.enums import IntentState, Mode
from trading_system.storage.repository import OrderIdentity, Repository

SIDE_KO = {"BUY": "매수", "SELL": "매도"}


def build_m2_day_summary(db: Path, account: str, day: date, notes: Sequence[str]) -> str:
    """`day`(UTC 거래일)의 요약. 저장소가 없으면 그 사실만(만들지 않는다). 한 부분이 실패해도 나머지는 쓴다."""
    head = f"[M2 {day} 일일 요약 — PAPER 감독(UTC)]"
    if not db.is_file():
        return "\n".join([head, "저장소 없음 — 집계 생략", *notes])
    lines = [head]
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        for part in (_fills, _halts, _recon, _signal):
            try:
                lines += part(repo, account, day)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"(집계 실패: {part.__name__.strip('_')} {type(exc).__name__})")
    finally:
        repo.close()
    lines.append("실현 손익: 이 요약에는 없음 — paper-review·/review로 확인")
    if notes:
        lines.append("감독 기록:")
        lines += [f"- {n}" for n in notes]
    return "\n".join(lines)


def _fills(repo: Repository, account: str, day: date) -> list[str]:
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


def _halts(repo: Repository, account: str, day: date) -> list[str]:
    today: Counter[str] = Counter()
    for h in repo.select("SELECT halt_type, account, created_at FROM halt ORDER BY created_at"):
        if h["account"] in (None, account) and from_iso(h["created_at"]).astimezone(UTC).date() == day:
            today[h["halt_type"]] += 1
    remaining = sorted({h["halt_type"] for h in repo.halt_rows(None) if h["account"] in (None, account)})
    return ["그날 발동한 정지: " + (", ".join(f"{k} {n}건" for k, n in sorted(today.items())) or "없음"),
            "남은 정지: " + (", ".join(remaining) or "없음")]


def _recon(repo: Repository, account: str, day: date) -> list[str]:
    last = repo.latest_reconciliation(account)
    items = repo.open_recon_items(account)
    kinds = sorted({i["kind"] for i in items})
    text = "없음" if last is None else (
        f"{last['trigger']} {last['result']} ({from_iso(last['created_at']).astimezone(UTC):%m-%d %H:%M} UTC)")
    return [f"마지막 대조: {text}, 열린 불일치 {len(items)}건" + (f" ({', '.join(kinds)})" if kinds else "")]


def _signal(repo: Repository, account: str, day: date) -> list[str]:
    """[CP-039 D-250] 신호 출처 괴리·참조 시세 지연 거부(G11-S) 건수 — 그날 1건 이상일 때만 한 줄(묶음 알림, 정지 아님)."""
    n = sum(1 for r in repo.select("SELECT created_at FROM gate_decision WHERE account=? AND result='REJECTED' "
                                   "AND failed_check='G11' AND reason LIKE 'G11-S%'", (account,))
            if from_iso(r["created_at"]).astimezone(UTC).date() == day)
    return [f"신호 출처 괴리·지연 진입 거부(G11-S) {n}건 — 정지 아님, 청산 영향 없음"] if n else []


__all__ = ["build_m2_day_summary"]
