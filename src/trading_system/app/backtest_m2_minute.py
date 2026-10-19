"""M2 1분봉 백테스트·검증 실행기 (CP-037 D-221·D-222, T-053, 승인 A-113 Q-5 / CP-040 T-068·CP-041 D-275~D-277, A-115·A-122).
BACKTEST 전용 — 주문 경로·PAPER 코드 무변경.

M2 일봉 실행기(app/backtest_m2 — 하루 여러 판정 일봉 근사)는 그대로 두고 1분봉용을 따로 둔다. 같은 전략·판정 실행기·게이트·
실행기·정지 관리·체결 모델(SpotBacktestBroker)을 쓰고, 판정 일정·입력·체결 기준만 1분봉이다.

- 데이터 (A-110 공개 실서버 1m CSV, 읽기만): 열 `open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trades,
  taker_buy_base,taker_buy_quote`. **스트리밍**(iter_binance_minute_csv) — 4.8M 행을 메모리에 올리지 않는다. 실행 시작 −
  창 길이 전 행은 시각 문자열 비교만으로 건너뛰고(해석 안 함), 끝(또는 최종 확인 구간 시작) 이후 행은 읽지 않는다.
  형식·품질(분 정렬·봉 끝 = 시작 + 59.999초·오름차순·중복 없음·가격 > 0·고저 관계·거래량 ≥ 0) 위반은 실행 거부(파일·줄만).
  결측 분은 채우지 않는다 — 판정 창 안이면 그 판정 분봉 입력 무효, 체결 봉이면 미체결(MISSING_BAR).
- 판정 일정 = 실운영과 같은 주기 격자(PeriodGrid — UTC 00:00 + k×N분 + 확정 여유, 주문 창 00:10~23:50, N = 전략 파라미터
  period_minutes, D-219). 판정 ID = 전략·버전·M2·UTC 판정 시각(실운영과 같은 식).
- 판정 입력 (D-220과 같은 규칙): 판정 직전 닫힌 봉(시작 + 60초 ≤ 판정 시각 − 여유)부터 lookback개, 모두 있어야 유효
  (minute_ok). CAP-DAT-02 선언 전략만 minute_bars를 받는다. 최근가 = 마지막 닫힌 봉 종가, 시세 시각 = 그 봉 끝(결측이 길면
  늙은 시세 → 게이트 G11·지연 매도 규칙). 60분 판정이면 HH:00 직전 분 종가 = 1시간 봉 종가와 같은 값(D-277 (1)).
  [T-068] 집계 봉 요청(agg_bar_minutes 등, minute_request_of)은 실운영과 같은 domain/minute_agg.check_agg_window로 계산하고,
  흐름 열 선언이면 확장 열 결측 = 분봉 입력 무효(D-259·D-260, DP-88 (3)).
- [CP-041 D-276] 확정 일봉: CAP-DAT-01 선언 전략만, 심볼별 1d 파일 봉을 실시간과 같은 함수 domain/daily_input.m2_daily_input
  (확정 봉만, 최근 199개 = 실운영 1d 조회 limit 200 − 진행 중 봉)으로 넣는다(D-088). 결측일은 채우지 않고 DP-82 규칙
  (진입·증액만 막고 감액·청산은 빈 날을 건너뛴 봉)으로 판정한다. 1분봉이 완전한 날은 1d 파일과 1분봉 일 집계를 비교해
  일치 보고(판정에 쓰지 않음).
- [CP-041 D-275] 여러 심볼 한 계좌: 판정 시각마다 스냅샷 하나에 모든 심볼(이름 정렬). 신호 처리 순서 = 판정 실행기 출력
  순서(위험 축소 먼저, 그다음 종목코드 — 실운영 엔진과 같은 run_evaluation 결과 순서) → 게이트 순차(앞 신호 예약이 뒤 신호
  검사에 반영). 체결 = 심볼별 다음 분 시가, 한 심볼 봉 결측은 그 심볼만 미체결, 봉 거래량 10% 상한 심볼별. 실행 기간을
  모든 심볼 데이터가 덮어야 한다(첫날·마지막 날에 봉이 없는 심볼이 있으면 거부).
- 체결 (D-221): 판정 시각 T가 속한 분에 시작하는 봉(= 격자 시각 봉)의 **시가** 기준. 매수 IOC 지정가 = 최근가 × (1 + 완충)
  tick 내림(시스템 규칙 apply_spot_terms, A-064), 체결가 = 시가 × (1 + 슬리피지) tick 올림이 지정가 이하일 때만. 매도 시장가 =
  시가 × (1 − 슬리피지) tick 내림. 봉 거래량 10% 상한(04 §2.2), 수수료 받는 자산에서 차감(BN §9), 수량 LOT_SIZE.
  그 봉이 결측이면 미체결(MISSING_BAR 기록). 판정은 주문 창 안에서만 나므로 창 밖 체결 없음.
- [CP-040 D-262] 손절 감시(CAP-HLD-02 선언 전략만): 실운영 감시(app/stop_monitor)와 같은 1분 격자(주문 창 안 — 창 밖 20분은
  실운영도 감시하지 않음, DP-89 (6))마다 소유 포지션의 직전 닫힌 1분 봉 종가 ≤ 손절가(domain/stop_watch)면 청산 요청 →
  게이트 → 실행기. 체결 = 감지 다음 1분 봉 시가 × (1 − 슬리피지). 감지 봉 결측 = 판단 불가(청산 안 함), 체결 봉 결측 =
  미체결 기록 뒤 다음 분 재판단. 손절 초과 손실(손절가 − 체결가) 분포를 보고한다.
- [CP-040 D-265·CP-041 D-277] 일일 손실 L4: 매 1분 봉마다 보유를 그 분 **저가**로 평가(여러 심볼 합산, 그 분 봉이 없는
  보유 심볼은 마지막 종가 + "평가 결측 분")해 L4 도달 → K2 발동, 다음 분부터 신규·증액 거부·감액·청산 허용. 판정 시점
  검사(종가)도 그대로. K2 재개 = 다음 UTC 일 첫 분(낙관적 — 실제 사용자 재개는 늦을 수 있음). 같은 실행의 종가 기준
  발동 일수를 참고로 센다.
- 최종 확인 구간 보호 (CP-030 D-163): 실행 끝 날짜가 final_from(2026-01-01) 이후면 거부, 그 이후 행은 읽지도 않는다.
  [DP-81, A-124 ④] 최종 확인 실행(run_minute_final_check)만 final_from 이후를 쓰고, 전략 버전당 1회(M1과 같은 장부).
- 일일: UTC 일 경계에서 기준 금액·K2 재개(M1 DP-6과 같음). 평가액은 날마다 마지막 닫힌 봉 종가. 연환산 365일.
- 재현성(D-074): 지문 = 신호·체결·평가액 해시. 룩어헤드 교란 검사(V-91): perturbation_check_minute — 판정 시각에 닫히지
  않은 봉(체결 봉·이후 봉)과 그날 이후 1d 봉을 바꾸거나 지워도 그 판정 신호가 같아야 한다(실행기와 같은 decision_points 사용).
- 검증 (D-222, A-113 Q-5): 학습 3년·검증 1년·1년 이동, 검증 2020~2025 6창. 데이터 시작(2017-08-17) 전 학습 구간은
  "학습 단축" 표시, 창별 결측 봉 수(심볼별), C-7 사후 선택 "과대 추정 가능" 표시(심볼별). 공개 실서버 성과는 테스트넷
  PAPER 성과와 직접 비교하지 않는다(2-M2.6).
- [CP-038 T-058] 저빈도 예외 경로(run_low_frequency_m2): 저빈도 선언 전략만(MinuteValidationInputs.low_frequency). 일반 판정
  뒤 적격(E-1~E-5)이면 전체 이력 실행·2배 슬리피지·변형·LF-8 표본 실행 → LF-0~LF-8(app/low_frequency) → D-234 문구.
  비용 요율은 일반 경로와 같은 체결 요율(taker, D-267 (3)).
"""

from __future__ import annotations

