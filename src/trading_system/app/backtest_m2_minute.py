"""M2 1분봉 백테스트·검증 실행기 (CP-037 D-221·D-222, T-053, 승인 A-113 Q-5). BACKTEST 전용 — 주문 경로·PAPER 코드 무변경.

M2 일봉 실행기(app/backtest_m2 — 하루 여러 판정 일봉 근사)는 그대로 두고 1분봉용을 따로 둔다. 같은 전략·판정 실행기·게이트·
실행기·정지 관리·체결 모델(SpotBacktestBroker)을 쓰고, 판정 일정·입력·체결 기준만 1분봉이다.

- 데이터 (A-110 공개 실서버 1m CSV, 읽기만): 열 `open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trades,
  taker_buy_base,taker_buy_quote`. **스트리밍**(iter_binance_minute_csv) — 4.8M 행을 메모리에 올리지 않는다. 실행 시작 −
  lookback분 전 행은 시각 문자열 비교만으로 건너뛰고(해석 안 함), 끝(또는 최종 확인 구간 시작) 이후 행은 읽지 않는다.
  형식·품질(분 정렬·봉 끝 = 시작 + 59.999초·오름차순·중복 없음·가격 > 0·고저 관계·거래량 ≥ 0) 위반은 실행 거부(파일·줄만).
  결측 분은 채우지 않는다 — 판정 창 안이면 그 판정 분봉 입력 무효, 체결 봉이면 미체결(MISSING_BAR).
- 판정 일정 = 실운영과 같은 주기 격자(PeriodGrid — UTC 00:00 + k×N분 + 확정 여유, 주문 창 00:10~23:50, N = 전략 파라미터
  period_minutes, D-219). 판정 ID = 전략·버전·M2·UTC 판정 시각(실운영과 같은 식).
- 판정 입력 (D-220과 같은 규칙): 판정 직전 닫힌 봉(시작 + 60초 ≤ 판정 시각 − 여유)부터 lookback개, 모두 있어야 유효
  (minute_ok). CAP-DAT-02 선언 전략만 minute_bars를 받는다. 최근가 = 마지막 닫힌 봉 종가, 시세 시각 = 그 봉 끝(결측이 길면
  늙은 시세 → 게이트 G11·지연 매도 규칙). 1d 봉은 넣지 않는다(daily_bars ()) [제안 — 분봉 전략 입력만].
- 체결 (D-221): 판정 시각 T가 속한 분에 시작하는 봉(= 격자 시각 봉)의 **시가** 기준. 매수 IOC 지정가 = 최근가 × (1 + 완충)
  tick 내림(시스템 규칙 apply_spot_terms, A-064), 체결가 = 시가 × (1 + 슬리피지) tick 올림이 지정가 이하일 때만. 매도 시장가 =
  시가 × (1 − 슬리피지) tick 내림. 봉 거래량 10% 상한(04 §2.2), 수수료 받는 자산에서 차감(BN §9), 수량 LOT_SIZE.
  그 봉이 결측이면 미체결(MISSING_BAR 기록). 판정은 주문 창 안에서만 나므로 창 밖 체결 없음.
- 최종 확인 구간 보호 (CP-030 D-163): 실행 끝 날짜가 final_from(2026-01-01) 이후면 거부, 그 이후 행은 읽지도 않는다. M2에는
  최종 확인 실행이 없다.
- 일일: UTC 일 경계 첫 판정에서 기준 금액·K2 다음 날 재개(M1 DP-6과 같음), 일일 손실 L4는 판정마다 검사. 평가액은 날마다
  마지막 닫힌 봉 종가. 연환산 365일.
- 재현성(D-074): 지문 = 신호·체결·평가액 해시. 룩어헤드 교란 검사(V-91): perturbation_check_minute — 판정 시각에 닫히지
  않은 봉(체결 봉·이후 봉)을 바꾸거나 지워도 그 판정 신호가 같아야 한다(실행기와 같은 decision_points 사용).
- 검증 (D-222, A-113 Q-5): 학습 3년·검증 1년·1년 이동, 검증 2020~2025 6창. 데이터 시작(2017-08-17) 전 학습 구간은
  "학습 단축" 표시, 창별 결측 봉 수, C-7(BTCUSDT 단일 심볼 사후 선택) "과대 추정 가능" 표시. 공개 실서버 성과는 테스트넷
  PAPER 성과와 직접 비교하지 않는다(2-M2.6).
"""

from __future__ import annotations

import hashlib
import json
import time as _time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.app.backtest import BacktestError, BacktestResult
from trading_system.app.backtest_m2 import (
    ACCOUNT, _Account, _ClockOk, _Limits, _Market, _round_trips, _Rules, _Strategies, compute_metrics_m2, sellable,
)
from trading_system.app.validation import Window, check_final_guard, param_variants, walkforward_windows
from trading_system.broker.fake.spot_fill_model import SpotBacktestBroker
from trading_system.config.capabilities import CapabilityTable
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import FixedClock
from trading_system.domain.enums import GateResult, HaltType, IntentState, Mode, Side
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import CashView, CostRates, DailyRisk, Quote
from trading_system.domain.market_time import M2_TIME
from trading_system.domain.minute_series import DEFAULT_CONFIRM_MARGIN, ONE_MINUTE, PeriodGrid
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

MINUTE_COLUMNS = ("open_time_utc", "open", "high", "low", "close", "volume", "close_time_utc", "quote_volume", "trades",
                  "taker_buy_base", "taker_buy_quote")
FINAL_FROM_DEFAULT = date(2026, 1, 1)            # CP-030 D-163 (config/validation.toml final_from)
MINUTE_CAPABILITY = "CAP-DAT-02"
PERIOD_PARAM = "period_minutes"
SLIPPAGE_BASE = Decimal("0.001")                 # CP-030 D-164 (2배 = 0.2%)
MIN_TRADES = 100                                 # CP-030 D-167 C-2
FIXED_PARAMS = frozenset({PERIOD_PARAM, "qty_step"})   # ±20% 변형 제외 — 일정 격자(1440 약수)·거래소 단위는 구조값 [제안]
_TS_LEN = len("2017-08-17T04:00:00.000Z")
_CLOSE_OFFSET = timedelta(seconds=59, milliseconds=999)
_ZERO = Decimal("0")


