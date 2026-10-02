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

from trading_system.domain.clock import last_kst_time_at_or_before, to_iso
from trading_system.domain.market import DailyRisk
from trading_system.domain.sinks import AlertSink
from trading_system.storage.repository import Repository


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


class DailyRiskTracker:
    """게이트 입력(AccountFacts.daily_risk)의 실운영 구현. 현금은 공급자(브로커 조회·내부 계산)가 준다."""

    def __init__(self, repo: Repository, price_of: Callable[[str], Decimal | None]) -> None:
        self._repo = repo
        self._price_of = price_of
        self._base: dict[str, Decimal] = {}
        self._cash: dict[str, Decimal] = {}

    def start_day(self, account: str, cash: Decimal) -> Decimal | None:
        """일일 기준 시각에 호출. 기준 금액을 고정한다. 평가 불능이면 None(그날 위험 확대 거부)."""
        value = self._positions_value(account)
        if value is None:
            self._base.pop(account, None)
            return None
        self._base[account] = cash + value
        self._cash[account] = cash
        return self._base[account]

    def update_cash(self, account: str, cash: Decimal) -> None:
        self._cash[account] = cash

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
        return DailyRisk(base, Decimal("0"), self._cash[account] + value - base, Decimal("0"))
