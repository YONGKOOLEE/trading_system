"""M2 판정 입력 일봉 — 실시간·백테스트 공용 전처리 (03 D-088, V-112·V-76, DP-82 [임시 — 사용자 확인 대기]).

- as_of 시점에 확정된 1d 봉만 쓴다(봉 시작 UTC 날짜 + 1일 ≤ as_of, D-108·V-142). 날짜형이 아닌 봉은 쓰지 않는다.
- 24/7 시장이라 모든 UTC 날짜에 봉이 있어야 한다. 확정 봉 사이에 빠진 날이 있으면 **마지막 빈 날 뒤의 연속 구간만** 넘긴다 —
  전략은 빈 날을 가로지르는 창으로 판정하지 않는다(필요한 봉 수가 모자라면 전략이 판정을 건너뛴다 → 신호 없음).
- 일봉 유효(daily_ok) = 넘기는 봉이 있고 마지막 봉이 어제(UTC) 이후. 시세·거래 규칙 등 나머지 조건은 호출부가 더한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta

from trading_system.domain.market_time import M2_TIME, daily_bar_is_final
from trading_system.domain.strategy import Bar

ONE_DAY = timedelta(days=1)


def final_daily(bars: Iterable[Bar], as_of: datetime) -> tuple[Bar, ...]:
    """as_of 시점에 확정된 1d 봉만 (시각 순서 유지)."""
    return tuple(b for b in bars if isinstance(b.at, date) and not isinstance(b.at, datetime)
                 and daily_bar_is_final(b.at, as_of))


def contiguous_tail(bars: tuple[Bar, ...]) -> tuple[Bar, ...]:
    """끝에서부터 UTC 날짜가 하루씩 이어지는 구간 (빈 날 직후부터 끝까지). 입력은 날짜 오름차순."""
    if not bars:
        return ()
    k = len(bars) - 1
    while k > 0 and bars[k - 1].at == bars[k].at - ONE_DAY:   # type: ignore[operator]
        k -= 1
    return bars[k:]


def m2_daily_input(bars: Iterable[Bar], as_of: datetime,
                   finalize: Callable[[Iterable[Bar], datetime], tuple[Bar, ...]] = final_daily,
                   ) -> tuple[tuple[Bar, ...], bool]:
    """(전략에 넘길 확정 일봉, 일봉 유효). 실시간(SpotProfile._view)·백테스트(backtest_m2)가 같이 부른다.
    finalize는 확정 봉 선택 함수 — 백테스트는 같은 규칙의 모듈 함수(final_bars)를 넘겨 교란 검사 시험이 바꿔 끼울 수 있게 한다."""
    tail = contiguous_tail(tuple(finalize(bars, as_of)))
    yesterday = M2_TIME.local(as_of).date() - ONE_DAY
    return tail, bool(tail) and tail[-1].at >= yesterday   # type: ignore[operator]
