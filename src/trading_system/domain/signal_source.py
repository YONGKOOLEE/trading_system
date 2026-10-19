"""M2 신호 시세 출처 — 값과 순수 계산 (CP-039 D-243·D-250·D-252, 승인 A-115). 표준 라이브러리만.

- 출처 종류: `testnet`(기본 — 설정 절이 없으면 이것, 현 동작 그대로) / `binance-public`(실서버 공개 시세, 사용자가 연결 승인 뒤
  설정에 직접 쓴다). 실행 출처(주문·잔고·대조·체결 가능 시세)는 어느 쪽이든 테스트넷이다(D-243·D-253).
- 괴리 d = |P_x ÷ P_s − 1| (P_x = 실행 출처 최근가, P_s = 신호 출처 참조 시세). θ 기본 1.0%(A-115 Q-3, PV-M2-15 뒤 확정).
  d = θ는 통과(D-250 경계).
- 신호 기준 평균 매입가(Q-5 (가)): 체결마다 그 시각의 P_s를 체결 수량으로 가중한 값. 원장(테스트넷 체결가)은 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

KIND_TESTNET = "testnet"
KIND_PUBLIC = "binance-public"
SIGNAL_KINDS = frozenset({KIND_TESTNET, KIND_PUBLIC})
DEFAULT_MAX_DIVERGENCE = Decimal("0.01")      # θ = 1.0% (A-115 Q-3)
MAX_DIVERGENCE_CEILING = Decimal("0.10")      # 설정 상한 10% [제안 — 오기 방지]
PUBLIC_BAR_SOURCE = "binance-public"          # minute_bar.source 값 (D-248)


@dataclass(frozen=True)
class SignalSourceSettings:
    """M2 앱 설정 `[signal_source]` 절. configured = 절이 파일에 있었는지(M1 거부 판정용)."""

    kind: str = KIND_TESTNET
    approval: str | None = None
    max_divergence: Decimal = DEFAULT_MAX_DIVERGENCE
    configured: bool = False

    @property
    def is_public(self) -> bool:
        return self.kind == KIND_PUBLIC


def divergence(exec_price: Decimal, signal_price: Decimal) -> Decimal:
    """d = |P_x ÷ P_s − 1|. P_s ≤ 0은 호출자가 먼저 거른다."""
    if signal_price <= 0:
        raise ValueError("신호 출처 가격은 0보다 커야 한다")
    return abs(exec_price / signal_price - 1)


def signal_basis_avg_price(fills: Iterable[tuple[str, Decimal, Decimal | None]]) -> Decimal | None:
    """체결 기록 (매수·매도 "BUY"|"SELL", 체결 수량, 체결 시각 P_s) 순서대로 → 지금 보유분의 신호 기준 평균 매입가.

    매수는 (보유 × 평균 + 수량 × P_s) ÷ (보유 + 수량), 매도는 평균 그대로(보유만 줄임), 보유가 0 이하가 되면 새로 시작.
    지금 보유분을 만든 매수 가운데 P_s가 없는 체결이 하나라도 있으면 None(모름 — 호출자가 원장 평균으로 대신하고 알린다)."""
    qty = Decimal("0")
    avg: Decimal | None = Decimal("0")
    for side, q, ref in fills:
        if side == "BUY":
            if ref is None or avg is None:
                avg = None
            else:
                avg = (qty * avg + q * ref) / (qty + q)
            qty += q
        else:
            qty -= q
            if qty <= 0:
                qty, avg = Decimal("0"), Decimal("0")
    return avg if qty > 0 else None
