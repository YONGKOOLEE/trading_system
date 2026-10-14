"""재무 스냅샷 상수 (CAP-DAT-05b, CP-033 D-194, 02 ENT-34 [제안]). 표준 라이브러리만.

축적만 한다 — 판정·백테스트 입력이 아니다(선언 불가 능력, D-187). 값 해석은 marketdata.fin_snapshot.
"""

from __future__ import annotations

# ka10001 주식기본정보 재무 필드 (docs/broker-specs/kiwoom-kr.md "비시세 데이터 조사")
FIN_FIELDS: tuple[str, ...] = ("per", "eps", "roe", "pbr", "bps", "ev", "sale_amt", "bus_pro", "cup_nga", "setl_mm")
FIN_SOURCE = "ka10001 /api/dostk/stkinfo"
FIN_BASIS = ("매출·이익 기준 기간 미확인(연간·분기·TTM), PER·ROE 등 외부 벤더 값 주 1회·실적 시즌 갱신 "
             "(kiwoom-kr 비시세 데이터 조사) — 판정·백테스트 입력 아님(D-194)")
CAPABILITY = "CAP-DAT-05b"
