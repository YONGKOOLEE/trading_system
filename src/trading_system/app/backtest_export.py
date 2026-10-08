"""백테스트 결과 파일 (T-009, 04 §2.6, D-078, 사용자 형식 승인 2026-10-08).

실행 폴더(`var/backtest/<실행ID>/`, trading.db 옆)에 세 파일을 쓴다. 계산 결과(지표·지문·체결)는 바꾸지 않고
엔진이 이미 가진 값만 옮겨 적는다.

- trades.csv: 왕복 1건(진입 → 전량 청산, D-078)마다 한 행. 엑셀이 바로 열도록 UTF-8 BOM, 줄바꿈 CRLF(csv 기본).
  * 일시 = 그 체결을 일으킨 판정 시각(엔진의 체결 시각, KST `YYYY-MM-DD HH:MM`).
  * 진입가·청산가 = 그 왕복의 매수·매도 체결 수량 가중 평균, 소수 둘째 자리 반올림(ROUND_HALF_UP) [제안].
  * 손익 = 비용 반영 후(Trade.pnl과 같음), 비용 = 왕복 체결별 추정 비용 합계(D-065, 원 미만 올림).
  * 청산사유 = 마지막 매도 체결 주문을 만든 신호의 규칙 ID. 신호가 아닌 주문이면 `신호 외`.
  * 기간 끝에 남은 보유는 청산 칸(청산일시·청산가·손익)을 비우고 청산사유 `미청산(기간 종료)`로 적는다 [제안].
    지표(거래 수 등)에는 넣지 않는다(D-078 그대로).
- equity.csv: 거래일마다 날짜·총자산·현금·보유평가액·낙폭. 총자산 = 현금 + 보유평가액(엔진의 일말 평가액과 같음),
  낙폭 = (고점 − 총자산)/고점, 고점은 초기 자금에서 시작(compute_metrics와 같은 정의, 소수 6자리).
- summary.json: 지표 전부 + 실행 조건. 소수는 문자열, ensure_ascii=False, indent 2.

모든 금액·가격·수량은 Decimal 문자열(지수 표기 없음). 같은 입력이면 같은 바이트(실행 ID 칸 제외).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

TRADE_HEADER = ("종목", "진입일시", "청산일시", "진입가", "청산가", "수량", "손익", "비용", "보유일", "청산사유")
EQUITY_HEADER = ("날짜", "총자산", "현금", "보유평가액", "낙폭")
OPEN_REASON = "미청산(기간 종료)"
NON_SIGNAL_REASON = "신호 외"
_PRICE_Q = Decimal("0.01")
_RATIO_Q = Decimal("0.000001")


@dataclass(frozen=True)
class FillDetail:
    """체결 한 건의 내보내기용 부가 정보 (fills 원장과 같은 순서·길이)."""

    stamp: str        # KST "YYYY-MM-DD HH:MM" (체결을 일으킨 판정 시각)
    reason: str       # 주문을 만든 신호의 규칙 ID, 없으면 NON_SIGNAL_REASON


def _num(d: Decimal) -> str:
    return format(d, "f")


def trade_rows(fills: list[tuple[date, str, str, str, str, str]], details: list[FillDetail],
               cost_of: Callable[[Decimal, bool], Decimal], holding_days: Callable[[date, date], int],
               last_day: date) -> tuple[list[tuple[str, ...]], int]:
    """(행 목록, 미청산 수). 행 순서: 청산 완료 순(체결 순), 그다음 미청산을 종목 코드 순."""
    if len(fills) != len(details):
        raise ValueError("체결 원장과 부가 정보 길이가 다름")
    rows: list[tuple[str, ...]] = []
    state: dict[str, dict] = {}
    for (day, _no, inst, side, qty_s, price_s), det in zip(fills, details, strict=True):
        qty, price = Decimal(qty_s), Decimal(price_s)
        st = state.get(inst)
        if st is None or st["qty"] == 0:
            st = state[inst] = {"qty": Decimal("0"), "entry_day": day, "entry_stamp": det.stamp,
                                "bq": Decimal("0"), "bamt": Decimal("0"), "sq": Decimal("0"), "samt": Decimal("0"),
                                "cost": Decimal("0")}
        amount = qty * price
        sell = side == "SELL"
        st["cost"] += cost_of(amount, sell)
        if sell:
            st["sq"] += qty
            st["samt"] += amount
            st["qty"] -= qty
        else:
            st["bq"] += qty
            st["bamt"] += amount
            st["qty"] += qty
        if st["qty"] == 0:
            pnl = st["samt"] - st["bamt"] - st["cost"]
            rows.append((inst, st["entry_stamp"], det.stamp, _avg(st["bamt"], st["bq"]), _avg(st["samt"], st["sq"]),
                         _num(st["bq"]), _num(pnl), _num(st["cost"]), str(holding_days(st["entry_day"], day)),
                         det.reason))
    opens = [(inst, st) for inst, st in sorted(state.items()) if st["qty"] != 0]
    for inst, st in opens:
        rows.append((inst, st["entry_stamp"], "", _avg(st["bamt"], st["bq"]), "", _num(st["bq"]), "",
                     _num(st["cost"]), str(holding_days(st["entry_day"], last_day)), OPEN_REASON))
    return rows, len(opens)


def _avg(amount: Decimal, qty: Decimal) -> str:
    return _num((amount / qty).quantize(_PRICE_Q, rounding=ROUND_HALF_UP)) if qty else ""


def equity_rows(initial: Decimal, daily: list[tuple[date, Decimal, Decimal]]) -> list[tuple[str, ...]]:
    """daily = (날짜, 현금, 보유평가액). 낙폭 고점은 초기 자금에서 시작 (compute_metrics와 같음)."""
    peak = initial
    out: list[tuple[str, ...]] = []
    for day, cash, held in daily:
        total = cash + held
        peak = max(peak, total)
        dd = ((peak - total) / peak).quantize(_RATIO_Q) if peak else Decimal("0")
        out.append((day.isoformat(), _num(total), _num(cash), _num(held), _num(dd)))
    return out


def write_csv(path: Path, header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    buf = io.StringIO(newline="")
    w = csv.writer(buf)   # 줄바꿈 CRLF (엑셀 기본)
    w.writerow(header)
    w.writerows(rows)
    path.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))


def write_summary(path: Path, summary: dict[str, object]) -> None:
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
