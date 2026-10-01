""".env 비밀 파일 파서 (R-12, R-11, CLAUDE.md §6).

값은 `Secret`에 담아 str·repr·format·로그 어디로도 나오지 않게 한다. 꺼내려면 `reveal()`을 명시적으로 부른다.
오류 메시지에는 줄 번호와 키 이름만 담고 줄 내용·값은 담지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from .errors import ConfigError

_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class EnvFileError(ConfigError):
    pass


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"

    def __format__(self, spec: str) -> str:
        return format("***", spec)

    __hash__ = None  # type: ignore[assignment]  # 값 기반 해시·비교로 내용이 새지 않게 한다

    def __eq__(self, other: object) -> bool:
        return self is other


class SecretStore:
    def __init__(self, items: Mapping[str, Secret]) -> None:
        self._items = dict(items)

    def has(self, key: str) -> bool:
        return key in self._items

    def get(self, key: str) -> Secret:
        return self._items[key]

    def keys(self) -> list[str]:
        return list(self._items)

    def __repr__(self) -> str:
        return f"SecretStore(keys={self.keys()})"

    __str__ = __repr__


def parse_env_text(text: str) -> SecretStore:
    items: dict[str, Secret] = {}
    for number, line in enumerate(text.lstrip("﻿").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE.match(stripped)
        if match is None:
            raise EnvFileError(f"줄 {number}: KEY=VALUE 형식이 아니다 (키는 대문자·숫자·밑줄)")
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in items:
            raise EnvFileError(f"줄 {number}: 키 {key}가 중복되었다")
        items[key] = Secret(value)
    return SecretStore(items)


def parse_env_file(path: Path) -> SecretStore:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvFileError(f"자격정보 파일을 읽을 수 없다: {path.name} ({type(exc).__name__})") from exc
    try:
        return parse_env_text(text)
    except EnvFileError as exc:
        raise EnvFileError([f"{path.name} {p}" for p in exc.problems]) from exc
