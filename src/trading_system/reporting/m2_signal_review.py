"""M2 신호 출처 분리 집계 (CP-039 D-251·D-255, T-063) — 읽기 전용. **판정은 하지 않고 표만** 만든다(PAPER 성과는 합격 판정 제외).

신호 출처 binance-public(DB 메타 signal_source)일 때만 내용이 있다 — testnet 출처 DB면 빈 문자열(기존 집계 결과 불변).
지표를 섞지 않고 따로 보고한다 (D-251):
① 실행 지표 — 테스트넷 기준: 체결가 vs 판정(게이트) 시점 테스트넷 최근가 P_x(매수 +, 매도 − 방향으로 불리하면 +, bps),
   매수 지정가(IOC) 의도의 미체결 비율, 부분 체결 의도 수. **테스트넷 호가 품질이라 실전 근거 아님**.
② 신호 대비 지표 — 체결가 vs 체결 시각 신호 출처 참조 시세 P_s(fill_signal_price, bps). 두 출처의 차이를 본다.
③ 괴리 분포 — 게이트 판정 스냅샷의 d = |P_x ÷ P_s − 1| (중앙·95%·최대), G11-S 거부 수.
④ 백테스트 가정 대비 — 참고. D-221 가정 체결가는 분봉 백테스트 도구 몫이라 이 집계에는 입력 연결이 없다(미계산).
D-255 (a) 봉 대조: 저장한 신호 출처 1분봉(minute_bar source = binance-public)과 같은 기간 재수집본(A-110 수집 도구 CSV)을 값
문자열 그대로 비교한다 — 차이는 원인 분석 대상(판정 아님). 응답 원문·계좌 식별·금액 원문(잔고)은 넣지 않는다.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from trading_system.domain.clock import from_iso, to_iso
from trading_system.domain.signal_source import PUBLIC_BAR_SOURCE
from trading_system.storage.repository import Repository

BPS = Decimal("10000")
Q4 = Decimal("0.0001")
META_SIGNAL = "signal_source"   # storage/market_meta.META_SIGNAL과 같은 값


@dataclass
class SignalReview:
    signal_source: str | None
    execution: dict = field(default_factory=dict)
    signal_relative: dict = field(default_factory=dict)
    divergence: dict = field(default_factory=dict)


def _bps(x: Decimal) -> Decimal:
    return (x * BPS).quantize(Q4, rounding=ROUND_HALF_EVEN)


def _rank(xs: Sequence[Decimal], q: Decimal) -> Decimal | None:
    """최근접 순위 분위수 (q ∈ (0, 1])."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(1, math.ceil(q * len(s)))
    return s[min(k, len(s)) - 1]


def collect_signal_review(repo: Repository, account: str) -> SignalReview:
    row = repo.select("SELECT value FROM meta WHERE key=?", (META_SIGNAL,))
    kind = row[0]["value"] if row else None
    if kind != PUBLIC_BAR_SOURCE:
        return SignalReview(None)
    return SignalReview(kind, _execution(repo, account), _signal_relative(repo, account), _divergence(repo, account))


def _snapshot(text: str | None) -> dict:
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _execution(repo: Repository, account: str) -> dict:
    fills = repo.select(
        "SELECT f.quantity AS q, f.price AS px, i.side AS side, d.limit_snapshot AS snap FROM fill f "
        "JOIN broker_order b ON b.order_key = f.order_key JOIN intent i ON i.intent_id = b.intent_id "
        "JOIN gate_decision d ON d.decision_id = i.decision_id WHERE b.account = ? ORDER BY f.at, f.rowid", (account,))
    slip: list[Decimal] = []
    no_px = 0
    for f in fills:
        px = _snapshot(f["snap"]).get("exec_price")
        if px is None:
            no_px += 1
            continue
        p_x, fill = Decimal(px), Decimal(f["px"])
        slip.append(_bps((fill - p_x) / p_x if f["side"] == "BUY" else (p_x - fill) / p_x))
    intents = repo.select("SELECT side, order_type, quantity, filled_quantity, state FROM intent WHERE account=? "
                          "AND request_type IN ('ENTRY','INCREASE','REDUCE','EXIT')", (account,))
    buy_limit = [i for i in intents if i["side"] == "BUY" and i["order_type"] == "LIMIT"]
    unfilled = [i for i in buy_limit if Decimal(i["filled_quantity"]) == 0 and i["state"] in ("EXPIRED", "CANCELLED")]
    partial = [i for i in intents if 0 < Decimal(i["filled_quantity"]) < Decimal(i["quantity"])]
    return {"fills": len(fills), "slippage_bps": slip, "fills_without_px": no_px, "buy_limit_intents": len(buy_limit),
            "buy_limit_unfilled": len(unfilled), "partial_intents": len(partial)}


