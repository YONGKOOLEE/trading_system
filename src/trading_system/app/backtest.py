"""백테스트 실행기 (R-10, 04 §2·D-071~D-077, 05 T-009, CP-016 A-5).

- 실운영과 같은 전략·판정 실행기·게이트·실행기·정지 관리를 쓰고, 브로커만 체결 모델(BacktestBroker)로 바꾼다 (D-071).
- 실행마다 `var/backtest/<실행ID>/trading.db` (모드 BACKTEST). 같은 DB를 다른 실행과 섞지 않는다 (A-5).
- 거래일마다: 판정 시각(기본 09:05 KST)에 판정 → 게이트 → 전송 → 그날 봉으로 체결(다음 봉 시가 원칙) → 마감 만료 → 평가.
- K2(일일 손실)는 실운영처럼 발동하되, 백테스트에서는 다음 거래일 시작에 사용자가 재개했다고 가정한다(DP-6, 보고서에 횟수 기록).
- 재현성(D-074): 결과 지문 = 체결·평가액·신호 ID의 해시. 같은 입력이면 같은 지문.
- 최종 평가 구간(D-075)은 전략 버전당 한 번만: 사용 장부(JSONL)에 기록하고 두 번째 사용은 거부한다.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from trading_system.broker.fake.fill_model import BacktestBroker, BarFacts
from trading_system.config.capabilities import CapabilityTable
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import GateResult, HaltType, IntentState, Mode, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import CashView, DailyRisk, GateSettings, InstrumentInfo, Quote, estimate_cost
from trading_system.domain.models import Account, GateRequest
from trading_system.domain.strategy import PositionView, evaluation_id_of
from trading_system.evaluation.runner import run_evaluation
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs, GatePolicy
from trading_system.halt.manager import Channel, HaltManager
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.quality import RawDaily, preprocess_daily
from trading_system.marketdata.store import MarketDataStore
from trading_system.marketdata.ticks import TickTable
from trading_system.storage.repository import Repository
from trading_system.strategy.base import Strategy

ACCOUNT = "KR-BT-01"
SEGMENTS = ("ALL", "TRAIN", "VALIDATION", "FINAL")
SPLIT = (Decimal("0.6"), Decimal("0.2"), Decimal("0.2"))  # D-075 기본 60/20/20


class BacktestError(RuntimeError):
    pass


@dataclass(frozen=True)
class BacktestConfig:
    run_id: str
    strategy: Strategy
    params: dict[str, object]
    caps: CapabilityTable
    account_limits: AccountLimits
    gate_settings: GateSettings
    calendar: KrxCalendar
    ticks: TickTable | None
    slippage_tick: Decimal              # 호가 표가 없을 때 슬리피지 1틱의 크기 (보고서에 기록)
    data: dict[str, list[RawDaily]]
    infos: dict[str, InstrumentInfo]
    start: date
    end: date
    initial_cash: Decimal
    out_dir: Path
    eval_time: time = time(9, 5)
    slippage_ticks: int = 1
    volume_cap: Decimal = Decimal("0.10")
    data_version: str = ""
    code_commit: str = ""
    seed: int = 0
    segment: str = "ALL"
    final_usage_ledger: Path | None = None


@dataclass(frozen=True)
class Trade:
    instrument: str
    entry_day: date
    exit_day: date
    pnl: Decimal          # 비용 반영 후
    holding_days: int


@dataclass
class BacktestResult:
    run_id: str
    segment: str
    days: list[date]
    equity: list[tuple[date, Decimal]]
    trades: list[Trade]
    metrics: dict[str, str]
    rejects: Counter
    digest: str
    db_path: Path
    signals: list[str] = field(default_factory=list)
    fills: list[tuple[date, str, str, str, str, str]] = field(default_factory=list)  # 일, 주문번호, 종목, 방향, 수량, 가격
    k2_resumes: int = 0
    costs: Decimal = Decimal("0")
    quality_failures: dict[str, list[str]] = field(default_factory=dict)


def segment_days(days: list[date], segment: str) -> list[date]:
    if segment not in SEGMENTS:
        raise BacktestError(f"알 수 없는 구간 {segment}")
    if segment == "ALL":
        return days
    n = len(days)
    a = int((Decimal(n) * SPLIT[0]).to_integral_value())
    b = a + int((Decimal(n) * SPLIT[1]).to_integral_value())
    return {"TRAIN": days[:a], "VALIDATION": days[a:b], "FINAL": days[b:]}[segment]


def _claim_final(cfg: BacktestConfig) -> None:
    if cfg.segment != "FINAL":
        return
    if cfg.final_usage_ledger is None:
        raise BacktestError("최종 평가 구간을 쓰려면 사용 장부 경로가 필요 (D-075)")
    spec = cfg.strategy.spec
    key = {"strategy": spec.strategy_id, "version": spec.version}
    if cfg.final_usage_ledger.exists():
        for line in cfg.final_usage_ledger.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("strategy") == key["strategy"] and rec.get("version") == key["version"]:
                raise BacktestError(f"최종 평가 구간은 전략 버전당 한 번만 (이미 사용: {rec.get('run_id')})")
    cfg.final_usage_ledger.parent.mkdir(parents=True, exist_ok=True)
    with cfg.final_usage_ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**key, "run_id": cfg.run_id, "used_at": datetime.now(UTC).isoformat()}) + "\n")


class _AccountFacts:
    def __init__(self) -> None:
        self.daily: DailyRisk | None = None
        self.cash_view: CashView | None = None

    def daily_risk(self, account: str) -> DailyRisk | None:
        return self.daily

    def cash(self, account: str) -> CashView | None:
        return self.cash_view


class _ClockOk:
    def clock_ok(self) -> bool | None:
        return True  # 백테스트 시계는 주입된 가짜 시계


class _Limits:
    def __init__(self, lim: AccountLimits) -> None:
        self._lim = lim

    def effective_limits(self, account: str, at: datetime | None = None) -> AccountLimits | None:
        return self._lim


def run_backtest(cfg: BacktestConfig) -> BacktestResult:
    spec = cfg.strategy.spec
    run_dir = cfg.out_dir / cfg.run_id
    if (run_dir / "trading.db").exists():
        raise BacktestError(f"같은 실행 ID의 DB가 이미 있음: {run_dir} (실행마다 새 ID)")
    all_days = []
    d = cfg.start
    while d <= cfg.end:
        if cfg.calendar.is_trading_day(d) is None:
            raise BacktestError(f"{d.year}년 달력 없음 — 백테스트 불가 (X-U3)")
        if cfg.calendar.is_trading_day(d):
            all_days.append(d)
        d += timedelta(days=1)
    days = segment_days(all_days, cfg.segment)
    if not days:
        raise BacktestError("구간에 거래일이 없음")
    _claim_final(cfg)

    repo = Repository(run_dir / "trading.db", Mode.BACKTEST)
    repo.register_account(Account(ACCOUNT, Mode.BACKTEST, "fake"))
    clock = FixedClock(datetime.combine(days[0], cfg.eval_time, tzinfo=KST))
    store = MarketDataStore(cfg.calendar, cfg.ticks, account=ACCOUNT)
    acct = _AccountFacts()
    policy = GatePolicy.from_settings(cfg.gate_settings)

    def tick_of(inst: str, price: Decimal) -> Decimal:
        if cfg.ticks is not None:
            t = cfg.ticks.tick_of(cfg.infos[inst].kind, price)
            if t is not None:
                return t
        return cfg.slippage_tick

    broker = BacktestBroker(tick_of, cfg.slippage_ticks, cfg.volume_cap)
    executor = Executor(repo, broker, clock, run_dir / "KILL")
    registry = StrategyRegistry(repo, clock, None)
    gate = Gate(repo, clock, GateInputs(_Limits(cfg.account_limits), store, acct, registry, _ClockOk()), policy)
    halts = HaltManager(repo, gate, executor, clock,
                        reference_time_of=lambda a: cfg.account_limits.daily_reference_time)
    registry._halts = halts  # noqa: SLF001 — 조립 지점 (T-020에서 app/wiring으로 이동)
    # 백테스트에서는 저장소에 정지를 연결하지 않는다: 전체 기간 품질로 K10을 걸면 미래 데이터가 과거를 막는다.
    # 품질은 판정마다 그 시점까지의 봉으로 다시 판정한다 (store.snapshot).
    reg = registry.register(spec, dict(cfg.params), cfg.caps, cfg.account_limits, Mode.BACKTEST)
    if not reg.ok:
        repo.close()
        raise BacktestError(f"전략 등록 거부: {'; '.join(reg.problems)}")

    # 데이터 준비: 단일 전처리 (D-088). 품질 실패 종목은 그 데이터로 판정하지 않는다
    bars: dict[str, dict[date, RawDaily]] = {}
    quality_failures: dict[str, list[str]] = {}
    for inst, raw in cfg.data.items():
        store.register(cfg.infos[inst])
        store.update_status(inst, frozenset())
        prepared = store.update_daily(inst, raw, cfg.data_version)
        if not prepared.report.ok:
            quality_failures[inst] = list(prepared.report.issues)
        bars[inst] = {r.day: r for r in raw}

    cash = cfg.initial_cash
    costs_total = Decimal("0")
    equity: list[tuple[date, Decimal]] = []
    rejects: Counter = Counter()
    signals: list[str] = []
    fills_log: list[tuple[date, str, str, str, str, str]] = []
    k2_resumes = 0
    seen_fills = 0
    prev_close: dict[str, Decimal] = {}
    for inst, by_day in bars.items():
        before = [r for dd, r in by_day.items() if dd < days[0]]
        if before:
            prev_close[inst] = max(before, key=lambda r: r.day).close

    for i, day in enumerate(days):
        clock._now = datetime.combine(day, cfg.eval_time, tzinfo=KST).astimezone(UTC)  # noqa: SLF001
        if HaltType.K2.value in repo.active_halts(ACCOUNT):
            if halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "backtest").ok:
                k2_resumes += 1
        positions = {k: v for k, v in repo.positions(ACCOUNT).items() if v.quantity > 0}
        base = cash + sum((p.quantity * prev_close.get(k, p.avg_price) for k, p in positions.items()), Decimal("0"))
        for inst, by_day in bars.items():
            bar = by_day.get(day)
            if bar is None:
                continue
            ref = prev_close.get(inst)
            upper = None if ref is None else ref * (1 + cfg.gate_settings.price_limit_ratio)
            store.update_quote(Quote(inst, bar.open, clock.now(), upper_limit=upper))
        equity_open = cash + sum((p.quantity * bars[k][day].open if day in bars.get(k, {}) else
                                  p.quantity * prev_close.get(k, p.avg_price)) for k, p in positions.items())
        acct.daily = DailyRisk(base, Decimal("0"), equity_open - base, Decimal("0"))
        reserved = sum((r.cash_remaining for r in repo.active_reservations(ACCOUNT)), Decimal("0"))
        acct.cash_view = CashView(cash, cash - reserved)
        halts.check_daily_loss(ACCOUNT, acct.daily, cfg.account_limits.l4_daily_loss_ratio)

        next_eval = (datetime.combine(days[i + 1], cfg.eval_time, tzinfo=KST) if i + 1 < len(days)
                     else datetime.combine(day + timedelta(days=1), cfg.eval_time, tzinfo=KST))
        ev_id = evaluation_id_of(spec.strategy_id, spec.version, "M1", f"{day.isoformat()}T{cfg.eval_time:%H:%M}")

        def pos_view(inst: str) -> PositionView | None:
            p = repo.position(ACCOUNT, inst)
            if p.quantity <= 0:
                return None
            own = repo.active_ownership(ACCOUNT, inst)
            return PositionView(p.quantity, p.avg_price, 0, own is not None and own.owner == spec.owner)

        def pending(inst: str) -> bool:
            return bool(repo.active_new_intents(ACCOUNT, inst, Side.BUY.value)
                        or repo.active_new_intents(ACCOUNT, inst, Side.SELL.value))

        snap = store.snapshot(ev_id, "M1", ACCOUNT, clock.now(), base, sorted(cfg.data), pos_view, pending,
                              next_eval, history=lambda inst: repo.strategy_history(ACCOUNT, spec.owner, inst))
        result = run_evaluation(cfg.strategy, snap, cfg.params)
        for s in result.signals:
            signals.append(s.signal_id)
            decision = gate.decide(GateRequest.from_signal(s))
            if decision.result is GateResult.APPROVED and decision.approved is not None:
                executor.submit(decision.approved)
            else:
                rejects[str(decision.failed_check)] += 1
        executor.process_events()

        for inst, by_day in bars.items():
            bar = by_day.get(day)
            if bar is None:
                continue
            ref = prev_close.get(inst)
            lim = cfg.gate_settings.price_limit_ratio
            broker.match_bar(inst, BarFacts(bar.open, bar.high, bar.low, bar.volume,
                                            None if ref is None else ref * (1 + lim),
                                            None if ref is None else ref * (1 - lim)),
                             datetime.combine(day, time(9, 0), tzinfo=KST))
        executor.process_events()
        new_fills = broker.fills_for(ACCOUNT)[seen_fills:]
        seen_fills += len(new_fills)
        for f in new_fills:
            side = broker.side_of(f.order_no) or "BUY"
            inst = broker.instrument_of(f.order_no) or "?"
            amount = f.quantity * f.price
            cost = estimate_cost(amount, side == "SELL", cfg.gate_settings.cost_rates)
            costs_total += cost
            cash += -amount - cost if side == "BUY" else amount - cost
            fills_log.append((day, f.order_no, inst, side, str(f.quantity), str(f.price)))
        broker.end_of_day()
        executor.process_events()
        broker.new_day()

        for inst, by_day in bars.items():
            if day in by_day:
                prev_close[inst] = by_day[day].close
        positions = {k: v for k, v in repo.positions(ACCOUNT).items() if v.quantity > 0}
        eq = cash + sum((p.quantity * prev_close.get(k, p.avg_price) for k, p in positions.items()), Decimal("0"))
        equity.append((day, eq))

    unknown = repo.intents_in_state(IntentState.UNKNOWN)
    repo.close()
    if unknown:
        raise BacktestError("체결 모델에서 결과 불명이 생김 — 엔진 오류")
    trades = _round_trips(fills_log, cfg)
    metrics = compute_metrics(cfg.initial_cash, equity, trades, costs_total, len(days))
    digest = hashlib.sha256(json.dumps({"signals": signals, "fills": fills_log,
                                        "equity": [(d.isoformat(), str(e)) for d, e in equity]},
                                       sort_keys=True, default=str).encode()).hexdigest()
    return BacktestResult(cfg.run_id, cfg.segment, days, equity, trades, metrics, rejects, digest,
                          run_dir / "trading.db", signals, fills_log, k2_resumes, costs_total, quality_failures)


def _round_trips(fills: list[tuple[date, str, str, str, str, str]], cfg: BacktestConfig) -> list[Trade]:
    """거래 1건 = 진입부터 전량 청산까지 왕복 (04 D-078). 체결 순서대로 종목별 누적."""
    trades: list[Trade] = []
    state: dict[str, dict] = {}
    for day, _order_no, inst, side, qty_s, price_s in fills:
        qty, price = Decimal(qty_s), Decimal(price_s)
        st = state.setdefault(inst, {"qty": Decimal("0"), "cash": Decimal("0"), "entry": day})
        if st["qty"] == 0:
            st["entry"], st["cash"] = day, Decimal("0")
        amount = qty * price
        cost = estimate_cost(amount, side == "SELL", cfg.gate_settings.cost_rates)
        st["cash"] += (-amount - cost) if side == "BUY" else (amount - cost)
        st["qty"] += qty if side == "BUY" else -qty
        if st["qty"] == 0:
            trades.append(Trade(inst, st["entry"], day, st["cash"], _trading_days_between(cfg, st["entry"], day)))
    return trades


def _trading_days_between(cfg: BacktestConfig, a: date, b: date) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if cfg.calendar.is_trading_day(d):
            n += 1
    return n


def compute_metrics(initial: Decimal, equity: list[tuple[date, Decimal]], trades: list[Trade], costs: Decimal,
                    n_days: int) -> dict[str, str]:
    """04 §2.6 지표 (비용 반영 후). 소수는 문자열로 돌려준다."""
    final = equity[-1][1] if equity else initial
    total = (final - initial) / initial if initial else Decimal("0")
    years = Decimal(n_days) / Decimal("252") if n_days else Decimal("0")
    annual = ((1 + total) ** (1 / years) - 1) if years > 0 and (1 + total) > 0 else Decimal("0")
    peak, mdd, longest, cur_len = initial, Decimal("0"), 0, 0
    for _, e in equity:
        if e >= peak:
            peak, cur_len = e, 0
        else:
            cur_len += 1
            longest = max(longest, cur_len)
            mdd = max(mdd, (peak - e) / peak)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    avg_win = sum(wins, Decimal("0")) / len(wins) if wins else Decimal("0")
    avg_loss = -sum(losses, Decimal("0")) / len(losses) if losses else Decimal("0")
    q = Decimal("0.000001")
    return {
        "total_return": str(total.quantize(q)),
        "annual_return": str(Decimal(str(annual)).quantize(q)),
        "max_drawdown": str(mdd.quantize(q)),
        "longest_drawdown_days": str(longest),
        "trades": str(len(trades)),
        "win_rate": str((Decimal(len(wins)) / len(trades)).quantize(q)) if trades else "0",
        "payoff_ratio": str((avg_win / avg_loss).quantize(q)) if avg_loss > 0 else "n/a",
        "avg_pnl": str((sum((t.pnl for t in trades), Decimal("0")) / len(trades)).quantize(Decimal("1"))) if trades
        else "0",
        "avg_holding_days": str((Decimal(sum(t.holding_days for t in trades)) / len(trades)).quantize(q))
        if trades else "0",
        "costs": str(costs),
        "final_equity": str(final),
    }


# --- 룩어헤드 교란 검사 (D-073 (2), V-91) -------------------------------------------------------

def perturbation_check(cfg: BacktestConfig, check_days: list[date]) -> list[str]:
    """판정 기준 시각 이후 데이터를 크게 바꿔도 신호가 같아야 한다. 다르면 그 날짜를 돌려준다."""

    def signals_at(data: dict[str, list[RawDaily]], day: date) -> list[tuple]:
        store = MarketDataStore(cfg.calendar, cfg.ticks)
        for inst, raw in data.items():
            store.register(cfg.infos[inst])
            store.update_status(inst, frozenset())
            store.update_daily(inst, raw, cfg.data_version)
        as_of = datetime.combine(day, cfg.eval_time, tzinfo=KST)
        spec = cfg.strategy.spec
        ev = evaluation_id_of(spec.strategy_id, spec.version, "M1", f"{day.isoformat()}T{cfg.eval_time:%H:%M}")
        snap = store.snapshot(ev, "M1", ACCOUNT, as_of, cfg.initial_cash, sorted(data), lambda i: None,
                              lambda i: False)
        r = run_evaluation(cfg.strategy, snap, cfg.params)
        return [(s.signal_id, s.kind.value, s.instrument, str(s.quantity), str(s.price)) for s in r.signals]

    def factor(inst: str, d: date) -> Decimal:
        # 종목·날짜마다 다른 결정적 배율(0.50~1.99). 균일 배율은 미래 봉끼리의 비율을 보존해 누출을 놓친다
        h = hashlib.sha256(f"{inst}|{d.isoformat()}".encode()).digest()
        return Decimal(50 + int.from_bytes(h[:2], "big") % 150) / 100

    def scaled(r: RawDaily, f: Decimal, vf: Decimal) -> RawDaily:
        return RawDaily(r.day, r.open * f, r.high * f, r.low * f, r.close * f, r.volume * vf, r.corporate_action)

    bad: list[str] = []
    for day in check_days:
        base = signals_at(cfg.data, day)
        variants = (
            {inst: [scaled(r, Decimal(3), Decimal(3)) if r.day >= day else r for r in raw]   # 균일 배율
             for inst, raw in cfg.data.items()},
            {inst: [scaled(r, factor(inst, r.day), 2 - factor(inst, r.day) + Decimal("0.5")) if r.day >= day else r
                    for r in raw] for inst, raw in cfg.data.items()},                            # 봉마다 다른 배율
            {inst: [r for r in raw if r.day < day] for inst, raw in cfg.data.items()},       # 미래 봉 제거
        )
        if any(signals_at(v, day) != base for v in variants):
            bad.append(day.isoformat())
    return bad


# --- 민감도·보고서 (D-077, §2.8) --------------------------------------------------------------

def run_sensitivity(cfg: BacktestConfig, slippage_ticks: tuple[int, ...] = (0, 1, 2)) -> dict[int, dict[str, str]]:
    from dataclasses import replace as _replace
    out: dict[int, dict[str, str]] = {}
    for n in slippage_ticks:
        r = run_backtest(_replace(cfg, run_id=f"{cfg.run_id}-slip{n}", slippage_ticks=n, segment="ALL"))
        out[n] = r.metrics
    return out


def render_report(cfg: BacktestConfig, result: BacktestResult, *, settings_hash: str, reproducible: bool | None,
                  lookahead_bad: list[str] | None, sensitivity: dict[int, dict[str, str]] | None = None) -> str:
    """04 §2.8 보고서 (docs/strategies/<ID>/backtest-v<n>.md로 옮겨 쓰는 초안). 판정은 §2.7 v0.5 기준."""
    spec = cfg.strategy.spec
    m = result.metrics
    ok_run = True
    passed = ok_run and reproducible is True and lookahead_bad == []
    lines = [
        f"# 백테스트 보고서 — {spec.strategy_id} v{spec.version} ({cfg.segment})",
        "",
        f"- 실행 ID: `{cfg.run_id}` / DB: `{result.db_path.as_posix()}`",
        f"- 데이터: 버전 `{cfg.data_version or '미기재'}`, 기간 {result.days[0]} ~ {result.days[-1]} "
        f"({len(result.days)}거래일), 종목 {len(cfg.data)}개, 품질 실패 종목 {len(result.quality_failures)}개",
        f"- 설정 해시: `{settings_hash}` / 코드 커밋: `{cfg.code_commit or '미기재'}` / 시드: {cfg.seed}",
        f"- 체결 가정(D-072): 다음 봉 시가, 시장가 슬리피지 {cfg.slippage_ticks}틱(호가 표 없으면 {cfg.slippage_tick}원), "
        f"지정가 관통 체결, 봉 거래량 {cfg.volume_cap:%} 상한, 상한가 매수·하한가 매도 미체결",
        f"- 비용(D-065): 수수료율 {cfg.gate_settings.cost_rates.commission_rate}, 매도 세율 "
        f"{cfg.gate_settings.cost_rates.sell_tax_rate}, 비용 합계 {result.costs}원",
        f"- 구간(D-075): {cfg.segment} (기본 60/20/20)" + (" — 최종 평가 구간 사용 기록됨" if cfg.segment == "FINAL" else ""),
        f"- K2 정지 후 다음 날 재개 가정(DP-6): {result.k2_resumes}회",
        "",
        "## 지표 (비용 반영 후, §2.6)",
        "| 지표 | 값 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in m.items()],
        "",
        "## 게이트 거부 (한도 영향)",
        "| 검사 | 건수 |",
        "|---|---|",
        *([f"| {k} | {v} |" for k, v in sorted(result.rejects.items())] or ["| (없음) | 0 |"]),
    ]
    if sensitivity:
        lines += ["", "## 민감도 (슬리피지 틱, D-077)", "| 틱 | 총수익률 | 최대 낙폭 | 거래 수 |", "|---|---|---|---|"]
        lines += [f"| {n} | {s['total_return']} | {s['max_drawdown']} | {s['trades']} |" for n, s in sensitivity.items()]
    lines += [
        "",
        "## 한계",
        "- 생존편향: 상장폐지 종목 데이터 확보 여부 미확인(04 §2.1). U-A 고정 목록은 목록 작성 시점 이전 구간이 사후 선택.",
        "- 거래 상태(VI·거래정지) 이력 미반영: 그날 봉이 없으면 체결 없음으로만 처리.",
        "- 모의·실전 체결 차이는 PAPER에서 확인.",
        "- 일일 손실(K2)은 판정 시각에 한 번만 평가: 장중 손실로 K2가 걸려 이후 주문이 막히는 경우는 재현하지 않음(DP-6).",
        "- 판정 시각(기본 09:05) 주문을 그날 시가로 체결: 09:00~판정 시각 사이 가격 변동을 무시(DP-7).",
        "",
        "## 판정 (04 §2.7 v0.5 — 실행 기록, 성과 기준 아님)",
        f"- 오류 없이 완료: {'예' if ok_run else '아니오'}",
        f"- 재현성(D-074): {'통과' if reproducible else ('실패' if reproducible is False else '미실행')}",
        f"- 룩어헤드 교란 검사(D-073): " + ("통과" if lookahead_bad == [] else
                                       ("미실행" if lookahead_bad is None else f"실패 {lookahead_bad}")),
        f"- 결과: **{'백테스트 통과 조건 충족' if passed else '미충족'}** — 상태 전환(초안 → 백테스트 통과)은 사용자 승인 A-ID로",
        f"- 결과 지문: `{result.digest}`",
    ]
    return "\n".join(lines) + "\n"
