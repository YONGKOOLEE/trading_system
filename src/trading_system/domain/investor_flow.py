"""투자자별 일별 순매수 값 객체 (CAP-DAT-05a, CP-033 D-188·D-190, 02 ENT-33 [제안]). 표준 라이브러리만.

- 출처 ka10060(금액 `amt_qty_tp=1`, 순매수 `trde_tp=0`, 단위 백만원). 투자자 구분 키는 응답 필드 이름을 그대로 쓴다.
- 값은 부호 포함 Decimal(순매도 = 음수). 부동소수 금지(D-032).
- 저장 회차(FlowRevision): 같은 (종목, 일자, 단위)의 값이 다시 받았을 때 달라지면 새 회차(D-190 — 덮어쓰지 않음).
  seen_at = 그 회차 값을 받은 수집 시각들(처음 받은 시각 포함). 확정 판정(D-189)은 이 관측 시각으로 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

# ka10060 투자자 구분 (명세 S2 — docs/broker-specs/kiwoom-kr.md "비시세 데이터 조사")
INVESTOR_KEYS: tuple[str, ...] = ("ind_invsr", "frgnr_invsr", "orgn", "fnnc_invt", "insrnc", "invtrt", "etc_fnnc",
                                  "bank", "penfnd_etc", "samo_fund", "natn", "etc_corp", "natfor")
INVESTOR_NAMES: dict[str, str] = {
    "ind_invsr": "개인", "frgnr_invsr": "외국인", "orgn": "기관계", "fnnc_invt": "금융투자", "insrnc": "보험",
    "invtrt": "투신", "etc_fnnc": "기타금융", "bank": "은행", "penfnd_etc": "연기금등", "samo_fund": "사모펀드",
    "natn": "국가", "etc_corp": "기타법인", "natfor": "내외국인",
}
# 합계 검사(D-191 경고만): 개인 + 외국인 + 기관계 + 기타법인 + 국가 + 내외국인
TOTAL_KEYS: tuple[str, ...] = ("ind_invsr", "frgnr_invsr", "orgn", "etc_corp", "natn", "natfor")
UNIT_AMOUNT = "AMT"     # 금액(백만원), ka10060 amt_qty_tp=1
UNIT_QUANTITY = "QTY"   # 수량 — 이 작업에서는 수집하지 않음(D-188: 필요하면 ka10059)
CAPABILITY = "CAP-DAT-05a"


@dataclass(frozen=True)
class InvestorFlow:
    """한 종목 한 일자의 투자자별 순매수. values = (투자자 구분, 값) — INVESTOR_KEYS 순서."""

    instrument: str
    day: date
    unit: str
    values: tuple[tuple[str, Decimal], ...]

    def value(self, key: str) -> Decimal | None:
        return next((v for k, v in self.values if k == key), None)

    def as_dict(self) -> dict[str, Decimal]:
        return dict(self.values)


@dataclass(frozen=True)
class FlowRevision:
    """저장된 한 회차 (D-190). collected_at = 이 회차 값을 처음 받은 시각, seen_at = 이 값을 받은 모든 수집 시각(오름차순)."""

    flow: InvestorFlow
    revision: int
    collected_at: datetime
    seen_at: tuple[datetime, ...]


@dataclass(frozen=True)
class FlowWrite:
    """저장 결과: 새 키, 같은 값(관측만 추가), 정정(새 회차 + 감사)."""

    new: int
    same: int
    corrected: int
    corrections: tuple[tuple[str, date], ...] = ()
