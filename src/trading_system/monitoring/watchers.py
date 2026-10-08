"""한도 사용률·거부 급증 경고와 일일 손익 추적 (R-04, R-13, 01 §2.3·§2.4·§2.10, 03 §8.4, 04 V-116·V-117).

- 한도 사용률: 승인된 게이트 판정의 종목 비중(L2)·총 노출(L3)이 경고 기준(설정 warn_usage_ratio) 이상이면 경고 1회.
  같은 한도·같은 거래일에는 반복하지 않는다.
- 거부 급증: 기준 시간 안의 게이트 거부가 기준 건수 이상이면 경고 1회(같은 창에서 반복 억제). 기준값은 설정.
- 일일 손익: 일일 기준 시각에 기준 금액(예수금 + 보유 평가액)을 고정하고, 이후 손익 = 현재 평가액 − 기준 금액.
  보유 종목 시세가 하나라도 없으면 평가 불능(위험 확대 거부, 01 §2.4). L4 도달 시 K2 자동 발동은 정지 관리자가 한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from trading_system.domain.clock import Clock, from_iso, last_kst_time_at_or_before, to_iso
from trading_system.domain.daily_base import DailyBase, DailyBaseCorrupt
from trading_system.domain.market import CashView, CostRates, DailyRisk, estimate_cost
from trading_system.domain.sinks import AlertSink
from trading_system.storage.repository import DAILY_BASE_KIND, Repository


@dataclass(frozen=True)
class AlertThresholds:
    warn_usage_ratio: Decimal       # AccountLimits.warn_usage_ratio
    reject_surge_count: int         # 설정 [alerts]
    reject_surge_minutes: int


class AlertWatcher:
    def __init__(self, repo: Repository, alerts: AlertSink, thresholds: AlertThresholds,
                 reference_time: time) -> None:
        self._repo = repo
        self._alerts = alerts
        self._t = thresholds
        self._ref = reference_time
        self._warned: set[tuple[str, str, str]] = set()   # (거래일, 계좌, 한도)
        self._surge_until: datetime | None = None

    def scan(self, account: str, now: datetime) -> list[str]:
        day = last_kst_time_at_or_before(now, self._ref)
        fired: list[str] = []
        rows = self._repo.decisions_since(account, day)
        for r in rows:
            if r["result"] != "APPROVED" or r["symbol_after"] is None:
                continue
            for name, after, limit in (("L2 종목 비중", r["symbol_after"], r["symbol_limit"]),
                                       ("L3 총 노출", r["total_after"], r["total_limit"])):
                if limit is None or Decimal(limit) <= 0:
                    continue
                usage = Decimal(after) / Decimal(limit)
                key = (day.isoformat(), account, name)
                if usage >= self._t.warn_usage_ratio and key not in self._warned:
                    self._warned.add(key)
                    msg = f"{name} 사용률 {usage:.0%} (경고 기준 {self._t.warn_usage_ratio:.0%})"
                    self._alerts.alert("WARNING", msg, r["decision_id"])
                    fired.append(msg)
        window_start = now - timedelta(minutes=self._t.reject_surge_minutes)
        rejected = [r for r in rows if r["result"] == "REJECTED" and r["created_at"] >= to_iso(window_start)]
        if len(rejected) >= self._t.reject_surge_count and (self._surge_until is None or now >= self._surge_until):
            self._surge_until = now + timedelta(minutes=self._t.reject_surge_minutes)
            reasons: dict[str, int] = {}
            for r in rejected:
                reasons[r["failed_check"] or "?"] = reasons.get(r["failed_check"] or "?", 0) + 1
            msg = (f"거부 급증: 최근 {self._t.reject_surge_minutes}분 {len(rejected)}건 "
                   f"({json.dumps(reasons, ensure_ascii=False)})")
            self._alerts.alert("WARNING", msg, None)
            fired.append(msg)
        return fired


def same_day_buy_cash(repo: Repository, account: str, now: datetime, rates: CostRates,
                      since: datetime | None = None) -> Decimal:
    """오늘(거래일) 내부 기록 매수 체결 금액 + 추정 비용 (RV-23). since가 있으면 그 시각 이후 체결만.

    RV-23, PAPER-0에서 entr·d2_entra 의미 실측 후 재결정: 키움 kt00001 entr(예수금)가 당일 체결을 반영하는지(T+2)
    미확인이라, 반영 여부를 모르는 당일 매수 현금 유출을 보수적으로 따로 계산한다. 체결 시각은 통보 시각(없으면 수신 시각).
    """
    rows = repo.select(
        "SELECT f.quantity, f.price, f.at FROM fill f JOIN broker_order b ON f.order_key=b.order_key "
        "WHERE b.account=? AND b.side='BUY' AND b.trade_date=?", (account, repo.trade_date_of(now)))
    total = Decimal("0")
    for r in rows:
        if since is not None and from_iso(r["at"]) <= since:
            continue
        amount = Decimal(r["quantity"]) * Decimal(r["price"])
        total += amount + estimate_cost(amount, False, rates)
    return total


DAILY_BASE = DAILY_BASE_KIND   # 거래일별 일일 기준 감사 기록 (재검토2 #2). 형식은 domain.daily_base.DailyBase (A-084 ①)
DAILY_BASE_CORRUPT_ALERT = "일일 기준 기록 손상 — 사람 확인 필요"   # 재검토 M-1


def same_day_cash_flow(repo: Repository, account: str, now: datetime, rates: CostRates,
                       since: datetime | None = None) -> Decimal:
    """오늘(거래일) 내부 기록 체결의 현금흐름: 매도 대금 − 비용·세금 − (매수 대금 + 비용). since 이후 체결만 (재검토 F3).

    일일 손익을 브로커 예수금(entr)의 당일 반영 여부(T+2, 미실측)와 무관하게 계산하려고 쓴다. 매수·매도 대칭.
    """
    rows = repo.select(
        "SELECT b.side, f.quantity, f.price, f.at FROM fill f JOIN broker_order b ON f.order_key=b.order_key "
        "WHERE b.account=? AND b.trade_date=?", (account, repo.trade_date_of(now)))
    total = Decimal("0")
    for r in rows:
        if since is not None and from_iso(r["at"]) < since:   # 기준 시각과 같은 체결은 포함
            continue
        amount = Decimal(r["quantity"]) * Decimal(r["price"])
        if r["side"] == "SELL":
            total += amount - estimate_cost(amount, True, rates)
        else:
            total -= amount + estimate_cost(amount, False, rates)
    return total


class DailyRiskTracker:
    """게이트 입력(AccountFacts.daily_risk)의 실운영 구현. 현금은 공급자(브로커 조회·내부 계산)가 준다.

    clock·cost_rates를 주면(실운영 조립) RV-23 보정을 한다: 현금 조회 시각을 기록하고, 일일 손익의 현금은
    "기준 시각에 고정한 현금 + 그 뒤 내부 체결 현금흐름(매도 대금 − 비용·세금 − 매수 대금·비용)"으로 본다.
    장중 예수금(entr)이 당일 체결을 반영하는지(T+2)와 무관하고 매수·매도가 대칭이다 (재검토 F3 — 이전 방식은
    매수 이중 차감 또는 매도 대금 누락으로 어느 가정이든 K2 오발동). 외부·수동 거래는 대조(K4)가 잡는다.
    RV-23, PAPER-0에서 entr·d2_entra 의미 실측 후 재결정.
    """

    def __init__(self, repo: Repository, price_of: Callable[[str], Decimal | None], *,
                 clock: Clock | None = None, cost_rates: CostRates | None = None,
                 alerts: AlertSink | None = None) -> None:
        self._repo = repo
        self._alerts = alerts
        self._corrupt_alerted: set[tuple[str, str]] = set()   # (거래일, 계좌) — 손상 알림은 프로세스에서 하루 1회
        self._price_of = price_of
        self._clock = clock
        self._rates = cost_rates
        self._base: dict[str, Decimal] = {}
        self._cash: dict[str, Decimal] = {}
        self._cash_at: dict[str, datetime] = {}
        self._base_cash: dict[str, Decimal] = {}
        self._base_at: dict[str, datetime] = {}

    def start_day(self, account: str, cash: Decimal) -> Decimal | None:
        """일일 기준 시각에 호출. 기준 금액을 고정한다. 평가 불능이면 None(그날 위험 확대 거부).

        실운영(clock·cost_rates 있음)에서는 거래일별 기준(현금·평가액·시각)을 감사 기록 DAILY_BASE로 남기고, 같은
        거래일에 다시 부르면(재시작) 저장된 기준을 쓴다 — 재시작으로 그날 손실이 L4에서 사라지지 않게 (재검토2 #2).
        저장된 기준이 손상됐으면 새로 계산·기록하지 않고 None(모름 → 위험 확대 거부)과 EMERGENCY 알림 1회 (재검토 M-1).
        """
        try:
            saved = self._saved_base(account)
        except DailyBaseCorrupt:
            self._base.pop(account, None)
            self._corrupt_alert(account)
            return None
        if saved is not None:
            self._base[account] = saved.total
            self._base_cash[account] = saved.cash
            self._base_at[account] = saved.at
            self._cash.setdefault(account, cash)
            return self._base[account]
        value = self._positions_value(account)
        if value is None:
            self._base.pop(account, None)
            return None
        self._base[account] = cash + value
        self._cash[account] = cash
        self._base_cash[account] = cash
        if self._clock is not None:
            self._base_at[account] = self._clock.now()
            if self._rates is not None:
                now = self._clock.now()
                base = DailyBase(self._repo.trade_date_of(now), cash, value, now)
                with self._repo.tx():
                    self._repo.audit(now, DAILY_BASE, "monitor", account, base.to_summary())
        return self._base[account]

    def _corrupt_alert(self, account: str) -> None:
        assert self._clock is not None
        key = (self._repo.trade_date_of(self._clock.now()), account)
        if key in self._corrupt_alerted or self._alerts is None:
            return
        self._corrupt_alerted.add(key)
        self._alerts.alert("EMERGENCY", DAILY_BASE_CORRUPT_ALERT, account)

    def _saved_base(self, account: str) -> DailyBase | None:
        if self._clock is None or self._rates is None:
            return None
        return self._repo.daily_base(account, self._repo.trade_date_of(self._clock.now()))

    def update_cash(self, account: str, cash: Decimal) -> None:
        self._cash[account] = cash
        if self._clock is not None:
            self._cash_at[account] = self._clock.now()

    def cash_at(self, account: str) -> datetime | None:
        """마지막 현금 조회 반영 시각 (RV-23 신선도). 시계가 없으면 None."""
        return self._cash_at.get(account)

    def _pnl_cash(self, account: str) -> Decimal:
        cash = self._cash[account]
        if self._clock is None or self._rates is None:
            return cash
        base_cash = self._base_cash.get(account, cash)
        base_at = self._base_at.get(account)
        # 거래일은 기준 시각의 거래일로 고정한다 — 자정이 지나 다음 기준 시각 전까지 당일 체결이 빠지면 거짓 손익 (재검토2 #1)
        day_of = base_at if base_at is not None else self._clock.now()
        return base_cash + same_day_cash_flow(self._repo, account, day_of, self._rates, since=base_at)

    def _positions_value(self, account: str) -> Decimal | None:
        total = Decimal("0")
        for inst, p in self._repo.positions(account).items():
            if p.quantity <= 0:
                continue
            price = self._price_of(inst)
            if price is None:
                return None
            total += p.quantity * price
        return total

    def daily_risk(self, account: str) -> DailyRisk | None:
        base = self._base.get(account)
        if base is None or base <= 0:
            return None
        value = self._positions_value(account)
        if value is None:
            return DailyRisk(base, Decimal("0"), Decimal("0"), Decimal("0"), evaluable=False)
        return DailyRisk(base, Decimal("0"), self._pnl_cash(account) + value - base, Decimal("0"))


class ConservativeCashFacts:
    """게이트 입력 AccountFacts 감싸기 (RV-23). G8이 쓰는 현금을 보수적으로 준다.

    - 신선도: 마지막 현금 조회가 max_age보다 오래됐거나 기록이 없으면 None(모름) → G8이 위험 확대를 거부한다.
      위험 축소(감액·청산·취소)는 G8을 거치지 않아 영향이 없다.
    - 예수금: 조회 값 − 당일 매수 체결 전액(비용 포함). entr가 T+2로 당일 매수를 반영하지 않을 수 있어서.
    - 주문가능현금: 조회 값 − 조회 시각 이후 매수 체결(비용 포함). 조회 시점까지는 브로커가 반영했다고 본다.
    max_age는 판정 방식 값(임시): 조립 지점이 대조 주기 × 3으로 정한다. RV-23, PAPER-0에서 entr·d2_entra 의미 실측 후 재결정.
    """

    def __init__(self, inner: object, tracker: DailyRiskTracker, clock: Clock, max_age: timedelta) -> None:
        if tracker._rates is None:  # noqa: SLF001
            raise ValueError("ConservativeCashFacts에는 비용 요율이 있는 추적기가 필요")
        self._inner = inner
        self._tracker = tracker
        self._clock = clock
        self._max_age = max_age

    def daily_risk(self, account: str) -> DailyRisk | None:
        return self._inner.daily_risk(account)  # type: ignore[attr-defined, no-any-return]

    def cash(self, account: str) -> CashView | None:
        view = self._inner.cash(account)  # type: ignore[attr-defined]
        at = self._tracker.cash_at(account)
        now = self._clock.now()
        if view is None or at is None or now - at > self._max_age:
            return None
        rates = self._tracker._rates  # noqa: SLF001
        assert rates is not None
        return CashView(view.deposit - same_day_buy_cash(self._tracker._repo, account, now, rates),  # noqa: SLF001
                        view.broker_orderable - same_day_buy_cash(self._tracker._repo, account, now, rates,  # noqa: SLF001
                                                                  since=at))
