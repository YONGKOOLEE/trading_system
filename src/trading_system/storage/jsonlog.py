"""운영 로그 (JSON Lines) + 마스킹 (R-11, 03 D-059·§8.3, 04 V-104).

- 위치: `var/<mode>/logs/<이름>-YYYYMMDD.jsonl`. 보존 90일, 지난 로그는 사용자 명령으로 압축 보관 후 삭제 (D-089).
- 쓰기 전에 마스킹한다(원문을 먼저 저장하지 않음):
  - 민감 키(계좌번호·토큰·앱키·시크릿·비밀번호 등)는 값 전체를 "***"로.
  - 등록된 비밀값(앱키·토큰 등 실제 값)은 어디에 나와도 "***"로.
  - 계좌번호 형태(8·10자리 숫자, 8-2·4-4-2·3-6-2자리)는 "*"로. 같은 자릿수의 금액·날짜도 가려질 수 있다(과잉 마스킹 허용).
  - 문장 속 자격정보: `Bearer <값>`, URL 질의의 token·signature·apikey 등 값은 "***"로.
- 파일 날짜는 KST 기준(거래일·일일 보고와 같은 날짜).
- 주문 요청·응답 원문은 로그에 넣지 않는다(호출자 책임, 필요한 필드만).
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trading_system.domain.clock import KST

SENSITIVE_KEYS = re.compile(r"(acnt|account_?no|acct_?no|계좌번호|token|app_?key|api_?key|secret|password|passwd|pin|"
                            r"authorization|signature|listen_?key|cookie|credential)", re.I)
ACCOUNT_LIKE = re.compile(r"(?<!\d)(\d{10}|\d{8}-\d{2}|\d{4}-\d{4}-\d{2}|\d{3}-\d{6}-\d{2}|\d{8})(?!\d)")
INLINE_SECRET = re.compile(r"(?i)(bearer\s+)[^\s\"',;]+|((?:access_?token|token|signature|api_?key|listen_?key|secret|"
                           r"appkey)=)[^&\s\"',;]+")
MASK = "***"


class Masker:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def text(self, s: str) -> str:
        for secret in self._secrets:
            s = s.replace(secret, MASK)
        s = INLINE_SECRET.sub(lambda m: (m.group(1) or m.group(2)) + MASK, s)
        return ACCOUNT_LIKE.sub(lambda m: "*" * len(m.group(0)), s)

    def value(self, key: str, v: object) -> object:
        if SENSITIVE_KEYS.search(key):
            return MASK
        if isinstance(v, Mapping):
            return {str(k): self.value(str(k), x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.value(key, x) for x in v]
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, int) and not isinstance(v, bool):
            masked = self.text(str(v))
            return v if masked == str(v) else masked
        return v


class JsonLogger:
    def __init__(self, directory: Path, name: str, masker: Masker) -> None:
        self._dir = directory
        self._name = name
        self._masker = masker

    def path_for(self, day: date) -> Path:
        return self._dir / f"{self._name}-{day:%Y%m%d}.jsonl"

    def write(self, event: Mapping[str, object], at: datetime | None = None) -> None:
        now = at or datetime.now(UTC)
        record = {"at": now.isoformat()}
        record.update({str(k): self._masker.value(str(k), v) for k, v in event.items()})
        self._dir.mkdir(parents=True, exist_ok=True)
        with self.path_for(now.astimezone(KST).date()).open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=lambda o: self._masker.text(str(o))) + "\n")


def archive_logs(directory: Path, older_than_days: int, today: date, archive_dir: Path | None = None) -> list[str]:
    """보존 기간이 지난 로그를 압축 보관한 뒤 원본을 지운다 (D-089). 사용자 명령으로만 호출한다.

    압축 파일을 다시 열어 내용이 같은지 확인한 뒤에만 원본을 지운다. 감사 DB는 건드리지 않는다.
    """
    cutoff = today - timedelta(days=older_than_days)
    targets = []
    for p in sorted(directory.glob("*.jsonl")):
        stamp = p.stem.rsplit("-", 1)[-1]
        if len(stamp) == 8 and stamp.isdigit() and date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:])) < cutoff:
            targets.append(p)
    if not targets:
        return []
    out_dir = archive_dir or directory / "archive"
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"logs-before-{cutoff:%Y%m%d}-{datetime.now(UTC):%Y%m%dT%H%M%S}.zip"
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in targets:
            z.write(p, p.name)
    with zipfile.ZipFile(zpath) as z:
        for p in targets:
            if z.read(p.name) != p.read_bytes():
                raise RuntimeError(f"압축 확인 실패: {p.name} — 원본을 지우지 않음")
    for p in targets:
        p.unlink()
    return [p.name for p in targets]
