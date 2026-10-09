"""DB 메타의 시장·브로커 (CP-024 D-097(3)(4), 03 D-090 확장, T-M2-08, V-148·V-161).

- 메타 키 `market`·`broker`(meta 키-값 표). 표·열을 바꾸지 않으므로 스키마 버전은 그대로다.
- 새 DB: 생성 시 기록한다(created=True). 메타가 있으면 설정과 정확히 같아야 연다 — 다르면 MarketMismatch(ModeMismatch의
  하위 형식이라 기존 모드 불일치 처리 경로가 그대로 거부한다).
- 메타 없는 기존 DB(T-M2-08 이전에 만든 M1 DB): 설정이 M1이면 열기를 허용하고 **쓰지 않는다**(M1 실행 중 DB를 바꾸지 않음 —
  M1 동작 불변). 1회 기록은 사람이 migrate 도구로 한다: 백업 뒤 기록(03 D-060, M1 PAPER-1 중간 금지). 설정이 M2면 거부.
- 메타가 한쪽만 있으면 손상으로 보고 거부한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market

from .repository import ModeMismatch, Repository, StorageFailure

META_MARKET = "market"
META_BROKER = "broker"


class MarketMismatch(ModeMismatch):
    """DB의 시장·브로커 메타가 설정과 다름 (D-097, V-148·V-161)."""


def read_market_meta(conn: sqlite3.Connection) -> tuple[str, str] | None:
    rows = dict(conn.execute("SELECT key, value FROM meta WHERE key IN (?, ?)", (META_MARKET, META_BROKER)).fetchall())
    if not rows:
        return None
    if META_MARKET not in rows or META_BROKER not in rows:
        raise MarketMismatch(f"DB 시장 메타가 일부만 있다({', '.join(sorted(rows))}) — 손상 의심, 열지 않는다")
    return rows[META_MARKET], rows[META_BROKER]


def _verify(meta: tuple[str, str] | None, market: Market, broker: str) -> bool:
    """메타가 설정과 같으면 True, 메타 없음 + M1이면 False(기록 필요 없음·허용). 그 밖은 MarketMismatch."""
    if meta is None:
        if market is Market.M1:
            return False
        raise MarketMismatch(f"시장 메타 없는 기존 DB를 {market.value} 설정으로 열지 않는다 "
                             "(메타 없는 DB는 T-M2-08 이전 M1 DB — 다른 시장 디렉터리인지 확인)")
    if meta != (market.value, broker):
        raise MarketMismatch(f"이 DB는 {meta[0]}·{meta[1]}용이다 (설정 {market.value}·{broker}) — "
                             "다른 시장의 체크아웃·DB인지 확인")
    return True


def bind_market(conn: sqlite3.Connection, market: Market, broker: str, *, created: bool) -> None:
    """엔진·운영 도구가 DB를 열 때. created = 이번에 새로 만든 DB(열기 전 파일 없음)."""
    meta = read_market_meta(conn)
    if meta is None and created:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (META_MARKET, market.value))
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (META_BROKER, broker))
        return
    _verify(meta, market, broker)


def check_db_file(path: Path, mode: Mode, market: Market, broker: str) -> None:
    """DB를 만들거나 쓰지 않고 검사한다(시작 검사용). 파일이 없으면 통과 — 생성 시 기록된다."""
    p = path.resolve()
    if not p.exists():
        return
    try:
        conn = sqlite3.connect(f"file:{quote(p.as_posix(), safe='/:')}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise StorageFailure(f"DB 열기 실패 ({type(exc).__name__})") from exc
    try:
        try:
            mode_row = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            meta = read_market_meta(conn)
        except sqlite3.Error as exc:
            raise StorageFailure(f"DB 메타 읽기 실패 ({type(exc).__name__}) — 이 시스템 DB가 아니거나 잠김") from exc
    finally:
        conn.close()
    if mode_row is None or mode_row[0] != mode.value:
        raise ModeMismatch(f"이 저장소는 {mode_row[0] if mode_row else '모드 기록 없음'} 모드로 만들어졌다 "
                           f"(요청 모드 {mode.value})")
    _verify(meta, market, broker)


def stamp_market_meta(db: Path, backup_dir: Path, mode: Mode, market: Market, broker: str) -> str:
    """메타 없는 기존 M1 DB에 market·broker를 1회 기록한다(백업 선행). "기록" 또는 "이미 기록됨".

    사람이 migrate 도구로 실행한다(M1 PAPER-1 중간 금지는 운영 규칙 — 사용자 확인). M2 설정이면 거부(쓰지 않음·백업 없음)."""
    from .maintenance import backup
    check_db_file(db, mode, market, broker)        # 다른 시장·다른 모드면 여기서 거부 (쓰기 전)
    if not db.exists():
        raise StorageFailure(f"DB 없음: {db}")
    repo = Repository(db, mode)
    try:
        if read_market_meta(repo.connection) is not None:
            return "이미 기록됨"
    finally:
        repo.close()
    backup(db, backup_dir)
    repo = Repository(db, mode)
    try:
        with repo.tx():
            if read_market_meta(repo.connection) is not None:   # 백업 사이에 다른 도구가 기록한 경우
                _verify(read_market_meta(repo.connection), market, broker)
                return "이미 기록됨"
            bind_market(repo.connection, market, broker, created=True)
    finally:
        repo.close()
    return "기록"
