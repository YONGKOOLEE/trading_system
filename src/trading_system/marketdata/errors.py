"""시장 데이터 설정·형식 오류. 이 오류가 나면 그 데이터는 쓰지 않는다 (R-22)."""

from __future__ import annotations

from collections.abc import Sequence


class MarketDataError(Exception):
    def __init__(self, problems: str | Sequence[str]) -> None:
        self.problems: list[str] = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))