def _signal_relative(repo: Repository, account: str) -> dict:
    rows = repo.select("SELECT side, fill_price, ref_price FROM fill_signal_price WHERE account=? ORDER BY rowid",
                       (account,))
    diff: list[Decimal] = []
    missing = 0
    for r in rows:
        if r["ref_price"] is None:
            missing += 1
            continue
        ref, fill = Decimal(r["ref_price"]), Decimal(r["fill_price"])
        diff.append(_bps((fill - ref) / ref if r["side"] == "BUY" else (ref - fill) / ref))
    return {"fills": len(rows), "diff_bps": diff, "missing_reference": missing}


def _divergence(repo: Repository, account: str) -> dict:
    ds: list[Decimal] = []
    rejected = 0
    for r in repo.select("SELECT result, failed_check, reason, limit_snapshot FROM gate_decision WHERE account=?",
                         (account,)):
        snap = _snapshot(r["limit_snapshot"])
        if "divergence" in snap:
            ds.append(Decimal(snap["divergence"]))
        if r["result"] == "REJECTED" and r["failed_check"] == "G11" and (r["reason"] or "").startswith("G11-S"):
            rejected += 1
    return {"decisions": len(ds), "median": _rank(ds, Decimal("0.5")), "p95": _rank(ds, Decimal("0.95")),
            "max": max(ds) if ds else None, "g11s_rejections": rejected}


def _stats(xs: Sequence[Decimal]) -> str:
    if not xs:
        return "없음"
    return f"{len(xs)}건, 중앙 {_rank(xs, Decimal('0.5'))}, 95% {_rank(xs, Decimal('0.95'))}, 최대 {max(xs)}"


def _pct(x: Decimal | None) -> str:
    return "-" if x is None else f"{(x * 100).quantize(Q4)}%"


def render_signal_review(r: SignalReview) -> str:
    if r.signal_source is None:
        return ""
    ex, sg, dv = r.execution, r.signal_relative, r.divergence
    lines = ["## M2 신호 출처 분리 집계 (CP-039 D-251·D-255)", "",
             f"신호 출처: {r.signal_source} — 봉·참조 시세 = 실서버 공개 시세, 주문·체결·대조 = 테스트넷. PAPER 손익은 "
             "\"실서버 신호 + 테스트넷 체결\" 혼합이라 백테스트 손익도 실전 손익도 아니다(성과는 합격 판정 제외, 04 §4.2).", "",
             "### ① 실행 지표 (테스트넷 기준 — 테스트넷 호가 품질이라 실전 근거 아님)", "",
             f"- 체결 {ex['fills']}건, 체결가 vs 판정 시점 테스트넷 최근가(불리 +, bps): {_stats(ex['slippage_bps'])}"
             + (f" (판정 시점 최근가 기록 없는 체결 {ex['fills_without_px']}건)" if ex['fills_without_px'] else ""),
             f"- 매수 지정가(IOC) 의도 {ex['buy_limit_intents']}건 중 미체결 종결 {ex['buy_limit_unfilled']}건, "
             f"부분 체결 의도 {ex['partial_intents']}건", "",
             "### ② 신호 대비 지표 (체결가 vs 체결 시각 실서버 참조 시세 — 두 출처 차이)", "",
             f"- 체결 {sg['fills']}건, 차이(불리 +, bps): {_stats(sg['diff_bps'])}, 참조 시세 없음 {sg['missing_reference']}건", "",
             "### ③ 괴리 분포 (게이트 판정 시점 d = |P_x ÷ P_s − 1|)", "",
             f"- 판정 {dv['decisions']}건, 중앙 {_pct(dv['median'])}, 95% {_pct(dv['p95'])}, 최대 {_pct(dv['max'])}, "
             f"G11-S 거부 {dv['g11s_rejections']}건", "",
             "### ④ 백테스트 가정 대비 (참고)", "",
             "- 미계산 — D-221 가정 체결가(분봉 백테스트)는 이 집계에 입력 연결이 없다. 봉 자체의 일치는 아래 봉 대조로 본다.", ""]
    return "\n".join(lines)


