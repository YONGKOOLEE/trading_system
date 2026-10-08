"""M2(크립토 현물) 현금 정책과 일일 리스크 상태 (CP-024 §2.4·§2.5, D-103·D-108·D-118, T-M2-07, V-151·V-140).

게이트 입력 AccountFacts(gate/ports.py)의 M2 구현이다. M1은 기존 DailyRiskTracker·ConservativeCashFacts(watchers.py)를
그대로 쓰고, 이 모듈은 M1 경로에서 쓰지 않는다(시장별 분리).

현금 (§2.5):
- 키움 T+2 보수 차감(당일 매수 체결 전액을 조회값에서 다시 빼기)은 쓰지 않는다 — 바이낸스 잔고는 체결 즉시 반영되어
  이중 차감이 된다.
- 주문 가능 USDT = min(거래소 USDT free, 내부 기대 USDT − 활성 매수 예약 − 추정 수수료(D-118 올림)).
  이 모듈은 CashView(deposit = 내부 기대 USDT, broker_orderable = 거래소 free)를 주고, 게이트 G8이
  min(broker_orderable, deposit − 활성 예약 현금)과 "주문 금액 + 추정 수수료"를 비교한다(gate.py _g8_cash).
- 내부 기대 USDT = 기준선 + Σ(기준선 이후 체결의 USDT 흐름) (D-103). 매수 체결은 기록 즉시 기대 USDT를 줄인다(V-151).
- 거래소 잔고 조회가 없거나 max_age보다 오래됐거나, 기준선이 없거나, 호가 자산을 모르는 체결이 있으면 None(모름)
  → G8이 위험 확대를 거부한다. 위험 축소는 G8을 거치지 않는다.

일일 리스크 (§2.4, D-108):
- 기준 금액 = 일 경계(UTC 00:00) 이후에 조회한 USDT free + locked + 보유 평가액. 거래일 = 시장 시각(UTC) 날짜.
- 당일 손익 = (기준 시점 USDT + 기준 시점 이후 체결 USDT 흐름 + 현재 보유 평가액) − 기준 금액.
- 거래일이 바뀌었는데 새 기준을 고정하지 않았으면 None → 위험 확대 거부(전날 손실이 남거나 사라지지 않게).
- 거래일별 기준을 감사 기록 DAILY_BASE(market = M2)로 남기고, 같은 거래일에 재시작하면 저장된 기준을 쓴다.

[제안] 결정이 문서에 없어 보수 쪽으로 정한 것 (T-M2-07):
- (1) 체결 수수료·수수료 자산은 체결 기록 스키마에 아직 없다(T-M2-04). 그 전까지 매수·매도 모두 "USDT로 낸 수수료"로
  보고 추정 수수료(D-118 올림)를 기대 USDT에서 뺀다. 기초 자산 수수료 매수에서는 기대 USDT가 실제보다 작아진다(보수).
  T-M2-04에서 수수료 기록·거래소 보고 금액(Y·quoteQty)으로 바꾼다. 체결 금액은 price × qty(D-118 임시값).
- (2) 기준선 시각과 같은 시각의 체결: 매수는 포함(차감), 매도는 제외(수령액 미반영) — 어느 쪽이든 기대 USDT가 작아짐.
- (3) 기준선 설정 = set_baseline(사람 확정, D-103 최초 시작·리셋 확인). 감사 기록 SPOT_QUOTE_BASELINE으로 남기고
  재시작 때 마지막 값을 복원한다. 기준선 확정 절차·대조(K4)는 T-M2-06.
- (4) 기준 금액은 일 경계 이후에 조회한 거래소 잔고로만 정한다(경계 이전 조회값 거부). 같은 거래일의 첫 성공 값으로 고정.
  조회 시각 이후(같은 시각 포함) 체결이 있으면 정하지 않는다 — 현금은 조회 시점, 평가액은 현재 포지션이라 매도가
  끼면 가짜 이익으로 L4가 가려진다(재검토 2026-10-08). 다음 조회 뒤 다시 정한다.
- (5) "유니버스 보유"는 이 계좌의 내부 포지션(수량 > 0) 전체로 본다. 평가 가격을 모르면 기준 금액을 정하지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from trading_system.domain.clock import Clock, from_iso, require_aware
from trading_system.domain.market import CashView, CostRates, DailyRisk, estimate_cost
from trading_system.domain.market_time import M2_TIME, MarketTime
from trading_system.storage.repository import Repository

DAILY_BASE = "DAILY_BASE"                 # watchers.DAILY_BASE와 같은 종류, 요약에 market 필드
QUOTE_BASELINE = "SPOT_QUOTE_BASELINE"    # 내부 기대 USDT 기준선 (D-103)
_ZERO = Decimal("0")


@dataclass(frozen=True)
class QuoteBalance:
    """거래소 호가 자산(USDT) 잔고 조회 결과 (D-103: 수량 = free + locked)."""

    free: Decimal
    locked: Decimal
    at: datetime  # 조회 시각

    def __post_init__(self) -> None:
        for n in ("free", "locked"):
            v = getattr(self, n)
            if not isinstance(v, Decimal) or not v.is_finite() or v < 0:
                raise ValueError(f"{n}는 0 이상의 유한한 Decimal이어야 한다")
        require_aware(self.at)

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True)
class _DayBase:
    trade_date: str
    cash: Decimal       # 기준 시점 USDT free + locked
    value: Decimal      # 기준 시점 보유 평가액
    at: datetime        # 기준 USDT 조회 시각 (이후 체결이 흐름에 들어감)

    @property
    def amount(self) -> Decimal:
        return self.cash + self.value


def quote_flow(repo: Repository, account: str, since: datetime, rates: CostRates,
               quote_of: Callable[[str], str | None], quote_asset: str) -> Decimal | None:
    """`since` 이후 체결이 호가 자산(USDT)을 바꾼 양. 매도 +(금액 − 추정 수수료), 매수 −(금액 + 추정 수수료).

    금액 = 체결 수량 × 체결 가격(D-118 임시값), 추정 수수료 = estimate_cost(D-118 올림) — [제안](1).
    매수는 since와 같은 시각 포함, 매도는 제외 — [제안](2). 호가 자산이 다르거나 모르는 종목의 체결이 있으면 None.
    """
    rows = repo.select(
        "SELECT b.side, b.instrument, f.quantity, f.price, f.at FROM fill f "
        "JOIN broker_order b ON f.order_key=b.order_key WHERE b.account=?", (account,))
    total = _ZERO
    for r in rows:
        at = from_iso(r["at"])
        is_sell = r["side"] == "SELL"
        if at < since or (is_sell and at == since):
            continue
        if quote_of(r["instrument"]) != quote_asset:
            return None
        amount = Decimal(r["quantity"]) * Decimal(r["price"])
        cost = estimate_cost(amount, is_sell, rates)
        total += (amount - cost) if is_sell else -(amount + cost)
    return total


class SpotAccountFacts:
    """M2 게이트 입력 AccountFacts: 현금(CashView)과 일일 리스크(DailyRisk). 모듈 설명 참조."""

    def __init__(self, repo: Repository, clock: Clock, *, quote_asset: str,
                 asset_pair: Callable[[str], tuple[str, str] | None],
                 price_of: Callable[[str], Decimal | None], cost_rates: CostRates, reference_time: time,
                 max_age: timedelta, market_time: MarketTime = M2_TIME) -> None:
        if max_age <= timedelta(0):
            raise ValueError("max_age는 0보다 커야 한다")
        self._repo = repo
        self._clock = clock
        self._quote = quote_asset
        self._pair = asset_pair
        self._price_of = price_of
        self._rates = cost_rates
        self._ref = reference_time
        self._max_age = max_age
        self._mt = market_time
        self._balance: dict[str, QuoteBalance] = {}
        self._baseline: dict[str, tuple[Decimal, datetime]] = {}
        self._day: dict[str, _DayBase] = {}

    # --- 입력 -----------------------------------------------------------------
    def update_balance(self, account: str, balance: QuoteBalance) -> None:
        """거래소 USDT 잔고 조회 결과 반영 (시작·주기 대조·일 경계, T-M2-06·T-M2-08이 호출)."""
        prev = self._balance.get(account)
        if prev is None or balance.at >= prev.at:  # 늦게 도착한 오래된 조회로 덮지 않음
            self._balance[account] = balance

    def set_baseline(self, account: str, amount: Decimal, at: datetime) -> None:
        """내부 기대 USDT 기준선 확정 (D-103: 최초 시작·"리셋 확인" 때 사람이 확정). 감사 기록을 남긴다."""
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError("기준선은 0 이상의 유한한 Decimal이어야 한다")
        require_aware(at)
        with self._repo.tx():
            self._repo.audit(self._clock.now(), QUOTE_BASELINE, "operator", account, {
                "market": self._mt.market.value, "asset": self._quote, "amount": str(amount), "at": at.isoformat()})
        self._baseline[account] = (amount, at)

    def baseline(self, account: str) -> Decimal | None:
        b = self._load_baseline(account)
        return None if b is None else b[0]

    # --- 현금 (§2.5) ------------------------------------------------------------
    def expected_quote(self, account: str) -> Decimal | None:
        """내부 기대 USDT = 기준선 + 기준선 이후 체결 흐름. 모르면 None."""
        b = self._load_baseline(account)
        if b is None:
            return None
        flow = quote_flow(self._repo, account, b[1], self._rates, self._quote_of, self._quote)
        return None if flow is None else b[0] + flow

    def cash(self, account: str) -> CashView | None:
        bal = self._fresh_balance(account)
        expected = self.expected_quote(account)
        if bal is None or expected is None:
            return None
        return CashView(deposit=expected, broker_orderable=bal.free)

    # --- 일일 리스크 (§2.4, D-108) ------------------------------------------------
    def start_day(self, account: str) -> Decimal | None:
        """현재 거래일(UTC)의 기준 금액을 고정한다. 이미 고정(저장)됐으면 그 값. 정할 수 없으면 None."""
        today = self._today()
        cur = self._day.get(account)
        if cur is not None and cur.trade_date == today:
            return cur.amount
        saved = self._saved_day(account, today)
        if saved is not None:
            self._day[account] = saved
            return saved.amount
        boundary = self._mt.last_at_or_before(self._clock.now(), self._ref)
        bal = self._fresh_balance(account)
        if bal is None or bal.at < boundary:  # [제안](4) 일 경계 이전 조회값으로 정하지 않음
            return None
        if self._has_fill_since(account, bal.at):  # 조회 뒤 체결: 현금(조회 시점)·평가액(현재) 시점 불일치
            return None
        value = self._positions_value(account)
        if value is None or bal.total + value <= 0:
            return None
        day = _DayBase(today, bal.total, value, bal.at)
        with self._repo.tx():
            self._repo.audit(self._clock.now(), DAILY_BASE, "monitor", account, {
                "market": self._mt.market.value, "trade_date": today, "cash": str(day.cash),
                "value": str(day.value), "at": day.at.isoformat()})
        self._day[account] = day
        return day.amount

    def daily_risk(self, account: str) -> DailyRisk | None:
        day = self._day.get(account)
        if day is None or day.trade_date != self._today():
            return None  # 새 거래일 기준 미고정 → 모름
        value = self._positions_value(account)
        flow = quote_flow(self._repo, account, day.at, self._rates, self._quote_of, self._quote)
        if value is None or flow is None:
            return DailyRisk(day.amount, _ZERO, _ZERO, _ZERO, evaluable=False)
        return DailyRisk(day.amount, _ZERO, day.cash + flow + value - day.amount, _ZERO)

    # --- 내부 -----------------------------------------------------------------
    def _today(self) -> str:
        return self._mt.trade_date(self._clock.now(), self._ref).isoformat()

    def _quote_of(self, instrument: str) -> str | None:
        pair = self._pair(instrument)
        return None if pair is None else pair[1]

    def _fresh_balance(self, account: str) -> QuoteBalance | None:
        bal = self._balance.get(account)
        if bal is None or self._clock.now() - bal.at > self._max_age:
            return None
        return bal

    def _has_fill_since(self, account: str, since: datetime) -> bool:
        rows = self._repo.select(
            "SELECT f.at FROM fill f JOIN broker_order b ON f.order_key=b.order_key WHERE b.account=?", (account,))
        return any(from_iso(r["at"]) >= since for r in rows)

    def _positions_value(self, account: str) -> Decimal | None:
        total = _ZERO
        for inst, p in self._repo.positions(account).items():
            if p.quantity <= 0:
                continue
            price = self._price_of(inst)
            if price is None:
                return None
            total += p.quantity * price
        return total

    def _load_baseline(self, account: str) -> tuple[Decimal, datetime] | None:
        if account not in self._baseline:
            rows = self._repo.select(
                "SELECT summary FROM audit_event WHERE kind=? AND ref=? ORDER BY seq DESC LIMIT 1",
                (QUOTE_BASELINE, account))
            if not rows:
                return None
            rec = json.loads(rows[0]["summary"])
            if rec.get("asset") != self._quote:
                return None
            self._baseline[account] = (Decimal(rec["amount"]), datetime.fromisoformat(rec["at"]))
        return self._baseline[account]

    def _saved_day(self, account: str, trade_date: str) -> _DayBase | None:
        found = None
        for e in self._repo.select("SELECT summary FROM audit_event WHERE kind=? AND ref=? ORDER BY seq",
                                   (DAILY_BASE, account)):
            rec = json.loads(e["summary"])
            if rec.get("market") == self._mt.market.value and rec.get("trade_date") == trade_date:
                found = rec
        if found is None:
            return None
        return _DayBase(trade_date, Decimal(found["cash"]), Decimal(found["value"]),
                        datetime.fromisoformat(found["at"]))
