"""호출 가중치·주문 수 예산 (CP-024 D-113, BN §4, V-145·V-159).

- 한도: exchangeInfo `rateLimits`에서 읽는다(실제 값은 B-U1 미확인 → 시작 시 읽은 값 사용, 못 읽으면 시작 거부).
  내부 예산 = 한도의 50%(내림). REQUEST_WEIGHT(IP 기준)와 ORDERS(계정 기준) 모두.
- 우선순위(M1 03 §4 원칙과 같음): 결과 불명 조회 > 위험 축소 주문·취소 > 대조 > 위험 확대 주문 > 시세.
  [제안] 예산 안에서 우선순위별 사용 상한을 둔다: 결과 불명·위험 축소 100%, 대조 90%, 위험 확대 80%, 시세 70%.
  예산이 줄면 시세 → 위험 확대 → 대조 순으로 보류되고 결과 불명 조회·위험 축소는 마지막까지 남는다.
  보류는 '전송 전 실패'(거부·기록은 호출자).
- 사용량 보정: 응답 헤더 `X-MBX-USED-WEIGHT-(n)(단위)`·`X-MBX-ORDER-COUNT-(n)(단위)`. 지역 추정과 헤더 중 큰 값(보수).
  [추정] 한도 창은 UTC 기준 고정 구간(간격 단위로 나눈 몫)으로 본다 — 창 정렬 방식은 문서 미확인(B-U1, PV-M2-01).
  헤더가 지역 추정을 바로잡으므로 정렬이 달라도 예산 50%가 완충한다.
- 429: `Retry-After`(초)까지 모든 호출 보류. 헤더가 없으면 60초(보수값 [제안]). 주문 변경 재시도 여부는 호출자 정책
  (주문은 재시도 없음, 조회는 대기 후 최대 3회).
- 418: 즉시 모든 호출 중단. 시간이 지나도 자동 재개하지 않는다 — `resume_after_ban()`은 사람이 재개할 때만 부른다.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum

BUDGET_FRACTION_PERCENT = 50
DEFAULT_RETRY_AFTER_S = 60
_UNIT_MS = {"S": 1_000, "M": 60_000, "H": 3_600_000, "D": 86_400_000}
_INTERVAL_MS = {"SECOND": 1_000, "MINUTE": 60_000, "HOUR": 3_600_000, "DAY": 86_400_000}
_WEIGHT_HEADER = re.compile(r"x-mbx-used-weight-(\d+)([smhd])", re.I)
_ORDER_HEADER = re.compile(r"x-mbx-order-count-(\d+)([smhd])", re.I)


class CallPriority(IntEnum):
    RESOLVE = 0      # 결과 불명 해소 조회
    REDUCE = 1       # 위험 축소 주문·취소
    RECONCILE = 2    # 대조
    INCREASE = 3     # 위험 확대 주문
    MARKET = 4       # 시세


# 우선순위별 사용 상한(내부 예산 대비 %) [제안]
TIER_PERCENT = {CallPriority.RESOLVE: 100, CallPriority.REDUCE: 100, CallPriority.RECONCILE: 90,
                CallPriority.INCREASE: 80, CallPriority.MARKET: 70}


class RateLimitConfigError(ValueError):
    """rateLimits를 읽을 수 없음 → 시작 거부 (D-113)."""


@dataclass(frozen=True)
class Window:
    interval_ms: int
    limit: int


@dataclass(frozen=True)
class RateLimits:
    weight: tuple[Window, ...]
    orders: tuple[Window, ...]


def parse_rate_limits(raw: object) -> RateLimits:
    if not isinstance(raw, list):
        raise RateLimitConfigError("rateLimits가 목록이 아님")
    weight: list[Window] = []
    orders: list[Window] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RateLimitConfigError("rateLimits 항목 형식 아님")
        kind, interval, num, limit = (item.get(k) for k in ("rateLimitType", "interval", "intervalNum", "limit"))
        if interval not in _INTERVAL_MS:
            raise RateLimitConfigError(f"알 수 없는 interval: {interval}")
        for v in (num, limit):
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise RateLimitConfigError("intervalNum·limit은 양의 정수")
        w = Window(_INTERVAL_MS[str(interval)] * int(num), int(limit))  # type: ignore[arg-type]
        if kind == "REQUEST_WEIGHT":
            weight.append(w)
        elif kind == "ORDERS":
            orders.append(w)
        elif kind != "RAW_REQUESTS":
            raise RateLimitConfigError(f"알 수 없는 rateLimitType: {kind}")
    if not weight:
        raise RateLimitConfigError("REQUEST_WEIGHT 한도 없음")
    return RateLimits(tuple(weight), tuple(orders))


@dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str = ""
    wait_s: float = 0.0


class WeightBudget:
    def __init__(self, limits: RateLimits, now_ms: Callable[[], int]) -> None:
        self._limits = limits
        self._now = now_ms
        self._lock = threading.Lock()
        self._used: dict[int, tuple[int, int]] = {}     # 간격 ms → (창 번호, 사용량)
        self._orders: dict[int, tuple[int, int]] = {}
        self._cooldown_until = 0
        self._banned = False

    # --- 조회 ---
    def weight_budget(self, interval_ms: int) -> int:
        w = next(w for w in self._limits.weight if w.interval_ms == interval_ms)
        return w.limit * BUDGET_FRACTION_PERCENT // 100

    def used_weight(self, interval_ms: int) -> int:
        with self._lock:
            return self._current(self._used, interval_ms)

    def order_count(self, interval_ms: int) -> int:
        with self._lock:
            return self._current(self._orders, interval_ms)

    @property
    def banned(self) -> bool:
        return self._banned

    # --- 허가 ---
    def admit(self, weight: int, priority: CallPriority) -> Admission:
        """weight만큼 쓸 수 있으면 사용량에 더하고 ok. 아니면 아무것도 바꾸지 않고 보류 사유."""
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
            raise ValueError("weight는 0 이상 정수")
        with self._lock:
            blocked = self._blocked()
            if blocked:
                return blocked
            short = self._short(self._used, self._limits.weight, weight, priority, "가중치")
            if short:
                return short
            self._add(self._used, self._limits.weight, weight)
            return Admission(True)

    def admit_order(self, priority: CallPriority) -> Admission:
        """주문 1건(ORDERS 한도)과 실패 시 부과될 가중치 1을 함께 확인한다(성공 주문은 weight 0, BN §4)."""
        with self._lock:
            blocked = self._blocked()
            if blocked:
                return blocked
            short = self._short(self._orders, self._limits.orders, 1, priority, "주문 수") or \
                self._short(self._used, self._limits.weight, 1, priority, "가중치")
            if short:
                return short
            self._add(self._orders, self._limits.orders, 1)
            self._add(self._used, self._limits.weight, 1)
            return Admission(True)

    # --- 응답 반영 ---
    def observe_headers(self, headers: Mapping[str, str]) -> None:
        with self._lock:
            for name, value in headers.items():
                for pattern, table in ((_WEIGHT_HEADER, self._used), (_ORDER_HEADER, self._orders)):
                    m = pattern.fullmatch(name.strip())
                    if m and value.strip().isdigit():
                        interval = int(m.group(1)) * _UNIT_MS[m.group(2).upper()]
                        now_win = self._now() // interval
                        cur = self._current(table, interval)
                        table[interval] = (now_win, max(cur, int(value.strip())))

    def on_429(self, retry_after_s: float | None) -> None:
        wait = DEFAULT_RETRY_AFTER_S if retry_after_s is None or retry_after_s < 0 else retry_after_s
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._now() + int(wait * 1000))

    def on_418(self) -> None:
        with self._lock:
            self._banned = True

    def resume_after_ban(self) -> None:
        """사람이 재개할 때만 부른다(운영 절차, D-113). 자동 호출 금지."""
        with self._lock:
            self._banned = False
            self._cooldown_until = 0

    # --- 내부 ---
    def _blocked(self) -> Admission | None:
        if self._banned:
            return Admission(False, "HTTP 418 IP 차단 — 사람 재개 전 모든 호출 중단")
        now = self._now()
        if now < self._cooldown_until:
            return Admission(False, "HTTP 429 대기 중", (self._cooldown_until - now) / 1000)
        return None

    def _current(self, table: dict[int, tuple[int, int]], interval_ms: int) -> int:
        win, used = table.get(interval_ms, (-1, 0))
        return used if win == self._now() // interval_ms else 0

    def _short(self, table: dict[int, tuple[int, int]], windows: tuple[Window, ...], amount: int,
               priority: CallPriority, label: str) -> Admission | None:
        for w in windows:
            cap = (w.limit * BUDGET_FRACTION_PERCENT // 100) * TIER_PERCENT[priority] // 100
            if self._current(table, w.interval_ms) + amount > cap:
                return Admission(False, f"{label} 예산 부족({priority.name}, 창 {w.interval_ms // 1000}초)")
        return None

    def _add(self, table: dict[int, tuple[int, int]], windows: tuple[Window, ...], amount: int) -> None:
        for w in windows:
            table[w.interval_ms] = (self._now() // w.interval_ms, self._current(table, w.interval_ms) + amount)
