"""U-B 사람이 제출한 종목 목록 (CP-034 D-198, A-092 Q-8). 표준 라이브러리만.

- 파일 형식: 한 줄에 `code[,memo]`, UTF-8(BOM 허용). 코드 = KRX 6자리 영숫자(대문자·숫자). 빈 줄은 건너뛴다.
  첫 쉼표 뒤는 전부 메모(쉼표 포함 가능). 형식·중복 오류가 하나라도 있으면 제출 전체를 거부한다.
- 버전: 목록 ID별 일련번호(1부터), 내용 해시(정규화한 `code,memo` 줄의 SHA-256), 제출 시각, 제출 채널.
- 적용 시점: 판정 시각 T에는 T − 10분(APPLY_LAG) 이전(같은 시각 포함)에 제출 완료된 최신 버전만 쓴다.
- 등재 시각(listed_at): 적용 버전까지 그 종목이 **연속으로** 들어 있기 시작한 버전의 제출 시각(KST).
  빠졌다 다시 들어오면 새로 시작한다. 적용 버전에 없으면 None.
- 계좌 감시 종목 상한: 모든 전략 목록 합집합 + 보유 ≤ 15 (CP-032 단계 3a 뒤 재검토).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .clock import KST, require_aware

APPLY_LAG = timedelta(minutes=10)          # Q-8: 판정 T에는 T − 10분 이전 제출분만
MAX_WATCHED = 15                           # Q-8: 계좌 감시 종목 상한
SUBMIT_CHANNEL = "LOCAL"                   # Q-8: 목록 제출은 로컬 도구만 (원격·대화 불가)
LIST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
CODE_RE = re.compile(r"[0-9A-Z]{6}")


@dataclass(frozen=True)
class ListEntry:
    code: str
    memo: str = ""


@dataclass(frozen=True)
class ListVersion:
    list_id: str
    version: int
    content_hash: str
    submitted_at: datetime
    channel: str
    codes: tuple[str, ...]


def valid_list_id(list_id: object) -> bool:
    return isinstance(list_id, str) and LIST_ID_RE.fullmatch(list_id) is not None


def parse_list_file(data: bytes) -> tuple[tuple[ListEntry, ...], list[str]]:
    """파일 내용 → (항목, 문제 목록). 문제가 있으면 항목을 쓰지 않는다(제출 거부)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return (), ["파일 인코딩이 UTF-8이 아님 (BOM은 허용)"]
    entries: list[ListEntry] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        code, _, memo = line.partition(",")
        code, memo = code.strip(), memo.strip()
        if not CODE_RE.fullmatch(code):
            problems.append(f"{lineno}행: 종목코드 형식 오류 {code[:20]!r} (KRX 6자리 영숫자, 대문자)")
            continue
        if code in seen:
            problems.append(f"{lineno}행: 종목코드 중복 {code} ({seen[code]}행과 같음)")
            continue
        seen[code] = lineno
        entries.append(ListEntry(code, memo))
    return tuple(entries), problems


def content_hash(entries: Sequence[ListEntry]) -> str:
    text = "\n".join(f"{e.code},{e.memo}" for e in entries)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def select_version(versions: Sequence[ListVersion], at: datetime) -> ListVersion | None:
    """판정 시각 at에 쓸 버전: at − 10분 이전(같은 시각 포함)에 제출된 것 중 번호가 가장 큰 버전."""
    require_aware(at)
    cutoff = at - APPLY_LAG
    usable = [v for v in versions if v.submitted_at <= cutoff]
    return max(usable, key=lambda v: v.version) if usable else None


def listed_since(versions: Sequence[ListVersion], applied: ListVersion, code: str) -> datetime | None:
    """applied 버전까지 code가 연속으로 들어 있기 시작한 버전의 제출 시각(KST). applied에 없으면 None."""
    if code not in applied.codes:
        return None
    ordered = sorted((v for v in versions if v.list_id == applied.list_id and v.version <= applied.version),
                     key=lambda v: v.version)
    start = applied
    for v in reversed(ordered):
        if code not in v.codes:
            break
        start = v
    return start.submitted_at.astimezone(KST)


def watched_set(lists: Iterable[Iterable[str]], holdings: Iterable[str]) -> set[str]:
    """계좌 감시 종목 = 모든 전략 목록 합집합 + 보유 (D-198 (6))."""
    out: set[str] = set(holdings)
    for codes in lists:
        out |= set(codes)
    return out