# --- 데이터 (스트리밍) --------------------------------------------------------------------------------------------

@dataclass
class MinuteReadStats:
    skipped_before: int = 0      # 시작 전이라 해석하지 않고 건너뛴 행
    used: int = 0                # 해석해 넘긴 행
    stopped_at: str | None = None   # 읽기를 멈춘 첫 행의 시각 문자열(끝·최종 확인 구간) — 그 행은 해석하지 않음


def _ts_key(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def iter_binance_minute_csv(path: Path, start: datetime, stop: datetime, *, final_from: date = FINAL_FROM_DEFAULT,
                            stats: MinuteReadStats | None = None) -> Iterator[Bar]:
    """A-110 공개 1m CSV → 1분봉(Bar.at = 봉 시작 UTC). start ≤ 봉 시작 < min(stop, final_from 00:00 UTC)만 해석해 넘긴다.
    파일은 읽기만 한다. 형식·품질 위반은 ConfigError(파일 이름·줄 번호, 값 없음)."""
    limit = min(stop, datetime(final_from.year, final_from.month, final_from.day, tzinfo=UTC))
    start_key, stop_key = _ts_key(start), _ts_key(limit)
    st = stats if stats is not None else MinuteReadStats()
    name = path.name
    prev: datetime | None = None
    with path.open(encoding="utf-8-sig", newline="") as f:
        header = f.readline().strip()
        if tuple(header.split(",")) != MINUTE_COLUMNS:
            raise ConfigError(f"{name}: 열이 A-110 1m 형식이 아님 (필요: {','.join(MINUTE_COLUMNS)})")
        for i, line in enumerate(f, start=2):
            key = line[:_TS_LEN]
            if len(key) != _TS_LEN or line[_TS_LEN:_TS_LEN + 1] != ",":
                raise ConfigError(f"{name}:{i}: open_time_utc 형식 오류 (YYYY-MM-DDTHH:MM:SS.000Z)")
            if key >= stop_key:
                st.stopped_at = key
                return
            if key < start_key:
                st.skipped_before += 1
                continue
            cols = line.rstrip("\r\n").split(",")
            if len(cols) != len(MINUTE_COLUMNS):
                raise ConfigError(f"{name}:{i}: 열 수가 {len(MINUTE_COLUMNS)}개가 아님")
            try:
                opened = datetime.fromisoformat(cols[0])
                closed = datetime.fromisoformat(cols[6])
                o, h, lo, c, v = (Decimal(x) for x in cols[1:6])
            except (ValueError, InvalidOperation):
                raise ConfigError(f"{name}:{i}: 시각·숫자 형식 오류") from None
            if opened.utcoffset() != timedelta(0) or opened.second or opened.microsecond:
                raise ConfigError(f"{name}:{i}: 봉 시작이 UTC 분 경계가 아님 (1m 봉 아님)")
            if closed != opened + _CLOSE_OFFSET:
                raise ConfigError(f"{name}:{i}: 봉 끝이 시작 + 59.999초가 아님 (1m 봉 아님 또는 불완전 봉)")
            if prev is not None and opened <= prev:
                raise ConfigError(f"{name}:{i}: 봉 시작이 오름차순이 아님(중복·역순)")
            if not all(x.is_finite() for x in (o, h, lo, c, v)) or min(o, h, lo, c) <= 0 or v < 0 \
                    or not (lo <= min(o, c) and max(o, c) <= h):
                raise ConfigError(f"{name}:{i}: 1분봉 품질 실패 (0 이하 가격·고저 모순·음수 거래량)")
            prev = opened
            st.used += 1
            yield Bar(opened.astimezone(UTC), o, h, lo, c, v)


def first_minute_start(path: Path) -> date:
    """파일 첫 데이터 행의 봉 시작 UTC 날짜(검증 창 '학습 단축' 표시용). 읽기만."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        f.readline()
        line = f.readline()
    try:
        return datetime.fromisoformat(line.split(",")[0]).astimezone(UTC).date()
    except ValueError:
        raise ConfigError(f"{path.name}:2: open_time_utc 형식 오류") from None


# --- 판정 시점 (실행기·교란 검사 공용) -------------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionPoint:
    at: datetime                 # 판정 시각 = 격자 시각 + 확정 여유
    boundary: datetime           # 격자 시각 = 체결 봉의 시작
    window: tuple[Bar, ...]      # minute_ok일 때만 lookback개(오름차순), 아니면 ()
    minute_ok: bool
    last: Bar | None             # 판정 시각에 닫힌 가장 최근 봉 (최근가 = 종가)
    fill: Bar | None             # 체결 봉(격자 시각에 시작) — 결측이면 None
    prev_close: Bar | None = None  # 이 판정 날짜 UTC 00:00 전 마지막 봉 — 전날 평가액·일일 기준 금액(UTC 00:00 가격, A-124)


def decision_points(bars: Iterable[Bar], grid: PeriodGrid, days: Sequence[date], lookback: int,
                    counter: list[int] | None = None,
                    end_bar: list[Bar | None] | None = None) -> Iterator[DecisionPoint]:
    """판정 시각 순서로 (창·최근 봉·체결 봉). 창에는 판정 시각에 닫힌 봉(시작 ≤ 격자 시각 − 1분)만 들어간다 — 체결 봉과 그
    이후 봉은 판정 입력에 쓰이지 않는다(룩어헤드 차단). counter[0]에는 첫 판정 날짜 00:00 이후 들어온 봉 수를 센다.
    end_bar[0]에는 마지막 날 끝(24:00) 전 마지막 봉을 남긴다(마지막 날 평가액 — 판정 입력 아님)."""
    window: deque[Bar] = deque(maxlen=lookback)
    last: Bar | None = None
    before_day: Bar | None = None
    it = iter(bars)
    nxt = next(it, None)
    span = ONE_MINUTE * lookback
    since = datetime(days[0].year, days[0].month, days[0].day, tzinfo=UTC) if days else None
    for day in days:
        midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
        for at in grid.day_times(day):
            b = at - grid.margin
            while nxt is not None and nxt.at < b:   # type: ignore[operator]
                window.append(nxt)
                last = nxt
                if nxt.at < midnight:   # type: ignore[operator]
                    before_day = nxt
                if counter is not None and since is not None and nxt.at >= since:  # type: ignore[operator]
                    counter[0] += 1
                nxt = next(it, None)
            ok = len(window) == lookback and window[-1].at == b - ONE_MINUTE and window[0].at == b - span
            fill = nxt if nxt is not None and nxt.at == b else None
            yield DecisionPoint(at, b, tuple(window) if ok else (), ok, last, fill, before_day)
    if days and (counter is not None or end_bar is not None):   # 마지막 판정 뒤 같은 날 봉: 결측 집계·마지막 날 평가액
        end = datetime(days[-1].year, days[-1].month, days[-1].day, tzinfo=UTC) + timedelta(days=1)
        while nxt is not None and nxt.at < end:   # type: ignore[operator]
            if counter is not None and since is not None and nxt.at >= since:   # type: ignore[operator]
                counter[0] += 1
            last = nxt
            nxt = next(it, None)
    if end_bar is not None:
        end_bar[0] = last


# --- 설정·결과 ---------------------------------------------------------------------------------------------------

MinuteSource = Callable[[datetime, datetime], Iterable[Bar]]   # (시작, 끝) → 봉 (시각 오름차순)


@dataclass(frozen=True)
class MinuteBacktestConfig:
    run_id: str
    strategy: Strategy
    params: dict[str, object]
    account_limits: AccountLimits
    rules: dict[str, SymbolRules]
    pairs: dict[str, tuple[str, str]]
    cost_rates: CostRates
    symbol: str
    source: MinuteSource
    start: date
    end: date
    initial_cash: Decimal
    out_dir: Path
    slippage: Decimal                       # 필수 — CP-030 D-164 기본 0.1%, 민감도 0.2%
    lookback_bars: int                      # 판정 창 길이 = 설정 [minute] lookback_bars와 같은 의미
    margin: timedelta = DEFAULT_CONFIRM_MARGIN
    buy_buffer: Decimal = Decimal("0.005")
    sell_buffer: Decimal = Decimal("0.005")
    quote_max_age: timedelta = timedelta(seconds=60)
    volume_cap: Decimal = Decimal("0.10")
    final_from: date = FINAL_FROM_DEFAULT
    caps: CapabilityTable | None = None
    data_version: str = ""
    data_source: str = ""
    code_commit: str = ""


@dataclass
class MinuteBacktestResult:
    result: BacktestResult
    decisions: int = 0
    minute_invalid: int = 0         # 분봉 입력 무효 판정 수 (CAP-DAT-02 전략)
    missing_bars: int = 0           # 실행 기간 안 결측 분 수
    bars_in_period: int = 0
    missing_fill: int = 0           # 체결 봉 결측으로 미체결 처리한 주문 수
    first_open: Decimal | None = None
    last_close: Decimal | None = None
    seconds: float = 0.0
    extras: dict = field(default_factory=dict)


def grid_of(cfg: MinuteBacktestConfig) -> PeriodGrid:
    n = cfg.params.get(PERIOD_PARAM)
    if "CAP-TRG-02" not in cfg.strategy.spec.capabilities or isinstance(n, bool) or not isinstance(n, int):
        raise BacktestError("1분봉 백테스트는 CAP-TRG-02 주기 판정 전략만 (정수 파라미터 period_minutes, D-219)")
    try:
        return PeriodGrid(n, cfg.margin)
    except ValueError as exc:
        raise BacktestError(f"period_minutes {n}: {exc}") from None


def _days(cfg: MinuteBacktestConfig) -> list[date]:
    if not isinstance(cfg.slippage, Decimal) or not cfg.slippage.is_finite() or not _ZERO <= cfg.slippage < 1:
        raise BacktestError("슬리피지는 0 이상 1 미만의 Decimal (CP-030 D-164 기본 0.001)")
    if cfg.symbol not in cfg.rules or cfg.symbol not in cfg.pairs:
        raise BacktestError(f"규칙·자산 쌍 없는 심볼: {cfg.symbol}")
    if not 1 <= cfg.lookback_bars <= 997:
        raise BacktestError("lookback_bars는 1~997 (설정 [minute]와 같은 범위)")
    if cfg.end < cfg.start:
        raise BacktestError("구간에 날짜가 없음")
    if cfg.end >= cfg.final_from:
        raise BacktestError(f"실행 날짜가 최종 확인 구간({cfg.final_from} 이후)과 겹침 — M2 최종 확인 실행 없음 (D-163)")
    return [cfg.start + timedelta(days=k) for k in range((cfg.end - cfg.start).days + 1)]


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


# --- 실행기 ------------------------------------------------------------------------------------------------------

def run_minute_backtest(cfg: MinuteBacktestConfig) -> MinuteBacktestResult:
    t0 = _time.perf_counter()
    grid = grid_of(cfg)
    days = _days(cfg)
    spec = cfg.strategy.spec
    sym = cfg.symbol
    rules = cfg.rules[sym]
    minute_declared = MINUTE_CAPABILITY in spec.capabilities
    run_dir = cfg.out_dir / cfg.run_id
    if (run_dir / "trading.db").exists():
        raise BacktestError(f"같은 실행 ID의 DB가 이미 있음: {run_dir}")
    repo = Repository(run_dir / "trading.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    repo.register_account(Account(ACCOUNT, Mode.BACKTEST, "fake"))
    clock = FixedClock(_midnight(days[0]))
    market = _Market(cfg)  # type: ignore[arg-type]
    acct = _Account()
    policy = GatePolicy(cfg.quote_max_age, Decimal("0.05"), cfg.cost_rates, market_time=M2_TIME)
    order_policy = SpotOrderPolicy(cfg.buy_buffer, cfg.sell_buffer, cfg.quote_max_age)
    broker = SpotBacktestBroker(pairs=cfg.pairs, fee_rate=cfg.cost_rates.commission_rate, slippage=cfg.slippage,
                                tick_of=lambda i: cfg.rules[i].tick_size, step_of=lambda i: cfg.rules[i].lot_step,
                                volume_cap=cfg.volume_cap)
    executor = Executor(repo, broker, clock, run_dir / "KILL", asset_pair=cfg.pairs.get, client_ids=True)
    registry: StrategyRegistry | None = None
    if cfg.caps is not None:
        registry = StrategyRegistry(repo, clock, None, market="M2")
        reg = registry.register(spec, dict(cfg.params), cfg.caps, cfg.account_limits, Mode.BACKTEST)
        if not reg.ok:
            repo.close()
            raise BacktestError(f"전략 등록 거부: {'; '.join(reg.problems)}")
    gate = Gate(repo, clock, GateInputs(_Limits(cfg.account_limits), market, acct,
                                        registry if registry is not None else _Strategies(), _ClockOk(),
                                        spot_rules=_Rules(cfg, market)), policy)  # type: ignore[arg-type]
    halts = HaltManager(repo, gate, executor, clock, reference_time_of=lambda a: cfg.account_limits.daily_reference_time,
                        market_time=M2_TIME)
    if registry is not None:
        registry.attach_halts(halts)
    lots = {sym: rules.lot_step}

    out = MinuteBacktestResult(result=None)  # type: ignore[arg-type]
    cash, costs = cfg.initial_cash, _ZERO
    equity: list[tuple[date, Decimal]] = []
    rejects: Counter = Counter()
    signals: list[str] = []
    fills_log: list[tuple[date, str, str, str, str, str]] = []
    by_eval: dict[str, list[tuple]] = {}
    seen = k2_resumes = 0
    cur_day: date | None = None
    k2_day: date | None = None
    base = _ZERO
    last_close: Decimal | None = None
    dirty = True
    held = avg = _ZERO
    pv: PositionView | None = None
    pending = False
    reserved = _ZERO

    def absorb(day: date) -> None:
        nonlocal seen, cash, costs
        for f in broker.fills_for(ACCOUNT)[seen:]:
            seen += 1
            if f.side == "BUY":
                cash -= f.quote_qty
                costs += f.commission * f.price if f.commission_asset != cfg.pairs[f.instrument][1] else f.commission
            else:
                cash += f.quote_qty - f.commission
                costs += f.commission
            fills_log.append((day, f.order_no, f.instrument, f.side, str(f.quantity), str(f.price)))

    warm = _midnight(days[0]) - ONE_MINUTE * cfg.lookback_bars
    stop = _midnight(days[-1]) + timedelta(days=1)
    counter = [0]
    end_bar: list[Bar | None] = [None]
    for dp in decision_points(cfg.source(warm, stop), grid, days, cfg.lookback_bars, counter, end_bar):
        day = dp.at.date()
        clock._now = dp.at  # noqa: SLF001
        prev_last = last_close
        if dp.last is not None:
            last_close = dp.last.close
        if dirty:
            pos = repo.position(ACCOUNT, sym)
            held, avg = pos.quantity, pos.avg_price
            own = repo.active_ownership(ACCOUNT, sym)
            q = sellable(held, rules)
            pv = PositionView(q, avg, 0, own is not None and own.owner == spec.owner) if q > 0 else None
            pending = bool(repo.active_new_intents(ACCOUNT, sym, Side.BUY.value)
                           or repo.active_new_intents(ACCOUNT, sym, Side.SELL.value))
            reserved = sum((r.cash_remaining for r in repo.active_reservations(ACCOUNT)), _ZERO)
            dirty = False
        mark = held * (last_close if last_close is not None else avg)
        if day != cur_day:   # UTC 일 경계 첫 판정: 전날 평가액·K2 다음 날 재개·기준 금액 — 모두 UTC 00:00 전 마지막 봉 종가
            px0 = dp.prev_close.close if dp.prev_close is not None else prev_last
            mark0 = held * (px0 if px0 is not None else avg)   # 판정 사이에는 체결이 없어 보유·현금은 전날 끝과 같다
            if cur_day is not None:
                equity.append((cur_day, cash + mark0))
            cur_day = day
            if HaltType.K2.value in repo.active_halts(ACCOUNT):
                if halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "backtest").ok:
                    k2_resumes += 1
            base = cash + mark0
            if base <= 0:
                repo.close()
                raise BacktestError(f"{day} 기준 금액 0 이하")
        quote = None if dp.last is None else Quote(sym, dp.last.close, min(dp.last.at + ONE_MINUTE, dp.at))  # type: ignore[operator]
        market.quotes = {sym: quote} if quote is not None else {}
        acct.daily = DailyRisk(base, _ZERO, cash + mark - base, _ZERO)
        acct.cash_view = CashView(cash, cash - reserved)
        if k2_day != day and halts.check_daily_loss(ACCOUNT, acct.daily,
                                                     cfg.account_limits.l4_daily_loss_ratio) is not None:
            k2_day, dirty = day, True   # 같은 날 다시 발동해도 같은 정지(중복 없음) — 그날은 더 검사하지 않는다
        if out.first_open is None and dp.fill is not None:
            out.first_open = dp.fill.open
        minute_ok = dp.minute_ok if minute_declared else True
        out.decisions += 1
        out.minute_invalid += 0 if minute_ok else 1
        view = InstrumentView(sym, minute_bars=dp.window if minute_declared else (),
                              last_price=quote.price if quote is not None else None, position=pv,
                              has_pending_order=pending, data_ok=quote is not None, minute_ok=minute_ok)
        ev_id = evaluation_id_of(spec.strategy_id, spec.version, "M2", dp.at.isoformat())
        snap = EvaluationSnapshot(ev_id, "M2", ACCOUNT, dp.at, base, (view,), next_evaluation_at=grid.next_after(dp.at))
        res = run_evaluation(cfg.strategy, snap, cfg.params, lots)
        if not res.signals:
            continue
        by_eval[dp.at.isoformat()] = [(s.signal_id, s.kind.value, s.instrument, str(s.quantity), str(s.price))
                                      for s in res.signals]
        submitted = 0
        for s in res.signals:
            signals.append(s.signal_id)
            req = apply_spot_terms(GateRequest.from_signal(s), quote, dp.at, rules, order_policy)
            if req is None:
                rejects["TERMS"] += 1
                continue
            decision = gate.decide(req)
            if decision.result is GateResult.APPROVED and decision.approved is not None:
                executor.submit(decision.approved)
                submitted += 1
            else:
                rejects[str(decision.failed_check)] += 1
        if not submitted:
            dirty = True
            continue
        executor.process_events()
        if dp.fill is not None:   # D-221: 판정 시각이 속한 분의 봉 시가 + 불리 슬리피지
            broker.match(sym, dp.fill.open, dp.fill.volume, dp.at)
        else:
            rejects["MISSING_BAR"] += submitted
            out.missing_fill += submitted
        broker.end_of_day()   # IOC·시장가 미체결분 소멸, 봉 거래량 사용량 초기화(상한은 봉마다)
        executor.process_events()
        absorb(day)
        dirty = True
    if dirty:
        held = repo.position(ACCOUNT, sym).quantity
    if cur_day is not None:   # 마지막 날 평가액 = 그날 마지막 봉 종가(마지막 판정 뒤 봉 포함)
        px_end = end_bar[0].close if end_bar[0] is not None else last_close
        equity.append((cur_day, cash + held * (px_end if px_end is not None else _ZERO)))
    unknown = repo.intents_in_state(IntentState.UNKNOWN)
    repo.close()
    if unknown:
        raise BacktestError("체결 모델에서 결과 불명이 생김 — 엔진 오류")
    trades = _round_trips(broker.fills_for(ACCOUNT), cfg)  # type: ignore[arg-type]
    metrics = compute_metrics_m2(cfg.initial_cash, equity, trades, costs, len(days))
    digest = hashlib.sha256(json.dumps({"signals": signals, "fills": fills_log,
                                        "equity": [(d.isoformat(), str(e)) for d, e in equity]},
                                       sort_keys=True, default=str).encode()).hexdigest()
    out.result = BacktestResult(cfg.run_id, "ALL", days, equity, trades, metrics, rejects, digest, run_dir / "trading.db",
                                signals, fills_log, k2_resumes, costs, {}, by_eval)
    out.bars_in_period = counter[0]
    out.missing_bars = len(days) * 1440 - counter[0]
    out.last_close = last_close
    out.seconds = round(_time.perf_counter() - t0, 2)
    return out


# --- 룩어헤드 교란 검사 (V-91 분봉판) ------------------------------------------------------------------------------

def _flat_signals(cfg: MinuteBacktestConfig, grid: PeriodGrid, dp: DecisionPoint) -> list[tuple]:
    spec = cfg.strategy.spec
    declared = MINUTE_CAPABILITY in spec.capabilities
    price = dp.last.close if dp.last is not None else None
    view = InstrumentView(cfg.symbol, minute_bars=dp.window if declared else (), last_price=price,
                          data_ok=price is not None, minute_ok=dp.minute_ok if declared else True)
    ev = evaluation_id_of(spec.strategy_id, spec.version, "M2", dp.at.isoformat())
    snap = EvaluationSnapshot(ev, "M2", ACCOUNT, dp.at, cfg.initial_cash, (view,), next_evaluation_at=grid.next_after(dp.at))
    r = run_evaluation(cfg.strategy, snap, cfg.params, {cfg.symbol: cfg.rules[cfg.symbol].lot_step})
    return [(s.signal_id, s.kind.value, s.instrument, str(s.quantity), str(s.price)) for s in r.signals]


def perturbation_check_minute(cfg: MinuteBacktestConfig, check_days: Sequence[date], probes: int = 4) -> list[str]:
    """V-91: 판정 시각 T에 닫히지 않은 봉(체결 봉·이후 봉 — 시작 ≥ 격자 시각)을 크게 바꾸거나 지워도 T의 신호가 같아야 한다.
    무보유 스냅샷 비교. 날마다 판정 시각 probes개(고르게)를 본다. 다르면 그 판정 시각을 돌려준다."""
    grid = grid_of(cfg)
    bad: list[str] = []
    for day in check_days:
        bars = list(cfg.source(_midnight(day) - ONE_MINUTE * cfg.lookback_bars, _midnight(day) + timedelta(days=1)))
        points = list(decision_points(bars, grid, [day], cfg.lookback_bars))
        if not points:
            continue
        step = max(1, len(points) // probes)
        for dp in points[::step][:probes]:
            base = _flat_signals(cfg, grid, dp)

            def scaled(b: Bar) -> Bar:
                return Bar(b.at, b.open * 3, b.high * 3, b.low * 3, b.close * 3, b.volume * 3)
            variants = ([scaled(b) if b.at >= dp.boundary else b for b in bars],    # type: ignore[operator]
                        [b for b in bars if b.at < dp.boundary])                    # type: ignore[operator]
            for var in variants:
                again = next(p for p in decision_points(var, grid, [day], cfg.lookback_bars) if p.at == dp.at)
                if _flat_signals(cfg, grid, again) != base:
                    bad.append(dp.at.isoformat())
                    break
    return bad


# --- 보고서 ------------------------------------------------------------------------------------------------------

def buy_hold_return(first_open: Decimal | None, last_close: Decimal | None, slippage: Decimal, fee: Decimal) -> str | None:
    """참고(C-5): 첫 체결 봉 시가 × (1 + s) 매수, 마지막 종가 × (1 − s) 매도, 수수료 양쪽(받는 자산)."""
    if first_open is None or last_close is None or first_open <= 0:
        return None
    r = (1 - fee) * last_close * (1 - slippage) * (1 - fee) / (first_open * (1 + slippage)) - 1
    return format(r.quantize(Decimal("0.000001")), "f")


def render_minute_report(cfg: MinuteBacktestConfig, out: MinuteBacktestResult, *, reproducible: bool | None,
                         lookahead_bad: list[str] | None) -> str:
    spec = cfg.strategy.spec
    r = out.result
    grid = grid_of(cfg)
    lines = [
        f"# M2 1분봉 백테스트 보고서 — {spec.strategy_id} v{spec.version}", "",
        f"- 실행 ID: `{cfg.run_id}` / 기간 {r.days[0]} ~ {r.days[-1]} ({len(r.days)}일, UTC) / 심볼 {cfg.symbol} / "
        f"데이터 버전 `{cfg.data_version or '미기재'}` / 코드 `{cfg.code_commit or '미기재'}`",
        f"- 판정(D-219·실운영과 같은 격자): {grid.period_minutes}분마다 UTC 00:00 + k×N분 + 여유 {cfg.margin.total_seconds():g}초, "
        f"주문 창 00:10~23:50 안 — 판정 {out.decisions}회, 분봉 창 {cfg.lookback_bars}봉"
        + (f", 분봉 입력 무효 판정 {out.minute_invalid}회" if MINUTE_CAPABILITY in spec.capabilities else ""),
        f"- 체결(D-221): 판정 시각이 속한 분의 봉 시가 기준. 매수 = 시가 × (1 + 슬리피지 {cfg.slippage}) tick 올림이 IOC 지정가"
        f"(최근가 = 직전 닫힌 봉 종가 × (1 + 완충 {cfg.buy_buffer}) tick 내림) 이하일 때만, 매도 = 시가 × (1 − 슬리피지) tick 내림, "
        f"봉 거래량 {cfg.volume_cap:%} 상한, 체결 봉 결측 → 미체결({out.missing_fill}건)",
        f"- 비용: 수수료율 {cfg.cost_rates.commission_rate}(받는 자산에서 차감, BN §9 — 실제 요율 미확인), 비용 합계 {r.costs} USDT",
        f"- 데이터: 실행 기간 봉 {out.bars_in_period}개, 결측 분 {out.missing_bars}개(채우지 않음 — 판정 창 안이면 무효)",
        f"- 단순 보유 참고(C-5): {buy_hold_return(out.first_open, out.last_close, cfg.slippage, cfg.cost_rates.commission_rate)}",
        f"- K2 다음 날 재개 가정: {r.k2_resumes}회 / 실행 시간 {out.seconds}초", "",
        "## 지표 (비용 반영 후, 연환산 365일)", "| 지표 | 값 |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in r.metrics.items()], "",
        "## 한계",
        f"- 데이터 출처: {cfg.data_source or '바이낸스 공개 실서버 1m klines(A-110)'} — 테스트넷 PAPER 성과와 직접 비교하지 않는다(2-M2.6).",
        "- 거래 규칙은 테스트넷 BTCUSDT 실측값(실서버 과거 시점 규칙 아님). 1d 봉 입력 없음(분봉 전략 입력만).",
        "- BTCUSDT 단일 심볼은 사후 선택 — 성과는 과대 추정 가능(C-7).", "",
        "## 판정 (04 §2.7 — 실행 기록)",
        f"- 재현성(D-074): {'통과' if reproducible else ('실패' if reproducible is False else '미실행')}",
        "- 룩어헤드 교란 검사(V-91 분봉판): " + ("통과" if lookahead_bad == [] else
                                         ("미실행" if lookahead_bad is None else f"실패 {lookahead_bad[:5]}")),
        f"- 결과 지문: `{r.digest}`",
    ]
    return "\n".join(lines) + "\n"


def summary_of(cfg: MinuteBacktestConfig, out: MinuteBacktestResult, *, reproducible: bool | None,
               lookahead_bad: list[str] | None) -> dict:
    r = out.result
    return {
        "run_id": cfg.run_id, "strategy": cfg.strategy.spec.strategy_id, "version": cfg.strategy.spec.version,
        "interval": "1m", "symbol": cfg.symbol, "period": [r.days[0].isoformat(), r.days[-1].isoformat()],
        "days": len(r.days), "period_minutes": grid_of(cfg).period_minutes, "lookback_bars": cfg.lookback_bars,
        "margin_s": cfg.margin.total_seconds(), "decisions": out.decisions, "minute_invalid": out.minute_invalid,
        "bars_in_period": out.bars_in_period, "missing_bars": out.missing_bars, "missing_fill": out.missing_fill,
        "data_version": cfg.data_version, "final_from": cfg.final_from.isoformat(),
        "fee_rate": str(cfg.cost_rates.commission_rate), "slippage": str(cfg.slippage),
        "initial_cash": str(cfg.initial_cash), "signals": len(r.signals),
        "fills": {side: sum(1 for f in r.fills if f[3] == side) for side in ("BUY", "SELL")},
        "round_trips": len(r.trades), "rejects": dict(sorted(r.rejects.items())), "k2_resumes": r.k2_resumes,
        "buy_hold_return": buy_hold_return(out.first_open, out.last_close, cfg.slippage, cfg.cost_rates.commission_rate),
        "reproducible": reproducible, "lookahead_bad": lookahead_bad, "digest": r.digest, "metrics": r.metrics,
        "seconds": out.seconds, **out.extras,
    }


# --- 검증 (D-222) ------------------------------------------------------------------------------------------------

def m2_windows() -> tuple[Window, ...]:
    """D-222 (A-113 Q-5 (가)): 학습 3년·검증 1년·1년 이동, 검증 2020~2025 6창."""
    return walkforward_windows(first_train_year=2017, train_years=3, n=6)


def _expectancy(trades: list) -> Decimal | None:  # type: ignore[type-arg]
    return sum((t.pnl for t in trades), _ZERO) / len(trades) if trades else None


def _fmt(x: Decimal | None) -> str | None:
    return None if x is None else format(x.quantize(Decimal("0.0001")), "f")


def decide_verdict_m2(*, pooled_trades: int, c3_expectancy: Decimal | None, c4: Sequence[Decimal | None],
                      variants_run: bool, reproducible: bool, lookahead_ok: bool) -> dict[str, object]:
    """CP-030 판정 규칙(D-167 C-2·D-164 C-3·D-165 C-4·D-168 C-7)의 M2 1분봉판. 금액 단위 USDT. BTCUSDT 단일 심볼 = 사후 선택
    → 통과여도 "통과(과대 추정 가능)". 판정표는 기록·제안이며 전환 승인은 사용자."""
    c2 = "충족" if pooled_trades >= MIN_TRADES else "판단 불가"
    c3 = "충족" if c3_expectancy is not None and c3_expectancy > 0 else "미충족"
    if not variants_run:
        c4s = "미실행"
    elif not c4:
        c4s = "판단 불가"
    else:
        c4s = "충족" if all(e is not None and e > 0 for e in c4) else "미충족"
    reasons: list[str] = []
    if not reproducible:
        reasons.append("재현성(D-074) 실패 창 있음 → 판단 불가")
    if not lookahead_ok:
        reasons.append("룩어헤드 교란 검사(V-91) 실패 → 판단 불가")
    if c2 != "충족":
        reasons.append(f"C-2 합산 거래 {pooled_trades}건 < {MIN_TRADES}")
    if c3 != "충족":
        reasons.append(f"C-3 슬리피지 2배 거래당 기대값 {_fmt(c3_expectancy)} USDT ≤ 0 또는 거래 없음")
    if c4s == "미충족":
        reasons.append("C-4 ±20% 변형 중 거래당 기대값 ≤ 0 또는 거래 없음이 있음")
    elif c4s in ("판단 불가", "미실행"):
        reasons.append(f"C-4 {c4s} (변형 가능한 수치 파라미터 없음 또는 --skip-variants)")
    if not reproducible or not lookahead_ok or c2 != "충족":
        verdict = "판단 불가"
    elif c3 != "충족" or c4s == "미충족":
        verdict = "미충족"
    elif c4s != "충족":
        verdict = "판단 불가"
    else:
        verdict = "통과(과대 추정 가능)"
    return {"verdict": verdict, "C-2": c2, "C-3": c3, "C-4": c4s, "C-7": "해당 — BTCUSDT 단일 심볼 사후 선택(과대 추정 가능)",
            "reasons": reasons}


@dataclass(frozen=True)
class MinuteValidationInputs:
    template: MinuteBacktestConfig      # run_id·start·end·slippage·params·out_dir는 창마다 바꾼다
    out_dir: Path
    data_start: date
    windows: tuple[Window, ...] = field(default_factory=m2_windows)
    slippage_base: Decimal = SLIPPAGE_BASE
    skip_variants: bool = False
    perturbation_days: int = 3          # 창마다 교란 검사 표본 날짜 수
    command: str = ""


def run_minute_validation(inp: MinuteValidationInputs) -> dict:
    """D-222 창 실행 → 판정표(dict). 창마다 기본 슬리피지·2배·재실행(재현성)·±20% 변형, 교란 검사 표본."""
    t = inp.template
    for w in inp.windows:
        check_final_guard(w.val_start, w.val_end, t.final_from, final_check=False)
    variants = [] if inp.skip_variants else [v for v in param_variants(t.strategy.spec, t.params)
                                             if v.param not in FIXED_PARAMS]
    runs = inp.out_dir / "runs"
    windows_out: list[dict] = []
    base_trades: list = []  # type: ignore[type-arg]
    slip2_trades: list = []  # type: ignore[type-arg]
    var_trades: dict[str, list] = {v.label: [] for v in variants}  # type: ignore[type-arg]
    all_repro = all_lookahead = True
    for w in inp.windows:
        def one(label: str, slip: Decimal, params: dict | None = None) -> MinuteBacktestResult:
            return run_minute_backtest(replace(t, run_id=f"w{w.k}-{label}", start=w.val_start, end=w.val_end,
                                               slippage=slip, out_dir=runs,
                                               params=dict(t.params if params is None else params)))
        base = one("base", inp.slippage_base)
        slip2 = one("slip2x", inp.slippage_base * 2)
        repro = one("repro", inp.slippage_base).result.digest == base.result.digest
        days = base.result.days
        sample = days[:: max(1, len(days) // max(1, inp.perturbation_days))][:inp.perturbation_days]
        bad = perturbation_check_minute(replace(t, start=w.val_start, end=w.val_end), sample)
        vrows = []
        for v in variants:
            vr = one(v.label, inp.slippage_base, v.params)
            var_trades[v.label] += vr.result.trades
            vrows.append({"label": v.label, "param": v.param, "value": str(v.value), "trades": len(vr.result.trades),
                          "expectancy": _fmt(_expectancy(vr.result.trades)), "note": v.note})
        all_repro &= repro
        all_lookahead &= bad == []
        base_trades += base.result.trades
        slip2_trades += slip2.result.trades
        windows_out.append({
            "k": w.k, "train": [w.train_start.isoformat(), w.train_end.isoformat()],
            "train_shortened": w.train_start < inp.data_start,
            "train_effective_start": max(w.train_start, inp.data_start).isoformat(),
            "validation": [w.val_start.isoformat(), w.val_end.isoformat()],
            "trades": len(base.result.trades), "expectancy": _fmt(_expectancy(base.result.trades)),
            "total_return": base.result.metrics["total_return"], "max_drawdown": base.result.metrics["max_drawdown"],
            "slip2x_trades": len(slip2.result.trades), "slip2x_expectancy": _fmt(_expectancy(slip2.result.trades)),
            "missing_bars": base.missing_bars, "minute_invalid": base.minute_invalid, "decisions": base.decisions,
            "buy_hold_return": buy_hold_return(base.first_open, base.last_close, inp.slippage_base,
                                               t.cost_rates.commission_rate),
            "reproducible": repro, "lookahead_bad": bad, "variants": vrows, "seconds": base.seconds,
            "digest": base.result.digest,
        })
    verdict = decide_verdict_m2(pooled_trades=len(base_trades), c3_expectancy=_expectancy(slip2_trades),
                                c4=[_expectancy(v) for v in var_trades.values()], variants_run=not inp.skip_variants,
                                reproducible=all_repro, lookahead_ok=all_lookahead)
    out = {
        "strategy": t.strategy.spec.strategy_id, "version": t.strategy.spec.version, "symbol": t.symbol,
        "data_version": t.data_version, "data_start": inp.data_start.isoformat(), "final_from": t.final_from.isoformat(),
        "slippage_base": str(inp.slippage_base), "slippage_double": str(inp.slippage_base * 2),
        "period_minutes": grid_of(t).period_minutes, "lookback_bars": t.lookback_bars,
        "windows": windows_out, "pooled": {"trades": len(base_trades), "expectancy": _fmt(_expectancy(base_trades)),
                                           "slip2x_trades": len(slip2_trades),
                                           "slip2x_expectancy": _fmt(_expectancy(slip2_trades)),
                                           "missing_bars": sum(w["missing_bars"] for w in windows_out),
                                           "loss_windows": sum(1 for w in windows_out
                                                               if Decimal(w["total_return"]) < 0)},
        "variants": [{"label": k, "trades": len(v), "expectancy": _fmt(_expectancy(v))} for k, v in var_trades.items()],
        "verdict": verdict, "command": inp.command,
    }
    inp.out_dir.mkdir(parents=True, exist_ok=True)
    (inp.out_dir / "validation.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                                newline="\n")
    (inp.out_dir / "validation.md").write_text(render_minute_validation(out), encoding="utf-8", newline="\n")
    return out


def render_minute_validation(o: dict) -> str:
    v = o["verdict"]
    lines = [f"# M2 1분봉 검증 판정표 — {o['strategy']} v{o['version']} ({o['symbol']})", "",
             f"- 창(D-222, A-113 Q-5): 학습 3년·검증 1년·1년 이동, 검증 2020~2025 — 데이터 시작 {o['data_start']}, "
             f"최종 확인 구간 {o['final_from']} 이후 미사용(D-163)",
             f"- 판정 격자 {o['period_minutes']}분, 분봉 창 {o['lookback_bars']}봉, 슬리피지 {o['slippage_base']}"
             f" / 민감도 {o['slippage_double']}", "",
             "| 창 | 학습 | 검증 | 거래 | 기대값(USDT) | 수익률 | MDD | 2배 슬리피지 기대값 | 결측 분 | 재현 | 교란 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for w in o["windows"]:
        tr = f"{w['train'][0]}~{w['train'][1]}" + (f" (학습 단축: {w['train_effective_start']}부터)"
                                                    if w["train_shortened"] else "")
        lines.append(f"| {w['k']} | {tr} | {w['validation'][0]}~{w['validation'][1]} | {w['trades']} | "
                     f"{w['expectancy'] or '—'} | {w['total_return']} | {w['max_drawdown']} | "
                     f"{w['slip2x_expectancy'] or '—'} | {w['missing_bars']} | {'통과' if w['reproducible'] else '실패'} | "
                     f"{'통과' if not w['lookahead_bad'] else '실패'} |")
    p = o["pooled"]
    lines += ["", "## 판정 (CP-030 M2판 — 기록·제안, 전환 승인은 사용자)", "",
              f"- C-1 비용: 수수료·슬리피지 반영 결과만 — 적용",
              f"- C-2 거래 수 ≥ {MIN_TRADES}: 합산 {p['trades']}건 — {v['C-2']}",
              f"- C-3 슬리피지 2배 거래당 기대값 > 0: {p['slip2x_expectancy'] or '—'} USDT ({p['slip2x_trades']}건) — {v['C-3']}",
              f"- C-4 파라미터 ±20%(period_minutes·qty_step 제외): "
              + (", ".join(f"{x['label']} {x['expectancy'] or '—'}" for x in o["variants"]) or "변형 없음")
              + f" — {v['C-4']}",
              "- C-5 단순 보유: 창별 참고 값(validation.json buy_hold_return) — 참고",
              "- C-6 무작위 진입: 삭제(A-083) — 미실행",
              f"- C-7 생존편향: {v['C-7']}",
              "- C-8 최종 확인: 이 실행에서 미사용 — 해당 없음",
              f"- 창별 결측 분 합계 {p['missing_bars']}, 손실 창 {p['loss_windows']}/{len(o['windows'])}",
              f"- **판정: {v['verdict']}**", *[f"- 사유: {x}" for x in v["reasons"]],
              "- 공개 실서버 성과는 테스트넷 PAPER 성과와 직접 비교하지 않는다(2-M2.6). PAPER 신호 일치율은 PAPER 중 저장한 "
              "테스트넷 봉으로 다시 계산(D-222)."]
    return "\n".join(lines) + "\n"


__all__ = ["MINUTE_COLUMNS", "MinuteBacktestConfig", "MinuteBacktestResult", "MinuteReadStats", "MinuteValidationInputs",
           "decision_points", "first_minute_start", "iter_binance_minute_csv", "m2_windows", "perturbation_check_minute",
           "render_minute_report", "run_minute_backtest", "run_minute_validation", "summary_of"]
