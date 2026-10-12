"""계좌 한도·시간 설정 값 객체와 논리 제약 (01 §2.3·§2.3.1, D-026).

값(리스크 수치)은 사용자가 설정 파일로 정한다. 이 모듈은 값이 아니라 형식과 논리 제약만 안다.
기본값은 없다. 비율·금액은 Decimal이다 (float 금지).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import time
from decimal import Decimal

from .market_time import Market

# M1 KRX 정규장 (docs/broker-specs/krx-market.md §1 확인). 시장 달력·시장별 값은 T-010·M2·M3에서 확장.
KRX_REGULAR_OPEN = time(9, 0)
KRX_REGULAR_CLOSE = time(15, 30)
# M2 일 경계 UTC 00:00 (A-055, CP-024 D-108)
M2_DAY_BOUNDARY = time(0, 0)

RATIO_FIELDS = (
    "l1_max_loss_ratio", "l2_max_symbol_ratio", "l3_max_total_ratio", "l4_daily_loss_ratio",
)
DECIMAL_FIELDS = RATIO_FIELDS + ("l7_max_order_amount", "warn_usage_ratio")
REQUIRED_DECIMAL_FIELDS = RATIO_FIELDS + ("warn_usage_ratio",)   # [CP-034 D-196] L7은 금액 또는 비율 중 하나
TIME_FIELDS = ("order_window_start", "order_window_end", "daily_reference_time", "report_time")
# [CP-034] 선택 값. 없으면(None) 기존 동작이며 표준 형태·해시에 넣지 않는다(기존 설정 버전 해시 불변).
OPTIONAL_DECIMAL_FIELDS = ("l1_entry_buffer_ratio", "l7_max_order_amount", "l7_max_order_ratio")
OPTIONAL_TEXT_FIELDS = ("profile", "approved_by")
L1_BUFFER_MAX = Decimal("0.2")   # 여유 상한(미포함) — 리드 지시 0 ≤ 여유 < 0.2
LEGACY_PROFILE = "legacy"        # [CP-034 D-196] 한도 파일에 profile이 없을 때의 표시 이름 (기존 동작)
UNAPPROVED_LABEL = "미승인 한도 가정"   # approved_by(A-ID)가 없을 때 — 공식 BACKTEST_PASSED 근거로 쓰지 않음


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class AccountLimits:
    l1_max_loss_ratio: Decimal       # L1 1회 최대 손실 (기준 금액 대비)
    l2_max_symbol_ratio: Decimal     # L2 종목 비중
    l3_max_total_ratio: Decimal      # L3 총 노출
    l4_daily_loss_ratio: Decimal     # L4 일일 손실
    l5_daily_new_orders: int         # L5 일일 신규 주문 수
    l6_max_positions: int            # L6 보유 종목 수
    l7_max_order_amount: Decimal | None  # L7 1회 주문 최대 금액 (계좌 통화). None = 비율 형식 [CP-034 D-196]
    l10_leverage_total_ratio: Decimal | None  # L10 레버리지 상품 총 노출. None = 별도 한도 없음
    warn_usage_ratio: Decimal        # 한도 사용률 경고 기준
    order_window_start: time         # L8 위험 확대 주문 허용 시작 (시장 현지 시각)
    order_window_end: time           # L8 종료
    daily_reference_time: time       # 일일 기준 시각
    report_time: time                # 일일 보고 시각
    # [CP-034 D-195] L1 시장가 매수 진입 기준가 여유: 신호 기준가 × (1 + 여유). None = 기존 계산(예약 가격 = 상한가)
    l1_entry_buffer_ratio: Decimal | None = None
    # [CP-034 D-196] L7 비율 형식: 기준 금액 × 비율 (금액과 둘 중 하나만)
    l7_max_order_ratio: Decimal | None = None
    # [CP-034 D-196] 정책 프로필 이름(None = legacy)과 그 프로필 값의 사용자 승인 A-ID(None = 미승인 한도 가정)
    profile: str | None = None
    approved_by: str | None = None

    def __post_init__(self) -> None:
        for name in REQUIRED_DECIMAL_FIELDS:
            _require_decimal(name, getattr(self, name))
        if (self.l7_max_order_amount is None) == (self.l7_max_order_ratio is None):
            raise ValueError("L7은 l7_max_order_amount와 l7_max_order_ratio 중 하나만 둔다 (CP-034 D-196)")
        for name in OPTIONAL_DECIMAL_FIELDS:
            if getattr(self, name) is not None:
                _require_decimal(name, getattr(self, name))
        if self.l10_leverage_total_ratio is not None:
            _require_decimal("l10_leverage_total_ratio", self.l10_leverage_total_ratio)
        for name in ("l5_daily_new_orders", "l6_max_positions"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int):
                raise TypeError(f"{name}은 정수여야 한다")

    def version_hash(self) -> str:
        """설정 버전 해시 (02 ENT-08). 게이트 판정에 적용 설정 버전으로 기록한다 (ENT-14)."""
        text = json.dumps(self.to_canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_canonical(self) -> dict[str, str | int | None]:
        """해시·저장·감사용 표준 형태. 표기 차이(0.20 / 0.2)는 같은 값으로 본다."""
        out: dict[str, str | int | None] = {}
        for name in REQUIRED_DECIMAL_FIELDS:
            out[name] = _decimal_text(getattr(self, name))
        out["l5_daily_new_orders"] = self.l5_daily_new_orders
        out["l6_max_positions"] = self.l6_max_positions
        l10 = self.l10_leverage_total_ratio
        out["l10_leverage_total_ratio"] = None if l10 is None else _decimal_text(l10)
        for name in TIME_FIELDS:
            out[name] = getattr(self, name).strftime("%H:%M")
        for name in OPTIONAL_DECIMAL_FIELDS:
            v = getattr(self, name)
            if v is not None:
                out[name] = _decimal_text(v)
        for name in OPTIONAL_TEXT_FIELDS:
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        return dict(sorted(out.items()))

    @classmethod
    def from_canonical(cls, data: dict[str, str | int | None]) -> AccountLimits:
        def t(name: str) -> time:
            hh, mm = str(data[name]).split(":")
            return time(int(hh), int(mm))

        l10 = data["l10_leverage_total_ratio"]
        opt = {name: Decimal(str(data[name])) for name in OPTIONAL_DECIMAL_FIELDS if data.get(name) is not None}
        return cls(
            l1_max_loss_ratio=Decimal(str(data["l1_max_loss_ratio"])),
            l2_max_symbol_ratio=Decimal(str(data["l2_max_symbol_ratio"])),
            l3_max_total_ratio=Decimal(str(data["l3_max_total_ratio"])),
            l4_daily_loss_ratio=Decimal(str(data["l4_daily_loss_ratio"])),
            l5_daily_new_orders=int(data["l5_daily_new_orders"]),  # type: ignore[arg-type]
            l6_max_positions=int(data["l6_max_positions"]),  # type: ignore[arg-type]
            l10_leverage_total_ratio=None if l10 is None else Decimal(str(l10)),
            warn_usage_ratio=Decimal(str(data["warn_usage_ratio"])),
            order_window_start=t("order_window_start"),
            order_window_end=t("order_window_end"),
            daily_reference_time=t("daily_reference_time"),
            report_time=t("report_time"),
            l7_max_order_amount=opt.pop("l7_max_order_amount", None),  # type: ignore[arg-type]
            **opt,  # type: ignore[arg-type]
            **{name: str(data[name]) for name in OPTIONAL_TEXT_FIELDS if data.get(name) is not None},
        )

    def l7_cap(self, base_amount: Decimal) -> Decimal:
        """L7 1회 주문 최대 금액: 금액 형식이면 그 값, 비율 형식이면 기준 금액 × 비율 (CP-034 D-196)."""
        if self.l7_max_order_amount is not None:
            return self.l7_max_order_amount
        assert self.l7_max_order_ratio is not None   # __post_init__이 둘 중 하나를 보장
        return base_amount * self.l7_max_order_ratio


def profile_label(lim: AccountLimits) -> str:
    """판정표·결과 표시용 프로필 이름 (CP-034 D-196). 없으면 legacy."""
    return lim.profile or LEGACY_PROFILE


def profile_approval_label(lim: AccountLimits) -> str:
    """승인된 프로필이면 '승인 A-xxx', 아니면 '미승인 한도 가정' (CP-034 D-196, V-271)."""
    return f"승인 {lim.approved_by}" if lim.approved_by else UNAPPROVED_LABEL


def profile_line(profile: str | None, approved_by: str | None) -> str:
    """결과 보고서·판정표 한 줄 표시 (V-271). 미승인이면 공식 BACKTEST_PASSED 근거가 아님을 함께 적는다."""
    label = profile or LEGACY_PROFILE
    if approved_by:
        return f"한도 프로필: {label} (승인 {approved_by}, CP-034 D-196)"
    return f"한도 프로필: {label} ({UNAPPROVED_LABEL} — 공식 BACKTEST_PASSED 근거로 쓰지 않음, CP-034 D-196)"


def _require_decimal(name: str, value: object) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name}은 Decimal이어야 한다 (float 금지)")
    if not value.is_finite():
        raise ValueError(f"{name}이 유한한 값이 아니다")


def check_limits(lim: AccountLimits, market: str | Market = Market.M1) -> tuple[list[str], list[str]]:
    """논리 제약(01 §2.3.1-3)을 검사한다. (오류, 경고)를 돌려준다.

    한도 값 제약은 시장 공통, 시간 제약은 시장별이다(CP-024 D-108): M1 = KRX 정규장 기준(기존 그대로),
    M2 = UTC 24/7 기준. 시장 기본값은 M1이다(M1 호출부 변경 없음).
    """
    market = Market(market)  # 알 수 없는 시장이면 ValueError
    problems: list[str] = []
    warnings: list[str] = []

    for name in RATIO_FIELDS:
        if getattr(lim, name) <= 0:
            problems.append(f"{name}: 0보다 커야 한다")
    if lim.l10_leverage_total_ratio is not None and lim.l10_leverage_total_ratio <= 0:
        problems.append("l10_leverage_total_ratio: 0보다 커야 한다 (한도 없음은 'none')")
    if not (0 < lim.warn_usage_ratio <= 1):
        problems.append("warn_usage_ratio: 0보다 크고 1 이하여야 한다")

    if lim.l1_max_loss_ratio > lim.l2_max_symbol_ratio:
        problems.append("l1_max_loss_ratio: L1 ≤ L2(l2_max_symbol_ratio)여야 한다")
    if lim.l2_max_symbol_ratio > lim.l3_max_total_ratio:
        problems.append("l2_max_symbol_ratio: L2 ≤ L3(l3_max_total_ratio)여야 한다")
    if lim.l10_leverage_total_ratio is not None and lim.l10_leverage_total_ratio > lim.l3_max_total_ratio:
        problems.append("l10_leverage_total_ratio: L10 ≤ L3(l3_max_total_ratio)여야 한다")

    for name in ("l5_daily_new_orders", "l6_max_positions"):
        if getattr(lim, name) < 1:
            problems.append(f"{name}: 1 이상이어야 한다")
    if lim.l7_max_order_amount is not None and lim.l7_max_order_amount <= 0:
        problems.append("l7_max_order_amount: 0보다 커야 한다")
    if lim.l7_max_order_ratio is not None and lim.l7_max_order_ratio <= 0:
        problems.append("l7_max_order_ratio: 0보다 커야 한다")
    buf = lim.l1_entry_buffer_ratio
    if buf is not None and not (0 <= buf < L1_BUFFER_MAX):
        problems.append(f"l1_entry_buffer_ratio: 0 이상 {L1_BUFFER_MAX} 미만이어야 한다 (CP-034 D-195)")

    if market is Market.M2:
        problems.extend(_utc_247_time_problems(lim))
    else:
        problems.extend(_krx_time_problems(lim))

    if lim.l3_max_total_ratio > 1:
        warnings.append("l3_max_total_ratio: 100% 초과 (레버리지 배수 반영 노출 기준인지 확인)")
    for name in ("l1_max_loss_ratio", "l2_max_symbol_ratio", "l4_daily_loss_ratio"):
        if getattr(lim, name) > 1:
            warnings.append(f"{name}: 100% 초과")
    if lim.l10_leverage_total_ratio is not None and lim.l10_leverage_total_ratio > 1:
        warnings.append("l10_leverage_total_ratio: 100% 초과")
    if lim.l7_max_order_ratio is not None and lim.l7_max_order_ratio > 1:
        warnings.append("l7_max_order_ratio: 100% 초과")
    return problems, warnings


def _krx_time_problems(lim: AccountLimits) -> list[str]:
    """M1(KRX) 시간 제약 — 기존 검사 그대로 (문구·순서 불변, V-152)."""
    problems: list[str] = []
    if not lim.order_window_start < lim.order_window_end:
        problems.append("order_window: 시작이 종료보다 빨라야 한다")
    if lim.order_window_start < KRX_REGULAR_OPEN:
        problems.append(f"order_window_start: 정규장 시작({KRX_REGULAR_OPEN:%H:%M}) 이후여야 한다")
    if lim.order_window_end > KRX_REGULAR_CLOSE:
        problems.append(f"order_window_end: 정규장 종료({KRX_REGULAR_CLOSE:%H:%M}) 이전이어야 한다")
    if not lim.daily_reference_time < KRX_REGULAR_OPEN:
        problems.append(f"daily_reference_time: 장 시작({KRX_REGULAR_OPEN:%H:%M}) 전이어야 한다")
    if not lim.report_time > KRX_REGULAR_CLOSE:
        problems.append(f"report_time: 장 마감({KRX_REGULAR_CLOSE:%H:%M}) 후여야 한다")
    return problems


def _utc_247_time_problems(lim: AccountLimits) -> list[str]:
    """M2(UTC 24/7) 시간 제약 (CP-024 D-108, A-055·A-064). 시각은 모두 UTC.

    - 일일 기준 시각 = UTC 00:00 [결정 A-055]. 거래일 = UTC 날짜.
    - 주문 창(L8)은 한 거래일 안의 한 구간: 시작 < 종료. 시작 > 종료(자정을 넘는 창)는 일 경계를 넘어
      L4·L5 집계 구간이 갈리므로 거부한다 [제안].
    - 주문 창 시작은 일일 기준 시각 뒤 [제안]: 일 경계 직후 일일 확정·대조(D-109) 전 위험 확대 방지 (M1 "기준 시각 < 장 시작"과 같은 취지).
    - 보고 시각: 제약 없음(24/7에는 장 마감이 없다).
    """
    problems: list[str] = []
    if lim.daily_reference_time != M2_DAY_BOUNDARY:
        problems.append(f"daily_reference_time: M2 일일 기준 시각은 UTC {M2_DAY_BOUNDARY:%H:%M}이어야 한다 (A-055, D-108)")
    if lim.order_window_start > lim.order_window_end:
        problems.append("order_window: 자정(UTC)을 넘는 주문 창은 일 경계를 넘으므로 허용하지 않는다 — 시작이 종료보다 빨라야 한다")
    elif lim.order_window_start == lim.order_window_end:
        problems.append("order_window: 시작이 종료보다 빨라야 한다 (빈 주문 창)")
    if not lim.order_window_start > M2_DAY_BOUNDARY:
        problems.append(f"order_window_start: 일일 기준 시각(UTC {M2_DAY_BOUNDARY:%H:%M}) 뒤여야 한다 (일 경계 확정 전 주문 방지)")
    return problems