import hashlib
import json
import time as _time
from bisect import bisect_right
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.app import low_frequency as lfm
from trading_system.app.backtest import BacktestError, BacktestResult
from trading_system.app.backtest_m2 import (
    ACCOUNT, _Account, _ClockOk, _Limits, _Market, _round_trips, _Rules, _Strategies, check_fee_model,
    compute_metrics_m2, fee_model_lines, fee_model_summary, fill_rate_of, final_bars, sellable,
)
from trading_system.app.m2_minutes import MinuteRequest, minute_request_of
from trading_system.app.validation import (
    _LEDGER_APPROVAL, ValidationError, Window, check_final_guard, claim_final_check, param_variants,
    walkforward_windows,
)
from trading_system.broker.fake.spot_fill_model import SpotBacktestBroker
from trading_system.config.capabilities import CapabilityTable
from trading_system.config.errors import ConfigError
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import FixedClock
from trading_system.domain.daily_input import DailyInput, m2_daily_input
from trading_system.domain.enums import (
    Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.fees import FeeModel
from trading_system.domain.halts import HaltView, RequestView, halt_rejection
from trading_system.domain.limits import AccountLimits
from trading_system.domain.market import CashView, CostRates, DailyRisk, Quote
from trading_system.domain.market_time import M2_TIME
from trading_system.domain.minute_agg import AggStatus, check_agg_window
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import DEFAULT_CONFIRM_MARGIN, ONE_MINUTE, PeriodGrid
from trading_system.domain.models import Account, GateRequest
from trading_system.domain.stop_watch import STOP_CAPABILITY, STOP_RULE_ID, latest_stop_price, stop_idem_key, stop_triggered
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
DAILY_CAPABILITY = "CAP-DAT-01"
PERIOD_PARAM = "period_minutes"
SLIPPAGE_BASE = Decimal("0.001")                 # CP-030 D-164 (2배 = 0.2%)
MIN_TRADES = 100                                 # CP-030 D-167 C-2
DAILY_LIVE_FINAL = 199                           # 실운영 1d 조회 limit 200(feed kline_limit) − 진행 중 봉 1 (D-276 (2)) [임시 판단]
FIXED_PARAMS = frozenset({PERIOD_PARAM, "qty_step", "agg_bar_minutes", "agg_lookback_bars", "max_missing_bars"})
# ±20% 변형 제외 — 일정 격자(1440 약수)·거래소 단위·집계 봉 규칙(15·60, 창·허용 결측 정수)은 구조값 [제안]
_TS_LEN = len("2017-08-17T04:00:00.000Z")
_CLOSE_OFFSET = timedelta(seconds=59, milliseconds=999)
_ZERO = Decimal("0")
_RISK_UP = (RequestType.ENTRY, RequestType.INCREASE)
_Q6 = Decimal("0.000001")


# --- 데이터 (스트리밍) --------------------------------------------------------------------------------------------

@dataclass
class MinuteReadStats:
    skipped_before: int = 0      # 시작 전이라 해석하지 않고 건너뛴 행
    used: int = 0                # 해석해 넘긴 행
    stopped_at: str | None = None   # 읽기를 멈춘 첫 행의 시각 문자열(끝·최종 확인 구간) — 그 행은 해석하지 않음


def _ts_key(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def iter_binance_minute_csv(path: Path, start: datetime, stop: datetime, *, final_from: date = FINAL_FROM_DEFAULT,
                            stats: MinuteReadStats | None = None, ext: bool = False,
                            incomplete: set[datetime] | None = None) -> Iterator[Bar]:
    """A-110 공개 1m CSV → 1분봉(Bar.at = 봉 시작 UTC). start ≤ 봉 시작 < min(stop, final_from 00:00 UTC)만 해석해 넘긴다.
    파일은 읽기만 한다. 형식·품질 위반은 ConfigError(파일 이름·줄 번호, 값 없음). ext면 확장 열(quote_volume·trades·
    taker_buy_base·taker_buy_quote)도 해석한다(흐름 열 선언 전략, D-260 — 형식 오류인 확장 값은 None).
    불완전 봉(거래소 거래 중단으로 분 안에서 일찍 끝난 봉 — 시작 ≤ 끝 < 시작 + 59.999초, 끝 = 시작인 길이 0 봉 포함)은 기본 거부. `incomplete` 집합을
    넘기면 그 봉만 건너뛰고 시작 시각을 기록한다 — 결측 분이 되어 그 창의 판정은 무효(보수적). 일봉
    load_binance_daily_csv(incomplete=)와 같은 규칙(A-112 선례). 다른 끝 시각 위반은 여전히 거부."""
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
            if prev is not None and opened <= prev:
                raise ConfigError(f"{name}:{i}: 봉 시작이 오름차순이 아님(중복·역순)")
            if closed != opened + _CLOSE_OFFSET:
                if incomplete is not None and opened <= closed < opened + _CLOSE_OFFSET:   # 길이 0 봉 포함
                    incomplete.add(opened.astimezone(UTC))   # 일찍 끝난 불완전 봉 → 결측 (넘기지 않음)
                    prev = opened
                    continue
                raise ConfigError(f"{name}:{i}: 봉 끝이 시작 + 59.999초가 아님 (1m 봉 아님 또는 불완전 봉)")
            if not all(x.is_finite() for x in (o, h, lo, c, v)) or min(o, h, lo, c) <= 0 or v < 0 \
                    or not (lo <= min(o, c) and max(o, c) <= h):
                raise ConfigError(f"{name}:{i}: 1분봉 품질 실패 (0 이하 가격·고저 모순·음수 거래량)")
            prev = opened
            st.used += 1
            if ext:
                yield Bar(opened.astimezone(UTC), o, h, lo, c, v, *_ext_values(cols[7:11]))
            else:
                yield Bar(opened.astimezone(UTC), o, h, lo, c, v)


def _ext_values(cols: Sequence[str]) -> tuple[Decimal | None, int | None, Decimal | None, Decimal | None]:
    def dec(x: str) -> Decimal | None:
        try:
            d = Decimal(x)
        except InvalidOperation:
            return None
        return d if d.is_finite() and d >= 0 else None
    try:
        trades: int | None = int(cols[1])
    except ValueError:
        trades = None
    return dec(cols[0]), trades, dec(cols[2]), dec(cols[3])


def first_minute_start(path: Path) -> date:
    """파일 첫 데이터 행의 봉 시작 UTC 날짜(검증 창 '학습 단축' 표시용). 읽기만."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        f.readline()
        line = f.readline()
    try:
        return datetime.fromisoformat(line.split(",")[0]).astimezone(UTC).date()
    except ValueError:
        raise ConfigError(f"{path.name}:2: open_time_utc 형식 오류") from None


def last_minute_start(path: Path) -> date:
    """파일 마지막 데이터 행의 봉 시작 UTC 날짜(여러 심볼 기간 검사·LF-8 표본 데이터 끝). 끝 부분만 읽는다."""
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8", errors="replace").splitlines()
    rows = [x for x in tail if x[:4].isdigit()]
    if not rows:
        raise ConfigError(f"{path.name}: 마지막 데이터 행 없음")
    try:
        return datetime.fromisoformat(rows[-1].split(",")[0]).astimezone(UTC).date()
    except ValueError:
        raise ConfigError(f"{path.name}: 마지막 행 open_time_utc 형식 오류") from None


# --- 판정 시점 (실행기·교란 검사 공용) -------------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionPoint:
    at: datetime                 # 판정 시각 = 격자 시각 + 확정 여유
    boundary: datetime           # 격자 시각 = 체결 봉의 시작
    window: tuple[Bar, ...]      # minute_ok일 때만 lookback개(오름차순), 아니면 () — window_at이 거짓인 시각도 ()
    minute_ok: bool
    last: Bar | None             # 판정 시각에 닫힌 가장 최근 봉 (최근가 = 종가)
    fill: Bar | None             # 체결 봉(격자 시각에 시작) — 결측이면 None
    prev_close: Bar | None = None  # 이 판정 날짜 UTC 00:00 전 마지막 봉 — 전날 평가액·일일 기준 금액(UTC 00:00 가격, A-124)


def decision_points(bars: Iterable[Bar], grid: PeriodGrid, days: Sequence[date], lookback: int,
                    counter: list[int] | None = None,
                    end_bar: list[Bar | None] | None = None, *,
                    window_at: Callable[[datetime], bool] | None = None,
                    on_bar: Callable[[Bar], None] | None = None) -> Iterator[DecisionPoint]:
    """판정 시각 순서로 (창·최근 봉·체결 봉). 창에는 판정 시각에 닫힌 봉(시작 ≤ 격자 시각 − 1분)만 들어간다 — 체결 봉과 그
    이후 봉은 판정 입력에 쓰이지 않는다(룩어헤드 차단). counter[0]에는 첫 판정 날짜 00:00 이후 들어온 봉 수를 센다.
    end_bar[0]에는 마지막 날 끝(24:00) 전 마지막 봉을 남긴다(마지막 날 평가액 — 판정 입력 아님).
    window_at: 주면 그 시각에만 창 튜플을 만든다(1분 격자 손절 감시에서 매분 복사하지 않게 — minute_ok는 그대로 계산).
    on_bar: 봉을 소비할 때마다(시각 순서) 부른다 — 매분 L4·손절 감지 봉·일 집계용(판정 입력 아님)."""
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
                if on_bar is not None:
                    on_bar(nxt)
                if nxt.at < midnight:   # type: ignore[operator]
                    before_day = nxt
                if counter is not None and since is not None and nxt.at >= since:  # type: ignore[operator]
                    counter[0] += 1
                nxt = next(it, None)
            ok = len(window) == lookback and window[-1].at == b - ONE_MINUTE and window[0].at == b - span
            fill = nxt if nxt is not None and nxt.at == b else None
            keep = ok and (window_at is None or window_at(at))
            yield DecisionPoint(at, b, tuple(window) if keep else (), ok, last, fill, before_day)
    if days and (counter is not None or end_bar is not None):   # 마지막 판정 뒤 같은 날 봉: 결측 집계·마지막 날 평가액
        end = datetime(days[-1].year, days[-1].month, days[-1].day, tzinfo=UTC) + timedelta(days=1)
        while nxt is not None and nxt.at < end:   # type: ignore[operator]
            if counter is not None and since is not None and nxt.at >= since:   # type: ignore[operator]
                counter[0] += 1
            if on_bar is not None:
                on_bar(nxt)
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
    fees: FeeModel | None = None            # 수수료 모델(CP-040 D-267) — 체결 taker, 추정 max. None = 기존 단일 요율
    sources: Mapping[str, MinuteSource] | None = None   # [D-275] 여러 심볼(한 계좌). 주면 symbol·source 대신 이것
    daily: Mapping[str, tuple[Bar, ...]] | None = None  # [D-276] 심볼별 1d 파일 봉(CAP-DAT-01 전략 입력·일치 보고)
    daily_limit: int = DAILY_LIVE_FINAL     # [D-276 (2)] 판정에 넣는 확정 일봉 최대 개수
    final_check: bool = False               # [DP-81] 최종 확인 실행(run_minute_final_check만 True로)

    @property
    def fill_rate(self) -> Decimal:
        return fill_rate_of(self.fees, self.cost_rates)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.sources)) if self.sources else (self.symbol,)

    def source_of(self, sym: str) -> MinuteSource:
        return self.sources[sym] if self.sources else self.source


@dataclass
class MinuteBacktestResult:
    result: BacktestResult
    decisions: int = 0
    minute_invalid: int = 0         # 분봉 입력 무효 판정 수 (CAP-DAT-02 전략, 심볼 합)
    missing_bars: int = 0           # 실행 기간 안 결측 분 수 (심볼 합)
    bars_in_period: int = 0
    missing_fill: int = 0           # 체결 봉 결측으로 미체결 처리한 주문 수
    first_open: Decimal | None = None
    last_close: Decimal | None = None
    seconds: float = 0.0
    extras: dict = field(default_factory=dict)
    # [CP-038 T-058] 거래별 진입 체결 금액이 있는 왕복 기록·끝까지 열린 왕복·마지막 봉 종가 (보고서·지문에 넣지 않음)
    trade_records: list = field(default_factory=list)
    open_lots: dict = field(default_factory=dict)
    end_close: Decimal | None = None
    # [T-068] 보고서 칸 (지문에 넣지 않음)
    k2: dict = field(default_factory=dict)          # 장중 L4·K2 통계 (D-265·D-277)
    stop: dict = field(default_factory=dict)        # 손절 감시 통계·초과 손실 분포 (D-262)
    by_symbol: dict = field(default_factory=dict)   # 심볼별 거래·손익·결측 (D-275 (7))
    daily_check: dict | None = None                 # 1d 파일 vs 1분봉 일 집계 일치 보고 (D-276 (5)), 1d 없으면 None


def grid_of(cfg: MinuteBacktestConfig) -> PeriodGrid:
    n = cfg.params.get(PERIOD_PARAM)
    if "CAP-TRG-02" not in cfg.strategy.spec.capabilities or isinstance(n, bool) or not isinstance(n, int):
        raise BacktestError("1분봉 백테스트는 CAP-TRG-02 주기 판정 전략만 (정수 파라미터 period_minutes, D-219)")
    try:
        return PeriodGrid(n, cfg.margin)
    except ValueError as exc:
        raise BacktestError(f"period_minutes {n}: {exc}") from None


def minute_request(cfg: MinuteBacktestConfig) -> MinuteRequest:
    """CAP-DAT-02 전략의 분봉 입력 요청(집계 봉·흐름 열) — 실운영과 같은 minute_request_of (D-259·D-260)."""
    if MINUTE_CAPABILITY not in cfg.strategy.spec.capabilities:
        return MinuteRequest()
    try:
        return minute_request_of(cfg.params)
    except ValueError as exc:
        raise BacktestError(f"분봉 입력 파라미터: {exc}") from None


def _warm_minutes(cfg: MinuteBacktestConfig, req: MinuteRequest) -> int:
    agg = (req.agg_minutes or 0) * req.agg_lookback
    return max(cfg.lookback_bars, agg + (req.agg_minutes or 0))


def _days(cfg: MinuteBacktestConfig) -> list[date]:
    if not isinstance(cfg.slippage, Decimal) or not cfg.slippage.is_finite() or not _ZERO <= cfg.slippage < 1:
        raise BacktestError("슬리피지는 0 이상 1 미만의 Decimal (CP-030 D-164 기본 0.001)")
    check_fee_model(cfg.fees, cfg.cost_rates)
    for sym in cfg.symbols:
        if sym not in cfg.rules or sym not in cfg.pairs:
            raise BacktestError(f"규칙·자산 쌍 없는 심볼: {sym}")
    if not 1 <= cfg.lookback_bars <= 997:
        raise BacktestError("lookback_bars는 1~997 (설정 [minute]와 같은 범위)")
    if cfg.end < cfg.start:
        raise BacktestError("구간에 날짜가 없음")
    if cfg.final_check:
        if cfg.start < cfg.final_from:
            raise BacktestError(f"최종 확인 실행은 최종 확인 구간({cfg.final_from} 이후)만 쓴다 (D-163)")
    elif cfg.end >= cfg.final_from:
        raise BacktestError(f"실행 날짜가 최종 확인 구간({cfg.final_from} 이후)과 겹침 — 최종 확인 실행(--final-check)으로만 "
                            "(D-163, DP-81)")
    if DAILY_CAPABILITY in cfg.strategy.spec.capabilities and (
            cfg.daily is None or any(s not in cfg.daily for s in cfg.symbols)):
        raise BacktestError("CAP-DAT-01(일봉) 선언 전략은 심볼마다 확정 일봉 입력(1d 파일, --daily)이 필요 (CP-041 D-276)")
    return [cfg.start + timedelta(days=k) for k in range((cfg.end - cfg.start).days + 1)]


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _minute_bar(sym: str, b: Bar) -> MinuteBar:
    return MinuteBar(sym, 1, b.at, b.open, b.high, b.low, b.close, b.volume,  # type: ignore[arg-type]
                     quote_volume=b.quote_volume, trades=b.trades, taker_buy_base=b.taker_buy_base,
                     taker_buy_quote=b.taker_buy_quote)


def _has_ext(b: Bar) -> bool:
    return None not in (b.quote_volume, b.trades, b.taker_buy_base, b.taker_buy_quote)


@dataclass(frozen=True)
class _MinuteView:
    bars: tuple[Bar, ...]
    ok: bool
    agg_bars: tuple[Bar, ...] = ()
    agg_missing: tuple[datetime, ...] = ()


def _minute_view(req: MinuteRequest, dp: DecisionPoint, agg_hist: Iterable[MinuteBar], margin: timedelta) -> _MinuteView:
    """실운영 M2MinuteInput._one·_agg와 같은 판단: 1분 창 무효 → 무효, 흐름 열 선언이면 확장 열 결측 → 무효(EXT_MISSING),
    집계 봉 요청이면 check_agg_window(같은 함수) — OK가 아니면 그 심볼 분봉 입력 전체 무효(DP-88 (3))."""
    if not dp.minute_ok:
        return _MinuteView((), False)
    if req.flow and not all(_has_ext(b) for b in dp.window):
        return _MinuteView((), False)
    if req.agg_minutes is None:
        return _MinuteView(dp.window, True)
    w = check_agg_window(agg_hist, dp.at, req.agg_minutes, req.agg_lookback, req.max_missing, margin, flow=req.flow)
    if w.status is not AggStatus.OK:
        return _MinuteView((), False)
    return _MinuteView(dp.window, True, w.bars, w.missing)


class _DailyFeed:
    """심볼별 1d 봉 → 판정 시각의 판정 입력(m2_daily_input, 실시간과 같은 함수). 같은 날짜는 같은 결과라 날짜별로 기억한다.
    후보는 판정 날짜 이하 봉 가운데 최근 limit + 1개(진행 중 봉 1개 포함)로 잘라 넘긴다 — 확정·개수 판단은 공용 함수가 한다."""

    def __init__(self, daily: Mapping[str, tuple[Bar, ...]], limit: int) -> None:
        self._bars = {s: tuple(sorted(bs, key=lambda b: b.at)) for s, bs in daily.items()}   # type: ignore[arg-type, return-value]
        self._dates = {s: [b.at for b in bs] for s, bs in self._bars.items()}
        self._limit = limit
        self._cache: dict[tuple[str, date], DailyInput] = {}

    def at(self, sym: str, at: datetime) -> DailyInput:
        key = (sym, at.astimezone(UTC).date())
        hit = self._cache.get(key)
        if hit is None:
            k = bisect_right(self._dates[sym], key[1])   # type: ignore[arg-type]
            cands = self._bars[sym][max(0, k - self._limit - 1):k]
            hit = self._cache[key] = m2_daily_input(cands, at, final_bars, max_bars=self._limit)
        return hit


def _daily_view(feed: _DailyFeed | None, sym: str, at: datetime) -> dict:
    if feed is None:
        return {}
    di = feed.at(sym, at)
    return {"daily_bars": di.bars, "daily_ok": di.ok, "daily_exit": di.daily_exit}


# --- 실행기 ------------------------------------------------------------------------------------------------------

class _DayCheck:
    """[D-276 (5)] 1분봉 일 집계(시가 첫 분·고가 최대·저가 최소·종가 마지막 분·거래량 합 — D-259와 같은 식) vs 1d 파일."""

    def __init__(self, daily: Mapping[str, tuple[Bar, ...]], syms: Sequence[str], days: Sequence[date]) -> None:
        self.ref = {s: {b.at: b for b in daily.get(s, ())} for s in syms}
        self.days = set(days)
        self.cur: dict[str, tuple[date, list[Bar]]] = {}
        self.stats = {s: {"compared": 0, "mismatch": 0, "minute_incomplete": 0, "no_daily": 0, "max_price_diff": "0",
                          "max_volume_diff": "0", "mismatch_days": []} for s in syms}
        self.seen: dict[str, set[date]] = {s: set() for s in syms}

    def add(self, sym: str, b: Bar) -> None:
        d = b.at.date()   # type: ignore[union-attr]
        cur = self.cur.get(sym)
        if cur is None or cur[0] != d:
            if cur is not None:
                self._close(sym, *cur)
            self.cur[sym] = (d, [b])
        else:
            cur[1].append(b)

    def _close(self, sym: str, d: date, bars: list[Bar]) -> None:
        if d not in self.days:
            return
        self.seen[sym].add(d)
        st = self.stats[sym]
        if len(bars) != 1440:
            st["minute_incomplete"] += 1
            return
        ref = self.ref[sym].get(d)
        if ref is None:
            st["no_daily"] += 1
            return
        st["compared"] += 1
        agg = (bars[0].open, max(b.high for b in bars), min(b.low for b in bars), bars[-1].close)
        price = max(abs(x - y) for x, y in zip(agg, (ref.open, ref.high, ref.low, ref.close), strict=True))
        vol = abs(sum((b.volume for b in bars), _ZERO) - ref.volume)
        if price or vol:
            st["mismatch"] += 1
            if len(st["mismatch_days"]) < 20:
                st["mismatch_days"].append(d.isoformat())
        st["max_price_diff"] = str(max(Decimal(st["max_price_diff"]), price))
        st["max_volume_diff"] = str(max(Decimal(st["max_volume_diff"]), vol))

    def finish(self) -> dict:
        for sym, cur in list(self.cur.items()):
            self._close(sym, *cur)
        for sym, st in self.stats.items():
            st["minute_incomplete"] += len(self.days - self.seen[sym])   # 1분봉이 하나도 없는 날도 비교 불가
        return self.stats


def run_minute_backtest(cfg: MinuteBacktestConfig) -> MinuteBacktestResult:
    t0 = _time.perf_counter()
    grid = grid_of(cfg)
    days = _days(cfg)
    req = minute_request(cfg)
    spec = cfg.strategy.spec
    syms = cfg.symbols
    multi = len(syms) > 1
    minute_declared = MINUTE_CAPABILITY in spec.capabilities
    daily_feed = _DailyFeed(cfg.daily, cfg.daily_limit) \
        if DAILY_CAPABILITY in spec.capabilities and cfg.daily is not None else None
    stop_active = STOP_CAPABILITY in spec.capabilities
    point_grid = PeriodGrid(1, cfg.margin) if stop_active else grid
    period = timedelta(minutes=grid.period_minutes)
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
    broker = SpotBacktestBroker(pairs=cfg.pairs, fee_rate=cfg.fill_rate, slippage=cfg.slippage,
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
    strategies = registry if registry is not None else _Strategies()
    gate = Gate(repo, clock, GateInputs(_Limits(cfg.account_limits), market, acct, strategies, _ClockOk(),
                                        spot_rules=_Rules(cfg, market)), policy)  # type: ignore[arg-type]
    halts = HaltManager(repo, gate, executor, clock, reference_time_of=lambda a: cfg.account_limits.daily_reference_time,
                        market_time=M2_TIME)
    if registry is not None:
        registry.attach_halts(halts)
    lots = {s: cfg.rules[s].lot_step for s in syms}
    l4 = cfg.account_limits.l4_daily_loss_ratio

    out = MinuteBacktestResult(result=None)  # type: ignore[arg-type]
    cash, costs = cfg.initial_cash, _ZERO
    equity: list[tuple[date, Decimal]] = []
    rejects: Counter = Counter()
    signals: list[str] = []
    fills_log: list[tuple[date, str, str, str, str, str]] = []
    by_eval: dict[str, list[tuple]] = {}
    seen = k2_resumes = 0
    st: dict = {"cur_day": None, "base": _ZERO, "k2_day": None, "close_day": None}
    last_close: dict[str, Decimal] = {}          # 처리한(닫힌) 봉의 마지막 종가 — 평가·기준 금액
    eval_close: dict[str, Decimal] = {}          # 마지막 판정 시점 종가 (단순 보유 참고 C-5 — 종전 의미)
    first_open: dict[str, Decimal] = {}
    held: dict[str, Decimal] = {s: _ZERO for s in syms}
    avg: dict[str, Decimal] = {s: _ZERO for s in syms}
    dirty = True
    k2 = {"days": 0, "times": [], "hours": {}, "rejected_entries": {}, "close_days": 0, "eval_missing_minutes": 0,
          "measured": True}
    stop = {"active": stop_active, "triggers": 0, "requests": 0, "fills": 0, "unfilled": 0, "not_judged": 0,
            "skipped": 0, "no_stop_price": 0, "excess": []}
    buffer: list[tuple[datetime, str, Bar]] = []
    # 집계 봉 창 + 정렬 여유(판정 격자가 집계 길이에 맞지 않아도 첫 집계 봉 구성 분이 남게) — 집계 길이 × (창 + 1)
    agg_hist: dict[str, deque[MinuteBar]] = {s: deque(maxlen=(req.agg_minutes or 0) * (req.agg_lookback + 1) or 1)
                                             for s in syms}
    first_day_seen: set[str] = set()
    last_day_seen: set[str] = set()
    last_day_start = _midnight(days[-1])
    first_day_end = _midnight(days[0]) + timedelta(days=1)
    day_check = _DayCheck(cfg.daily, syms, days) if cfg.daily is not None else None

    def refresh() -> None:
        nonlocal dirty
        if dirty:
            for s in syms:
                pos = repo.position(ACCOUNT, s)
                held[s], avg[s] = pos.quantity, pos.avg_price
            dirty = False

    def absorb(day: date) -> list[tuple[date, str, str, str, str, str]]:
        nonlocal seen, cash, costs
        new = []
        for f in broker.fills_for(ACCOUNT)[seen:]:
            seen += 1
            if f.side == "BUY":
                cash -= f.quote_qty
                costs += f.commission * f.price if f.commission_asset != cfg.pairs[f.instrument][1] else f.commission
            else:
                cash += f.quote_qty - f.commission
                costs += f.commission
            row = (day, f.order_no, f.instrument, f.side, str(f.quantity), str(f.price))
            fills_log.append(row)
            new.append(row)
        return new

    def mark(px: Mapping[str, Decimal], fallback_avg: bool = True) -> Decimal:
        return sum((held[s] * (px.get(s, avg[s] if fallback_avg else _ZERO)) for s in syms if held[s]), _ZERO)

    def k2_note(at: datetime) -> None:
        k2["days"] += 1
        k2["times"].append(at.isoformat())
        h = f"{at.hour:02d}"
        k2["hours"][h] = k2["hours"].get(h, 0) + 1

    def rollover(day: date) -> None:
        """UTC 일 경계: 전날 평가액·K2 다음 날 재개·기준 금액 — 모두 UTC 00:00 전 마지막 봉 종가(판정 사이에는 체결이 없어
        보유·현금은 전날 끝과 같다)."""
        nonlocal k2_resumes
        refresh()
        mark0 = cash + mark(last_close)
        if st["cur_day"] is not None:
            equity.append((st["cur_day"], mark0))
        st["cur_day"] = day
        clock._now = _midnight(day)  # noqa: SLF001
        if HaltType.K2.value in repo.active_halts(ACCOUNT):
            if halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "backtest").ok:
                k2_resumes += 1
        st["base"] = mark0
        if mark0 <= 0:
            repo.close()
            raise BacktestError(f"{day} 기준 금액 0 이하")

    def advance(day: date) -> None:
        if st["cur_day"] is None:
            rollover(days[0])
        while st["cur_day"] < day:
            rollover(st["cur_day"] + timedelta(days=1))

    def minute_l4(m: datetime, group: dict[str, Bar]) -> None:
        """[D-265·D-277] 그 분 저가로 보유 평가 → L4 도달이면 K2(다음 분부터 신규 거부). 같은 실행의 종가 기준도 센다."""
        refresh()
        if not any(held.values()):
            return
        low = close = cash
        for s in syms:
            q = held[s]
            if not q:
                continue
            b = group.get(s)
            if b is None:
                k2["eval_missing_minutes"] += 1
                px = last_close.get(s, avg[s])
                low += q * px
                close += q * px
            else:
                low += q * b.low
                close += q * b.close
        base = st["base"]
        day = st["cur_day"]
        if st["close_day"] != day and (base - close) / base >= l4:
            st["close_day"] = day
            k2["close_days"] += 1
        if st["k2_day"] != day and (base - low) / base >= l4:
            clock._now = m + ONE_MINUTE + cfg.margin  # noqa: SLF001
            if halts.check_daily_loss(ACCOUNT, DailyRisk(base, _ZERO, low - base, _ZERO), l4) is not None:
                st["k2_day"] = day
                k2_note(m)

    def drain() -> None:
        """소비한 봉을 분 순서로 처리: 일 경계·매분 L4·마지막 종가·일 집계."""
        if not buffer:
            return
        buffer.sort(key=lambda x: (x[0], x[1]))
        i = 0
        while i < len(buffer):
            m = buffer[i][0]
            group: dict[str, Bar] = {}
            while i < len(buffer) and buffer[i][0] == m:
                group[buffer[i][1]] = buffer[i][2]
                i += 1
            if m >= _midnight(days[0]):
                advance(m.date())
                minute_l4(m, group)
            for s, b in group.items():
                last_close[s] = b.close
                if day_check is not None:
                    day_check.add(s, b)
                if m < first_day_end and m >= _midnight(days[0]):
                    first_day_seen.add(s)
                if m >= last_day_start:
                    last_day_seen.add(s)
        buffer.clear()

    def on_bar_of(sym: str) -> Callable[[Bar], None]:
        hist = agg_hist[sym]
        want_agg = req.agg_minutes is not None

        def f(b: Bar) -> None:
            buffer.append((b.at, sym, b))   # type: ignore[arg-type]
            if want_agg:
                hist.append(_minute_bar(sym, b))
        return f

    def is_eval(at: datetime) -> bool:
        b = at - cfg.margin
        return (b - _midnight(b.date())) % period == timedelta(0)

    warm = _midnight(days[0]) - ONE_MINUTE * _warm_minutes(cfg, req)
    stop_at = _midnight(days[-1]) + timedelta(days=1)
    counters = {s: [0] for s in syms}
    end_bars: dict[str, list[Bar | None]] = {s: [None] for s in syms}
    gens = [decision_points(cfg.source_of(s)(warm, stop_at), point_grid, days, cfg.lookback_bars, counters[s],
                            end_bars[s], window_at=is_eval, on_bar=on_bar_of(s)) for s in syms]
    for dps_t in zip(*gens, strict=True):
        dps = dict(zip(syms, dps_t, strict=True))
        at = dps_t[0].at
        day = at.date()
        drain()
        advance(day)
        clock._now = at  # noqa: SLF001
        refresh()
        evaluate = is_eval(at)
        quotes: dict[str, Quote] = {}
        for s, dp in dps.items():
            if dp.last is not None:
                quotes[s] = Quote(s, dp.last.close, min(dp.last.at + ONE_MINUTE, at))  # type: ignore[operator]
                if evaluate:
                    eval_close[s] = dp.last.close
        market.quotes = quotes
        if not evaluate and not any(held.values()):
            continue   # 손절 감시 1분 격자의 무보유 분: 할 일 없음 (매분 L4는 봉 처리에서 이미 했다)
        acct.daily = DailyRisk(st["base"], _ZERO, cash + mark(last_close) - st["base"], _ZERO)
        reserved = sum((r.cash_remaining for r in repo.active_reservations(ACCOUNT)), _ZERO)
        acct.cash_view = CashView(cash, cash - reserved)
        if evaluate and st["k2_day"] != day and halts.check_daily_loss(ACCOUNT, acct.daily, l4) is not None:
            st["k2_day"], dirty = day, True   # 같은 날 다시 발동해도 같은 정지(중복 없음) — 그날은 더 검사하지 않는다
            k2_note(at)
        submitted: Counter = Counter()
        stop_intents: list[tuple[str, str, Decimal, datetime]] = []
        if stop_active:
            for s in syms:
                if not held[s]:
                    continue
                made = _stop_request(repo, strategies, spec.owner, s, dps[s], at, cfg.rules[s], quotes.get(s),
                                     order_policy, stop)
                if made is None:
                    continue
                r, stp = made
                stop["requests"] += 1
                if r is None:
                    rejects["TERMS"] += 1
                    stop["unfilled"] += 1
                    continue
                d = gate.decide(r)
                if d.result is GateResult.APPROVED and d.approved is not None:
                    executor.submit(d.approved)
                    submitted[s] += 1
                    stop_intents.append((d.approved.intent_id, s, stp, dps[s].boundary))
                else:
                    rejects[str(d.failed_check)] += 1
                    stop["unfilled"] += 1
            if submitted:
                dirty = True
        if evaluate:
            for s, dp in dps.items():
                if dp.fill is not None and s not in first_open:
                    first_open[s] = dp.fill.open
            out.decisions += 1
            views = []
            for s in syms:
                dp = dps[s]
                pos = repo.position(ACCOUNT, s)
                own = repo.active_ownership(ACCOUNT, s)
                q = sellable(pos.quantity, cfg.rules[s])
                pv = PositionView(q, pos.avg_price, 0, own is not None and own.owner == spec.owner) if q > 0 else None
                pending = bool(repo.active_new_intents(ACCOUNT, s, Side.BUY.value)
                               or repo.active_new_intents(ACCOUNT, s, Side.SELL.value))
                mv = _minute_view(req, dp, agg_hist[s], cfg.margin) if minute_declared else _MinuteView((), True)
                out.minute_invalid += 0 if mv.ok else 1
                qt = quotes.get(s)
                views.append(InstrumentView(s, minute_bars=mv.bars, last_price=qt.price if qt is not None else None,
                                            position=pv, has_pending_order=pending, data_ok=qt is not None,
                                            minute_ok=mv.ok, agg_bars=mv.agg_bars, agg_missing=mv.agg_missing,
                                            **_daily_view(daily_feed, s, at)))
            ev_id = evaluation_id_of(spec.strategy_id, spec.version, "M2", at.isoformat())
            snap = EvaluationSnapshot(ev_id, "M2", ACCOUNT, at, st["base"], tuple(views),
                                      next_evaluation_at=grid.next_after(at))
            res = run_evaluation(cfg.strategy, snap, cfg.params, lots)
            if res.signals:
                by_eval[at.isoformat()] = [(x.signal_id, x.kind.value, x.instrument, str(x.quantity), str(x.price))
                                           for x in res.signals]
            for sg in res.signals:
                signals.append(sg.signal_id)
                req_ = apply_spot_terms(GateRequest.from_signal(sg), quotes.get(sg.instrument), at,
                                        cfg.rules[sg.instrument], order_policy)
                if req_ is None:
                    rejects["TERMS"] += 1
                    continue
                decision = gate.decide(req_)
                if decision.result is GateResult.APPROVED and decision.approved is not None:
                    executor.submit(decision.approved)
                    submitted[sg.instrument] += 1
                else:
                    rejects[str(decision.failed_check)] += 1
                    if sg.kind in _RISK_UP and decision.failed_check == Check.G2 \
                            and HaltType.K2.value in repo.active_halts(ACCOUNT):
                        k2["rejected_entries"][sg.instrument] = k2["rejected_entries"].get(sg.instrument, 0) + 1
            if res.signals and not submitted:
                dirty = True
        if not submitted:
            continue
        executor.process_events()
        for s in syms:   # D-221: 판정 시각이 속한 분의 봉 시가 + 불리 슬리피지 (심볼별, 제출 순서)
            if not submitted[s]:
                continue
            fb = dps[s].fill
            if fb is not None:
                broker.match(s, fb.open, fb.volume, at)
            else:
                rejects["MISSING_BAR"] += submitted[s]
                out.missing_fill += submitted[s]
        broker.end_of_day()   # IOC·시장가 미체결분 소멸, 봉 거래량 사용량 초기화(상한은 봉마다)
        executor.process_events()
        new = absorb(day)
        dirty = True
        for iid, s, stp, b in stop_intents:
            filled = repo.intent(iid).filled_quantity
            px = next((Decimal(r[5]) for r in new if r[2] == s and r[3] == "SELL"), None)
            if filled > 0 and px is not None:
                stop["fills"] += 1
                stop["excess"].append({"symbol": s, "detected_bar": (b - ONE_MINUTE).isoformat(), "stop": str(stp),
                                       "fill": str(px), "excess": str(stp - px),
                                       "ratio": str(((stp - px) / stp).quantize(_Q6))})
            else:
                stop["unfilled"] += 1
    drain()
    if st["cur_day"] is None:
        advance(days[0])
    advance(days[-1])
    refresh()
    if multi:
        lacking = sorted(s for s in syms if s not in first_day_seen or s not in last_day_seen)
        if lacking:
            repo.close()
            raise BacktestError(f"실행 기간 {days[0]}~{days[-1]}을 모든 심볼 데이터가 덮지 않음(첫날·마지막 날 봉 없음): "
                                f"{', '.join(lacking)} — 상장 시점 차이로 결과가 바뀌지 않게 거부 (CP-041 D-275 (1))")
    px_end = {s: (end_bars[s][0].close if end_bars[s][0] is not None else last_close.get(s)) for s in syms}
    equity.append((st["cur_day"], cash + sum((held[s] * (px_end[s] if px_end[s] is not None else _ZERO)
                                              for s in syms if held[s]), _ZERO)))
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
    out.bars_in_period = sum(c[0] for c in counters.values())
    out.missing_bars = len(days) * 1440 * len(syms) - out.bars_in_period
    first = syms[0]
    out.first_open = first_open.get(first)
    out.last_close = eval_close.get(first)
    out.end_close = px_end[first]
    out.trade_records, out.open_lots = lfm.round_trip_records(broker.fills_for(ACCOUNT), cfg.pairs, cfg.rules)
    out.k2 = k2
    stop["summary"] = _excess_summary(stop["excess"])
    out.stop = stop
    out.by_symbol = {s: {"trades": sum(1 for t in trades if t.instrument == s),
                         "pnl": str(sum((t.pnl for t in trades if t.instrument == s), _ZERO).quantize(Decimal("0.00000001"))),
                         "bars": counters[s][0], "missing_bars": len(days) * 1440 - counters[s][0],
                         "first_open": None if s not in first_open else str(first_open[s]),
                         "last_close": None if s not in eval_close else str(eval_close[s]),
                         "end_close": None if px_end[s] is None else str(px_end[s])} for s in syms}
    out.daily_check = day_check.finish() if day_check is not None else None
    out.seconds = round(_time.perf_counter() - t0, 2)
    return out


def _stop_price(repo: Repository, sym: str, owner: str, intent_id: str | None) -> Decimal | None:
    """실운영 StopMonitor._stop_price와 같은 조회: 이 소유 기간의 진입·증액 의도(최신 순) → latest_stop_price."""
    if intent_id is None:
        return None
    rows = repo.select(
        "SELECT filled_quantity, stop_price FROM intent WHERE account=? AND instrument=? AND owner=? "
        "AND request_type IN ('ENTRY','INCREASE') AND created_seq >= "
        "(SELECT created_seq FROM intent WHERE intent_id=?) ORDER BY created_seq DESC",
        (ACCOUNT, sym, owner, intent_id))
    return latest_stop_price((Decimal(r["filled_quantity"]),
                              Decimal(r["stop_price"]) if r["stop_price"] is not None else None) for r in rows)


def _stop_request(repo: Repository, strategies: object, owner: str, sym: str, dp: DecisionPoint, at: datetime,
                  rules: SymbolRules, quote: Quote | None, policy: SpotOrderPolicy,
                  stats: dict) -> tuple[GateRequest | None, Decimal] | None:
    """[D-262·D-263] 실운영 StopMonitor._judge와 같은 판단(감지·요청 안 함 조건·요청 형식). 요청할 일이면 (주문 조건을 넣은
    요청 — 정할 수 없으면 None, 손절가), 아니면 None."""
    own = repo.active_ownership(ACCOUNT, sym)
    if own is None or own.owner != owner:
        return None   # 비소유(수동·다른 전략) 보유는 감시하지 않는다 (R-28)
    q = sellable(repo.position(ACCOUNT, sym).quantity, rules)
    if q <= 0:
        return None
    stop = _stop_price(repo, sym, owner, own.intent_id)
    if stop is None:
        stats["no_stop_price"] += 1
        return None
    bar_start = dp.boundary - ONE_MINUTE
    b = dp.last if dp.last is not None and dp.last.at == bar_start else None
    if b is None:
        stats["not_judged"] += 1   # 직전 닫힌 봉 결측 → 판단하지 않음(청산 안 함, §8-10)
        return None
    if not stop_triggered(b.close, stop):
        return None
    stats["triggers"] += 1
    if repo.unknown_intents(ACCOUNT, sym) or repo.active_new_intents(ACCOUNT, sym, Side.SELL.value) \
            or repo.active_new_intents(ACCOUNT, sym, Side.BUY.value):
        stats["skipped"] += 1
        return None
    view = RequestView(RequestSource.SIGNAL, RequestType.EXIT, Side.SELL, sym, owner)
    for row in repo.halt_rows(ACCOUNT):
        h = HaltView(HaltType(row["halt_type"]), row["scope_instrument"], row["scope_owner"], row["variant"])
        if halt_rejection(h, view):
            stats["skipped"] += 1
            return None
    if strategies.signal_rejection(owner, RequestType.EXIT) is not None:  # type: ignore[attr-defined]
        stats["skipped"] += 1
        return None
    key = stop_idem_key(own.ownership_id, bar_start)
    req = GateRequest(source=RequestSource.SIGNAL, request_type=RequestType.EXIT, account=ACCOUNT, instrument=sym,
                      side=Side.SELL, quantity=q, order_type=OrderType.MARKET, price=b.close, idem_key=key, owner=owner,
                      valid_until=at + ONE_MINUTE, signal_id=key, rule_id=STOP_RULE_ID)
    return apply_spot_terms(req, quote, at, rules, policy), stop


def _excess_summary(rows: list[dict]) -> dict:
    """손절 초과 손실 분포(손절가 대비 체결가 차 비율, 양수 = 손절가보다 불리)."""
    if not rows:
        return {"count": 0, "gaps": 0, "mean_ratio": None, "median_ratio": None, "p90_ratio": None, "max_ratio": None}
    rs = sorted(Decimal(r["ratio"]) for r in rows)
    n = len(rs)
    return {"count": n, "gaps": sum(1 for r in rs if r > 0),
            "mean_ratio": str((sum(rs, _ZERO) / n).quantize(_Q6)), "median_ratio": str(rs[n // 2]),
            "p90_ratio": str(rs[min(n - 1, (n * 9) // 10)]), "max_ratio": str(rs[-1])}


# --- 룩어헤드 교란 검사 (V-91 분봉판) ------------------------------------------------------------------------------

def _flat_signals(cfg: MinuteBacktestConfig, grid: PeriodGrid, dps: Mapping[str, DecisionPoint],
                  hist: Mapping[str, Sequence[Bar]], daily: Mapping[str, tuple[Bar, ...]] | None) -> list[tuple]:
    spec = cfg.strategy.spec
    declared = MINUTE_CAPABILITY in spec.capabilities
    req = minute_request(cfg)
    feed = _DailyFeed(daily, cfg.daily_limit) if DAILY_CAPABILITY in spec.capabilities and daily is not None else None
    at = next(iter(dps.values())).at
    views = []
    for s in cfg.symbols:
        dp = dps[s]
        price = dp.last.close if dp.last is not None else None
        agg = [_minute_bar(s, b) for b in hist[s] if b.at < dp.boundary] if req.agg_minutes else ()  # type: ignore[operator]
        mv = _minute_view(req, dp, agg, cfg.margin) if declared else _MinuteView((), True)
        views.append(InstrumentView(s, minute_bars=mv.bars, last_price=price, data_ok=price is not None, minute_ok=mv.ok,
                                    agg_bars=mv.agg_bars, agg_missing=mv.agg_missing, **_daily_view(feed, s, at)))
    ev = evaluation_id_of(spec.strategy_id, spec.version, "M2", at.isoformat())
    snap = EvaluationSnapshot(ev, "M2", ACCOUNT, at, cfg.initial_cash, tuple(views), next_evaluation_at=grid.next_after(at))
    r = run_evaluation(cfg.strategy, snap, cfg.params, {s: cfg.rules[s].lot_step for s in cfg.symbols})
    return [(x.signal_id, x.kind.value, x.instrument, str(x.quantity), str(x.price)) for x in r.signals]


def perturbation_check_minute(cfg: MinuteBacktestConfig, check_days: Sequence[date], probes: int = 4) -> list[str]:
    """V-91: 판정 시각 T에 닫히지 않은 봉(체결 봉·이후 봉 — 시작 ≥ 격자 시각)과 그날 이후 1d 봉(진행 중·미래)을 크게 바꾸거나
    지워도 T의 신호가 같아야 한다. 무보유 스냅샷 비교, 모든 심볼을 함께 교란. 날마다 판정 시각 probes개(고르게)를 본다."""
    grid = grid_of(cfg)
    req = minute_request(cfg)
    warm = _warm_minutes(cfg, req)
    bad: list[str] = []
    syms = cfg.symbols

    def pts(bars: Mapping[str, list[Bar]], day: date) -> list[dict[str, DecisionPoint]]:
        gens = [decision_points(bars[s], grid, [day], cfg.lookback_bars) for s in syms]
        return [dict(zip(syms, t, strict=True)) for t in zip(*gens, strict=True)]

    def scaled(b: Bar) -> Bar:
        return Bar(b.at, b.open * 3, b.high * 3, b.low * 3, b.close * 3, b.volume * 3)

    for day in check_days:
        bars = {s: list(cfg.source_of(s)(_midnight(day) - ONE_MINUTE * warm, _midnight(day) + timedelta(days=1)))
                for s in syms}
        points = pts(bars, day)
        if not points:
            continue
        step = max(1, len(points) // probes)
        for dps in points[::step][:probes]:
            first = next(iter(dps.values()))
            base = _flat_signals(cfg, grid, dps, bars, cfg.daily)
            cut = first.boundary
            dvars: list[Mapping[str, tuple[Bar, ...]] | None] = [cfg.daily, cfg.daily]
            if cfg.daily is not None:
                dvars = [{s: tuple(scaled(b) if b.at >= day else b for b in bs) for s, bs in cfg.daily.items()},   # type: ignore[operator]
                         {s: tuple(b for b in bs if b.at < day) for s, bs in cfg.daily.items()}]   # type: ignore[operator]
            variants = (({s: [scaled(b) if b.at >= cut else b for b in bars[s]] for s in syms}, dvars[0]),   # type: ignore[operator]
                        ({s: [b for b in bars[s] if b.at < cut] for s in syms}, dvars[1]))                   # type: ignore[operator]
            for var, dv in variants:
                again = next(p for p in pts(var, day) if next(iter(p.values())).at == first.at)
                if _flat_signals(cfg, grid, again, var, dv) != base:
                    bad.append(first.at.isoformat())
                    break
    return bad


# --- 보고서 ------------------------------------------------------------------------------------------------------

def buy_hold_return(first_open: Decimal | None, last_close: Decimal | None, slippage: Decimal, fee: Decimal) -> str | None:
    """참고(C-5): 첫 체결 봉 시가 × (1 + s) 매수, 마지막 종가 × (1 − s) 매도, 수수료 양쪽(받는 자산)."""
    if first_open is None or last_close is None or first_open <= 0:
        return None
    r = (1 - fee) * last_close * (1 - slippage) * (1 - fee) / (first_open * (1 + slippage)) - 1
    return format(r.quantize(Decimal("0.000001")), "f")


def hold_return_of(r: MinuteBacktestResult, slippage: Decimal, fee: Decimal, *, end: bool = False) -> str | None:
    """단순 보유 참고: 단일 심볼은 종전 식 그대로, 여러 심볼은 심볼별 단순 보유 수익률의 단순 평균(동일 비중) [임시 판단].
    end면 마지막 봉 종가(end_close) 기준(LF-8 표본·C-5+와 같은 기준)."""
    if len(r.by_symbol) <= 1:
        return buy_hold_return(r.first_open, r.end_close if end else r.last_close, slippage, fee)
    vals = []
    for x in r.by_symbol.values():
        last = x["end_close"] if end else x["last_close"]
        v = buy_hold_return(None if x["first_open"] is None else Decimal(x["first_open"]),
                            None if last is None else Decimal(last), slippage, fee)
        if v is None:
            return None
        vals.append(Decimal(v))
    return format((sum(vals, _ZERO) / len(vals)).quantize(_Q6), "f")


def _intraday_lines(out: MinuteBacktestResult) -> list[str]:
    k, s = out.k2, out.stop
    lines = [f"- 장중 L4(D-265·D-277): 매 1분 봉 **저가 기준** 평가 — K2 발동 {k.get('days', 0)}일"
             + (f"(시각 UTC {', '.join(k['times'][:10])}{' …' if len(k.get('times', [])) > 10 else ''}, 시간대별 "
                f"{json.dumps(k['hours'], sort_keys=True)})" if k.get("times") else "")
             + f", K2로 거부된 진입 {json.dumps(k.get('rejected_entries', {}), sort_keys=True)}, 참고 **종가 기준** 발동 "
             f"{k.get('close_days', 0)}일, 평가 결측 분 {k.get('eval_missing_minutes', 0)}. K2 재개 = 다음 UTC 일 첫 분"
             "(낙관 — 실제 사용자 재개는 더 늦을 수 있음)"]
    if s.get("active"):
        sm = s.get("summary", {})
        lines.append(f"- 손절 감시(CAP-HLD-02, D-262): 감지 {s['triggers']}회, 요청 {s['requests']}, 체결 {s['fills']}, "
                     f"미체결(체결 봉 결측 등) {s['unfilled']}, 판단 불가(감지 봉 결측) {s['not_judged']}, 요청 안 함 "
                     f"{s['skipped']}, 손절가 없음 {s['no_stop_price']}. 체결 = 감지 다음 분 시가 × (1 − 슬리피지). "
                     f"손절 초과 손실(손절가 − 체결가)/손절가: {sm.get('count', 0)}건, 손절가 아래 체결 {sm.get('gaps', 0)}, "
                     f"평균 {sm.get('mean_ratio')}, 중앙 {sm.get('median_ratio')}, 90% {sm.get('p90_ratio')}, 최대 "
                     f"{sm.get('max_ratio')}. 주문 창 밖 UTC 23:50~00:10은 감시 없음(실운영과 같음, DP-89 (6))")
    else:
        lines.append("- 손절 감시: 해당 없음(CAP-HLD-02 미선언) — 손절 초과 손실 칸 비어 있음")
    return lines


def _symbol_lines(cfg: MinuteBacktestConfig, out: MinuteBacktestResult) -> list[str]:
    if len(cfg.symbols) <= 1:
        return []
    rows = ["", "## 심볼별 (D-275 (7) — 한 계좌, 합산은 위 지표)", "| 심볼 | 거래 | 손익(USDT) | 결측 분 | C-7 |", "|---|---|---|---|---|"]
    for s, x in out.by_symbol.items():
        rows.append(f"| {s} | {x['trades']} | {x['pnl']} | {x['missing_bars']} | 사후 선택 — 과대 추정 가능 |")
    return rows


def _daily_check_lines(out: MinuteBacktestResult) -> list[str]:
    if out.daily_check is None:
        return []
    return ["- 1d 파일 vs 1분봉 일 집계 일치(D-276 (5), 판정에 쓰지 않음): "
            + "; ".join(f"{s} 비교 {x['compared']}일·불일치 {x['mismatch']}일(최대 가격 차 {x['max_price_diff']}, 거래량 차 "
                        f"{x['max_volume_diff']})·1분봉 불완전(비교 불가) {x['minute_incomplete']}일·1d 없음 {x['no_daily']}일"
                        for s, x in out.daily_check.items())]


def render_minute_report(cfg: MinuteBacktestConfig, out: MinuteBacktestResult, *, reproducible: bool | None,
                         lookahead_bad: list[str] | None) -> str:
    spec = cfg.strategy.spec
    r = out.result
    grid = grid_of(cfg)
    syms = cfg.symbols
    sym_text = cfg.symbol if len(syms) == 1 else f"{', '.join(syms)} (한 계좌)"
    daily_text = ("1d 봉 입력 없음(분봉 전략 입력만)" if DAILY_CAPABILITY not in spec.capabilities else
                  f"확정 1d 봉(1d 파일, 최근 {cfg.daily_limit}개, DP-82 결측 규칙) 입력")
    lines = [
        f"# M2 1분봉 백테스트 보고서 — {spec.strategy_id} v{spec.version}", "",
        f"- 실행 ID: `{cfg.run_id}` / 기간 {r.days[0]} ~ {r.days[-1]} ({len(r.days)}일, UTC) / 심볼 {sym_text} / "
        f"데이터 버전 `{cfg.data_version or '미기재'}` / 코드 `{cfg.code_commit or '미기재'}`",
        f"- 판정(D-219·실운영과 같은 격자): {grid.period_minutes}분마다 UTC 00:00 + k×N분 + 여유 {cfg.margin.total_seconds():g}초, "
        f"주문 창 00:10~23:50 안 — 판정 {out.decisions}회, 분봉 창 {cfg.lookback_bars}봉"
        + (f", 분봉 입력 무효 판정 {out.minute_invalid}회" if MINUTE_CAPABILITY in spec.capabilities else ""),
        f"- 체결(D-221): 판정 시각이 속한 분의 봉 시가 기준. 매수 = 시가 × (1 + 슬리피지 {cfg.slippage}) tick 올림이 IOC 지정가"
        f"(최근가 = 직전 닫힌 봉 종가 × (1 + 완충 {cfg.buy_buffer}) tick 내림) 이하일 때만, 매도 = 시가 × (1 − 슬리피지) tick 내림, "
        f"봉 거래량 {cfg.volume_cap:%} 상한, 체결 봉 결측 → 미체결({out.missing_fill}건)",
        *(fee_model_lines(cfg.fees, r.costs) if cfg.fees is not None else
          [f"- 비용: 수수료율 {cfg.cost_rates.commission_rate}(받는 자산에서 차감, BN §9 — 실제 요율 미확인), 비용 합계 {r.costs} USDT"]),
        f"- 데이터: 실행 기간 봉 {out.bars_in_period}개, 결측 분 {out.missing_bars}개(채우지 않음 — 판정 창 안이면 무효)",
        f"- 단순 보유 참고(C-5): {hold_return_of(out, cfg.slippage, cfg.fill_rate)}",
        f"- K2 다음 날 재개 가정: {r.k2_resumes}회 / 실행 시간 {out.seconds}초",
        *_intraday_lines(out), *_daily_check_lines(out), "",
        "## 지표 (비용 반영 후, 연환산 365일)", "| 지표 | 값 |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in r.metrics.items()],
        *_symbol_lines(cfg, out), "",
        "## 한계",
        f"- 데이터 출처: {cfg.data_source or '바이낸스 공개 실서버 1m klines(A-110)'} — 테스트넷 PAPER 성과와 직접 비교하지 않는다(2-M2.6).",
        f"- 거래 규칙은 테스트넷 실측값(실서버 과거 시점 규칙 아님). {daily_text}.",
        (f"- {cfg.symbol} 단일 심볼은 사후 선택 — 성과는 과대 추정 가능(C-7)." if len(syms) == 1 else
         f"- {', '.join(syms)} 각각 사후 선택 — 성과는 과대 추정 가능(C-7, 심볼별)."), "",
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
        "interval": "1m", "symbol": cfg.symbol if len(cfg.symbols) == 1 else ",".join(cfg.symbols),
        "symbols": list(cfg.symbols), "period": [r.days[0].isoformat(), r.days[-1].isoformat()],
        "days": len(r.days), "period_minutes": grid_of(cfg).period_minutes, "lookback_bars": cfg.lookback_bars,
        "margin_s": cfg.margin.total_seconds(), "decisions": out.decisions, "minute_invalid": out.minute_invalid,
        "bars_in_period": out.bars_in_period, "missing_bars": out.missing_bars, "missing_fill": out.missing_fill,
        "data_version": cfg.data_version, "final_from": cfg.final_from.isoformat(),
        "fee_rate": str(cfg.fill_rate), "slippage": str(cfg.slippage),
        "initial_cash": str(cfg.initial_cash), "signals": len(r.signals),
        "fills": {side: sum(1 for f in r.fills if f[3] == side) for side in ("BUY", "SELL")},
        "round_trips": len(r.trades), "rejects": dict(sorted(r.rejects.items())), "k2_resumes": r.k2_resumes,
        "buy_hold_return": hold_return_of(out, cfg.slippage, cfg.fill_rate),
        "reproducible": reproducible, "lookahead_bad": lookahead_bad, "digest": r.digest, "metrics": r.metrics,
        "seconds": out.seconds, "k2": out.k2, "stop": out.stop, "by_symbol": out.by_symbol,
        "daily_check": out.daily_check, **out.extras,
        **({"fees": fee_model_summary(cfg.fees)} if cfg.fees is not None else {}),
    }


# --- 검증 (D-222) ------------------------------------------------------------------------------------------------

def m2_windows() -> tuple[Window, ...]:
    """D-222 (A-113 Q-5 (가)): 학습 3년·검증 1년·1년 이동, 검증 2020~2025 6창."""
    return walkforward_windows(first_train_year=2017, train_years=3, n=6)


def _expectancy(trades: list) -> Decimal | None:  # type: ignore[type-arg]
    return sum((t.pnl for t in trades), _ZERO) / len(trades) if trades else None


def _fmt(x: Decimal | None) -> str | None:
    return None if x is None else format(x.quantize(Decimal("0.0001")), "f")


def c7_text(symbols: Sequence[str]) -> str:
    if tuple(symbols) == ("BTCUSDT",):
        return "해당 — BTCUSDT 단일 심볼 사후 선택(과대 추정 가능)"
    return f"해당 — {', '.join(symbols)} 각각 사후 선택(과대 추정 가능, 심볼별)"


def decide_verdict_m2(*, pooled_trades: int, c3_expectancy: Decimal | None, c4: Sequence[Decimal | None],
                      variants_run: bool, reproducible: bool, lookahead_ok: bool,
                      symbols: Sequence[str] = ("BTCUSDT",)) -> dict[str, object]:
    """CP-030 판정 규칙(D-167 C-2·D-164 C-3·D-165 C-4·D-168 C-7)의 M2 1분봉판. 금액 단위 USDT. 사후 선택 심볼(BTC·ETH 고정
    대상도 사후 선택) → 통과여도 "통과(과대 추정 가능)". C-2 = 전략 합산 거래 수(D-275 (7)). 판정표는 기록·제안이며 전환
    승인은 사용자."""
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
    return {"verdict": verdict, "C-2": c2, "C-3": c3, "C-4": c4s, "C-7": c7_text(symbols), "reasons": reasons}


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
    low_frequency: lfm.LowFrequencyInputs | None = None   # [CP-038] 저빈도 선언 전략만. None = 일반 경로


def _window_intraday(r: MinuteBacktestResult) -> dict:
    return {"k2_days": r.k2.get("days", 0), "k2_close_days": r.k2.get("close_days", 0),
            "k2_rejected_entries": r.k2.get("rejected_entries", {}), "stop_fills": r.stop.get("fills", 0),
            "stop_excess": [x["ratio"] for x in r.stop.get("excess", [])]}


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
    base_runs: list[tuple[Window, MinuteBacktestResult]] = []
    excess: list[dict] = []
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
        base_runs.append((w, base))
        base_trades += base.result.trades
        slip2_trades += slip2.result.trades
        excess += base.stop.get("excess", [])
        row = {
            "k": w.k, "train": [w.train_start.isoformat(), w.train_end.isoformat()],
            "train_shortened": w.train_start < inp.data_start,
            "train_effective_start": max(w.train_start, inp.data_start).isoformat(),
            "validation": [w.val_start.isoformat(), w.val_end.isoformat()],
            "trades": len(base.result.trades), "expectancy": _fmt(_expectancy(base.result.trades)),
            "total_return": base.result.metrics["total_return"], "max_drawdown": base.result.metrics["max_drawdown"],
            "slip2x_trades": len(slip2.result.trades), "slip2x_expectancy": _fmt(_expectancy(slip2.result.trades)),
            "missing_bars": base.missing_bars, "minute_invalid": base.minute_invalid, "decisions": base.decisions,
            "buy_hold_return": hold_return_of(base, inp.slippage_base, t.fill_rate),
            "reproducible": repro, "lookahead_bad": bad, "variants": vrows, "seconds": base.seconds,
            "digest": base.result.digest, "intraday": _window_intraday(base),
        }
        if len(t.symbols) > 1:
            row["by_symbol"] = {s: {"trades": x["trades"], "pnl": x["pnl"], "missing_bars": x["missing_bars"]}
                                for s, x in base.by_symbol.items()}
        windows_out.append(row)
    verdict = decide_verdict_m2(pooled_trades=len(base_trades), c3_expectancy=_expectancy(slip2_trades),
                                c4=[_expectancy(v) for v in var_trades.values()], variants_run=not inp.skip_variants,
                                reproducible=all_repro, lookahead_ok=all_lookahead, symbols=t.symbols)
    out = {
        "strategy": t.strategy.spec.strategy_id, "version": t.strategy.spec.version,
        "symbol": t.symbol if len(t.symbols) == 1 else ",".join(t.symbols), "symbols": list(t.symbols),
        "data_version": t.data_version, "data_start": inp.data_start.isoformat(), "final_from": t.final_from.isoformat(),
        "slippage_base": str(inp.slippage_base), "slippage_double": str(inp.slippage_base * 2),
        "period_minutes": grid_of(t).period_minutes, "lookback_bars": t.lookback_bars,
        "windows": windows_out, "pooled": {"trades": len(base_trades), "expectancy": _fmt(_expectancy(base_trades)),
                                           "slip2x_trades": len(slip2_trades),
                                           "slip2x_expectancy": _fmt(_expectancy(slip2_trades)),
                                           "missing_bars": sum(w["missing_bars"] for w in windows_out),
                                           "loss_windows": sum(1 for w in windows_out
                                                               if Decimal(w["total_return"]) < 0),
                                           "k2_days": sum(w["intraday"]["k2_days"] for w in windows_out),
                                           "k2_close_days": sum(w["intraday"]["k2_close_days"] for w in windows_out),
                                           "stop_excess": _excess_summary(excess)},
        "intraday_note": {"stop_active": STOP_CAPABILITY in t.strategy.spec.capabilities,
                          "daily_input": DAILY_CAPABILITY in t.strategy.spec.capabilities},
        "variants": [{"label": k, "trades": len(v), "expectancy": _fmt(_expectancy(v))} for k, v in var_trades.items()],
        "verdict": verdict, "command": inp.command,
    }
    if inp.low_frequency is not None:   # [CP-038 D-234] 같은 실행 묶음에서 추가 판정 — 선언 없는 전략은 이 칸 자체가 없다
        out["low_frequency"] = run_low_frequency_m2(inp, verdict, base_runs, variants, reproducible=all_repro,
                                                    lookahead_ok=all_lookahead)
    md = render_minute_validation(out)
    if not lfm.table_valid(md, out):
        raise BacktestError("저빈도 예외 표시가 빠진 판정표 — 무효 (CP-038 D-240)")
    inp.out_dir.mkdir(parents=True, exist_ok=True)
    (inp.out_dir / "validation.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                                newline="\n")
    (inp.out_dir / "validation.md").write_text(md, encoding="utf-8", newline="\n")
    return out


# --- [DP-81, A-124 ④] 최종 확인 실행 (D-163, V-295) -------------------------------------------------------------------

def run_minute_final_check(template: MinuteBacktestConfig, out_dir: Path, *, final_from: date, end: date, ledger: Path,
                           confirmed_by: str, slippage_base: Decimal = SLIPPAGE_BASE) -> dict:
    """M1·일봉 경로(app/validation.run_final_check)와 같은 방식: 전략 확정(사용자 선언 A-ID) 뒤 전략 버전당 1회, final_from ~
    end를 기본·2배 슬리피지로 실행. 입력을 모두 검사한 뒤에만 장부(백테스트 FINAL과 같은 JSONL)에 기록하고 실행한다 —
    실패한 입력은 1회 사용을 소모하지 않는다. 검사 통과 뒤 실행 중 실패는 1회 사용으로 남는다(M1과 같음)."""
    if end < final_from:
        raise ValidationError(f"종료일 {end}가 최종 확인 시작일 {final_from} 전")
    check_final_guard(final_from, end, final_from, final_check=True)
    if out_dir.exists():
        raise ValidationError(f"결과 폴더가 이미 있음: {out_dir}")
    if not _LEDGER_APPROVAL.fullmatch(confirmed_by or ""):
        raise ValidationError("최종 확인은 전략 확정(사용자 선언 A-ID) 뒤에만 실행 (D-163)")
    if not isinstance(slippage_base, Decimal) or slippage_base < 0:
        raise ValidationError("슬리피지는 0 이상")
    probe = replace(template, final_from=final_from, final_check=True, start=final_from, end=end,
                    out_dir=out_dir / "runs")
    grid_of(probe)
    _days(probe)   # 입력 검사(BacktestError) — 장부 기록 전
    claim_final_check(ledger, template.strategy.spec, out_dir.name, confirmed_by=confirmed_by)
    out_dir.mkdir(parents=True)
    rows: dict[str, dict] = {}
    last: MinuteBacktestResult | None = None
    for label, s in (("final-base", slippage_base), ("final-slip2x", slippage_base * 2)):
        last = run_minute_backtest(replace(probe, run_id=label, slippage=s))
        rows[label] = {"metrics": dict(last.result.metrics), "trades": len(last.result.trades),
                       "expectancy": _fmt(_expectancy(last.result.trades)), "digest": last.result.digest,
                       "slippage": format(s, "f"), "missing_bars": last.missing_bars,
                       "intraday": _window_intraday(last)}
    assert last is not None
    res = {"strategy_id": template.strategy.spec.strategy_id, "strategy_version": template.strategy.spec.version,
           "symbols": list(template.symbols), "confirmed_by": confirmed_by,
           "start": last.result.days[0].isoformat(), "end": last.result.days[-1].isoformat(), **rows}
    (out_dir / "final-check.json").write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                              newline="\n")
    return res


# --- [CP-038 T-058] 저빈도 예외 경로 (D-234~D-245) -------------------------------------------------------------------

def lf_history_runs(template: MinuteBacktestConfig, *, start: date, end: date, out_dir: Path, slippage_base: Decimal,
                    multiplier: int, variants: Sequence[object], only_base: bool = False) -> dict[str, MinuteBacktestResult]:
    """전체 이력(데이터 시작 + 워밍업 ~ final_from 전날) 연속 1회 실행: 기본·슬리피지 배수·±20% 변형(D-165 규칙 그대로).
    끝 날짜가 final_from 이후면 실행기가 거부한다(C-8·D-163). 원천은 끝 + 1일 전 봉만 넘긴다(2026 행 미전달, V-308)."""
    def one(label: str, slip: Decimal, params: dict | None = None) -> MinuteBacktestResult:
        return run_minute_backtest(replace(template, run_id=f"lf-history-{label}", start=start, end=end, slippage=slip,
                                           out_dir=out_dir, params=dict(template.params if params is None else params)))
    out = {"base": one("base", slippage_base)}
    if only_base:
        return out
    out["slip2x"] = one("slip2x", slippage_base * multiplier)
    for v in variants:
        out[v.label] = one(v.label, slippage_base, v.params)  # type: ignore[attr-defined]
    return out


@dataclass
class Lf8RunResult:
    trades: list
    hold_return: Decimal | None
    blocked_entries: int = 0
    delisted: int = 0          # 보유 중 데이터 끝 → 마지막 종가 × haircut 청산으로 집계한 거래 수
    open_at_end: int = 0       # 전체 이력 끝(2025-12-31) 보유 — C-2와 같이 미청산 제외


def _usdt_base(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") and len(symbol) > 4 else symbol


def run_lf8_sample(template: MinuteBacktestConfig, settings: lfm.LowFreqSettings, *, alias: str, symbol: str,
                   sample: lfm.Lf8Sample, history_end: date, slippage: Decimal, out_dir: Path,
                   label: str = "base") -> Lf8RunResult:
    """D-244 (3)~(5): 표본 심볼 하나를 단독 계좌로. 배포 전략과 같은 코드·파라미터·한도 프로필, 표본 봉을 배포 심볼 이름
    (alias — 전략 유니버스 안)으로 넣는다(전략 코드 무변경, DP-86). 목록 해 밖 신규 진입·증액 차단, 보유는 청산 신호까지.
    데이터가 전체 이력 끝 전에 끝나면 남은 보유를 마지막 종가 × haircut으로 청산한 거래로 집계(D-244 (4)).
    [T-068] 여러 심볼 배포 전략도 별칭 하나씩 단독 실행(sources 없음). CAP-DAT-01 전략은 표본 1d 봉(sample.daily)이 필요.
    요율은 체결 요율(taker, D-267 (3) — 일반 경로와 같음)."""
    start = max(date(min(sample.years), 1, 1), sample.data_start)
    end = min(sample.data_end, history_end)
    wrapper = lfm.EntryYears(template.strategy, sample.years)
    if DAILY_CAPABILITY in template.strategy.spec.capabilities and sample.daily is None:
        raise BacktestError(f"LF-8 표본 {symbol}: 일봉 전략인데 표본 1d 봉 없음 (D-276·D-244)")
    cfg = replace(template, run_id=f"lf8-{alias}-{symbol}-{label}", symbol=alias, sources=None,
                  daily=None if sample.daily is None else {alias: tuple(sample.daily)},
                  rules={alias: lfm.continuous_rules(alias)}, pairs={alias: (_usdt_base(symbol), "USDT")},
                  source=sample.source, start=start, end=end, slippage=slippage, out_dir=out_dir,
                  strategy=wrapper)  # type: ignore[arg-type]
    out = run_minute_backtest(cfg)
    res = Lf8RunResult(list(out.trade_records), None, wrapper.blocked)
    lot = out.open_lots.get(alias)
    fee = cfg.fill_rate
    if lot is not None:
        if sample.data_end < history_end and out.end_close is not None:
            res.trades.append(lfm.close_out(lot, out.end_close, fee, settings.lf8_delist_haircut, end))
            res.delisted = 1
        else:
            res.open_at_end = 1
    hold = buy_hold_return(out.first_open, out.end_close, slippage, fee)
    res.hold_return = None if hold is None else Decimal(hold)
    return res


def _c5_plus(r: MinuteBacktestResult, slippage: Decimal, fee: Decimal, initial: Decimal) -> dict:
    """C-5+ 참고(판정 제외): 전략 vs 단순 보유 총수익률·MDD·보유일 비율(근사)·진입 비중 중앙/최대."""
    eq = dict(r.result.equity)
    days = len(r.result.days) or 1
    weights: list[Decimal] = []
    held_days = 0
    for t in r.trade_records:
        prev = eq.get(t.entry_day - timedelta(days=1), initial)
        if prev > 0:
            weights.append(t.entry_amount / prev)
        held_days += (t.exit_day - t.entry_day).days + 1
    weights.sort()
    med = weights[len(weights) // 2] if weights else None
    q = Decimal("0.0001")
    return {"total_return": r.result.metrics["total_return"], "max_drawdown": r.result.metrics["max_drawdown"],
            "buy_hold_return": hold_return_of(r, slippage, fee),
            "held_day_ratio": format((Decimal(held_days) / Decimal(days)).quantize(q), "f"),
            "held_day_ratio_note": "진입일~청산일 포함 일수 합 ÷ 기간 일수(근사 — 같은 날 왕복도 1일)",
            "entry_weight_median": None if med is None else format(med.quantize(q), "f"),
            "entry_weight_max": format(weights[-1].quantize(q), "f") if weights else None}


def run_low_frequency_m2(inp: MinuteValidationInputs, verdict: dict, base_runs: list[tuple[Window, MinuteBacktestResult]],
                         variants: Sequence[object], *, reproducible: bool, lookahead_ok: bool) -> dict:
    """D-234: 일반 판정 뒤 (C-2만 판단 불가 + E-1~E-5) → 전체 이력·변형·LF-8 표본 실행 → LF-0~LF-8 → 문구. 적격이면
    verdict["verdict"]를 종합 문구로 바꾸고 일반 판정은 general_verdict에 남긴다. 적격이 아니면 verdict는 그대로."""
    low = inp.low_frequency
    assert low is not None
    s, t = low.settings, inp.template
    spec = t.strategy.spec
    lfm.check_ledger(low.ledger, spec.strategy_id, spec.version, low.params_sha256, s.sha256)
    head = {"label": lfm.LABEL, "cp": s.cp, "cp_version": s.cp_version, "approved_by": s.approved_by,
            "settings_sha256": s.sha256, "history_period": [low.history_start.isoformat(), low.history_end.isoformat()],
            "declaration": None if low.declaration is None else {
                "version": low.declaration.version, "annual_range": [str(low.declaration.annual_min),
                                                                     str(low.declaration.annual_max)],
                "reasons": list(low.declaration.reasons), "commit": low.declaration.commit,
                "committed_at": None if low.declaration.committed_at is None
                else low.declaration.committed_at.isoformat()}}
    kw = dict(market=low.market, strategy_id=spec.strategy_id, version=spec.version, declaration=low.declaration,
              declaration_problems=low.declaration_problems, first_official_at=low.first_official_at, general=verdict,
              reproducible=reproducible, lookahead_ok=lookahead_ok, limits_profile=t.account_limits.profile,
              limits_approved_by=t.account_limits.approved_by, windows=len(inp.windows))
    elig = lfm.check_eligibility(s, history_trades=None, history_days=None, **kw)  # type: ignore[arg-type]

    def done(result: dict) -> dict:
        lfm.record_ledger(low.ledger, spec.strategy_id, spec.version, low.params_sha256, s.sha256, s.cp_version)
        return result
    if not elig["pre_ok"]:
        return done({"applied": False, **head, "eligibility": elig})
    hist_dir = inp.out_dir / "runs"
    first = lf_history_runs(t, start=low.history_start, end=low.history_end, out_dir=hist_dir,
                            slippage_base=inp.slippage_base, multiplier=s.lf6_slippage_multiplier, variants=[],
                            only_base=True)["base"]
    hist_days = (low.history_end - low.history_start).days + 1
    elig = lfm.check_eligibility(s, history_trades=len(first.trade_records), history_days=hist_days,
                                 **kw)  # type: ignore[arg-type]
    history_info = {"trades": len(first.trade_records), "days": hist_days, "missing_bars": first.missing_bars,
                    "digest": first.result.digest}
    if not elig["eligible"]:
        return done({"applied": False, **head, "eligibility": elig, "history": history_info})
    rest = {v.label: run_minute_backtest(replace(t, run_id=f"lf-history-{v.label}", start=low.history_start,  # type: ignore[attr-defined]
                                                 end=low.history_end, slippage=inp.slippage_base, out_dir=hist_dir,
                                                 params=dict(v.params)))  # type: ignore[attr-defined]
            for v in variants}
    slip2 = run_minute_backtest(replace(t, run_id="lf-history-slip2x", start=low.history_start, end=low.history_end,
                                        slippage=inp.slippage_base * s.lf6_slippage_multiplier, out_dir=hist_dir,
                                        params=dict(t.params)))
    fee = t.fill_rate   # [재검토 지적] 체결 요율(taker, D-267 (3)) — 일반 경로 buy_hold_return과 같은 요율
    windows = [_window_lf_simple(w, r, inp.slippage_base, fee, t.initial_cash) for w, r in base_runs]
    runs8: dict[str, dict[str, lfm.Lf8SymbolResult]] | None = None
    lf8_counts: dict[str, int] = {}
    if low.lf8 is not None and low.lf8.samples:
        runs8 = {}
        mult = s.lf8_slippage_multiplier
        for alias in low.lf8.aliases:
            runs8[alias] = {}
            for sym, smp in sorted(low.lf8.samples.items()):
                a = run_lf8_sample(t, s, alias=alias, symbol=sym, sample=smp, history_end=low.history_end,
                                   slippage=inp.slippage_base, out_dir=inp.out_dir / "lf8", label="base")
                b = run_lf8_sample(t, s, alias=alias, symbol=sym, sample=smp, history_end=low.history_end,
                                   slippage=inp.slippage_base * mult, out_dir=inp.out_dir / "lf8", label="slip2x")
                runs8[alias][sym] = lfm.Lf8SymbolResult(a.trades, b.trades, a.hold_return)
                for k, n in (("blocked_entries", a.blocked_entries), ("delisted", a.delisted),
                             ("open_at_end", a.open_at_end)):
                    lf8_counts[k] = lf8_counts.get(k, 0) + n
    reason = low.lf8_no_data_reason or (lfm.NO_MINUTE_SAMPLE if "CAP-TRG-02" in spec.capabilities else "")
    lf8 = lfm.judge_lf8(s, runs8, deployed=first.trade_records,
                        list_sha256=low.lf8.list_sha256 if low.lf8 is not None else None, no_data_reason=reason)
    if lf8_counts:
        lf8["counts"] = lf8_counts
        lf8["notes"] = ["표본 수량은 연속값(단위 1e-8)·최소 주문 5 USDT — 실제 거래 규칙 아님(D-244 (5))",
                        "표본 봉은 배포 심볼 이름으로 넣어 같은 코드·파라미터로 실행(DP-86)"]
    assert low.declaration is not None
    judged = lfm.judge_lf(s, declaration=low.declaration, windows=windows, history=first.trade_records,
                          history_slip2=slip2.trade_records, variants={k: v.trade_records for k, v in rest.items()},
                          variants_run=not inp.skip_variants, lf8=lf8, c7=True)
    boot = lfm.bootstrap_reference(s, [x.log_return for x in first.trade_records],
                                   lfm.seed_of(spec.strategy_id, spec.version))
    verdict["general_verdict"] = verdict["verdict"]
    verdict["verdict"] = judged["verdict"]
    verdict["label"] = lfm.LABEL
    history_info.update({"slip2x_trades": len(slip2.trade_records),
                         "variants": {k: len(v.trade_records) for k, v in rest.items()},
                         "c5_plus": _c5_plus(first, inp.slippage_base, fee, t.initial_cash)})
    return done({"applied": True, **head, "eligibility": elig, "history": history_info, "items": judged["items"],
                 "lf8": lf8, "LF-R": boot, "verdict": judged["verdict"], "reasons": judged["reasons"],
                 "windows_c5_plus": [{"k": w.k, **_c5_plus(r, inp.slippage_base, fee, t.initial_cash)}
                                     for w, r in base_runs]})


def _window_lf_simple(w: Window, r: MinuteBacktestResult, slippage: Decimal, fee: Decimal,
                      initial: Decimal) -> lfm.WindowLF:
    """창별 LF 입력: 그 창에서 청산된 거래(창 실행 거래), 단순 보유 수익률(C-5 방식 — 일반 경로와 같은 함수·요율), 창 끝 미청산
    평가 손익 = 마지막 평가액 − 초기 자금 − 청산 거래 손익 합 (표시만)."""
    hold = hold_return_of(r, slippage, fee)
    pnl = sum((x.pnl for x in r.trade_records), _ZERO)
    final = r.result.equity[-1][1] if r.result.equity else initial
    return lfm.WindowLF(w.k, list(r.trade_records), None if hold is None else Decimal(hold),
                        (final - initial - pnl).quantize(Decimal("0.00000001")))


def _intraday_summary_lines(o: dict) -> list[str]:
    p = o["pooled"]
    note = o.get("intraday_note", {})
    ex = p.get("stop_excess", {})
    lines = [f"- 장중 L4(D-265·D-277, 1분 봉 저가 기준): K2 발동 합계 {p.get('k2_days', 0)}일, 참고 종가 기준 "
             f"{p.get('k2_close_days', 0)}일 — K2 재개 = 다음 UTC 일 첫 분(낙관). 창별 값은 validation.json intraday"]
    if note.get("stop_active"):
        lines.append(f"- 손절 감시(CAP-HLD-02, D-262 — 체결 = 감지 다음 분 시가): 손절 초과 손실 {ex.get('count', 0)}건, "
                     f"손절가 아래 체결 {ex.get('gaps', 0)}, 평균 비율 {ex.get('mean_ratio')}, 최대 {ex.get('max_ratio')}")
    else:
        lines.append("- 손절 감시: 해당 없음(CAP-HLD-02 미선언)")
    if note.get("daily_input"):
        lines.append("- 확정 일봉 입력(D-276): 1d 파일·최근 199봉·DP-82 결측 규칙 — 일치 보고는 창별 실행 summary")
    return lines


def render_minute_validation(o: dict) -> str:
    v = o["verdict"]
    low = o.get("low_frequency")
    applied = bool(low and low.get("applied"))
    banner = ([f"- **{lfm.LABEL}** — C-7 생존편향 표시: {v['C-7']} / {low['cp']} {low['cp_version']}(승인 "
               f"{low['approved_by']}) 설정 SHA-256 `{low['settings_sha256']}` / 일반 판정 {v['general_verdict']} "
               "(C-2만 미달) → 대체 증거 경로. 일반 '통과'와 섞어 세지 않는다(D-240)"] if applied else [])
    lines = [f"# M2 1분봉 검증 판정표 — {o['strategy']} v{o['version']} ({o['symbol']})", "", *banner,
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
    if len(o.get("symbols", [])) > 1:
        lines += ["", "### 심볼별 (D-275 (7) — 한 계좌 실행, C-2는 합산)", "| 창 | 심볼 | 거래 | 손익(USDT) | 결측 분 |",
                  "|---|---|---|---|---|"]
        for w in o["windows"]:
            for s, x in w.get("by_symbol", {}).items():
                lines.append(f"| {w['k']} | {s} | {x['trades']} | {x['pnl']} | {x['missing_bars']} |")
    p = o["pooled"]
    lines += ["", "## 판정 (CP-030 M2판 — 기록·제안, 전환 승인은 사용자)", "",
              f"- C-1 비용: 수수료·슬리피지 반영 결과만 — 적용",
              f"- C-2 거래 수 ≥ {MIN_TRADES}: 합산 {p['trades']}건 — {v['C-2']}",
              f"- C-3 슬리피지 2배 거래당 기대값 > 0: {p['slip2x_expectancy'] or '—'} USDT ({p['slip2x_trades']}건) — {v['C-3']}",
              f"- C-4 파라미터 ±20%(period_minutes·qty_step·집계 봉 구조값 제외): "
              + (", ".join(f"{x['label']} {x['expectancy'] or '—'}" for x in o["variants"]) or "변형 없음")
              + f" — {v['C-4']}",
              "- C-5 단순 보유: 창별 참고 값(validation.json buy_hold_return) — 참고",
              "- C-6 무작위 진입: 삭제(A-083) — 미실행",
              f"- C-7 생존편향: {v['C-7']}",
              "- C-8 최종 확인: 이 실행에서 미사용 — 해당 없음(최종 확인은 --final-check, 버전당 1회)",
              f"- 창별 결측 분 합계 {p['missing_bars']}, 손실 창 {p['loss_windows']}/{len(o['windows'])}",
              *_intraday_summary_lines(o),
              f"- **판정: {v['verdict']}**" + (f" ({lfm.LABEL})" if applied else ""),
              *[f"- 사유: {x}" for x in v["reasons"]],
              "- 공개 실서버 성과는 테스트넷 PAPER 성과와 직접 비교하지 않는다(2-M2.6). PAPER 신호 일치율은 PAPER 중 저장한 "
              "테스트넷 봉으로 다시 계산(D-222)."]
    if low:
        lines += render_low_frequency(low, v)
    return "\n".join(lines) + "\n"


def _status_line(k: str, item: dict) -> str:
    extra = {kk: vv for kk, vv in item.items() if kk not in ("status", "reason", "variants", "windows", "items",
                                                             "by_alias", "reference", "notes", "counts")}
    tail = f" — {item['reason']}" if item.get("reason") else ""
    return f"| {k} | {item.get('status')} | {json.dumps(extra, ensure_ascii=False, default=str)}{tail} |"


def render_low_frequency(low: dict, v: dict) -> list[str]:
    """[CP-038 D-240] 판정표의 저빈도 예외 절. 적용(적격)이면 LF-0~LF-8·LF-R·C-5+, 아니면 적격 검사 결과만."""
    e = low["eligibility"]
    head = [f"- {low['cp']} {low['cp_version']}(승인 {low['approved_by']}) · 설정 SHA-256 `{low['settings_sha256']}` · "
            f"전체 이력 {low['history_period'][0]}~{low['history_period'][1]}",
            f"- 적격 검사(D-235): {e['result']} — " + ", ".join(f"{k} {x}" for k, x in e["items"].items())
            + (f" / 전체 이력 연평균 거래 {e['annual_trades']} (기준 < {e['threshold']})" if e.get("annual_trades") else ""),
            *[f"- 적격 사유: {x}" for x in e["reasons"]]]
    if not low.get("applied"):
        return ["", f"## {lfm.LABEL} — 열지 않음 (일반 판정 그대로)", "", *head]
    items = low["items"]
    out = ["", f"## {lfm.LABEL} 경로 (D-234·D-236 — 기록·제안, 전환 승인은 사용자)", "", *head,
           f"- 일반 판정(04 §2.9): {v['general_verdict']} / **종합: {v['verdict']}**", "",
           "| 항목 | 결과 | 값 |", "|---|---|---|", *[_status_line(k, x) for k, x in items.items()], ""]
    lf5 = items.get("LF-5", {})
    for w in lf5.get("windows", []):
        out.append(f"- LF-5 창 {w['k']}: 단순 보유 {w['hold_return']}, 거래 {w['trades']}건 로그 합 {w['log_sum']} "
                   f"vs ln(1+보유) {w['threshold']} — {'충족' if w['met'] else '미충족'}, 창 끝 미청산 평가 손익 "
                   f"{w['open_pnl']}")
    for r in items.get("LF-7", {}).get("variants", []):
        out.append(f"- LF-7 {r['label']}: 거래 {r['trades']}건, 로그 평균 {r['mean_log']}, 손익 평균 {r['mean_pnl']} — "
                   f"{r['status']}")
    lf8 = low["lf8"]
    out.append(f"- LF-8 교차 자산(D-244·D-245, 역할 {lf8['role']}): {lf8['status']}"
               + (f" — {lf8['reason']}" if lf8.get("reason") else "")
               + f" / 표본 목록 SHA-256 `{lf8.get('list_sha256') or '없음'}`")
    if lf8.get("items"):
        out.append("  - " + ", ".join(f"{k} {x}" for k, x in lf8["items"].items())
                   + f" / 참고 배포 ÷ 표본 거래당 로그 수익 {lf8['reference']['deployed_over_sample']}")
    for n in lf8.get("notes", []):
        out.append(f"  - {n}")
    b = low["LF-R"]
    out += [f"- LF-R 부트스트랩(참고 — 판정 제외, D-242): 시드 `{b['seed']}`, {b['draws']}회, n {b['n']}, p⁺ {b['p_plus']}, "
            f"거래당 평균 로그 수익 {b['quantile']} 분위 {b['q_mean']} — {b['limits']}",
            f"- C-5+ 단순 보유 확장(참고 — 판정 제외): 전체 이력 {json.dumps(low['history']['c5_plus'], ensure_ascii=False)}",
            "- 보완 통제(D-238~D-240): PAPER-1 최소 90 UTC일 + 왕복 1건(상한 180일), LIVE 첫 규모 일반의 50% 이하. "
            "이 판정표는 PAPER·LIVE 승인이 아니다."]
    return out


__all__ = ["MINUTE_COLUMNS", "MinuteBacktestConfig", "MinuteBacktestResult", "MinuteReadStats", "MinuteValidationInputs",
           "decision_points", "first_minute_start", "hold_return_of", "iter_binance_minute_csv", "last_minute_start",
           "lf_history_runs", "m2_windows", "minute_request", "perturbation_check_minute", "render_minute_report",
           "run_lf8_sample", "run_low_frequency_m2", "run_minute_backtest", "run_minute_final_check",
           "run_minute_validation", "summary_of"]
