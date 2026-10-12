"""M2(크립토 현물) 백테스트 실행기 (R-10, 04 D-071~D-077·2-M2.2, CP-024 §2.6·D-101·D-104·D-108, T-M2-12).

M1 실행기(app/backtest.py — KRX 달력·KST·호가 표)는 그대로 두고 M2용을 따로 둔다(일반화 = 같은 전략·판정 실행기·게이트·
실행기·정지 관리, 브로커만 M2 체결 모델 SpotBacktestBroker, D-071).
- 데이터: UTC 일봉(봉 시작 날짜로 식별). 1d 봉은 "봉 시작 + 1일" 이후에만 확정 → 판정 시각에 진행 중인 봉은 전략에 주지 않는다
  (V-142, V-90 as-of 필터). 테스트넷 klines 이력 길이·품질은 미확인(B-U4) — 보고서 한계에 적는다.
- 하루(UTC 날짜) 한 번 판정(기본 00:15 UTC — 주문 허용 창 00:10~23:50 안, 일 경계 = UTC 00:00, D-108). 판정 시세 = 그날 봉 시가(판정 시점에 아는 값).
  주문 유형·가격은 의도를 만들 때 정한다(D-104: 매수 IOC 지정가 = 시가 × (1 + 완충)을 tick 내림, 매도 시장가).
  체결은 체결 모델이 같은 시점 가격(시가)에 슬리피지를 불리하게 적용해 정한다 — 매수는 지정가 이하일 때만(IOC 모사).
- 비용: 수수료는 받는 자산에서 차감(D-101), 매도세 0. 현금(USDT) = 매수 −금액, 매도 +금액 − USDT 수수료.
- 보유 표시(전략 입력): 청산 가능 수량(보유를 stepSize로 내림)만 보여 주고, minQty 미만(dust)은 미보유로 본다(D-102) [제안].
  왕복(거래 1건)은 매도 뒤 남은 보유가 dust 이하가 되면 끝난다(남은 dust 가치는 손익에 넣지 않음 — 보수).
- 일일 손실 K2는 판정 시점에 한 번 평가, 다음 날 재개 가정(M1 DP-6과 같음).
- 재현성(D-074): 지문 = 신호 ID·체결·평가액 해시. 룩어헤드 교란 검사(V-91): perturbation_check_m2.
- 연환산은 365일(24/7) — M1의 252와 다르다.
[제안] 전략 등록(StrategyRegistry·capabilities M2 열)은 아직 없어(capabilities.md 수정 금지 구간) 전략 상태 검사는 생략하고
보고서에 적는다. 최종 확인 구간 보호(D-163)는 final_from 이후 날짜가 섞이면 거부하는 것만 둔다(장부 연동은 M2 전략 등록 때).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from trading_system.app.backtest import BacktestError, BacktestResult, Trade
from trading_system.broker.fake.spot_fill_model import SpotBacktestBroker
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import GateResult, HaltType, IntentState, Mode, RequestType, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import (
    CashView, CostRates, DailyRisk, InstrumentInfo, InstrumentKind, Quote, SessionInfo, TradingStatus,
)
from trading_system.domain.market_time import M2_TIME, daily_bar_is_final, session_247
from trading_system.domain.models import Account, GateRequest
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView, evaluation_id_of
from trading_system.domain.trading_rules import SpotOrderPolicy, SymbolRules, apply_spot_terms
from trading_system.evaluation.runner import run_evaluation
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs, GatePolicy
from trading_system.halt.manager import Channel, HaltManager
from trading_system.storage.repository import OrderIdentity, Repository
from trading_system.strategy.base import Strategy

ACCOUNT = "BN-BT-01"
FINAL_FROM_DEFAULT = date(2026, 1, 1)   # CP-030 D-163과 같은 날짜 (M2 적용은 [제안])
_ZERO = Decimal("0")


@dataclass(frozen=True)
class SpotBacktestConfig:
    run_id: str
    strategy: Strategy
    params: dict[str, object]
    account_limits: AccountLimits          # M2 한도 (UTC 시간 값)
    rules: dict[str, SymbolRules]          # 심볼별 거래 규칙 (가상 또는 exchangeInfo 적재값)
    pairs: dict[str, tuple[str, str]]      # 심볼 → (기초 자산, 호가 자산)
    cost_rates: CostRates                  # 수수료율 = 백테스트 요율(사용자 확인 ▢), 매도세 0
    data: dict[str, list[Bar]]             # UTC 일봉 (Bar.at = 봉 시작 날짜)
    start: date
    end: date
    initial_cash: Decimal                  # USDT
    out_dir: Path
    slippage: Decimal                      # 필수 — 사용자 결정 값(2-M2.2 ▢). 기본값 없음
    eval_time: time = time(0, 15)          # UTC — 주문 허용 창(UTC 00:10~23:50, A-064) 안 [제안]
    buy_buffer: Decimal = Decimal("0.005")   # A-064
    sell_buffer: Decimal = Decimal("0.005")  # A-065 Q4
    quote_max_age: timedelta = timedelta(seconds=60)   # A-064
    volume_cap: Decimal = Decimal("0.10")
    data_version: str = ""
    code_commit: str = ""
    final_from: date = FINAL_FROM_DEFAULT


class _Market:
    def __init__(self, cfg: SpotBacktestConfig) -> None:
        self._cfg = cfg
        self.quotes: dict[str, Quote] = {}

    def quote(self, instrument: str) -> Quote | None:
        return self.quotes.get(instrument)

    def instrument(self, instrument: str) -> InstrumentInfo | None:
        r = self._cfg.rules.get(instrument)
        # 상품 유형 열거(InstrumentKind)는 M1 코드가 전체 순회하므로 늘리지 않는다(T-M2-01과 같음). 레버리지 없음
        return None if r is None else InstrumentInfo(instrument, InstrumentKind.STOCK, Decimal("1"), r.lot_step)

    def trading_status(self, instrument: str) -> frozenset[TradingStatus] | None:
        return frozenset() if instrument in self._cfg.rules else None

    def session(self, trade_date: date) -> SessionInfo | None:
        return session_247(trade_date)

    def tick_valid(self, instrument: str, price: Decimal) -> bool | None:
        r = self._cfg.rules.get(instrument)
        return None if r is None else (r.tick_size == 0 or price % r.tick_size == 0)

    def tick_of(self, instrument: str, price: Decimal) -> Decimal | None:
        r = self._cfg.rules.get(instrument)
        return None if r is None else r.tick_size


class _Rules:
    def __init__(self, cfg: SpotBacktestConfig, market: _Market) -> None:
        self._cfg, self._m = cfg, market

    def rules(self, instrument: str) -> SymbolRules | None:
        return self._cfg.rules.get(instrument)

    def market_prices(self, instrument: str) -> tuple[Decimal, ...]:
        q = self._m.quote(instrument)
        return () if q is None else (q.price,)


class _Account:
    def __init__(self) -> None:
        self.daily: DailyRisk | None = None
        self.cash_view: CashView | None = None

    def daily_risk(self, account: str) -> DailyRisk | None:
        return self.daily

    def cash(self, account: str) -> CashView | None:
        return self.cash_view


class _Strategies:
    def signal_rejection(self, owner: str, request_type: RequestType) -> str | None:
        return None   # [제안] M2 전략 등록·상태 검사는 T-M2-17에서 연결

    def strategy_limits(self, owner: str):  # type: ignore[no-untyped-def]
        return None


class _ClockOk:
    def clock_ok(self) -> bool | None:
        return True


class _Limits:
    def __init__(self, lim: AccountLimits) -> None:
        self._lim = lim

    def effective_limits(self, account: str, at: datetime | None = None) -> AccountLimits | None:
        return self._lim


def final_bars(bars: list[Bar], as_of: datetime) -> tuple[Bar, ...]:
    """as_of 시점에 확정된 1d 봉만 (V-90·V-142): 봉 시작 + 1일 ≤ as_of."""
    return tuple(b for b in bars if isinstance(b.at, date) and not isinstance(b.at, datetime)
                 and daily_bar_is_final(b.at, as_of))


def sellable(quantity: Decimal, r: SymbolRules) -> Decimal:
    step = r.market_step if r.market_step > 0 else r.lot_step
    q = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step if quantity > 0 else _ZERO
    return q if q >= max(r.market_min_qty, r.lot_min_qty) else _ZERO


def _check_config(cfg: SpotBacktestConfig) -> list[date]:
    if not isinstance(cfg.slippage, Decimal) or not cfg.slippage.is_finite() or not _ZERO <= cfg.slippage < 1:
        raise BacktestError("슬리피지는 0 이상 1 미만의 Decimal — 사용자 결정 값(2-M2.2 ▢)을 지정해야 함")
    for inst, bars in cfg.data.items():
        if inst not in cfg.rules or inst not in cfg.pairs:
            raise BacktestError(f"규칙·자산 쌍 없는 심볼: {inst}")
        days = [b.at for b in bars]
        if len(days) != len(set(days)) or any(not (b.low <= min(b.open, b.close) and max(b.open, b.close) <= b.high
                                                   and b.low > 0 and b.volume >= 0) for b in bars):
            raise BacktestError(f"일봉 품질 실패: {inst} (중복 날짜·가격 관계·음수)")
    days = []
    d = cfg.start
    while d <= cfg.end:
        days.append(d)
        d += timedelta(days=1)
    if not days:
        raise BacktestError("구간에 날짜가 없음")
    if days[-1] >= cfg.final_from:
        raise BacktestError(f"실행 날짜가 최종 확인 구간({cfg.final_from} 이후)과 겹침 (D-163 [제안] M2 적용)")
    return days


def run_backtest_m2(cfg: SpotBacktestConfig) -> BacktestResult:
    days = _check_config(cfg)
    spec = cfg.strategy.spec
    run_dir = cfg.out_dir / cfg.run_id
    if (run_dir / "trading.db").exists():
        raise BacktestError(f"같은 실행 ID의 DB가 이미 있음: {run_dir}")
    repo = Repository(run_dir / "trading.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    repo.register_account(Account(ACCOUNT, Mode.BACKTEST, "fake"))
    clock = FixedClock(datetime.combine(days[0], cfg.eval_time, tzinfo=UTC))
    market = _Market(cfg)
    acct = _Account()
    policy = GatePolicy(cfg.quote_max_age, Decimal("0.05"), cfg.cost_rates, market_time=M2_TIME)
    order_policy = SpotOrderPolicy(cfg.buy_buffer, cfg.sell_buffer, cfg.quote_max_age)
    broker = SpotBacktestBroker(pairs=cfg.pairs, fee_rate=cfg.cost_rates.commission_rate, slippage=cfg.slippage,
                                tick_of=lambda i: cfg.rules[i].tick_size, step_of=lambda i: cfg.rules[i].lot_step,
                                volume_cap=cfg.volume_cap)
    executor = Executor(repo, broker, clock, run_dir / "KILL", asset_pair=cfg.pairs.get, client_ids=True)
    gate = Gate(repo, clock, GateInputs(_Limits(cfg.account_limits), market, acct, _Strategies(), _ClockOk(),
                                        spot_rules=_Rules(cfg, market)), policy)
    halts = HaltManager(repo, gate, executor, clock, reference_time_of=lambda a: cfg.account_limits.daily_reference_time,
                        market_time=M2_TIME)
    by_day = {inst: {b.at: b for b in bars} for inst, bars in cfg.data.items()}

    cash = cfg.initial_cash
    costs = _ZERO
    equity: list[tuple[date, Decimal]] = []
    rejects: Counter = Counter()
    signals: list[str] = []
    fills_log: list[tuple[date, str, str, str, str, str]] = []
    signals_by_eval: dict[str, list[tuple]] = {}
    seen = 0
    k2_resumes = 0
    prev_close: dict[str, Decimal] = {}

    for day in days:
        now = datetime.combine(day, cfg.eval_time, tzinfo=UTC)
        clock._now = now  # noqa: SLF001
        if HaltType.K2.value in repo.active_halts(ACCOUNT):
            if halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "backtest").ok:
                k2_resumes += 1
        market.quotes = {inst: Quote(inst, b[day].open, now) for inst, b in by_day.items() if day in b}
        positions = {k: v for k, v in repo.positions(ACCOUNT).items() if v.quantity > 0}

        def mark(inst: str, qty: Decimal) -> Decimal:
            b = by_day[inst].get(day)
            return qty * (b.open if b is not None else prev_close.get(inst, _ZERO))

        base = cash + sum((p.quantity * prev_close.get(k, p.avg_price) for k, p in positions.items()), _ZERO)
        if base <= 0:
            raise BacktestError(f"{day} 기준 금액 0 이하")
        equity_open = cash + sum((mark(k, p.quantity) for k, p in positions.items()), _ZERO)
        acct.daily = DailyRisk(base, _ZERO, equity_open - base, _ZERO)
        reserved = sum((r.cash_remaining for r in repo.active_reservations(ACCOUNT)), _ZERO)
        acct.cash_view = CashView(cash, cash - reserved)
        halts.check_daily_loss(ACCOUNT, acct.daily, cfg.account_limits.l4_daily_loss_ratio)

        ev_key = f"{day.isoformat()}T{cfg.eval_time:%H:%M}Z"
        ev_id = evaluation_id_of(spec.strategy_id, spec.version, "M2", ev_key)
        views = []
        for inst in sorted(cfg.data):
            pos = repo.position(ACCOUNT, inst)
            own = repo.active_ownership(ACCOUNT, inst)
            q = sellable(pos.quantity, cfg.rules[inst])
            pv = PositionView(q, pos.avg_price, 0, own is not None and own.owner == spec.owner) if q > 0 else None
            pending = bool(repo.active_new_intents(ACCOUNT, inst, Side.BUY.value)
                           or repo.active_new_intents(ACCOUNT, inst, Side.SELL.value))
            qt = market.quote(inst)
            views.append(InstrumentView(inst, daily_bars=final_bars(cfg.data[inst], now),
                                        last_price=qt.price if qt else None, position=pv, has_pending_order=pending,
                                        data_ok=qt is not None))
        snap = EvaluationSnapshot(ev_id, "M2", ACCOUNT, now, base, tuple(views),
                                  next_evaluation_at=now + timedelta(days=1))
        result = run_evaluation(cfg.strategy, snap, cfg.params,
                                lot_of={i: r.lot_step for i, r in cfg.rules.items()})
        signals_by_eval[ev_key] = [(s.signal_id, s.kind.value, s.instrument, str(s.quantity), str(s.price))
                                   for s in result.signals]
        for s in result.signals:
            signals.append(s.signal_id)
            req = apply_spot_terms(GateRequest.from_signal(s), market.quote(s.instrument), now,
                                   cfg.rules.get(s.instrument), order_policy)
            if req is None:
                rejects["TERMS"] += 1   # 주문 유형·가격을 정할 수 없음 (시세·규칙 없음) — 의도 없음
                continue
            decision = gate.decide(req)
            if decision.result is GateResult.APPROVED and decision.approved is not None:
                executor.submit(decision.approved)
            else:
                rejects[str(decision.failed_check)] += 1
        executor.process_events()
        for inst, b in by_day.items():
            if day in b:
                broker.match(inst, b[day].open, b[day].volume, now)
        broker.end_of_day()
        executor.process_events()

        for f in broker.fills_for(ACCOUNT)[seen:]:
            seen += 1
            if f.side == "BUY":
                cash -= f.quote_qty
                costs += f.commission * f.price if f.commission_asset != cfg.pairs[f.instrument][1] else f.commission
            else:
                cash += f.quote_qty - f.commission
                costs += f.commission
            fills_log.append((day, f.order_no, f.instrument, f.side, str(f.quantity), str(f.price)))

        for inst, b in by_day.items():
            if day in b:
                prev_close[inst] = b[day].close
        held = sum((p.quantity * prev_close.get(k, p.avg_price)
                    for k, p in repo.positions(ACCOUNT).items() if p.quantity > 0), _ZERO)
        equity.append((day, cash + held))

    unknown = repo.intents_in_state(IntentState.UNKNOWN)
    repo.close()
    if unknown:
        raise BacktestError("체결 모델에서 결과 불명이 생김 — 엔진 오류")
    trades = _round_trips(broker.fills_for(ACCOUNT), cfg)
    metrics = compute_metrics_m2(cfg.initial_cash, equity, trades, costs, len(days))
    digest = hashlib.sha256(json.dumps({"signals": signals, "fills": fills_log,
                                        "equity": [(d.isoformat(), str(e)) for d, e in equity]},
                                       sort_keys=True, default=str).encode()).hexdigest()
    return BacktestResult(cfg.run_id, "ALL", days, equity, trades, metrics, rejects, digest, run_dir / "trading.db",
                          signals, fills_log, k2_resumes, costs, {}, signals_by_eval)


def _round_trips(fills: list, cfg: SpotBacktestConfig) -> list[Trade]:  # type: ignore[type-arg]
    """거래 1건 = 진입부터 남은 보유가 dust 이하(청산 가능 수량 0)가 될 때까지 (D-102). 손익 = USDT 흐름 합."""
    trades: list[Trade] = []
    state: dict[str, dict] = {}  # type: ignore[type-arg]
    for f in fills:
        base = cfg.pairs[f.instrument][0]
        st = state.setdefault(f.instrument, {"qty": _ZERO, "cash": _ZERO, "entry": None})
        day = f.at.astimezone(UTC).date()
        if st["entry"] is None:
            st["entry"], st["cash"] = day, _ZERO
        if f.side == "BUY":
            st["qty"] += f.quantity - (f.commission if f.commission_asset == base else _ZERO)
            st["cash"] -= f.quote_qty
        else:
            st["qty"] -= f.quantity
            st["cash"] += f.quote_qty - f.commission
        if sellable(st["qty"], cfg.rules[f.instrument]) == 0:
            trades.append(Trade(f.instrument, st["entry"], day, st["cash"], (day - st["entry"]).days))
            st["entry"] = None
            st["qty"] = _ZERO if st["qty"] < 0 else st["qty"]
    return trades


def compute_metrics_m2(initial: Decimal, equity: list[tuple[date, Decimal]], trades: list[Trade], costs: Decimal,
                       n_days: int) -> dict[str, str]:
    """04 §2.6 지표 (비용 반영 후). 연환산 365일(24/7), 금액 자릿수는 8자리."""
    final = equity[-1][1] if equity else initial
    total = (final - initial) / initial if initial else _ZERO
    years = Decimal(n_days) / Decimal("365") if n_days else _ZERO
    annual = ((1 + total) ** (1 / years) - 1) if years > 0 and (1 + total) > 0 else _ZERO
    peak, mdd, longest, cur = initial, _ZERO, 0, 0
    for _, e in equity:
        if e >= peak:
            peak, cur = e, 0
        else:
            cur += 1
            longest = max(longest, cur)
            mdd = max(mdd, (peak - e) / peak)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    avg_win = sum(wins, _ZERO) / len(wins) if wins else _ZERO
    avg_loss = -sum(losses, _ZERO) / len(losses) if losses else _ZERO
    q, money = Decimal("0.000001"), Decimal("0.00000001")
    return {
        "total_return": str(total.quantize(q)),
        "annual_return": str(Decimal(str(annual)).quantize(q)),
        "max_drawdown": str(mdd.quantize(q)),
        "longest_drawdown_days": str(longest),
        "trades": str(len(trades)),
        "win_rate": str((Decimal(len(wins)) / len(trades)).quantize(q)) if trades else "0",
        "payoff_ratio": str((avg_win / avg_loss).quantize(q)) if avg_loss > 0 else "n/a",
        "avg_pnl": str((sum((t.pnl for t in trades), _ZERO) / len(trades)).quantize(money)) if trades else "0",
        "avg_holding_days": str((Decimal(sum(t.holding_days for t in trades)) / len(trades)).quantize(q))
        if trades else "0",
        "costs": str(costs.quantize(money)),
        "final_equity": str(final.quantize(money)),
    }


def perturbation_check_m2(cfg: SpotBacktestConfig, check_days: list[date]) -> list[str]:
    """V-91: 판정 시각 이후 데이터(그날 봉의 시가 외 값과 이후 봉 전부)를 크게 바꿔도 그날 신호가 같아야 한다.

    무보유 스냅샷 비교(flat). 그날 시가는 판정 시세(판정 시점에 아는 값)라 바꾸지 않는다. 다르면 그 날짜를 돌려준다."""
    spec = cfg.strategy.spec

    def signals_at(data: dict[str, list[Bar]], day: date) -> list[tuple]:  # type: ignore[type-arg]
        now = datetime.combine(day, cfg.eval_time, tzinfo=UTC)
        views = []
        for inst in sorted(data):
            today = next((b for b in data[inst] if b.at == day), None)
            views.append(InstrumentView(inst, daily_bars=final_bars(data[inst], now),
                                        last_price=today.open if today else None, data_ok=today is not None))
        ev = evaluation_id_of(spec.strategy_id, spec.version, "M2", f"{day.isoformat()}T{cfg.eval_time:%H:%M}Z")
        snap = EvaluationSnapshot(ev, "M2", ACCOUNT, now, cfg.initial_cash, tuple(views),
                                  next_evaluation_at=now + timedelta(days=1))
        r = run_evaluation(cfg.strategy, snap, cfg.params, lot_of={i: x.lot_step for i, x in cfg.rules.items()})
        return [(s.signal_id, s.kind.value, s.instrument, str(s.quantity), str(s.price)) for s in r.signals]

    def scaled(b: Bar, f: Decimal, keep_open: bool) -> Bar:
        o = b.open if keep_open else b.open * f
        hi, lo, cl = b.high * f, b.low * f, b.close * f
        return replace(b, open=o, high=max(hi, o, cl), low=min(lo, o, cl), close=cl, volume=b.volume * f)

    bad: list[str] = []
    for day in check_days:
        base = signals_at(cfg.data, day)
        variants = (
            {i: [scaled(b, Decimal(3), b.at == day) if b.at >= day else b for b in bars]
             for i, bars in cfg.data.items()},
            {i: [b if b.at < day else (replace(b, high=b.open, low=b.open, close=b.open, volume=_ZERO)
                                       if b.at == day else None) for b in bars if b.at <= day]
             for i, bars in cfg.data.items()},
        )
        if any(signals_at({i: [b for b in v if b is not None] for i, v in var.items()}, day) != base
               for var in variants):
            bad.append(day.isoformat())
    return bad


def render_report_m2(cfg: SpotBacktestConfig, result: BacktestResult, *, reproducible: bool | None,
                     lookahead_bad: list[str] | None) -> str:
    spec = cfg.strategy.spec
    lines = [
        f"# M2 백테스트 보고서 — {spec.strategy_id} v{spec.version}",
        "",
        f"- 실행 ID: `{cfg.run_id}` / 기간 {result.days[0]} ~ {result.days[-1]} ({len(result.days)}일, UTC) / "
        f"심볼 {len(cfg.data)}개 / 데이터 버전 `{cfg.data_version or '미기재'}` / 코드 `{cfg.code_commit or '미기재'}`",
        f"- 판정: 매일 {cfg.eval_time:%H:%M} UTC, 확정된 1d 봉만 사용(봉 시작 + 1일 이후, V-142), 판정 시세 = 그날 시가",
        f"- 체결(2-M2.2): 매수 = 시가 × (1 + 슬리피지 {cfg.slippage}) tick 올림이 IOC 지정가(시가 × (1 + 완충 "
        f"{cfg.buy_buffer}) tick 내림) 이하일 때만, 매도 = 시가 × (1 − 슬리피지) tick 내림, 가격제한 없음, "
        f"수량 stepSize, 봉 거래량 {cfg.volume_cap:%} 상한",
        f"- 비용: 수수료율 {cfg.cost_rates.commission_rate}(받는 자산에서 차감, D-101), 매도세 0, 비용 합계 {result.costs} USDT",
        f"- K2 다음 날 재개 가정: {result.k2_resumes}회",
        "",
        "## 지표 (비용 반영 후, 연환산 365일)",
        "| 지표 | 값 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in result.metrics.items()],
        "",
        "## 한계",
        "- 데이터 출처: 테스트넷 1d klines 기준(D-116). 이력 길이·품질 미확인(B-U4).",
        "- 슬리피지·수수료율은 사용자 결정 값(2-M2.2 ▢) — 이 실행의 값은 위에 적은 값.",
        "- 일봉 하나로 체결: 판정~체결 사이 장중 경로 무시, 시가 기준. 거래소 수수료 자릿수 반올림 미반영.",
        "- 남은 dust 가치는 손익에서 뺌(보수). 전략 등록·상태 검사 생략([제안], T-M2-17).",
        "",
        "## 판정 (04 §2.7 — 실행 기록)",
        f"- 재현성(D-074): {'통과' if reproducible else ('실패' if reproducible is False else '미실행')}",
        "- 룩어헤드 교란 검사(V-91): " + ("통과" if lookahead_bad == [] else
                                    ("미실행" if lookahead_bad is None else f"실패 {lookahead_bad}")),
        f"- 결과 지문: `{result.digest}`",
    ]
    return "\n".join(lines) + "\n"
