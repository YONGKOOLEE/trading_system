"""M2 설정 파일 로더: 시장 설정·유니버스 (CP-024 D-097·D-104·D-105·D-115·D-116·§2.4, T-M2-08).

파일: `config/m2/market.<mode>.toml`, `config/m2/universe.<mode>.toml`. 한도는 `config/m2/limits.<mode>.toml`
(limits_file.load_limits_file(path, market="M2")), 앱은 `config/m2/app.<mode>.toml`(app_file, market = "M2").

market:
  [gate]   quote_max_age_seconds(정수 초, A-064 60), buy_buffer·sell_buffer(문자열 비율 0 이상 1 미만, A-064·A-065 Q4 0.5%),
           server_time_warn_ms(정수 밀리초, A-064 500)
  [costs]  commission_rate(문자열 비율, 비용 추정용 — 값은 사용자 확인 항목 CP-024 §2.4, 비차단)
  [alerts] reject_surge_count·reject_surge_minutes (M1과 같은 형식)
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
class M2MarketSettings:
    quote_max_age_seconds: int
    buy_buffer: Decimal
    sell_buffer: Decimal
    server_time_warn_ms: int
    commission_rate: Decimal
    alerts: tuple[int, int]


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


def parse_m2_market(raw: dict) -> M2MarketSettings:
    problems: list[str] = []
    for k in raw:
        if k not in ("gate", "costs", "alerts"):
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
    problems += [f"costs.{k}: 알 수 없는 키" for k in costs if k != "commission_rate"]
    age = _pos_int(gate.get("quote_max_age_seconds"), "gate.quote_max_age_seconds", problems)
    buy = _ratio(gate.get("buy_buffer"), "gate.buy_buffer", problems)
    sell = _ratio(gate.get("sell_buffer"), "gate.sell_buffer", problems)
    warn = _pos_int(gate.get("server_time_warn_ms"), "gate.server_time_warn_ms", problems)
    com = _ratio(costs.get("commission_rate"), "costs.commission_rate", problems)
    alerts: tuple[int, int] | None = None
    try:
        alerts = parse_alert_settings(raw)
    except ConfigError as exc:
        problems += exc.problems
    if problems:
        raise ConfigError(problems)
    return M2MarketSettings(age, buy, sell, warn, com, alerts)  # type: ignore[arg-type]


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
