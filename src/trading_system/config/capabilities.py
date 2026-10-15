"""능력(CAP) 지원 목록 읽기 (`docs/capabilities.md`, 00 §2.3·D-004).

문서의 표가 원본이다. 표 형식이 깨졌거나 알 수 없는 상태 값이면 ConfigError (모르면 등록하지 않음).
"""

from __future__ import annotations

from pathlib import Path

from .errors import ConfigError

STATUSES = frozenset({"미지원", "검증 중", "지원", "해당 없음"})
MARKET_COLUMNS = ("M1", "M2", "M3")

CapabilityTable = dict[str, dict[str, str]]  # CAP-ID → 시장 → 상태

# [CP-032 D-175] 미체결 정정·취소 능력의 분리: "CAP-ORD-03" 선언 = 03a(정정)·03b(취소) 둘 다 요구.
# 취소만 쓰는 전략은 "CAP-ORD-03b"로 선언한다.
ORD03, ORD03A, ORD03B = "CAP-ORD-03", "CAP-ORD-03a", "CAP-ORD-03b"
# [CP-033 D-187] 비시세 데이터 분리: "CAP-DAT-05" 선언 = 05a(투자자별 순매수)·05b(재무 스냅샷)·05c(공시·일정) 모두 요구
DAT05, DAT05A, DAT05B, DAT05C = "CAP-DAT-05", "CAP-DAT-05a", "CAP-DAT-05b", "CAP-DAT-05c"
CAPABILITY_PARTS: dict[str, tuple[str, ...]] = {ORD03: (ORD03A, ORD03B), DAT05: (DAT05A, DAT05B, DAT05C)}
# 표 상태와 무관하게 이 코드 버전에 구현이 없는 능력 (표가 먼저 바뀌어도 등록을 막는다)
NOT_IMPLEMENTED: dict[str, str] = {ORD03A: "정정(03a) 미구현 — CP-032 단계 5(T-033) 전까지 등록 불가"}
# 표 상태와 무관하게 전략이 선언할 수 없는 능력 (판정 입력이 아님)
NOT_DECLARABLE: dict[str, str] = {DAT05B: "재무 스냅샷(05b)은 축적만 — 판정 입력이 아니라 선언 불가 (CP-033 D-187·D-194)"}


def required_parts(cap: str) -> tuple[str, ...]:
    """선언한 능력이 실제로 요구하는 능력들 (묶음 선언은 부분으로 펼친다)."""
    return CAPABILITY_PARTS.get(cap, (cap,))


def capability_row(caps: CapabilityTable, cap: str) -> dict[str, str] | None:
    """능력의 시장별 상태 행. 표에 없으면 None(정의되지 않은 능력).

    기존 표 호환: 03a·03b 행이 없고 "CAP-ORD-03" 한 행만 있으면 그 행 상태를 03a·03b 각각에 적용한다
    (docs/capabilities.md는 사용자 지시 시 03a·03b 행으로 나눈다 — CP-032 §3.1)."""
    row = caps.get(cap)
    if row is None and cap in CAPABILITY_PARTS[ORD03]:
        row = caps.get(ORD03)
    return row


def uses_unfilled_cancel(capabilities: tuple[str, ...]) -> bool:
    """실행 정책의 미체결 시간 초과 취소(03b)를 쓰는 선언인지 (03 2-M1.4, T-020 경로)."""
    return any(ORD03B in required_parts(c) for c in capabilities)


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