# --- D-255 (a) 저장 봉 ↔ 재수집본 ----------------------------------------------------------------------------------

BAR_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class BarCompare:
    matched: int = 0
    differing: list[tuple[datetime, tuple[str, ...]]] = field(default_factory=list)
    only_stored: list[datetime] = field(default_factory=list)
    only_recollected: list[datetime] = field(default_factory=list)
    excluded_other_source: int = 0


def load_recollected_csv(path: Path) -> dict[datetime, tuple[str, ...]]:
    """A-110 수집 도구 CSV(open_time_utc,open,high,low,close,volume,...) → 봉 시작 → OHLCV 문자열(API 값 그대로)."""
    out: dict[datetime, tuple[str, ...]] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            at = datetime.fromisoformat(row["open_time_utc"].replace("Z", "+00:00"))
            out[at] = tuple(row[k] for k in BAR_FIELDS)
    return out


def compare_recollected_bars(repo: Repository, instrument: str, recollected: Mapping[datetime, tuple[str, ...]], *,
                             start: datetime, end: datetime, minutes: int = 1) -> BarCompare:
    """start ≤ 봉 시작 < end. 저장 봉 중 신호 출처(binance-public)가 아닌 행은 비교하지 않고 센다."""
    rows = repo.select("SELECT bar_start, open, high, low, close, volume, source FROM minute_bar WHERE instrument=? "
                       "AND bar_minutes=? AND bar_start >= ? AND bar_start < ? ORDER BY bar_start",
                       (instrument, minutes, to_iso(start), to_iso(end)))
    out = BarCompare()
    stored: dict[datetime, tuple[str, ...]] = {}
    for r in rows:
        if r["source"] != PUBLIC_BAR_SOURCE:
            out.excluded_other_source += 1
            continue
        stored[from_iso(r["bar_start"])] = tuple(r[k] for k in BAR_FIELDS)
    recol = {t: v for t, v in recollected.items() if start <= t < end}
    for t in sorted(set(stored) | set(recol)):
        if t not in recol:
            out.only_stored.append(t)
        elif t not in stored:
            out.only_recollected.append(t)
        elif stored[t] == recol[t]:
            out.matched += 1
        else:
            out.differing.append((t, tuple(k for k, a, b in zip(BAR_FIELDS, stored[t], recol[t], strict=True) if a != b)))
    return out


def render_bar_compare(instrument: str, c: BarCompare) -> str:
    return "\n".join([
        f"## 신호 출처 봉 대조 {instrument} (저장 binance-public ↔ 재수집본, D-255 (a))", "",
        f"- 일치 {c.matched}, 값 다름 {len(c.differing)}, 저장에만 {len(c.only_stored)}, 재수집본에만 {len(c.only_recollected)}, "
        f"다른 출처 저장 봉(비교 제외) {c.excluded_other_source}",
        *[f"  - 다름 {to_iso(t)}: {', '.join(fs)}" for t, fs in c.differing[:20]],
        "- 차이는 원인 분석 대상(판정 아님).", ""])
