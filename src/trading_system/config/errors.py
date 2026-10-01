"""설정 오류. 검증에 실패하면 시작을 거부한다 (R-19, 01 §2.3.1-2).

메시지에는 문제 위치(키 이름·줄 번호)만 담고 값은 담지 않는다 (R-12).
"""

from __future__ import annotations

from collections.abc import Sequence


class ConfigError(Exception):
    def __init__(self, problems: str | Sequence[str]) -> None:
        self.problems: list[str] = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))
