"""일일 보고 (R-26, 업무정의서 R-26, 02 ENT-27, 04 V-110·V-114, 01 §2.3.1-7).

일일 기준 시각 이후 생성한다. 내용: 주문·체결·거부(사유별), 실현 손익(전략·버전별, 비용 추정 반영)·보유·미실현,
한도 사용률 최대치, 결과 불명·대조 불일치·운영 개입, 정지 이력(K별), 적용 설정 버전.
생성에 실패하면 실패를 기록하고 긴급 알림한다. 같은 날 다시 만들면 보고 버전이 올라간다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trading_system.domain.clock import to_iso
from trading_system.domain.market_time import M1_TIME, MarketTime
from trading_system.domain.daily_base import DailyBaseCorrupt
from trading_system.domain.enums import IntentState
from trading_system.domain.market import CostRates, estimate_cost
from trading_system.domain.sinks import AlertSink
from trading_system.storage.repository import Repository


def build_report(repo: Repository, account: str, trade_day: date, reference_time: time, cost_rates: CostRates,
                 price_of: Callable[[str], Decimal | None], settings_hash: str | None, *,
                 market_time: MarketTime = M1_TIME) -> dict:
    # 거래일 범위 = 그 시장 현지 시각의 일일 기준 시각부터 24시간 (CP-024 D-108). 기본 M1(KST) — 기존 동작 그대로
    start = datetime.combine(trade_day, reference_time, tzinfo=market_time.tz)
    end = start + timedelta(days=1)
    s, e = to_iso(start), to_iso(end)
    rows = repo.select

    decisions = rows("SELECT * FROM gate_decision WHERE account=? AND created_at>=? AND created_at<?", (account, s, e))
    rejects = Counter(d["failed_check"] for d in decisions if d["result"] == "REJECTED")
    fills = rows("""SELECT f.quantity, f.price, f.at, o.instrument, o.side, i.owner FROM fill f
                    JOIN broker_order o ON o.order_key=f.order_key LEFT JOIN intent i ON i.intent_id=o.intent_id
                    WHERE o.account=? ORDER BY f.at, f.rowid""", (account,))
    # 전략·버전별 실현 손익: 평균단가 방식, 비용 추정 반영 (V-114). 전체 이력으로 평균단가를 만들고 당일분만 집계
    book: dict[tuple[str, str], tuple[Decimal, Decimal]] = defaultdict(lambda: (Decimal("0"), Decimal("0")))
    per: dict[str, dict[str, Decimal | int]] = defaultdict(lambda: {"fills": 0, "realized": Decimal("0"),
                                                                     "costs": Decimal("0")})
    for f in fills:
        owner = f["owner"] or "(외부)"
        qty, price = Decimal(f["quantity"]), Decimal(f["price"])
        key = (owner, f["instrument"])
        held, avg = book[key]
        amount = qty * price
        cost = estimate_cost(amount, f["side"] == "SELL", cost_rates)
        today = s <= f["at"] < e
        if f["side"] == "BUY":
            new_held = held + qty
            book[key] = (new_held, (held * avg + amount) / new_held if new_held else Decimal("0"))
            realized = -cost
        else:
            realized = (price - avg) * qty - cost
            book[key] = (held - qty, avg if held - qty > 0 else Decimal("0"))
        if today:
            per[owner]["fills"] += 1  # type: ignore[operator]
            per[owner]["realized"] += realized  # type: ignore[operator]
            per[owner]["costs"] += cost  # type: ignore[operator]
    orders = Counter((d["owner"] or "(운영)") for d in decisions if d["result"] == "APPROVED")
    positions = []
    unrealized = Decimal("0")
    evaluable = True
    for inst, p in repo.positions(account).items():
        if p.quantity <= 0:
            continue
        px = price_of(inst)
        if px is None:
            evaluable = False
        else:
            unrealized += (px - p.avg_price) * p.quantity
        positions.append({"instrument": inst, "quantity": str(p.quantity), "avg_price": str(p.avg_price),
                          "price": None if px is None else str(px)})
    usage = {"L2_max": "0", "L3_max": "0"}
    for d in decisions:
        if d["result"] == "APPROVED" and d["symbol_after"] and d["symbol_limit"]:
            usage["L2_max"] = str(max(Decimal(usage["L2_max"]), Decimal(d["symbol_after"]) / Decimal(d["symbol_limit"])))
            usage["L3_max"] = str(max(Decimal(usage["L3_max"]), Decimal(d["total_after"]) / Decimal(d["total_limit"])))
    halts = rows("SELECT halt_type, state, created_at, reason FROM halt WHERE created_at>=? AND created_at<?", (s, e))
    events = Counter(r["kind"] for r in rows("SELECT kind FROM audit_event WHERE at>=? AND at<?", (s, e)))
    ops = rows("SELECT command, channel, status FROM ops_command WHERE created_at>=? AND created_at<?", (s, e))
    recon = rows("SELECT kind, state FROM recon_item WHERE account=?", (account,))
    return {
        "account": account, "trade_day": trade_day.isoformat(), "settings_hash": settings_hash,
        "orders_approved": sum(orders.values()), "orders_by_owner": dict(orders),
        "rejects_by_check": dict(rejects), "fills_today": sum(int(v["fills"]) for v in per.values()),
        "by_strategy": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in per.items()},
        "realized_total": str(sum((v["realized"] for v in per.values()), Decimal("0"))),
        "unrealized": str(unrealized) if evaluable else "평가 불능", "positions": positions,
        "limit_usage": usage, "halts": halts, "unknown_results": events.get("UNKNOWN_RESOLVED", 0),
        "unknown_open": len([i for i in repo.intents_in_state(IntentState.UNKNOWN) if i.account == account]),
        "recon_open": len([r for r in recon if r["state"] == "OPEN"]),
        "ops_commands": ops, "alerts": events.get("ALERT", 0),
    }


BASE_CORRUPT_NOTE = "일일 기준 기록 손상"   # 일일 보고·요약 공통 문구 (재검토 M-1)


def daily_base_total(repo: Repository, account: str, trade_day: date) -> Decimal | None:
    """거래일의 일일 기준 금액(현금 + 보유 평가액, DAILY_BASE 감사 기록). 기록 없음은 None (A-084 ①).

    기록이 손상됐으면 DailyBaseCorrupt (재검토 M-1) — 호출자는 비율 대신 "기준 기록 손상"을 쓴다."""
    base = repo.daily_base(account, trade_day.isoformat())
    return None if base is None else base.total


def realized_ratio_text(realized: Decimal, base: Decimal | None, *, corrupt: bool = False) -> str:
    """실현 손익을 일일 기준 금액 대비 비율로. 기준이 없으면 방향만 (D-156 일일 요약과 같은 정책, A-084 ④).
    기준 기록이 손상됐으면(corrupt) 방향과 "기준 기록 손상" (재검토 M-1).

    텔레그램으로 가는 알림에는 금액 원문(원)을 넣지 않는다 (A-069 ⑤)."""
    sign = "+" if realized > 0 else "-" if realized < 0 else "±"
    if corrupt:
        return f"실현 손익 방향 {sign} ({BASE_CORRUPT_NOTE} — 비율 생략, 사람 확인 필요)"
    if base is None or base <= 0:
        return f"실현 손익 방향 {sign} (일일 기준 금액 기록 없음 — 비율 생략)"
    return f"실현 손익 일일 기준 대비 {realized / base * 100:+.3f}%"


def current_trade_day(now: datetime, reference_time: time, market_time: MarketTime = M1_TIME) -> date:
    """`now`가 속한 거래일 = 직전(같은 시각 포함) 일일 기준 시각의 시장 현지 날짜(M1 = KST). report_time(예: 16:00) 보고에 쓴다."""
    return market_time.trade_date(now, reference_time)


class DailyReporter:
    """market_time: 거래일을 나누는 시장 시각 기준 (CP-024 D-108, T-M2-09 V-110·V-114). 기본 M1(KST) — M1 조립 그대로.
    M2는 M2_TIME(UTC). 비용은 시장의 추정 요율(cost_rates)로 계산한다(M2 실제 수수료 기록 fill_fee 반영은 [제안])."""

    def __init__(self, repo: Repository, alerts: AlertSink, cost_rates: CostRates,
                 price_of: Callable[[str], Decimal | None], *, market_time: MarketTime = M1_TIME) -> None:
        self._repo = repo
        self._alerts = alerts
        self._rates = cost_rates
        self._price_of = price_of
        self._market_time = market_time

    def generate(self, account: str, now: datetime, reference_time: time, settings_hash: str | None, *,
                 current_day: bool = False) -> dict | None:
        """기본: 방금 끝난 거래일(직전 일일 기준 시각부터 24시간)의 보고서.

        current_day=True: 지금 진행 중인 거래일(직전 기준 시각부터 지금까지)의 보고서 — report_time(예: 16:00)에
        부른다 (검토 RV-21). 같은 거래일을 기준 시각 경계에서 다시 만들면 보고 버전이 올라간다(최종본).
        """
        trade_day = current_trade_day(now, reference_time, self._market_time)
        if not current_day:
            trade_day -= timedelta(days=1)
        try:
            content = build_report(self._repo, account, trade_day, reference_time, self._rates, self._price_of,
                                   settings_hash, market_time=self._market_time)
        except Exception as exc:  # noqa: BLE001 — 보고 실패는 기록·알림 (V-110)
            with self._repo.tx():
                self._repo.insert_daily_report(account, trade_day, "FAILED", {"error": type(exc).__name__}, now,
                                               settings_hash)
            self._alerts.alert("EMERGENCY", f"일일 보고 생성 실패 ({trade_day}): {type(exc).__name__}", None)
            return None
        with self._repo.tx():
            self._repo.insert_daily_report(account, trade_day, "OK", content, now, settings_hash)
        try:
            base, corrupt = daily_base_total(self._repo, account, trade_day), False
        except DailyBaseCorrupt:
            base, corrupt = None, True
        ratio = realized_ratio_text(Decimal(content["realized_total"]), base, corrupt=corrupt)
        self._alerts.alert("INFO", f"일일 보고 {trade_day}: 체결 {content['fills_today']}건, {ratio}", None)
        return content
