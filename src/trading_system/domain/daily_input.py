"""M2 판정 입력 일봉 — 실시간·백테스트 공용 전처리 (03 D-088, V-112·V-76, DP-82 확정 A-126 ②).

- as_of 시점에 확정된 1d 봉만 쓴다(봉 시작 UTC 날짜 + 1일 ≤ as_of, D-108·V-142). 날짜형이 아닌 봉은 쓰지 않는다.
  max_bars를 주면 확정 봉 가운데 최근 max_bars개만(1분봉 실행기 = 실운영 1d 조회 limit 200 − 진행 중 봉 1 = 199, CP-041 D-276 (2)).
- 24/7 시장이라 모든 UTC 날짜에 봉이 있어야 한다. 빈 날이 있거나 어제 봉이 없으면 판정 입력이 **불완전**하다.
- [DP-82 (가), A-126 ②] 불완전할 때 신규 진입·증액만 막고 감액·청산 판정은 계속한다(DP-78과 같은 원칙):
  · 진입용(bars) = **마지막 빈 날 뒤의 연속 구간**(종전 동작) — 진입은 빈 날을 가로지르는 창으로 판정하지 않는다.
    진입 유효(ok) = 그 구간이 있고 마지막 봉이 어제(UTC) 이후.
  · 청산용(exit_bars) = 빈 날을 건너뛴 확정 봉 전체(채우지 않음). 판정 실행기(evaluation/runner)가 불완전한 종목에 대해
    이 봉으로 전략을 한 번 더 불러 감액·청산 신호만 쓴다.
  · missing = exit_bars 첫 봉부터 어제까지 빠진 UTC 날짜(경고 문구용, DP-82 (나)).
시세·거래 규칙 등 나머지 조건은 호출부가 data_ok에 더한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DailyInput:
    bars: tuple[Bar, ...]          # 진입용 — 빈 날 뒤 연속 구간
    ok: bool                       # 진입용 일봉 유효 (구간이 있고 마지막 봉이 어제 이후)
    exit_bars: tuple[Bar, ...]     # 청산용 — 빈 날을 건너뛴 확정 봉 전체
    missing: tuple[date, ...]      # exit_bars 첫 봉 ~ 어제 사이 빠진 날짜 (봉이 하나도 없으면 ())

    @property
    def complete(self) -> bool:
        """빈 날 없음 AND 어제 봉 있음 — 이때만 한 번 판정(종전과 같음)."""
        return self.ok and not self.missing and len(self.bars) == len(self.exit_bars)

    @property
    def daily_exit(self) -> tuple[Bar, ...] | None:
        """InstrumentView.daily_exit 값: 불완전할 때만 청산용 봉, 완전하면 None."""
        return None if self.complete else self.exit_bars


def m2_daily_input(bars: Iterable[Bar], as_of: datetime,
                   finalize: Callable[[Iterable[Bar], datetime], tuple[Bar, ...]] = final_daily, *,
                   max_bars: int | None = None) -> DailyInput:
    """실시간(SpotProfile._view)·백테스트(backtest_m2·backtest_m2_minute)가 같이 부른다 (D-088).
    finalize는 확정 봉 선택 함수 — 백테스트는 같은 규칙의 모듈 함수(final_bars)를 넘겨 교란 검사 시험이 바꿔 끼울 수 있게 한다."""
    final = tuple(finalize(bars, as_of))
    if max_bars is not None:
        final = final[-max_bars:] if max_bars > 0 else ()
    tail = contiguous_tail(final)
    yesterday = M2_TIME.local(as_of).date() - ONE_DAY
    missing: list[date] = []
    if final:
        have = {b.at for b in final}
        d = final[0].at
        while d <= yesterday:   # type: ignore[operator]
            if d not in have:
                missing.append(d)   # type: ignore[arg-type]
            d += ONE_DAY   # type: ignore[operator]
    return DailyInput(tail, bool(tail) and tail[-1].at >= yesterday, final, tuple(missing))   # type: ignore[operator]
