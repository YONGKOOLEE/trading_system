"""백테스트 실행기 (R-10, 04 §2·D-071~D-077, 05 T-009, CP-016 A-5).

- 실운영과 같은 전략·판정 실행기·게이트·실행기·정지 관리를 쓰고, 브로커만 체결 모델(BacktestBroker)로 바꾼다 (D-071).
- 실행마다 `var/backtest/<실행ID>/trading.db` (모드 BACKTEST). 같은 DB를 다른 실행과 섞지 않는다 (A-5).
  같은 폴더에 결과 파일 `trades.csv`(왕복별)·`equity.csv`(거래일별)·`summary.json`(지표·실행 조건)을 쓴다
  (형식은 backtest_export 모듈, 2026-10-08 사용자 승인 형식). `report.md`는 명령(main)이 따로 쓴다.
- 거래일마다: 판정 시각(기본 09:05 KST, 여러 개 가능)에 판정 → 게이트 → 전송 → 그날 봉으로 체결(다음 봉 시가 원칙)
  → 마감 만료 → 평가.
- 하루 여러 판정(제안 가정, 일봉만 있을 때 — 승인 전 기록용):
  * 첫 판정의 주문은 그날 시가(+슬리피지)로 체결한다(DP-7). 그 체결은 다음 판정 전에 반영된다.
  * 둘째 이후 판정의 주문은 그날 **종가**(+불리한 슬리피지)로, 그날 마지막 판정 뒤에 체결한다. 일봉에서 판정 시각
    이후 가격으로 확정된 것은 종가뿐이기 때문이다(시가 체결 = 판정 전 가격으로 체결하는 불가능한 가정, 종가 = 판정 후
    가격). 이 체결은 마지막 판정 뒤에 반영하므로 같은 날 뒤 판정이 종가 체결가(미래)를 보지 않는다.
    지정가는 종가가 관통할 때만 체결(봉을 종가 한 점으로 본다 — 장중 고가·저가는 판정 전 가격일 수 있어 쓰지 않음).
  * 모든 판정의 시세(last_price)는 그날 시가다: 일봉만으로는 장중 가격을 알 수 없고 종가는 미래(룩어헤드)다.
    당일 일봉은 DP-5(마감과 20:00 중 늦은 쪽) 전에는 보이지 않는다(14:30 판정도 전일 봉까지).
  * 체결 시각은 체결을 일으킨 판정 시각으로 기록한다(첫 판정 = 그 판정 시각, 이후 = 그날 마지막 판정 시각).
    주문 생성 시각보다 앞서지 않게 해 감사 순서 역전을 막는다(이전: 09:00 기록).
  * 일일 손실(K2) 검사는 첫 판정에서만(DP-6 유지).
- 상·하한가 = 기준가(전일 종가) × (1 ± 비율)을 호가 단위로 상한은 내림, 하한은 올림. 호가 표가 없으면(X-U1)
  `slippage_tick`을 1틱으로 대체한다(보고서 한계에 기록).
- K2(일일 손실)는 실운영처럼 발동하되, 백테스트에서는 다음 거래일 시작에 사용자가 재개했다고 가정한다(DP-6, 보고서에 횟수 기록).
- 재현성(D-074): 결과 지문 = 체결·평가액·신호 ID의 해시. 같은 입력이면 같은 지문.
- 최종 평가 구간(D-075)은 전략 버전당 한 번만: 사용 장부(JSONL)에 기록하고 두 번째 사용은 거부한다.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from trading_system.app import backtest_export as bx
from trading_system.broker.fake.fill_model import BacktestBroker, BarFacts
from trading_system.config.capabilities import CapabilityTable
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import GateResult, HaltType, IntentState, Mode, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import (  # price_limits: CP-032 D-174로 게이트와 공유, 이 모듈 이름도 유지
    CashView, DailyRisk, GateSettings, InstrumentInfo, Quote, estimate_cost, price_limits,
)
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
SESSION_FIRST, SESSION_END = time(9, 0), time(15, 20)       # 판정 시각 허용 범위 [09:00, 15:20) — 종가 동시호가 전
FINAL_FROM_DEFAULT = date(2026, 1, 1)   # [CP-030 D-163] 최종 확인 구간 시작일 (설정 config/validation.toml, 변경은 CP)


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
    eval_times: tuple[time, ...] = ()   # 비어 있으면 (eval_time,). 있으면 이것을 쓴다 (오름차순·중복 없음)
    slippage_ticks: int = 1
    volume_cap: Decimal = Decimal("0.10")
    data_version: str = ""
    code_commit: str = ""
    seed: int = 0
    segment: str = "ALL"
    final_usage_ledger: Path | None = None
    write_exports: bool = True          # 결과 파일(trades.csv·equity.csv·summary.json) 쓰기. 교란 검사 내부 실행은 끈다
    slippage_pct: Decimal | None = None  # [CP-030 D-164] 주면 % 슬리피지(검증 실행기). None = 틱 방식(기존 기본)
    # [CP-030 D-163] 실행 거래일이 final_from 이후와 겹치면 구간·날짜 지정과 무관하게 거부. 허용은 최종 확인 실행
    # (final_check=True, 사용 장부에 이 전략 버전의 최종 확인 기록이 있어야 함, 모든 거래일이 final_from 이후)뿐.
    final_from: date = FINAL_FROM_DEFAULT
    final_check: bool = False
    # [CP-034 D-198 (8)] U-B 전략의 목록 이력: 날짜 → 그날 판정에 쓰는 목록(date,code 파일). 판정일 d에는 d 이하 날짜 중
    # 가장 늦은 날짜의 목록만 쓴다(룩어헤드 없음). None이면 데이터 전 종목을 사후 선택 목록으로 쓰고 C-7 표시
    universe_history: dict[date, tuple[str, ...]] | None = None


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
    signals_by_eval: dict[str, list[tuple]] = field(default_factory=dict)  # "YYYY-MM-DDTHH:MM" → 신호 요약
    universe_point_in_time: bool | None = None   # [CP-034] U-B만: 목록 이력으로 재현했으면 True, 없으면 False(C-7)


def segment_days(days: list[date], segment: str) -> list[date]:
    if segment not in SEGMENTS:
        raise BacktestError(f"알 수 없는 구간 {segment}")
    if segment == "ALL":
        return days
    n = len(days)
    a = int((Decimal(n) * SPLIT[0]).to_integral_value())
    b = a + int((Decimal(n) * SPLIT[1]).to_integral_value())
    return {"TRAIN": days[:a], "VALIDATION": days[a:b], "FINAL": days[b:]}[segment]


def eval_times_of(cfg: BacktestConfig) -> tuple[time, ...]:
    times = tuple(cfg.eval_times) or (cfg.eval_time,)
    if list(times) != sorted(set(times)):
        raise BacktestError(f"판정 시각은 오름차순·중복 없음이어야 함: {[t.strftime('%H:%M') for t in times]}")
    for t in times:
        if not (SESSION_FIRST <= t < SESSION_END):
            raise BacktestError(f"판정 시각 {t:%H:%M}은 09:00 이상 15:20 미만이어야 함 (일봉 체결 가정 범위)")
    return times


def _run_days(cfg: BacktestConfig) -> list[date]:
    all_days = []
    d = cfg.start
    while d <= cfg.end:
        if cfg.calendar.is_trading_day(d) is None:
            raise BacktestError(f"{d.year}년 달력 없음 — 백테스트 불가 (X-U3)")
        if cfg.calendar.is_trading_day(d):
            all_days.append(d)
        d += timedelta(days=1)
    return segment_days(all_days, cfg.segment)


UB_NO_HISTORY_NOTE = "U-B 목록 이력 없음 — 데이터 전 종목을 사후 선택 목록으로 사용, 판정 시점 목록 아님(C-7, D-168)"


def ub_list_on(history: dict[date, tuple[str, ...]] | None, day: date,
               all_codes: tuple[str, ...]) -> tuple[tuple[str, ...], dict[str, datetime | None]]:
    """U-B 판정일 day의 목록과 등재 시각 (CP-034 D-198 (7)(8)). 이력의 날짜 = 그날 판정에 쓰는 목록 버전.

    day 이하 날짜 중 가장 늦은 것을 쓴다(이후 날짜는 보지 않는다). 등재 시각 = 연속 등재가 시작된 이력 날짜의 00:00 KST
    (제출 시각을 모르므로 그날 판정 전으로 본다). 이력이 없으면 all_codes, 등재 시각 없음."""
    if history is None:
        return all_codes, {}
    dates = sorted(d for d in history if d <= day)
    if not dates:
        return (), {}
    codes = history[dates[-1]]
    listed: dict[str, datetime | None] = {}
    for c in codes:
        start = dates[-1]
        for d in reversed(dates):
            if c not in history[d]:
                break
            start = d
        listed[c] = datetime.combine(start, time(0, 0), tzinfo=KST)
    return codes, listed


def _guard_final_period(cfg: BacktestConfig, days: list[date]) -> None:
    """[CP-030 D-163] 최종 확인 구간 보호. DB·장부를 만들기 전에 검사한다."""
    if not cfg.final_check:
        if days[-1] >= cfg.final_from:
            raise BacktestError(f"실행 거래일 {days[0]}~{days[-1]}이 최종 확인 구간({cfg.final_from} 이후)과 겹침 — "
                                "최종 확인 실행(전략 확정 뒤 1회, 장부 기록)으로만 허용 (D-163)")
        return
    if days[0] < cfg.final_from:
        raise BacktestError(f"최종 확인 실행은 최종 확인 구간만 쓴다 (첫 거래일 {days[0]} < {cfg.final_from}, D-163)")
    spec = cfg.strategy.spec
    ledger = cfg.final_usage_ledger
    recs = []
    if ledger is not None and ledger.exists():
        recs = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not any(r.get("strategy") == spec.strategy_id and r.get("version") == spec.version
               and r.get("kind") == "final_check" for r in recs):
        raise BacktestError("최종 확인 실행인데 사용 장부에 이 전략 버전의 최종 확인 기록이 없음 (D-163)")


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
    times = eval_times_of(cfg)
    days = _run_days(cfg)
    if not days:
        raise BacktestError("구간에 거래일이 없음")
    _guard_final_period(cfg, days)
    _claim_final(cfg)

    repo = Repository(run_dir / "trading.db", Mode.BACKTEST)
    repo.register_account(Account(ACCOUNT, Mode.BACKTEST, "fake"))
    clock = FixedClock(datetime.combine(days[0], times[0], tzinfo=KST))
    store = MarketDataStore(cfg.calendar, cfg.ticks, account=ACCOUNT)
    acct = _AccountFacts()
    policy = GatePolicy.from_settings(cfg.gate_settings)

    def tick_of(inst: str, price: Decimal) -> Decimal:
        if cfg.ticks is not None:
            t = cfg.ticks.tick_of(cfg.infos[inst].kind, price)
            if t is not None:
                return t
        return cfg.slippage_tick

    def price_unit(inst: str, price: Decimal) -> Decimal:
        # % 슬리피지의 맞춤 단위: 호가 표가 있으면 그 가격대 호가, 없으면 1원 (D-164)
        if cfg.ticks is not None:
            t = cfg.ticks.tick_of(cfg.infos[inst].kind, price)
            if t is not None:
                return t
        return Decimal("1")

    broker = BacktestBroker(tick_of, cfg.slippage_ticks, cfg.volume_cap, slippage_pct=cfg.slippage_pct,
                            price_unit=price_unit if cfg.slippage_pct is not None else None)
    executor = Executor(repo, broker, clock, run_dir / "KILL")
    registry = StrategyRegistry(repo, clock, None)
    gate = Gate(repo, clock, GateInputs(_Limits(cfg.account_limits), store, acct, registry, _ClockOk()), policy)
    halts = HaltManager(repo, gate, executor, clock,
                        reference_time_of=lambda a: cfg.account_limits.daily_reference_time)
    registry.attach_halts(halts)
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
    fill_details: list[bx.FillDetail] = []      # 결과 파일용 (지문·지표에 쓰지 않음)
    order_rule: dict[str, str] = {}             # 주문 키 → 신호 규칙 ID
    daily_split: list[tuple[date, Decimal, Decimal]] = []   # 일말 (날짜, 현금, 보유평가액)
    k2_resumes = 0
    seen_fills = 0
    prev_close: dict[str, Decimal] = {}
    for inst, by_day in bars.items():
        before = [r for dd, r in by_day.items() if dd < days[0]]
        if before:
            prev_close[inst] = max(before, key=lambda r: r.day).close

    signals_by_eval: dict[str, list[tuple]] = {}
    ratio = cfg.gate_settings.price_limit_ratio
    is_ub = spec.universe_method == "U-B"

    def absorb_fills(day: date) -> None:
        nonlocal seen_fills, cash, costs_total
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
            fill_details.append(bx.FillDetail(f.at.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
                                              order_rule.get(repo.order_key(ACCOUNT, f.at, f.order_no),
                                                             bx.NON_SIGNAL_REASON)))

    for i, day in enumerate(days):
        # 상·하한가: 기준가(전일 종가) × (1 ± 비율)을 호가 단위로 내림·올림
        lims = {inst: price_limits(prev_close[inst], ratio, lambda p, inst=inst: tick_of(inst, p))
                for inst in bars if inst in prev_close}
        base = Decimal("0")
        for k, et in enumerate(times):
            clock._now = datetime.combine(day, et, tzinfo=KST).astimezone(UTC)  # noqa: SLF001
            positions = {kk: v for kk, v in repo.positions(ACCOUNT).items() if v.quantity > 0}
            if k == 0:
                if HaltType.K2.value in repo.active_halts(ACCOUNT):
                    if halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "backtest").ok:
                        k2_resumes += 1
                base = cash + sum((p.quantity * prev_close.get(kk, p.avg_price) for kk, p in positions.items()),
                                  Decimal("0"))
            for inst, by_day in bars.items():
                bar = by_day.get(day)
                if bar is None:
                    continue
                # 판정 시세 = 당일 시가 (이후 판정도 같음: 일봉에는 장중 가격이 없고 종가는 미래)
                # 상·하한가·기준가(전일 종가)를 함께 준다: 게이트가 지정가를 상·하한가로 검사한다 (CP-032 D-174).
                # 시장가 예약은 상한가만 쓰므로 시장가 결과는 그대로다 (V-240)
                store.update_quote(Quote(inst, bar.open, clock.now(),
                                         upper_limit=lims[inst][0] if inst in lims else None,
                                         lower_limit=lims[inst][1] if inst in lims else None,
                                         base_price=prev_close.get(inst)))
            equity_open = cash + sum((p.quantity * bars[kk][day].open if day in bars.get(kk, {}) else
                                      p.quantity * prev_close.get(kk, p.avg_price)) for kk, p in positions.items())
            acct.daily = DailyRisk(base, Decimal("0"), equity_open - base, Decimal("0"))
            reserved = sum((r.cash_remaining for r in repo.active_reservations(ACCOUNT)), Decimal("0"))
            acct.cash_view = CashView(cash, cash - reserved)
            if k == 0:
                halts.check_daily_loss(ACCOUNT, acct.daily, cfg.account_limits.l4_daily_loss_ratio)

            if k + 1 < len(times):
                next_eval = datetime.combine(day, times[k + 1], tzinfo=KST)
            else:
                next_eval = (datetime.combine(days[i + 1], times[0], tzinfo=KST) if i + 1 < len(days)
                             else datetime.combine(day + timedelta(days=1), times[0], tzinfo=KST))
            ev_key = f"{day.isoformat()}T{et:%H:%M}"
            ev_id = evaluation_id_of(spec.strategy_id, spec.version, "M1", ev_key)

            def pos_view(inst: str) -> PositionView | None:
                p = repo.position(ACCOUNT, inst)
                if p.quantity <= 0:
                    return None
                own = repo.active_ownership(ACCOUNT, inst)
                return PositionView(p.quantity, p.avg_price, 0, own is not None and own.owner == spec.owner)

            def pending(inst: str) -> bool:
                return bool(repo.active_new_intents(ACCOUNT, inst, Side.BUY.value)
                            or repo.active_new_intents(ACCOUNT, inst, Side.SELL.value))

            insts, ub_extra, ub_entry = sorted(cfg.data), {}, {}
            if is_ub:   # [CP-034 D-198] 그날 목록 + 이 전략 보유(청산·감액 판정용), 진입·증액은 목록 안에서만
                codes, listed = ub_list_on(cfg.universe_history, day, tuple(sorted(cfg.data)))
                owned = {o["instrument"] for o in repo.active_ownerships_of(spec.owner)}
                insts = sorted((set(codes) | owned) & set(cfg.data))
                ub_extra = {"listed_at": listed.get}
                ub_entry = {"entry_universe": frozenset(codes)}
            snap = store.snapshot(ev_id, "M1", ACCOUNT, clock.now(), base, insts, pos_view, pending,
                                  next_eval, history=lambda inst: repo.strategy_history(ACCOUNT, spec.owner, inst),
                                  **ub_extra)
            result = run_evaluation(cfg.strategy, snap, cfg.params, **ub_entry)
            signals_by_eval[ev_key] = [(s.signal_id, s.kind.value, s.instrument, str(s.quantity), str(s.price))
                                       for s in result.signals]
            for s in result.signals:
                signals.append(s.signal_id)
                decision = gate.decide(GateRequest.from_signal(s))
                if decision.result is GateResult.APPROVED and decision.approved is not None:
                    executor.submit(decision.approved)
                    sent_key = repo.intent(decision.approved.intent_id).broker_order_key
                    if sent_key is not None:
                        order_rule[sent_key] = s.rule_id
                else:
                    rejects[str(decision.failed_check)] += 1
            executor.process_events()

            if k == 0:
                # 첫 판정 주문: 그날 봉 시가로 체결(DP-7). 체결 시각 = 이 판정 시각(주문 생성보다 앞서지 않음)
                for inst, by_day in bars.items():
                    bar = by_day.get(day)
                    if bar is None:
                        continue
                    up, low = lims.get(inst, (None, None))
                    broker.match_bar(inst, BarFacts(bar.open, bar.high, bar.low, bar.volume, up, low), clock.now())
                executor.process_events()
                absorb_fills(day)

        if len(times) > 1:
            # 이후 판정 주문(과 남은 주문): 그날 마지막 판정 뒤에 종가 한 점으로 체결, 체결 시각 = 마지막 판정 시각.
            # 거래량 상한은 첫 판정 체결과 합산해 그날 거래량 기준 (체결 모델이 일중 사용량을 누적)
            for inst, by_day in bars.items():
                bar = by_day.get(day)
                if bar is None:
                    continue
                up, low = lims.get(inst, (None, None))
                broker.match_bar(inst, BarFacts(bar.close, bar.close, bar.close, bar.volume, up, low), clock.now())
            executor.process_events()
            absorb_fills(day)
        broker.end_of_day()
        executor.process_events()
        broker.new_day()

        for inst, by_day in bars.items():
            if day in by_day:
                prev_close[inst] = by_day[day].close
        positions = {k: v for k, v in repo.positions(ACCOUNT).items() if v.quantity > 0}
        held = sum((p.quantity * prev_close.get(k, p.avg_price) for k, p in positions.items()), Decimal("0"))
        eq = cash + held
        equity.append((day, eq))
        daily_split.append((day, cash, held))

    unknown = repo.intents_in_state(IntentState.UNKNOWN)
    repo.close()
    if unknown:
        raise BacktestError("체결 모델에서 결과 불명이 생김 — 엔진 오류")
    trades = _round_trips(fills_log, cfg)
    metrics = compute_metrics(cfg.initial_cash, equity, trades, costs_total, len(days))
    digest = hashlib.sha256(json.dumps({"signals": signals, "fills": fills_log,
                                        "equity": [(d.isoformat(), str(e)) for d, e in equity]},
                                       sort_keys=True, default=str).encode()).hexdigest()
    result = BacktestResult(cfg.run_id, cfg.segment, days, equity, trades, metrics, rejects, digest,
                            run_dir / "trading.db", signals, fills_log, k2_resumes, costs_total, quality_failures,
                            signals_by_eval,
                            universe_point_in_time=(cfg.universe_history is not None) if is_ub else None)
    if cfg.write_exports:
        _write_exports(cfg, result, fill_details, daily_split, times)
    return result


def _write_exports(cfg: BacktestConfig, result: BacktestResult, details: list[bx.FillDetail],
                   daily_split: list[tuple[date, Decimal, Decimal]], times: tuple[time, ...]) -> None:
    """결과 파일 3종 (backtest_export 모듈 docstring). 이미 계산된 값만 옮긴다 — 지표·지문에 영향 없음."""
    run_dir = result.db_path.parent
    rates = cfg.gate_settings.cost_rates
    rows, open_n = bx.trade_rows(result.fills, details, lambda amt, sell: estimate_cost(amt, sell, rates),
                                 lambda a, b: _trading_days_between(cfg, a, b), result.days[-1])
    bx.write_csv(run_dir / "trades.csv", bx.TRADE_HEADER, rows)
    bx.write_csv(run_dir / "equity.csv", bx.EQUITY_HEADER, bx.equity_rows(cfg.initial_cash, daily_split))
    spec = cfg.strategy.spec
    bx.write_summary(run_dir / "summary.json", {
        "run_id": cfg.run_id,
        "segment": cfg.segment,
        "start": result.days[0].isoformat(),
        "end": result.days[-1].isoformat(),
        "trading_days": len(result.days),
        "eval_times": [t.strftime("%H:%M") for t in times],
        "strategy_id": spec.strategy_id,
        "strategy_version": spec.version,
        "params": {k: str(v) for k, v in sorted(cfg.params.items())},
        "initial_cash": str(cfg.initial_cash),
        "cost_rates": {"commission_rate": str(rates.commission_rate), "sell_tax_rate": str(rates.sell_tax_rate)},
        "slippage_ticks": cfg.slippage_ticks,
        "slippage_tick": str(cfg.slippage_tick),
        **({} if cfg.slippage_pct is None else {"slippage_pct": str(cfg.slippage_pct)}),
        "tick_table": cfg.ticks is not None,
        "volume_cap": str(cfg.volume_cap),
        "price_limit_ratio": str(cfg.gate_settings.price_limit_ratio),
        "data_version": cfg.data_version,
        "code_commit": cfg.code_commit,
        "seed": cfg.seed,
        "instruments": sorted(cfg.data),
        "settings_hash": cfg.account_limits.version_hash(),
        "digest": result.digest,
        "reproducible": None,
        "reproducible_note": "재현성(D-074)은 두 번 실행해 지문을 비교해야 알 수 있음 — 이 파일에서는 미산출, report.md 참조",
        "signals": len(result.signals),
        "fills": len(result.fills),
        "open_positions": open_n,
        "k2_resumes": result.k2_resumes,
        "quality_failures": len(result.quality_failures),
        "quality_failure_instruments": sorted(result.quality_failures),
        "rejects": {k: v for k, v in sorted(result.rejects.items())},
        "costs": str(result.costs),
        "metrics": dict(result.metrics),
        "files": {"trades.csv": "UTF-8 BOM, CRLF", "equity.csv": "UTF-8 BOM, CRLF"},
        **({"universe": {"method": "U-B", "list": spec.universe_list, "point_in_time": result.universe_point_in_time,
                         "history_dates": len(cfg.universe_history or {}),
                         "note": "목록 이력으로 판정일 목록 재현" if result.universe_point_in_time
                         else UB_NO_HISTORY_NOTE}} if spec.universe_method == "U-B" else {}),
    })


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

def perturbation_check(cfg: BacktestConfig, check_days: list[date], *, flat: bool = True,
                       stateful: bool = True) -> list[str]:
    """판정 기준 시각 이후 데이터를 크게 바꿔도 신호가 같아야 한다. 다르면 그 날짜를 돌려준다 (V-91).

    - flat: 보유·미체결·이력 없는 상태로 그 날의 각 판정 시각 스냅샷만 만들어 비교한다. 검사일 이후 봉(당일 포함)을 교란.
    - stateful: 실제 실행 상태(보유·미체결·이력·현금·게이트·체결)로 비교한다. 전체 기간을 한 번 돌린 기준 결과와,
      검사일까지만 돌리되 데이터를 교란한 실행의 **검사일 모든 판정 신호**를 비교한다.
      교란 2종: 검사일 다음 날 이후 봉은 전부(균일 ×3 / 봉마다 다른 배율), 검사일 당일 봉은 **종가만** 그날 고가 또는
      저가로 바꾼다. 당일 시가·고가·저가·거래량은 체결 모델(첫 판정 시가 체결·지정가 관통·거래량 상한)과 판정 시세(시가)가
      정당하게 쓰므로 바꾸지 않는다 — 따라서 당일 고가·저가·거래량의 누출은 이 검사로 잡지 못한다(flat 검사가 담당).
      고가 = 저가 = 종가인 봉은 당일 교란이 없다. 판정 시각 사이의 체결(종가)은 그날 마지막 판정 뒤라 신호에 영향 없음.
      (미래 봉 제거 변형은 검사일에서 끝나는 실행에서는 의미가 없어 뺐다 — 실행 시간 절약.)
      실행은 임시 폴더에서 하고(var/backtest를 어지럽히지 않음), 최종 평가 구간 장부를 쓰지 않는다(segment=ALL로 같은 날짜).
    """
    times = eval_times_of(cfg)
    spec = cfg.strategy.spec

    def signals_at(data: dict[str, list[RawDaily]], day: date, et: time) -> list[tuple]:
        store = MarketDataStore(cfg.calendar, cfg.ticks)
        for inst, raw in data.items():
            store.register(cfg.infos[inst])
            store.update_status(inst, frozenset())
            store.update_daily(inst, raw, cfg.data_version)
        as_of = datetime.combine(day, et, tzinfo=KST)
        ev = evaluation_id_of(spec.strategy_id, spec.version, "M1", f"{day.isoformat()}T{et:%H:%M}")
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

    def close_moved(r: RawDaily, to_high: bool) -> RawDaily:
        target = r.high if to_high else r.low
        if target == r.close:
            target = r.low if to_high else r.high
        return replace(r, close=target)

    bad: set[str] = set()
    if flat:
        for day in check_days:
            variants = (
                {inst: [scaled(r, Decimal(3), Decimal(3)) if r.day >= day else r for r in raw]   # 균일 배율
                 for inst, raw in cfg.data.items()},
                {inst: [scaled(r, factor(inst, r.day), 2 - factor(inst, r.day) + Decimal("0.5")) if r.day >= day
                        else r for r in raw] for inst, raw in cfg.data.items()},                  # 봉마다 다른 배율
                {inst: [r for r in raw if r.day < day] for inst, raw in cfg.data.items()},       # 미래 봉 제거
            )
            for et in times:
                base = signals_at(cfg.data, day, et)
                if any(signals_at(v, day, et) != base for v in variants):
                    bad.add(day.isoformat())
                    break

    if stateful:
        run_days = _run_days(cfg)
        targets = [d for d in check_days if run_days and run_days[0] <= d <= run_days[-1]]
        if targets:
            with tempfile.TemporaryDirectory(prefix="v91-") as tmp:
                probe = replace(cfg, segment="ALL", start=run_days[0], end=run_days[-1], out_dir=Path(tmp),
                                final_usage_ledger=None, write_exports=False)
                base_run = run_backtest(replace(probe, run_id="base"))
                for n, day in enumerate(targets):
                    keys = [f"{day.isoformat()}T{et:%H:%M}" for et in times]
                    want = [base_run.signals_by_eval.get(k) for k in keys]
                    variants = (
                        {inst: [scaled(r, Decimal(3), Decimal(3)) if r.day > day else
                                (close_moved(r, True) if r.day == day else r) for r in raw]
                         for inst, raw in cfg.data.items()},
                        {inst: [scaled(r, factor(inst, r.day), 2 - factor(inst, r.day) + Decimal("0.5")) if r.day > day
                                else (close_moved(r, False) if r.day == day else r) for r in raw]
                         for inst, raw in cfg.data.items()},
                    )
                    for m, v in enumerate(variants):
                        got = run_backtest(replace(probe, run_id=f"d{n}v{m}", data=v, end=day))
                        if [got.signals_by_eval.get(k) for k in keys] != want:
                            bad.add(day.isoformat())
                            break
    return sorted(bad)


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
    times = eval_times_of(cfg)
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
        f"지정가 관통 체결, 봉 거래량 {cfg.volume_cap:%} 상한, 상한가 매수·하한가 매도 미체결 "
        f"(상·하한가 = 기준가×(1±{cfg.gate_settings.price_limit_ratio})를 호가 단위로 내림·올림)",
        f"- 판정 시각: {','.join(t.strftime('%H:%M') for t in times)} — 첫 판정 주문은 그날 시가(DP-7), "
        "이후 판정 주문은 그날 종가로 마지막 판정 뒤 체결(제안 가정), 판정 시세는 모두 그날 시가, "
        "체결 시각 = 체결을 일으킨 판정 시각",
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
        *([f"- {UB_NO_HISTORY_NOTE}"] if result.universe_point_in_time is False else []),
        "- 거래 상태(VI·거래정지) 이력 미반영: 그날 봉이 없으면 체결 없음으로만 처리.",
        "- 모의·실전 체결 차이는 PAPER에서 확인.",
        "- 일일 손실(K2)은 판정 시각에 한 번만 평가: 장중 손실로 K2가 걸려 이후 주문이 막히는 경우는 재현하지 않음(DP-6).",
        f"- 첫 판정({times[0]:%H:%M}) 주문을 그날 시가로 체결: 09:00~판정 시각 사이 가격 변동을 무시(DP-7). "
        "체결 시각은 판정 시각으로 기록(시가 가격과 시각이 다름).",
        *([] if len(times) == 1 else [
            "- 이후 판정 주문을 그날 종가로 체결(제안 가정): 판정 시각~종가 사이 가격 경로 무시, 판정 시세는 그날 시가"
            "(장중 가격 없음) — 실제 장중 판정과 신호 가격·체결가가 다를 수 있음. 거래량 상한은 하루 합산."]),
        "- 일봉 지정가 한계(CP-032 P-5): 첫 판정 지정가는 하루 고가·저가로 관통 판정 — 판정 시각 전 가격이 포함될 수 "
        "있어 장중 지정가 체결이 낙관적일 수 있음. 분봉 데이터(CAP-DAT-02) 전까지 유지. 접촉만은 미체결, 체결가 = 지정가.",
        *([f"- 호가 표 없음(X-U1): 슬리피지 1틱과 상·하한가 호가 내림·올림에 `--slippage-tick` {cfg.slippage_tick}원을 "
           "모든 가격대에 일괄 사용 — 실제 가격대별 호가 단위와 다를 수 있음."] if cfg.ticks is None else []),
        "- 룩어헤드 교란 검사(V-91) 범위: 무보유 상태 스냅샷 + 실제 실행 상태(보유·미체결·이력) 재실행 비교. "
        "실제 상태 비교에서 검사일 당일 봉은 종가만 교란(시가·고가·저가·거래량은 체결 모델이 쓰므로 제외).",
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
