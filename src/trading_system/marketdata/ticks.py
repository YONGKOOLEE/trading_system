"""호가 가격 단위 (03 D-056, krx-market §2, X-U1).

- 원본은 설정 데이터 `config/ticks/krx.toml` (출처·버전 필수). X-U1 확인(2026-10-08)으로 CP-032 D-173(A-085)에서
  실제 파일을 두었다. 규칙 파일이 없으면 tick_valid는 None → 게이트가 지정가를 거부한다(시장가만 가능).
- 지정가 맞춤: 매수는 내림, 매도는 올림 (D-056).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from trading_system.domain.market import InstrumentKind

from .errors import MarketDataError


@dataclass(frozen=True)
class Band:
    below: Decimal | None  # 이 가격 미만 구간 (None = 상한 없음)
    tick: Decimal


@dataclass(frozen=True)
class TickTable:
    source: str
    version: str
    bands: dict[InstrumentKind, tuple[Band, ...]]

    def tick_of(self, kind: InstrumentKind, price: Decimal) -> Decimal | None:
        for b in self.bands.get(kind, ()):
            if b.below is None or price < b.below:
                return b.tick
        return None

    def valid(self, kind: InstrumentKind, price: Decimal) -> bool | None:
        tick = self.tick_of(kind, price)
        if tick is None:
            return None
        return price > 0 and price % tick == 0

    def align(self, kind: InstrumentKind, price: Decimal, is_buy: bool) -> Decimal | None:
        tick = self.tick_of(kind, price)
        if tick is None:
            return None
        rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
        return (price / tick).to_integral_value(rounding=rounding) * tick


def parse_ticks(raw: dict) -> TickTable:
    problems: list[str] = []
    for k in ("source", "version"):
        if not isinstance(raw.get(k), str) or not raw.get(k):
            problems.append(f"{k}: 출처·버전 필수 (X-U1)")
    bands: dict[InstrumentKind, tuple[Band, ...]] = {}
    for kind in InstrumentKind:
        rows = raw.get("bands", {}).get(kind.value)
        if rows is None:
            continue
        parsed: list[Band] = []
        last = Decimal("0")
        for i, row in enumerate(rows):
            try:
                below = None if row.get("below") in (None, "none") else Decimal(str(row["below"]))
                tick = Decimal(str(row["tick"]))
            except (KeyError, ArithmeticError, TypeError, AttributeError) as exc:
                problems.append(f"bands.{kind.value}[{i}]: {exc}")
                continue
            if isinstance(row.get("tick"), float) or isinstance(row.get("below"), float):
                problems.append(f"bands.{kind.value}[{i}]: 소수는 문자열로 (부동소수 금지)")
            if tick <= 0:
                problems.append(f"bands.{kind.value}[{i}]: tick > 0")
            if below is not None and below <= last:
                problems.append(f"bands.{kind.value}[{i}]: 구간이 오름차순이 아님")
            if below is None and i != len(rows) - 1:
                problems.append(f"bands.{kind.value}[{i}]: 상한 없는 구간은 마지막이어야 함")
            last = below if below is not None else last
            parsed.append(Band(below, tick))
        if parsed and parsed[-1].below is not None:
            problems.append(f"bands.{kind.value}: 마지막 구간은 상한 없음(\"none\")이어야 함")
        bands[kind] = tuple(parsed)
    if not bands:
        problems.append("bands: 상품 유형별 표가 하나도 없음")
    if problems:
        raise MarketDataError(problems)
    return TickTable(raw["source"], raw["version"], bands)


def load_ticks(path: Path) -> TickTable | None:
    """파일이 없으면 None (지정가 미지원 상태 유지, D-056)."""
    if not path.exists():
        return None
    with path.open("rb") as f:
        return parse_ticks(tomllib.load(f))
