"""검증 실행기 (T-026, CP-030 A-082: D-161~D-168·D-170, V-221~V-230). BACKTEST 전용.

주문 경로·게이트·실행기·브로커 어댑터·PAPER 코드는 바꾸지 않는다. 각 실행은 기존 `run_backtest`(실운영과 같은
전략·판정 실행기·게이트·실행기·정지 관리 + 체결 모델)를 그대로 부르고, 이 모듈은 창·변형·비교 실행을 묶어 판정표를 만든다.

- D-162 워크포워드: 창 k=1..7, 학습 2016+k−1 ~ 2018+k−1, 검증 2019+k−1. 창마다 자금·보유를 새로 시작한다.
  창 시작 전 일봉은 워밍업(as-of 스냅샷)으로만 쓰이고, 창 끝 이후 일봉은 실행 전에 잘라낸다(룩어헤드 차단, V-222).
  시스템은 파라미터를 고르지 않는다(자동 최적화 없음). 창별 파라미터를 주면 그대로 쓰고 기록한다(D-077).
- D-163 최종 확인 구간(`final_from` 이후): 겹치는 실행은 최종 확인 실행(`run_final_check`)으로만, 전략 확정 A-ID와
  함께, 전략 버전당 1회(백테스트 FINAL과 같은 장부 형식 — 둘 중 하나만 쓸 수 있다).
- D-164 % 슬리피지: 기본 0.1%, 2배 0.2%. C-3 = 0.2%에서 검증 구간(7개 창 합산) 거래당 기대값 > 0.
- D-165 파라미터 ±20%: 수치 파라미터 하나씩, 실행 수 2P(창마다). C-4 = 모든 변형의 합산 기대값 > 0. 변형은 기본 슬리피지.
- D-166 비교(참고, 판정 제외): 단순 보유(첫날 동일 비중 매수·끝까지 보유, 비용·슬리피지 반영) / 무작위 진입(전략과 같은
  거래 수·보유 기간 분포, 같은 게이트·비용·슬리피지, 시드 고정 기본 200회)의 백분위.
- D-167 C-2: 합산 거래 수 < 100 → 판단 불가. D-168 C-7: 사후 선택 목록이면 생존편향 표시 + "통과(과대 추정 가능)".
- D-161 검증용 전략(paper0-verify 계열)은 성과 판정 제외.
- 거래 1건 = 진입~전량 청산(D-078). 창 끝에 남은 보유는 거래 수·기대값에 넣지 않고 창별 "미청산"으로 기록한다.
판정표는 기록과 판정 제안이며 전환 승인은 사용자다.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import tempfile
import time as _time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path

from trading_system.app import backtest_export as bx
from trading_system.app.backtest import BacktestConfig, BacktestResult, run_backtest
from trading_system.domain.clock import KST
from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.market import CostRates, estimate_cost
from trading_system.domain.models import Signal
from trading_system.domain.strategy import EvaluationSnapshot, RuleSpec, StrategySpec
from trading_system.marketdata.quality import RawDaily
from trading_system.strategy.base import make_signal

FINAL_FROM = date(2026, 1, 1)          # D-163 (설정값, 변경은 CP)
SLIPPAGE_BASE = Decimal("0.001")       # D-164 기본 0.1% (2배 = 0.2%)
MIN_TRADES = 100                       # D-167
RANDOM_RUNS = 200                      # D-166 [제안 — 횟수]
VERIFY_FAMILY = ("paper0-verify",)     # D-161 검증용 전략 (D-076 PAPER-0)
BANNER = "생존편향 포함 — 모든 수익은 과대 추정된 것으로 본다"
_Q6 = Decimal("0.000001")
_Q2 = Decimal("0.01")
_LEDGER_APPROVAL = re.compile(r"A-\d+")


class ValidationError(RuntimeError):
    pass


# --- 창 (D-162) ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    k: int
    train_start: date
    train_end: date
    val_start: date
    val_end: date


def walkforward_windows(first_train_year: int = 2016, train_years: int = 3, n: int = 7) -> tuple[Window, ...]:
    """D-162: 학습 3년 / 검증 1년 / 1년 이동. 기본 = 검증 2019~2025 7개."""
    out = []
    for k in range(1, n + 1):
        ts = first_train_year + k - 1
        te = ts + train_years - 1
        out.append(Window(k, date(ts, 1, 1), date(te, 12, 31), date(te + 1, 1, 1), date(te + 1, 12, 31)))
    return tuple(out)


# --- 최종 확인 구간 (D-163) ---------------------------------------------------------------------

def check_final_guard(start: date, end: date, final_from: date, *, final_check: bool) -> None:
    if final_check:
        if start < final_from:
            raise ValidationError(f"최종 확인 실행은 최종 확인 구간만 쓴다 (시작 {start} < {final_from}, D-163)")
        return
    if end >= final_from:
        raise ValidationError(f"실행 날짜 {start}~{end}가 최종 확인 구간({final_from} 이후)과 겹침 — "
                              "최종 확인 옵션으로만 허용 (D-163)")


def claim_final_check(ledger: Path, spec: StrategySpec, run_id: str, *, confirmed_by: str) -> None:
    """전략 확정(사용자 선언 A-ID) 뒤 1회만. 장부는 백테스트 FINAL(D-075)과 같은 JSONL 형식·키."""
    if not _LEDGER_APPROVAL.fullmatch(confirmed_by or ""):
        raise ValidationError("최종 확인은 전략 확정(사용자 선언 A-ID) 뒤에만 실행 (D-163)")
    key = {"strategy": spec.strategy_id, "version": spec.version}
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("strategy") == key["strategy"] and rec.get("version") == key["version"]:
                raise ValidationError(f"최종 확인 구간은 전략 버전당 한 번만 (이미 사용: {rec.get('run_id')})")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**key, "run_id": run_id, "kind": "final_check", "confirmed_by": confirmed_by,
                            "used_at": datetime.now(UTC).isoformat()}, ensure_ascii=False) + "\n")


# --- 창 실행 ------------------------------------------------------------------------------------

class _Recorder:
    """전략을 감싸 진입 신호(종목·날짜·수량·가격·손절가)를 기록한다. 판정·신호는 그대로 (무작위 진입 크기용)."""

    def __init__(self, inner: object, sink: list[tuple[str, date, Decimal, Decimal, Decimal | None]]) -> None:
        self._inner = inner
        self.spec = inner.spec  # type: ignore[attr-defined]
        self._sink = sink

    def evaluate(self, snapshot: EvaluationSnapshot, params: dict[str, object]) -> list[Signal]:
        out = self._inner.evaluate(snapshot, params)  # type: ignore[attr-defined]
        for s in out:
            if s.kind is RequestType.ENTRY:
                self._sink.append((s.instrument, s.as_of.astimezone(KST).date(), s.quantity, s.price, s.stop_price))
        return out


def window_data(data: dict[str, list[RawDaily]], end: date) -> dict[str, list[RawDaily]]:
    """창 끝 이후 일봉을 잘라낸다. 그때까지 봉이 없는 종목(늦은 상장)은 뺀다."""
    out = {i: [r for r in raw if r.day <= end] for i, raw in data.items()}
    return {i: v for i, v in out.items() if v}


def run_window(template: BacktestConfig, w: Window, *, label: str, out_dir: Path, slippage_pct: Decimal,
               params: dict[str, object] | None = None, export: bool = True, run_id: str | None = None,
               strategy: object | None = None,
               recorder: list[tuple[str, date, Decimal, Decimal, Decimal | None]] | None = None) -> BacktestResult:
    """창 하나의 검증 구간 실행 (자금·보유 새로 시작). 최종 구간 검사는 호출하는 쪽(run_validation·run_final_check)."""
    data = window_data(template.data, w.val_end)
    strat = strategy if strategy is not None else template.strategy
    if recorder is not None:
        strat = _Recorder(strat, recorder)
    cfg = replace(template, run_id=run_id or f"w{w.k}-{label}", data=data,
                  infos={i: template.infos[i] for i in data}, start=w.val_start, end=w.val_end,
                  params=dict(template.params if params is None else params), slippage_pct=slippage_pct,
                  segment="ALL", out_dir=out_dir, final_usage_ledger=None, write_exports=export,
                  strategy=strat)  # type: ignore[arg-type]
    return run_backtest(cfg)


# --- 파라미터 변형 (D-165) ----------------------------------------------------------------------

@dataclass(frozen=True)
class ParamVariant:
    param: str
    direction: str           # "+" / "-"
    base: object
    value: object
    note: str
    params: dict[str, object]

    @property
    def label(self) -> str:
        return f"p-{self.param}{'plus' if self.direction == '+' else 'minus'}"


def param_variants(spec: StrategySpec, params: dict[str, object]) -> list[ParamVariant]:
    """수치 파라미터(int·decimal)를 하나씩 ±20%. 정수는 가장 가까운 정수(반올림), 같은 값이면 ±1,
    명세 허용 범위 밖이면 경계값. 실행 수 = 2 × 수치 파라미터 수."""
    out: list[ParamVariant] = []
    for p in spec.params:
        if p.type not in ("int", "decimal") or p.name not in params:
            continue
        base = params[p.name]
        for direction, factor in (("+", Decimal("1.2")), ("-", Decimal("0.8"))):
            notes: list[str] = []
            raw = Decimal(str(base)) * factor
            value: Decimal
            if p.type == "int":
                value = raw.to_integral_value(rounding=ROUND_HALF_UP)
                if value == Decimal(str(base)):
                    value = Decimal(str(base)) + (1 if direction == "+" else -1)
                    notes.append("±20%가 같은 정수 → ±1")
            else:
                value = raw
            if p.maximum is not None and value > p.maximum:
                value = p.maximum
                notes.append(f"허용 범위 밖 → 경계값 {p.maximum}")
            if p.minimum is not None and value < p.minimum:
                value = p.minimum
                notes.append(f"허용 범위 밖 → 경계값 {p.minimum}")
            if value == Decimal(str(base)):
                notes.append("기준값과 같음(변형 없음)")
            v_obj: object = int(value) if p.type == "int" else value
            out.append(ParamVariant(p.name, direction, base, v_obj, "; ".join(notes), {**params, p.name: v_obj}))
    return out


# --- 단순 보유 (D-166 (1)) ----------------------------------------------------------------------

def _mdd(initial: Decimal, values: Sequence[Decimal]) -> Decimal:
    peak, mdd = initial, Decimal("0")
    for e in values:
        if e >= peak:
            peak = e
        else:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def buy_and_hold(data: dict[str, list[RawDaily]], days: Sequence[date], initial_cash: Decimal,
                 slippage_pct: Decimal, rates: CostRates,
                 price_unit: Callable[[str, Decimal], Decimal]) -> dict[str, object]:
    """첫 거래일 시가 × (1 + s)(불리한 방향 단위 올림)로 첫날 봉이 있는 종목을 동일 금액(원 미만 내림) 매수,
    끝까지 보유. 매수 비용(D-065) 반영, 평가는 각 날짜 이전 마지막 종가(매도 비용 미반영 — 전략 평가액과 같은 기준)."""
    first = days[0]
    bars = {i: {r.day: r for r in raw} for i, raw in data.items()}
    inst = sorted(i for i in bars if first in bars[i])
    excluded = sorted(i for i in bars if first not in bars[i])
    cash, costs = initial_cash, Decimal("0")
    qty: dict[str, Decimal] = {}
    if inst:
        per = (initial_cash / len(inst)).to_integral_value(rounding=ROUND_FLOOR)
        for i in inst:
            raw = bars[i][first].open * (1 + slippage_pct)
            unit = price_unit(i, raw)
            price = (raw / unit).to_integral_value(rounding=ROUND_CEILING) * unit
            q = (per / price).to_integral_value(rounding=ROUND_FLOOR)
            while q > 0 and q * price + estimate_cost(q * price, False, rates) > per:
                q -= 1
            if q > 0:
                c = estimate_cost(q * price, False, rates)
                cash -= q * price + c
                costs += c
                qty[i] = q
    last: dict[str, Decimal] = {}
    values: list[Decimal] = []
    sorted_bars = {i: sorted(bars[i].values(), key=lambda r: r.day) for i in qty}
    pos = dict.fromkeys(qty, 0)
    for d in days:
        for i in qty:
            rows = sorted_bars[i]
            while pos[i] < len(rows) and rows[pos[i]].day <= d:
                last[i] = rows[pos[i]].close
                pos[i] += 1
        values.append(cash + sum((q * last[i] for i, q in qty.items() if i in last), Decimal("0")))
    final = values[-1] if values else initial_cash
    return {"instruments": inst, "excluded": excluded, "final_equity": format(final, "f"),
            "total_return": format(((final - initial_cash) / initial_cash).quantize(_Q6), "f"),
            "max_drawdown": format(_mdd(initial_cash, values).quantize(_Q6), "f"), "costs": format(costs, "f")}


# --- 무작위 진입 (D-166 (2)) --------------------------------------------------------------------

@dataclass(frozen=True)
class TradeTemplate:
    """전략 거래 1건에서 가져온 보유 거래일 수·진입 금액·손절 거리 비율(없으면 None)."""

    holding_days: int
    notional: Decimal
    stop_ratio: Decimal | None


@dataclass(frozen=True)
class ScheduledTrade:
    instrument: str
    entry_day: date
    exit_day: date
    notional: Decimal
    stop_ratio: Decimal | None
    holding_days: int


def random_schedule(key: str, days: Sequence[date], bars_on: dict[str, set[date]], templates: Sequence[TradeTemplate],
                    universe: Sequence[str], attempts: int = 500) -> tuple[tuple[ScheduledTrade, ...], int]:
    """시드 문자열로 결정적. 전략 거래마다 (종목, 진입일)을 무작위로 고르고 보유 기간은 그 거래 값 그대로.
    진입·청산일에 봉이 있어야 하고 같은 종목의 구간은 겹치지 않는다. 자리를 못 찾은 거래 수를 함께 돌려준다."""
    rng = random.Random(key)
    order = list(templates)
    rng.shuffle(order)
    taken: dict[str, list[tuple[int, int]]] = {}
    out: list[ScheduledTrade] = []
    skipped = 0
    n = len(days)
    for tpl in order:
        h = tpl.holding_days
        placed = False
        for _ in range(attempts if universe and h < n else 0):
            ei = rng.randrange(0, n - h)
            xi = ei + h
            inst = universe[rng.randrange(len(universe))]
            have = bars_on.get(inst, set())
            if days[ei] not in have or days[xi] not in have:
                continue
            if any(not (xi < a or ei > b) for a, b in taken.get(inst, [])):
                continue
            taken.setdefault(inst, []).append((ei, xi))
            out.append(ScheduledTrade(inst, days[ei], days[xi], tpl.notional, tpl.stop_ratio, h))
            placed = True
            break
        skipped += 0 if placed else 1
    out.sort(key=lambda t: (t.entry_day, t.instrument))
    return tuple(out), skipped


class RandomEntryStrategy:
    """무작위 진입 비교용 (백테스트 전용, D-166). 일정표대로 첫 판정에 시장가 진입, 보유 구간이 끝나면 시장가 청산.
    크기 = floor(진입 금액 / 판정 시세), 손절가 = 시세 × (1 − 손절 거리 비율) 원 미만 내림(게이트 L1 계산용).
    게이트·한도·비용·슬리피지는 전략 실행과 같다(같은 run_backtest)."""

    def __init__(self, base_spec: StrategySpec, schedule: Sequence[ScheduledTrade], eval_times: Sequence[time]) -> None:
        rules = (
            RuleSpec("EXIT-1", "EXIT", ("상태 입력",), "CAP-TRG-01", "보유 AND 일정표 보유 구간 밖", "청산 전량 시장가",
                     "데이터 품질 실패·처리 중 주문", 1),
            RuleSpec("ENTRY-1", "ENTRY", ("상태 입력",), "CAP-TRG-01", "보유 없음 AND 일정표 진입일 첫 판정",
                     "진입 시장가(일정표 금액)", "데이터 품질 실패·거래 상태 이상·처리 중 주문", 1),
        )
        self.spec = replace(base_spec, strategy_id=f"{base_spec.strategy_id}-random", name="무작위 진입 비교(D-166)",
                            description="검증 실행기 무작위 진입 비교 — 백테스트 전용", rules=rules, params=())
        self._by_inst: dict[str, list[ScheduledTrade]] = {}
        for t in schedule:
            self._by_inst.setdefault(t.instrument, []).append(t)
        self._times = tuple(eval_times)
        self._valid = timedelta(minutes=min(10, base_spec.validity_max_minutes))

    def evaluate(self, snapshot: EvaluationSnapshot, params: dict[str, object]) -> list[Signal]:
        at = snapshot.as_of.astimezone(KST)
        day, now = at.date(), time(at.hour, at.minute)
        first, last = now == self._times[0], now == self._times[-1]
        out: list[Signal] = []
        for v in snapshot.instruments:
            if not v.data_ok or v.has_pending_order or not v.daily_bars:
                continue
            price = v.last_price if v.last_price is not None else v.daily_bars[-1].close
            plan = self._by_inst.get(v.instrument, [])
            held = v.position is not None and v.position.owned_by_me and v.position.quantity > 0
            if held:
                assert v.position is not None
                active = any(t.entry_day <= day < t.exit_day or (t.entry_day == day == t.exit_day and not last)
                             for t in plan)
                if not active:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT,
                                           v.position.quantity, OrderType.MARKET, price, self._valid))
            elif (v.position is None or v.position.quantity == 0) and first and not v.statuses:
                t = next((t for t in plan if t.entry_day == day), None)
                if t is None or price <= 0:
                    continue
                qty = (t.notional / price).to_integral_value(rounding=ROUND_FLOOR)
                stop = None
                if t.stop_ratio is not None:
                    stop = (price * (1 - t.stop_ratio)).to_integral_value(rounding=ROUND_FLOOR)
                    if stop <= 0 or stop >= price:
                        stop = None
                if qty > 0:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY, qty,
                                           OrderType.MARKET, price, self._valid, stop_price=stop))
        return out


def percentile(value: Decimal, dist: Sequence[Decimal]) -> Decimal | None:
    """(분포 중 value보다 작은 수 + 같은 수 × ½) / 전체 × 100, 소수 첫째 자리."""
    if not dist:
        return None
    less = sum(1 for x in dist if x < value)
    eq = sum(1 for x in dist if x == value)
    return ((Decimal(less) + Decimal(eq) / 2) / len(dist) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


# --- 판정 (§3.3, D-161·D-167·D-168) -------------------------------------------------------------

def is_verification_strategy(spec: StrategySpec) -> bool:
    return any(spec.strategy_id == f or spec.strategy_id.startswith(f + "-") for f in VERIFY_FAMILY)


def decide_verdict(*, exempt: bool, pooled_trades: int, c3_expectancy: Decimal | None,
                   c4_expectancies: Sequence[Decimal | None], after_the_fact_universe: bool) -> dict[str, object]:
    banner = BANNER if after_the_fact_universe else ""
    if exempt:
        return {"verdict": "적용 제외(검증용 전략, D-161)", "banner": banner, "C-2": "적용 제외", "C-3": "적용 제외",
                "C-4": "적용 제외", "reasons": ["검증용 전략(D-076 PAPER-0)은 성과 기준 적용 제외 — 전환 조건 기존대로"]}
    c2 = "충족" if pooled_trades >= MIN_TRADES else "판단 불가"
    c3 = "충족" if c3_expectancy is not None and c3_expectancy > 0 else "미충족"
    c4 = "충족" if all(e is not None and e > 0 for e in c4_expectancies) else "미충족"
    reasons = []
    if c2 != "충족":
        reasons.append(f"C-2 합산 거래 {pooled_trades}건 < {MIN_TRADES}")
    if c3 != "충족":
        reasons.append(f"C-3 슬리피지 2배 거래당 기대값 {_d(_fmt(c3_expectancy))}원 ≤ 0 또는 거래 없음")
    if c4 != "충족":
        reasons.append("C-4 ±20% 변형 중 거래당 기대값 ≤ 0 또는 거래 없음이 있음")
    if c2 != "충족":
        verdict = "판단 불가"
    elif c3 != "충족" or c4 != "충족":
        verdict = "미충족"
    else:
        verdict = "통과(과대 추정 가능)" if after_the_fact_universe else "통과"
    return {"verdict": verdict, "banner": banner, "C-2": c2, "C-3": c3, "C-4": c4, "reasons": reasons}


# --- 실행기 (D-170) -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationInputs:
    template: BacktestConfig          # run_id·start·end·segment·out_dir·slippage는 창마다 바꾼다
    out_dir: Path                     # var/backtest/validation/<ID>-v<n>
    report_version: int
    windows: tuple[Window, ...] = field(default_factory=walkforward_windows)
    final_from: date = FINAL_FROM
    slippage_base: Decimal = SLIPPAGE_BASE
    window_params: dict[int, dict[str, object]] = field(default_factory=dict)   # 창별 파라미터(사람이 고른 값)
    window_params_files: dict[int, str] = field(default_factory=dict)          # 기록용 경로
    random_runs: int = RANDOM_RUNS
    random_seed: str = "cp030"
    universe_point_in_time: bool = False   # True = 판정 시점 목록(생존편향 표시 없음). 기본은 사후 선택 목록으로 본다
    universe_note: str = ""
    workers: int = 1
    command: str = ""


@dataclass(frozen=True)
class _Job:
    kind: str                          # base / slip2x / repro / variant / random
    k: int
    label: str
    slippage_pct: Decimal
    params: dict[str, object] | None = None
    export: bool = True
    record: bool = False
    schedule: tuple[ScheduledTrade, ...] = ()


_CTX: dict[str, object] = {}


def _init_worker(template: BacktestConfig, out_dir: Path, windows: tuple[Window, ...]) -> None:
    _CTX.update(template=template, out_dir=out_dir, windows={w.k: w for w in windows})


def _open_count(fills: list[tuple[date, str, str, str, str, str]]) -> int:
    net: Counter = Counter()
    for _d, _n, inst, side, q, _p in fills:
        net[inst] += Decimal(q) if side == "BUY" else -Decimal(q)
    return sum(1 for v in net.values() if v != 0)


def _execute(job: _Job) -> dict[str, object]:
    template: BacktestConfig = _CTX["template"]  # type: ignore[assignment]
    w: Window = _CTX["windows"][job.k]  # type: ignore[index]
    rec: list[tuple[str, date, Decimal, Decimal, Decimal | None]] | None = [] if job.record else None
    strat = None
    params = job.params
    if job.kind == "random":
        strat = RandomEntryStrategy(template.strategy.spec, job.schedule,
                                    tuple(template.eval_times) or (template.eval_time,))
        params = {}
    t0 = _time.perf_counter()
    if job.export:
        r = run_window(template, w, label=job.label, out_dir=_CTX["out_dir"] / "runs",  # type: ignore[operator]
                       slippage_pct=job.slippage_pct, params=params, strategy=strat, recorder=rec)
    else:
        tmp = tempfile.mkdtemp(prefix="t026-")
        try:
            r = run_window(template, w, label=job.label, out_dir=Path(tmp), slippage_pct=job.slippage_pct,
                           params=params, export=False, strategy=strat, recorder=rec)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    initial = template.initial_cash
    final = r.equity[-1][1] if r.equity else initial
    return {
        "kind": job.kind, "k": job.k, "label": job.label, "digest": r.digest, "metrics": dict(r.metrics),
        "total_return_exact": format((final - initial) / initial, "f"),
        "trades": [(t.instrument, t.entry_day.isoformat(), t.exit_day.isoformat(), format(t.pnl, "f"), t.holding_days)
                   for t in r.trades],
        "entries": [(i, d.isoformat(), format(q, "f"), format(p, "f"), None if s is None else format(s, "f"))
                    for i, d, q, p, s in (rec or [])],
        "days": [d.isoformat() for d in r.days], "open_positions": _open_count(r.fills),
        "rejects": dict(sorted(r.rejects.items())), "quality_failures": sorted(r.quality_failures),
        "signals": len(r.signals), "k2_resumes": r.k2_resumes, "run_dir": r.db_path.parent.as_posix(),
        "seconds": round(_time.perf_counter() - t0, 1),
    }


def _run_jobs(jobs: list[_Job], inputs: ValidationInputs) -> list[dict[str, object]]:
    args = (inputs.template, inputs.out_dir, inputs.windows)
    if inputs.workers <= 1 or len(jobs) <= 1:
        _init_worker(*args)
        return [_execute(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=inputs.workers, initializer=_init_worker, initargs=args) as ex:
        return list(ex.map(_execute, jobs, chunksize=1))


def _pnl_sum(trades: list[tuple]) -> Decimal:
    return sum((Decimal(t[3]) for t in trades), Decimal("0"))


def _expectancy(trades: list[tuple]) -> Decimal | None:
    return _pnl_sum(trades) / len(trades) if trades else None


def _fmt(x: Decimal | None, q: Decimal = _Q2) -> str | None:
    return None if x is None else format(x.quantize(q, rounding=ROUND_HALF_UP), "f")


def _d(x: object) -> str:
    """판정표 칸: 값이 없으면 —."""
    return "—" if x is None else str(x)


def _chain(returns: Sequence[Decimal]) -> Decimal:
    acc = Decimal("1")
    for r in returns:
        acc *= 1 + r
    return acc - 1


def _pooled(outs: list[dict[str, object]]) -> dict[str, object]:
    trades = [t for o in outs for t in o["trades"]]  # type: ignore[union-attr]
    exp = _expectancy(trades)
    rets = [Decimal(str(o["total_return_exact"])) for o in outs]
    return {"trades": len(trades), "pnl_sum": format(_pnl_sum(trades), "f"), "expectancy": _fmt(exp),
            "expectancy_positive": exp is not None and exp > 0,
            "chained_return": format(_chain(rets).quantize(_Q6), "f"),
            "worst_mdd": max((str(o["metrics"]["max_drawdown"]) for o in outs), key=Decimal, default="0"),  # type: ignore[index]
            "loss_windows": sum(1 for r in rets if r < 0), "_exp": exp}


def _templates(out: dict[str, object]) -> list[TradeTemplate]:
    entries: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    for i, d, q, p, s in out["entries"]:  # type: ignore[union-attr]
        entries[(i, d)] = (q, p, s)
    found: list[TradeTemplate] = []
    missing: list[int] = []
    for inst, entry, _x, _pnl, hold in out["trades"]:  # type: ignore[union-attr]
        e = entries.get((inst, entry))
        if e is None:
            missing.append(hold)
            continue
        q, p, s = Decimal(e[0]), Decimal(e[1]), e[2]
        ratio = None if s is None else (p - Decimal(s)) / p
        found.append(TradeTemplate(hold, q * p, ratio))
    if missing and found:
        avg = sum((t.notional for t in found), Decimal("0")) / len(found)
        found += [TradeTemplate(h, avg.to_integral_value(rounding=ROUND_FLOOR), None) for h in missing]
    return found


def _price_unit(template: BacktestConfig) -> Callable[[str, Decimal], Decimal]:
    def unit(inst: str, price: Decimal) -> Decimal:
        if template.ticks is not None:
            t = template.ticks.tick_of(template.infos[inst].kind, price)
            if t is not None:
                return t
        return Decimal("1")
    return unit


def _set_reproducible(run_dir: Path, ok: bool) -> None:
    p = run_dir / "summary.json"
    if not p.exists():
        return
    s = json.loads(p.read_text(encoding="utf-8"))
    s["reproducible"] = ok
    s["reproducible_note"] = "검증 실행기(T-026): 같은 입력으로 다시 실행해 결과 지문 비교 (D-074)"
    bx.write_summary(p, s)


def _stamps(run_dir: Path) -> dict[str, int]:
    p = run_dir / "trades.csv"
    c: Counter = Counter()
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines()[1:]:
            cols = line.split(",")
            for x in (cols[1], cols[2]):
                if x:
                    c[x[11:]] += 1
    return dict(sorted(c.items()))


def run_validation(inputs: ValidationInputs) -> dict[str, object]:
    t_start = _time.perf_counter()
    tpl = inputs.template
    spec = tpl.strategy.spec
    for w in inputs.windows:
        check_final_guard(w.val_start, w.val_end, inputs.final_from, final_check=False)
    if inputs.out_dir.exists():
        raise ValidationError(f"결과 폴더가 이미 있음: {inputs.out_dir} (새 보고서 버전으로)")
    if inputs.random_runs < 0 or inputs.slippage_base < 0:
        raise ValidationError("무작위 횟수·슬리피지는 0 이상")
    s1, s2 = inputs.slippage_base, inputs.slippage_base * 2
    after_the_fact = not inputs.universe_point_in_time
    eval_times = tuple(tpl.eval_times) or (tpl.eval_time,)
    out: dict[str, object] = {
        "strategy_id": spec.strategy_id, "strategy_version": spec.version, "report_version": inputs.report_version,
        "out_dir": inputs.out_dir.as_posix(),
        "conditions": {
            "windows": [{"k": w.k, "train": [w.train_start.isoformat(), w.train_end.isoformat()],
                         "validation": [w.val_start.isoformat(), w.val_end.isoformat()]} for w in inputs.windows],
            "final_from": inputs.final_from.isoformat(), "final_period_used": False,
            "slippage_base": format(s1, "f"), "slippage_double": format(s2, "f"),
            "eval_times": [t.strftime("%H:%M") for t in eval_times], "initial_cash": format(tpl.initial_cash, "f"),
            "params": {k: str(v) for k, v in sorted(tpl.params.items())},
            "window_params_files": {str(k): v for k, v in sorted(inputs.window_params_files.items())},
            "universe": sorted(set(tpl.data) & set(spec.universe)), "universe_method": spec.universe_method,
            "universe_point_in_time": inputs.universe_point_in_time, "universe_note": inputs.universe_note,
            "data_version": tpl.data_version, "code_commit": tpl.code_commit,
            "settings_hash": tpl.account_limits.version_hash(), "tick_table": tpl.ticks is not None,
            "price_limit_ratio": str(tpl.gate_settings.price_limit_ratio),
            "cost_rates": {"commission_rate": str(tpl.gate_settings.cost_rates.commission_rate),
                           "sell_tax_rate": str(tpl.gate_settings.cost_rates.sell_tax_rate)},
            "random_runs": inputs.random_runs, "random_seed": inputs.random_seed, "command": inputs.command,
        },
    }
    if is_verification_strategy(spec):
        out.update(verdict=decide_verdict(exempt=True, pooled_trades=0, c3_expectancy=None, c4_expectancies=[],
                                          after_the_fact_universe=after_the_fact),
                   runs=0, runs_by_kind={}, windows=[], variants=[], runtime_seconds={"total": 0})
        _write_outputs(inputs, out)
        return out
    inputs.out_dir.mkdir(parents=True)

    # 1단계: 전략 실행 (기본·2배·재현성·±20% 변형)
    jobs: list[_Job] = []
    variants_by_k: dict[int, list[ParamVariant]] = {}
    for w in inputs.windows:
        params = inputs.window_params.get(w.k, tpl.params)
        jobs += [_Job("base", w.k, "base", s1, params, record=True), _Job("slip2x", w.k, "slip2x", s2, params),
                 _Job("repro", w.k, "repro", s1, params, export=False)]
        variants_by_k[w.k] = param_variants(spec, params)
        jobs += [_Job("variant", w.k, v.label, s1, v.params) for v in variants_by_k[w.k]]
    t1 = _time.perf_counter()
    res = _run_jobs(jobs, inputs)
    t_phase1 = _time.perf_counter() - t1
    by = {(r["kind"], r["k"], r["label"]): r for r in res}

    # 2단계: 무작위 진입 (창마다 전략 기본 실행의 거래 수·보유 기간 분포)
    rjobs: list[_Job] = []
    random_meta: dict[int, dict[str, object]] = {}
    for w in inputs.windows:
        base = by[("base", w.k, "base")]
        tpls = _templates(base)
        days = [date.fromisoformat(d) for d in base["days"]]  # type: ignore[union-attr]
        data_k = window_data(tpl.data, w.val_end)
        universe = sorted(set(data_k) & set(spec.universe))
        bars_on = {i: {r.day for r in data_k[i]} for i in universe}
        skipped = []
        for n in range(inputs.random_runs if tpls else 0):
            sched, sk = random_schedule(f"{inputs.random_seed}:{w.k}:{n}", days, bars_on, tpls, universe)
            skipped.append(sk)
            rjobs.append(_Job("random", w.k, f"rnd{n:03d}", s1, export=False, schedule=sched))
        random_meta[w.k] = {"templates": len(tpls), "unplaced_total": sum(skipped)}
    t2 = _time.perf_counter()
    rres = _run_jobs(rjobs, inputs) if rjobs else []
    t_phase2 = _time.perf_counter() - t2

    # 재현성 → summary.json
    unit = _price_unit(tpl)
    windows_out: list[dict[str, object]] = []
    random_by_k: dict[int, list[dict[str, object]]] = {}
    for r in rres:
        random_by_k.setdefault(int(r["k"]), []).append(r)  # type: ignore[arg-type]
    for w in inputs.windows:
        base, dbl, rep = by[("base", w.k, "base")], by[("slip2x", w.k, "slip2x")], by[("repro", w.k, "repro")]
        reproducible = base["digest"] == rep["digest"]
        _set_reproducible(Path(str(base["run_dir"])), reproducible)
        days = [date.fromisoformat(d) for d in base["days"]]  # type: ignore[union-attr]
        data_k = window_data(tpl.data, w.val_end)
        bh = buy_and_hold({i: data_k[i] for i in data_k if i in spec.universe}, days, tpl.initial_cash, s1,
                          tpl.gate_settings.cost_rates, unit)
        rnd = random_by_k.get(w.k, [])
        dist = [Decimal(str(x["total_return_exact"])) for x in rnd]
        strat_ret = Decimal(str(base["total_return_exact"]))
        realized = [len(x["trades"]) for x in rnd]  # type: ignore[arg-type]
        windows_out.append({
            "k": w.k, "train": [w.train_start.isoformat(), w.train_end.isoformat()],
            "validation": [days[0].isoformat(), days[-1].isoformat(), len(days)],
            "reproducible": reproducible, "digest": base["digest"],
            "base": _run_summary(base), "slip2x": _run_summary(dbl),
            "eval_stamps": _stamps(Path(str(base["run_dir"]))),
            "buy_hold": bh,
            "random": {"runs": len(rnd), "templates": random_meta[w.k]["templates"],
                       "unplaced_total": random_meta[w.k]["unplaced_total"],
                       "realized_trades_mean": _fmt(Decimal(sum(realized)) / len(realized)) if realized else None,
                       "return_median": _fmt(_median(dist), _Q6) if dist else None,
                       "strategy_percentile": None if not dist else format(percentile(strat_ret, dist), "f")},
        })
    bases = [by[("base", w.k, "base")] for w in inputs.windows]
    dbls = [by[("slip2x", w.k, "slip2x")] for w in inputs.windows]
    pooled_base, pooled_dbl = _pooled(bases), _pooled(dbls)

    variants_out = []
    first_k = inputs.windows[0].k
    for idx, v in enumerate(variants_by_k[first_k]):
        outs = [by[("variant", w.k, variants_by_k[w.k][idx].label)] for w in inputs.windows]
        pv = _pooled(outs)
        variants_out.append({"param": v.param, "direction": v.direction, "base": str(v.base),
                             "values_by_window": {str(w.k): str(variants_by_k[w.k][idx].value) for w in inputs.windows},
                             "note": "; ".join(sorted({variants_by_k[w.k][idx].note for w in inputs.windows} - {""})),
                             **{k: x for k, x in pv.items() if k != "_exp"}, "_exp": pv["_exp"]})

    # 무작위 합산: 시드 n의 7개 창 수익률 연결
    pooled_random = None
    if inputs.random_runs and random_by_k:
        seeds: dict[str, list[Decimal]] = {}
        for k_list in random_by_k.values():
            for x in k_list:
                seeds.setdefault(str(x["label"]), []).append(Decimal(str(x["total_return_exact"])))
        complete = [_chain(v) for v in seeds.values() if len(v) == len(inputs.windows)]
        strat_chain = _chain([Decimal(str(o["total_return_exact"])) for o in bases])
        pooled_random = {"runs": len(complete), "median": _fmt(_median(complete), _Q6) if complete else None,
                         "strategy_percentile": None if not complete else format(percentile(strat_chain, complete), "f")}
    bh_chain = _chain([Decimal(str(x["buy_hold"]["total_return"])) for x in windows_out])  # type: ignore[index]

    verdict = decide_verdict(exempt=False, pooled_trades=int(pooled_base["trades"]),  # type: ignore[arg-type]
                             c3_expectancy=pooled_dbl["_exp"],  # type: ignore[arg-type]
                             c4_expectancies=[v["_exp"] for v in variants_out],  # type: ignore[misc]
                             after_the_fact_universe=after_the_fact)
    for v in variants_out:
        v.pop("_exp")
    pooled_base.pop("_exp")
    pooled_dbl.pop("_exp")
    n_var = sum(len(v) for v in variants_by_k.values())
    out.update(
        windows=windows_out, pooled={"base": pooled_base, "slip2x": pooled_dbl,
                                     "buy_hold_chained_return": format(bh_chain.quantize(_Q6), "f"),
                                     "random": pooled_random},
        variants=variants_out, verdict=verdict,
        trials={"param_sets_tried": 1 + (len(variants_out)), "per_window_params": bool(inputs.window_params),
                "note": "D-077 시도 조합 수 = 기준 1 + ±20% 변형 2P (창별 파라미터 파일이 있으면 그 파일 기준)"},
        runs=len(jobs) + len(rjobs),
        runs_by_kind={"base": len(inputs.windows), "slip2x": len(inputs.windows), "repro": len(inputs.windows),
                      "variants": n_var, "random": len(rjobs)},
        runtime_seconds={"strategy_runs": round(t_phase1, 1), "random_runs": round(t_phase2, 1),
                         "total": round(_time.perf_counter() - t_start, 1), "workers": inputs.workers},
    )
    _write_outputs(inputs, out)
    return out


def _median(xs: Sequence[Decimal]) -> Decimal:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _run_summary(o: dict[str, object]) -> dict[str, object]:
    m = o["metrics"]
    trades = o["trades"]
    return {"trades": len(trades), "expectancy": _fmt(_expectancy(trades)),  # type: ignore[arg-type]
            "total_return": m["total_return"], "max_drawdown": m["max_drawdown"],  # type: ignore[index]
            "win_rate": m["win_rate"], "costs": m["costs"], "final_equity": m["final_equity"],  # type: ignore[index]
            "open_positions": o["open_positions"], "rejects": o["rejects"], "signals": o["signals"],
            "quality_failures": o["quality_failures"], "k2_resumes": o["k2_resumes"]}


def _write_outputs(inputs: ValidationInputs, out: dict[str, object]) -> None:
    inputs.out_dir.mkdir(parents=True, exist_ok=True)
    (inputs.out_dir / "verdict.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8", newline="\n")
    (inputs.out_dir / f"validation-v{inputs.report_version}.md").write_text(render_validation(out), encoding="utf-8",
                                                                           newline="\n")


# --- 최종 확인 실행 (D-163) ---------------------------------------------------------------------

def run_final_check(template: BacktestConfig, out_dir: Path, *, final_from: date, end: date, ledger: Path,
                    confirmed_by: str, slippage_pct: Decimal = SLIPPAGE_BASE) -> dict[str, object]:
    """전략 확정 뒤 1회: final_from ~ end를 기본·2배 슬리피지로 실행. 장부는 실행 전에 기록(실패해도 1회 사용)."""
    check_final_guard(final_from, end, final_from, final_check=True)
    if out_dir.exists():
        raise ValidationError(f"결과 폴더가 이미 있음: {out_dir}")
    claim_final_check(ledger, template.strategy.spec, out_dir.name, confirmed_by=confirmed_by)
    out_dir.mkdir(parents=True)
    w = Window(0, final_from, final_from, final_from, end)
    rows = {}
    for label, s in (("final-base", slippage_pct), ("final-slip2x", slippage_pct * 2)):
        r = run_window(template, w, label=label, out_dir=out_dir / "runs", slippage_pct=s)
        trades = [(t.instrument, t.entry_day.isoformat(), t.exit_day.isoformat(), format(t.pnl, "f"), t.holding_days)
                  for t in r.trades]
        rows[label] = {"metrics": dict(r.metrics), "trades": len(trades), "expectancy": _fmt(_expectancy(trades)),
                       "digest": r.digest, "slippage_pct": format(s, "f")}
    res = {"strategy_id": template.strategy.spec.strategy_id, "strategy_version": template.strategy.spec.version,
           "confirmed_by": confirmed_by, "start": r.days[0].isoformat(), "end": r.days[-1].isoformat(), **rows}
    (out_dir / "final-check.json").write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                              newline="\n")
    return res


# --- 판정표 (docs/strategies/<ID>/validation-v<n>.md 양식) --------------------------------------

def render_validation(o: dict[str, object]) -> str:
    c = o["conditions"]  # type: ignore[assignment]
    v = o["verdict"]  # type: ignore[assignment]
    L: list[str] = [f"# 검증 판정표 — {o['strategy_id']} v{o['strategy_version']} (validation-v{o['report_version']})",
                    ""]
    if v["banner"]:  # type: ignore[index]
        L += [f"> **{v['banner']}** (C-7, D-168) — 유니버스가 판정 시점 목록이 아님"  # type: ignore[index]
              f"{(' · ' + c['universe_note']) if c['universe_note'] else ''}", ""]  # type: ignore[index]
    L += ["- 상태: **기록과 판정 제안** — PAPER 전환 승인은 사용자(A-ID). 성과 수치는 승인이 아니다.",
          f"- 결과 묶음: `{o['out_dir']}` (창별 trades.csv·equity.csv·summary.json, verdict.json)",
          f"- 명령: `{c['command'] or '(API 호출)'}`",  # type: ignore[index]
          ""]
    if not o.get("windows"):
        L += ["## 판정표", "", f"- 판정: **{v['verdict']}**", *[f"- {x}" for x in v["reasons"]], ""]  # type: ignore[index]
        return "\n".join(L) + "\n"
    pb, pd = o["pooled"]["base"], o["pooled"]["slip2x"]  # type: ignore[index]
    pr = o["pooled"]["random"]  # type: ignore[index]
    var = o["variants"]  # type: ignore[assignment]
    L += ["## 판정표 (04 §2.9 [CP-030], §3.3)", "",
          "| # | 항목 | 기준 | 결과 | 상태 |", "|---|---|---|---|---|",
          "| C-1 | 비용 | 수수료·세금·슬리피지 반영 결과만 | 모든 지표 비용 반영(D-065, 슬리피지 %) | 적용 |",
          f"| C-2 | 거래 수 | 검증 구간 합산 ≥ {MIN_TRADES} | {pb['trades']}건 | {v['C-2']} |",  # type: ignore[index]
          f"| C-3 | 슬리피지 2배 | {c['slippage_double']}에서 거래당 기대값 > 0 | {_d(pd['expectancy'])}원 "  # type: ignore[index]
          f"({pd['trades']}건) | {v['C-3']} |",  # type: ignore[index]
          f"| C-4 | 파라미터 ±20% | 모든 변형 거래당 기대값 > 0 [제안 정의] | 변형 {len(var)}개 중 "  # type: ignore[arg-type]
          f"{sum(1 for x in var if x['expectancy_positive'])}개 > 0 | {v['C-4']} |",  # type: ignore[index, union-attr]
          f"| C-5 | 단순 보유 비교 | 같은 기간 나란히 (참고) | 연결 수익률 {o['pooled']['buy_hold_chained_return']} "  # type: ignore[index]
          f"vs 전략 {pb['chained_return']} | 참고 |",
          f"| C-6 | 무작위 진입 비교 | {c['random_runs']}회 분포 대비 백분위 (참고) [제안] | "  # type: ignore[index]
          + (f"{pr['strategy_percentile']} 백분위 ({pr['runs']}회)" if pr else "미실행") + " | 참고 |",
          f"| C-7 | 생존편향 | 사후 선택 목록이면 표시 | {'표시함' if v['banner'] else '판정 시점 목록(표시 없음)'} | "  # type: ignore[index]
          f"{'해당' if v['banner'] else '해당 없음'} |",  # type: ignore[index]
          f"| C-8 | 최종 확인 | {c['final_from']} 이후는 전략 확정 뒤 1회 | 이 실행에서 미사용 | 해당 없음 |", "",  # type: ignore[index]
          f"- **판정: {v['verdict']}**", *[f"- 사유: {x}" for x in v["reasons"]],  # type: ignore[index]
          f"- 손실 창 수(기본 슬리피지): {pb['loss_windows']} / {len(o['windows'])}",  # type: ignore[arg-type]
          "", "## 실행 조건", "",
          f"- 창(D-162): " + ", ".join(f"k={w['k']} 학습 {w['train'][0]}~{w['train'][1]} / 검증 {w['validation'][0]}~"  # type: ignore[index]
                                      f"{w['validation'][1]}" for w in c["windows"]),  # type: ignore[index]
          "  — 학습 창은 사람이 파라미터를 고르는 구간(시스템 자동 최적화 없음), 실행하지 않음. 창마다 자금·보유 새로 시작",
          f"- 파라미터: {c['params']}" + (f" / 창별 파일 {c['window_params_files']}" if c["window_params_files"] else  # type: ignore[index]
                                       f" / {len(c['windows'])}개 창 같은 값"),  # type: ignore[arg-type]
          f"- 시도 조합 수(D-077): {o['trials']['param_sets_tried']} (기준 1 + 변형 {len(var)})",  # type: ignore[index, arg-type]
          f"- 슬리피지(D-164): 기본 {c['slippage_base']}, 2배 {c['slippage_double']} — 시가 × (1 ± s) 불리한 방향 "  # type: ignore[index]
          + ("호가 단위" if c["tick_table"] else "원 단위(호가 표 없음 X-U1)") + " 맞춤, 상·하한가 규칙 유지",  # type: ignore[index]
          f"- 비용(D-065): 수수료율 {c['cost_rates']['commission_rate']}, 매도 세율 {c['cost_rates']['sell_tax_rate']}",  # type: ignore[index]
          f"- 판정 시각: {', '.join(c['eval_times'])} / 초기 자금(창마다): {c['initial_cash']}원",  # type: ignore[index, arg-type]
          f"- 유니버스: {c['universe_method']} {len(c['universe'])}종목 "  # type: ignore[index, arg-type]
          f"({'판정 시점 목록 선언' if c['universe_point_in_time'] else '사후 선택 목록으로 취급'})",  # type: ignore[index]
          f"- 데이터 `{c['data_version']}` / 설정 해시 `{c['settings_hash']}` / 코드 `{c['code_commit']}`",  # type: ignore[index]
          f"- 최종 확인 구간(D-163): {c['final_from']} 이후 — 사용 안 함, 창 끝 이후 일봉은 실행 전에 잘라냄",  # type: ignore[index]
          "", "## 창별 결과 (기본 슬리피지, 비용 반영 후 — 기간별 일관성 §2.6)", "",
          "| 창 | 검증 구간 | 거래 | 기대값(원) | 총수익률 | MDD | 미청산 | 2배: 거래·기대값 | 단순 보유 | 무작위 백분위 | 재현성 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for w in o["windows"]:  # type: ignore[union-attr]
        b, d, bh, r = w["base"], w["slip2x"], w["buy_hold"], w["random"]
        L.append(f"| {w['k']} | {w['validation'][0]}~{w['validation'][1]} ({w['validation'][2]}일) | {b['trades']} | "
                 f"{_d(b['expectancy'])} | {b['total_return']} | {b['max_drawdown']} | {b['open_positions']} | "
                 f"{d['trades']}·{_d(d['expectancy'])} | {bh['total_return']} | {_d(r['strategy_percentile'])} | "
                 f"{'통과' if w['reproducible'] else '실패'} |")
    L += [f"| 합산 | | {pb['trades']} | {_d(pb['expectancy'])} | 연결 {pb['chained_return']} | 최악 {pb['worst_mdd']} | | "
          f"{pd['trades']}·{_d(pd['expectancy'])} | 연결 {o['pooled']['buy_hold_chained_return']} | "  # type: ignore[index]
          f"{pr['strategy_percentile'] if pr else '—'} | |", "",
          f"## 파라미터 ±20% (D-165, 기본 슬리피지, {len(o['windows'])}개 창 합산)", "",  # type: ignore[arg-type]
          "| 파라미터 | 방향 | 기준 | 변형 값 | 거래 | 기대값(원) | 연결 수익률 | 최악 MDD | 손실 창 | 비고 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for x in var:  # type: ignore[union-attr]
        vals = sorted(set(x["values_by_window"].values()))
        L.append(f"| {x['param']} | {x['direction']} | {x['base']} | {', '.join(vals)} | {x['trades']} | "
                 f"{_d(x['expectancy'])} | {x['chained_return']} | {x['worst_mdd']} | {x['loss_windows']} | {x['note'] or '—'} |")
    L += ["", "## 비교 (D-166, 참고 — 판정 제외)", "",
          "- 단순 보유: 창 첫 거래일 시가 × (1 + 기본 슬리피지)로 첫날 봉 있는 유니버스 종목을 동일 금액 매수·끝까지 보유, "
          "매수 비용 반영, 평가는 종가(매도 비용 미반영 — 전략 평가액과 같은 기준). 게이트 한도는 적용하지 않음.",
          "- 무작위 진입: 창마다 전략 기본 실행의 거래별 (보유 거래일 수, 진입 금액, 손절 거리)를 그대로 쓰고 종목·진입일만 "
          "무작위(시드 고정, 같은 종목 구간 겹침 없음). 같은 run_backtest(게이트·한도·비용·슬리피지)로 실행 — 한도 거부로 실제 "
          "거래 수는 전략보다 적을 수 있음. 백분위 = (무작위 수익률 < 전략 + 같음 × ½) / 횟수.",
          "", "| 창 | 단순 보유 수익률 | 단순 보유 MDD | 제외 종목(첫날 봉 없음) | 무작위 횟수 | 무작위 중앙값 | 무작위 평균 실현 거래 | 자리 못 찾은 거래 |",
          "|---|---|---|---|---|---|---|---|"]
    for w in o["windows"]:  # type: ignore[union-attr]
        bh, r = w["buy_hold"], w["random"]
        L.append(f"| {w['k']} | {bh['total_return']} | {bh['max_drawdown']} | {len(bh['excluded'])} | {r['runs']} | "
                 f"{_d(r['return_median'])} | {_d(r['realized_trades_mean'])} (전략 {w['base']['trades']}) | {r['unplaced_total']} |")
    L += ["", "## 판정 시각별 체결 시각 (trades.csv 진입·청산 일시, 기본 실행)", "",
          "| 창 | 시각별 건수 |", "|---|---|"]
    L += [f"| {w['k']} | {', '.join(f'{t} × {n}' for t, n in w['eval_stamps'].items()) or '—'} |"
          for w in o["windows"]]  # type: ignore[union-attr]
    rt = o["runtime_seconds"]
    L += ["", "## 실행 수·시간", "",
          f"- 실행 수 {o['runs']} = {o['runs_by_kind']}",
          f"- 시간(초): 전략 실행 {rt['strategy_runs']}, 무작위 {rt['random_runs']}, 전체 {rt['total']} "  # type: ignore[index]
          f"(병렬 {rt['workers']})",  # type: ignore[index]
          "", "## 한계", "",
          "- 창 끝 미청산 보유는 거래 수·기대값에 넣지 않음(D-078). 창별 미청산 수를 표에 적음 [제안].",
          "- 늦게 상장한 종목은 워밍업 부족 동안 신호 없음, 단순 보유에서는 첫날 봉이 없으면 제외.",
          "- 백테스트 체결 가정(D-072·DP-7)과 한계는 백테스트 보고서(§2.8)와 같음. 룩어헤드 교란 검사(V-91)는 이 실행기에서 "
          "다시 하지 않음(백테스트 통과 단계 항목) — 창 끝 이후 일봉은 실행 전에 잘라냄.",
          ""]
    return "\n".join(L) + "\n"
