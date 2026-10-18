"""M2 설정 파일 로더: 시장 설정·유니버스 (CP-024 D-097·D-104·D-105·D-115·D-116·§2.4, T-M2-08).

파일: `config/m2/market.<mode>.toml`, `config/m2/universe.<mode>.toml`. 한도는 `config/m2/limits.<mode>.toml`
(limits_file.load_limits_file(path, market="M2")), 앱은 `config/m2/app.<mode>.toml`(app_file, market = "M2").

market:
  [gate]   quote_max_age_seconds(정수 초, A-064 60), buy_buffer·sell_buffer(문자열 비율 0 이상 1 미만, A-064·A-065 Q4 0.5%),
           server_time_warn_ms(정수 밀리초, A-064 500)
  [costs]  (CP-040 D-267, T-065) maker_rate·taker_rate(문자열 비율, 둘 다) + rate_source("placeholder" |
           "account-commission" | "user", 필수). 기존 commission_rate는 maker·taker가 **둘 다 없을 때만** 읽어 maker = taker =
           그 값으로 본다(현 설정 파일 그대로 동작 — CP-036 D-216 방식). 이때 rate_source는 선택이며 없으면 "placeholder"
           (현 값 0.001은 "미확정 자리 값", A-115 Q-5 (가)). commission_rate와 maker·taker 키를 함께 쓰면 거부(셋 동시 포함),
           maker·taker 한쪽만 있어도 거부. 결과: fees(FeeModel), commission_rate = 비용 추정 요율 = max(maker, taker).
  [alerts] reject_surge_count·reject_surge_minutes (M1과 같은 형식)
  [minute] (선택, CP-037 D-220 T-056 전 별도 승인 — 표가 없으면 꺼짐) 테스트넷 1m klines 주기 조회 [DP-71]:
           enabled(bool, 기본 false), period_minutes(1440의 약수 정수 — 판정 격자 N분, 격자 시각 UTC 00:00 + k×N분이
           주문 창 00:10~23:50 안에 하나 이상 [DP-77] — 1440 거부), lookback_bars(1~997 — 판정 창),
           confirm_margin_seconds(0~59, 기본 2 [제안]), approval("A-숫자" — enabled = true면 필수, PAPER 시작 때
           docs/approvals.md 표에 그 행이 있어야 한다 — app/startup.m2_paper_problems).
  M1의 price_limit_ratio·sell_tax_rate는 M2에 없다(가격제한폭 없음, 매도세 0 — D-104, §2.6).
universe:
  quote_asset = "USDT" (계좌 기준 통화 A-055), symbols = ["<기초자산>USDT", ...] — 거래소 심볼 이름(대문자·숫자).
  구체 심볼은 PV-M2-03 뒤 사용자가 정한다(D-105, M2 PAPER-0 차단). 빈 목록은 읽을 수 있으나 시작 검사가 거부한다.
기본값은 없다. 누락·형식 오류·알 수 없는 키면 거부한다. 비밀정보는 넣지 않는다.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_system.domain.fees import RATE_SOURCES, FeeModel
from trading_system.domain.minute_series import ORDER_WINDOW, boundaries_in_window

from .errors import ConfigError
from .market_file import parse_alert_settings

QUOTE_ASSET = "USDT"
_SYMBOL = re.compile(r"[A-Z0-9]{1,20}")


@dataclass(frozen=True)
class Universe:
    quote_asset: str
    symbols: tuple[str, ...]

    def base_asset(self, symbol: str) -> str | None:
        """유니버스 심볼의 기초 자산(심볼 − 호가 자산 접미사). 유니버스 밖이면 None."""
        if symbol not in self.symbols:
            return None
        return symbol[: -len(self.quote_asset)]


@dataclass(frozen=True)
class M2MinuteSettings:
    """1m klines 주기 조회 설정 (CP-037 D-220). 기본 꺼짐 — 켜는 것은 사용자(별도 승인 A-ID)."""

    enabled: bool = False
    period_minutes: int | None = None
    lookback_bars: int | None = None
    confirm_margin_seconds: int = 2
    approval: str | None = None


MINUTE_KEYS = ("enabled", "period_minutes", "lookback_bars", "confirm_margin_seconds", "approval")
MAX_MINUTE_LOOKBACK = 997          # klines 한 쪽(limit 1000) − 여유 3 (broker/binance/feed.MAX_MINUTE_LOOKBACK과 같음)
_APPROVAL_ID = re.compile(r"A-\d{3,}")


@dataclass(frozen=True)
class M2MarketSettings:
    quote_max_age_seconds: int
    buy_buffer: Decimal
    sell_buffer: Decimal
    server_time_warn_ms: int
    commission_rate: Decimal               # 비용 추정 요율 = fees.estimate_rate = max(maker, taker) (D-267 (2))
    alerts: tuple[int, int]
    fees: FeeModel                         # 메이커·테이커 요율·출처 (D-267). 체결 모델 요율 = fees.fill_rate (taker)
    minute: M2MinuteSettings = M2MinuteSettings()


def _ratio(raw: object, name: str, problems: list[str]) -> Decimal | None:
    if isinstance(raw, bool) or not isinstance(raw, str):
        problems.append(f"{name}: 문자열 비율이어야 함 (부동소수 금지)")
        return None
    try:
        v = Decimal(raw)
    except InvalidOperation:
        problems.append(f"{name}: 소수 형식이 아님")
        return None
    if not v.is_finite() or v < 0 or v >= 1:
        problems.append(f"{name}: 범위 밖 (0 이상 1 미만)")
        return None
    return v


def _pos_int(raw: object, name: str, problems: list[str]) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        problems.append(f"{name}: 0보다 큰 정수")
        return None
    return raw


def _int_in(raw: object, name: str, lo: int, hi: int, problems: list[str]) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or not lo <= raw <= hi:
        problems.append(f"{name}: {lo}~{hi} 정수")
        return None
    return raw


def parse_minute(raw: object) -> M2MinuteSettings:
    """[minute] 표 (CP-037 D-220). 없으면 꺼짐. 형식 오류·알 수 없는 키면 거부."""
    if raw is None:
        return M2MinuteSettings()
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError("[minute]: 표여야 함")
    problems += [f"minute.{k}: 알 수 없는 키" for k in raw if k not in MINUTE_KEYS]
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        problems.append("minute.enabled: true/false")
        enabled = False
    period = lookback = None
    if "period_minutes" in raw or enabled:
        period = _int_in(raw.get("period_minutes"), "minute.period_minutes", 1, 1440, problems)
        if period is not None and 1440 % period:
            problems.append("minute.period_minutes: 1440의 약수여야 함 (UTC 00:00 기준 격자)")
        elif period is not None and boundaries_in_window(period) == 0:
            lo, hi = ORDER_WINDOW
            problems.append(f"minute.period_minutes: {period}분 격자 시각(UTC 00:00 + k×{period}분)이 주문 창 "
                            f"{lo:%H:%M}~{hi:%H:%M} 안에 없다 — 판정·분봉 조회가 일어나지 않는다. 720 이하 1440의 약수를 "
                            "쓴다 (DP-77)")
    if "lookback_bars" in raw or enabled:
        lookback = _int_in(raw.get("lookback_bars"), "minute.lookback_bars", 1, MAX_MINUTE_LOOKBACK, problems)
    margin = _int_in(raw.get("confirm_margin_seconds", 2), "minute.confirm_margin_seconds", 0, 59, problems)
    approval = raw.get("approval")
    if approval is not None and (not isinstance(approval, str) or not _APPROVAL_ID.fullmatch(approval)):
        problems.append("minute.approval: \"A-숫자\" 형식 (예 \"A-123\")")
        approval = None
    elif enabled and approval is None:
        problems.append("minute.enabled = true인데 approval(1분봉 주기 조회 승인 A-ID)이 없다 — 테스트넷 1m klines 주기 "
                        "조회는 별도 승인 범위 (CP-037 §4·T-056)")
    if problems:
        raise ConfigError(problems)
    return M2MinuteSettings(enabled, period, lookback, margin if margin is not None else 2, approval)


COST_KEYS = ("commission_rate", "maker_rate", "taker_rate", "rate_source")


def parse_costs(costs: dict, problems: list[str]) -> FeeModel | None:
    """[costs] → FeeModel (CP-040 D-267 (1)). 문제는 problems에 더하고 None."""
    new = [k for k in ("maker_rate", "taker_rate") if k in costs]
    source = costs.get("rate_source", None if new else "placeholder")
    if source not in RATE_SOURCES:
        problems.append(f"costs.rate_source: {' | '.join(RATE_SOURCES)} 중 하나 (maker_rate·taker_rate를 쓰면 필수)")
        source = None
    if new and "commission_rate" in costs:
        problems.append("costs: commission_rate와 maker_rate·taker_rate를 함께 쓸 수 없다 — 기존 키는 새 키가 둘 다 없을 "
                        "때만 읽는다 (D-267)")
        return None
    if new:
        if len(new) != 2:
            problems.append("costs: maker_rate·taker_rate는 둘 다 있어야 함 (D-267)")
            return None
        maker = _ratio(costs.get("maker_rate"), "costs.maker_rate", problems)
        taker = _ratio(costs.get("taker_rate"), "costs.taker_rate", problems)
    else:
        maker = taker = _ratio(costs.get("commission_rate"), "costs.commission_rate", problems)
    if maker is None or taker is None or source is None:
        return None
    return FeeModel(maker, taker, source)


def parse_m2_market(raw: dict) -> M2MarketSettings:
    problems: list[str] = []
    for k in raw:
        if k not in ("gate", "costs", "alerts", "minute"):
            problems.append(f"{k}: 알 수 없는 키")
    gate, costs = raw.get("gate"), raw.get("costs")
    if not isinstance(gate, dict):
        problems.append("[gate] 표가 없다")
        gate = {}
    if not isinstance(costs, dict):
        problems.append("[costs] 표가 없다")
        costs = {}
    problems += [f"gate.{k}: 알 수 없는 키" for k in gate
                 if k not in ("quote_max_age_seconds", "buy_buffer", "sell_buffer", "server_time_warn_ms")]
    problems += [f"costs.{k}: 알 수 없는 키" for k in costs if k not in COST_KEYS]
    age = _pos_int(gate.get("quote_max_age_seconds"), "gate.quote_max_age_seconds", problems)
    buy = _ratio(gate.get("buy_buffer"), "gate.buy_buffer", problems)
    sell = _ratio(gate.get("sell_buffer"), "gate.sell_buffer", problems)
    warn = _pos_int(gate.get("server_time_warn_ms"), "gate.server_time_warn_ms", problems)
    fees = parse_costs(costs, problems)
    alerts: tuple[int, int] | None = None
    try:
        alerts = parse_alert_settings(raw)
    except ConfigError as exc:
        problems += exc.problems
    minute = M2MinuteSettings()
    try:
        minute = parse_minute(raw.get("minute"))
    except ConfigError as exc:
        problems += exc.problems
    if problems or fees is None:
        raise ConfigError(problems or ["[costs] 해석 실패"])
    return M2MarketSettings(age, buy, sell, warn, fees.estimate_rate, alerts, fees, minute)  # type: ignore[arg-type]


def parse_universe(raw: dict) -> Universe:
    problems: list[str] = []
    for k in raw:
        if k not in ("quote_asset", "symbols"):
            problems.append(f"{k}: 알 수 없는 키")
    qa = raw.get("quote_asset")
    if qa != QUOTE_ASSET:
        problems.append(f"quote_asset: {QUOTE_ASSET}만 허용 (계좌 기준 통화 A-055)")
    syms = raw.get("symbols")
    out: list[str] = []
    if not isinstance(syms, list):
        problems.append("symbols: 문자열 목록이어야 함")
    else:
        for i, s in enumerate(syms):
            if not isinstance(s, str) or not _SYMBOL.fullmatch(s) or len(s) <= len(QUOTE_ASSET):
                problems.append(f"symbols[{i}]: 심볼 형식 아님 (대문자·숫자, 예: <기초자산>{QUOTE_ASSET})")
            elif not s.endswith(QUOTE_ASSET):
                problems.append(f"symbols[{i}]: 호가 자산이 {QUOTE_ASSET}가 아님")
            elif s in out:
                problems.append(f"symbols[{i}]: 중복 {s}")
            else:
                out.append(s)
    if problems:
        raise ConfigError(problems)
    return Universe(QUOTE_ASSET, tuple(out))


def _load(path: Path, what: str) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"{what} 파일을 읽을 수 없음: {path.name}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{what} 파일 형식 오류: {path.name}: {exc}") from exc


def load_m2_market(path: Path) -> M2MarketSettings:
    return parse_m2_market(_load(path, "M2 시장 설정"))


def load_universe(path: Path) -> Universe:
    return parse_universe(_load(path, "M2 유니버스"))
