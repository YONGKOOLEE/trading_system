"""재무 스냅샷 해석 (CAP-DAT-05b, CP-033 D-194, 02 ENT-34 [제안], 승인 A-088 Q-4 (b)·A-100). 네트워크 호출 없음.

- 출처 ka10001 주식기본정보(POST /api/dostk/stkinfo, 입력 stk_cd뿐 — 기준일 없음, 과거 조회 불가).
- 필드: per·eps·roe·pbr·bps·ev·sale_amt(매출액, 억원)·bus_pro(영업이익)·cup_nga(당기순이익)·setl_mm(결산월).
  명세 주의 문구: PER·ROE 등은 외부 벤더 값, 주 1회 또는 실적 시즌에 갱신. 매출·이익의 기준 기간(연간·분기·TTM)은
  명세에 없어 **기준 미확인**(BASIS)으로 함께 저장한다.
- **판정·백테스트 입력에 쓰지 않는다**(축적만 — 선언 불가 능력, D-187). 이 모듈은 판정 스냅샷·백테스트 공급 경로에서
  import하지 않는다(시험이 확인).
- 값: 키움 엄격 파서로 숫자면 Decimal 문자열, 숫자가 아니면 응답 문자열 그대로(32자까지, "해석 불가" 목록에 이름),
  필드가 없으면 None("없음" 목록에 이름). 모든 필드가 없으면 응답 형식이 다른 것으로 보고 실패.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from trading_system.broker.kiwoom import parse
from trading_system.domain.fin import FIN_BASIS, FIN_FIELDS, FIN_SOURCE

__all__ = ["FIN_BASIS", "FIN_FIELDS", "FIN_SOURCE", "FinParseError", "FinValues", "parse_ka10001_fin"]
RAW_MAX = 32


class FinParseError(ValueError):
    pass


@dataclass(frozen=True)
class FinValues:
    values: tuple[tuple[str, str | None], ...]
    unparsed: tuple[str, ...] = ()
    missing: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, str | None]:
        return dict(self.values)


def parse_ka10001_fin(body: Mapping[str, object]) -> FinValues:
    values: list[tuple[str, str | None]] = []
    unparsed: list[str] = []
    missing: list[str] = []
    for k in FIN_FIELDS:
        raw = body.get(k)
        if raw is None:
            values.append((k, None))
            missing.append(k)
            continue
        try:
            values.append((k, str(parse.dec(raw, k))))
        except parse.KiwoomParseError:
            values.append((k, str(raw).strip()[:RAW_MAX]))
            unparsed.append(k)
    if len(missing) == len(FIN_FIELDS):
        raise FinParseError("ka10001 응답에 재무 필드가 하나도 없음 — 형식이 다름")
    return FinValues(tuple(values), tuple(unparsed), tuple(missing))
