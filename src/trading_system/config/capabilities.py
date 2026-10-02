"""능력(CAP) 지원 목록 읽기 (`docs/capabilities.md`, 00 §2.3·D-004).

문서의 표가 원본이다. 표 형식이 깨졌거나 알 수 없는 상태 값이면 ConfigError (모르면 등록하지 않음).
"""

from __future__ import annotations

from pathlib import Path

from .errors import ConfigError

STATUSES = frozenset({"미지원", "검증 중", "지원", "해당 없음"})
MARKET_COLUMNS = ("M1", "M2", "M3")

CapabilityTable = dict[str, dict[str, str]]  # CAP-ID → 시장 → 상태


def parse_capabilities(text: str) -> CapabilityTable:
    table: CapabilityTable = {}
    header: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == "CAP-ID":
            header = cells
            continue
        if header is None or set(cells[0]) <= {"-", ":"}:
            continue
        if not cells[0].startswith("CAP-"):
            continue
        row = dict(zip(header, cells, strict=False))
        statuses: dict[str, str] = {}
        for m in MARKET_COLUMNS:
            value = row.get(m, "").split("(")[0].strip()
            if value not in STATUSES:
                raise ConfigError(f"capabilities: {cells[0]} {m} 상태 값이 잘못됨: {value!r}")
            statuses[m] = value
        table[cells[0]] = statuses
    if not table:
        raise ConfigError("capabilities: CAP 표를 찾지 못함")
    return table


def load_capabilities(path: Path) -> CapabilityTable:
    try:
        return parse_capabilities(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"capabilities 파일을 읽을 수 없음: {path}") from exc
