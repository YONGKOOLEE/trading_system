"""계좌 조회 응답 `GET /api/v3/account` 해석 (BN §7, CP-024 D-103, T-M2-14).

- 남기는 값: 거래 가능 여부(canTrade), 계좌 유형(accountType), 권한(permissions), 자산별 free·locked.
- **계좌 식별값(uid 등)은 읽지 않는다** — 결과 객체·로그·보고서에 들어갈 길을 만들지 않는다(BN §7 "uid 등 계정 식별값은
  로그 마스킹 대상", CLAUDE.md §6).
- 수수료율(commissionRates 등)은 이 작업에서 쓰지 않는다(비용 추정은 설정 값, CP-024 §2.4).
- 형식 오류(잔고 목록 아님·자산 중복·음수)면 BinanceParseError — 호출자는 조회 실패로 본다(0으로 보지 않음).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .numbers import BinanceParseError, parse_non_negative


@dataclass(frozen=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True)
class AccountInfo:
    can_trade: bool
    account_type: str | None
    permissions: tuple[str, ...]
    balances: tuple[Balance, ...]

    def balance_map(self) -> dict[str, tuple[Decimal, Decimal]]:
        """자산 → (free, locked) (D-103 대조 값 = free + locked)."""
        return {b.asset: (b.free, b.locked) for b in self.balances}


def parse_account(body: object) -> AccountInfo:
    if not isinstance(body, dict):
        raise BinanceParseError("계좌 응답이 객체가 아님")
    can_trade = body.get("canTrade")
    if not isinstance(can_trade, bool):
        raise BinanceParseError("canTrade: 참/거짓 아님")
    raw = body.get("balances")
    if not isinstance(raw, list):
        raise BinanceParseError("balances: 목록 아님")
    seen: set[str] = set()
    out: list[Balance] = []
    for row in raw:
        if not isinstance(row, dict):
            raise BinanceParseError("balances: 항목 형식 아님")
        asset = row.get("asset")
        if not isinstance(asset, str) or not asset:
            raise BinanceParseError("balances: asset 없음")
        if asset in seen:
            raise BinanceParseError(f"balances: 자산 중복 {asset}")
        seen.add(asset)
        out.append(Balance(asset, parse_non_negative(row.get("free"), "free"),
                           parse_non_negative(row.get("locked"), "locked")))
    acct_type = body.get("accountType")
    perms = body.get("permissions", [])
    if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
        raise BinanceParseError("permissions: 문자열 목록 아님")
    return AccountInfo(can_trade, acct_type if isinstance(acct_type, str) else None, tuple(perms),
                       tuple(sorted(out, key=lambda b: b.asset)))
