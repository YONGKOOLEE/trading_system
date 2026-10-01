"""비밀 파일 정책 (04 V-109, R-12, CLAUDE.md §6).

- 저장소에 `.env.*`(예제 제외)가 추적되지 않는다.
- `.env.example`에는 자리표시자와 BACKTEST 모드만 있다.
- `.gitignore`가 비밀 파일 패턴을 제외한다.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from trading_system.config.envfile import parse_env_file

ROOT = Path(__file__).resolve().parents[2]
SECRET_KEY = re.compile(r"(APP_KEY|SECRET|TOKEN|ACCOUNT_NO|PASSWORD)", re.IGNORECASE)
PLACEHOLDER = re.compile(r"^<[^<>\s][^<>]*>$")


def test_gitignore_excludes_secret_patterns() -> None:
    lines = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for needed in (".env", ".env.*", "!.env.example", "var/"):
        assert needed in lines, needed


def test_no_secret_files_tracked() -> None:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git을 실행할 수 없음 (미실행)")
    tracked = [Path(p) for p in out.stdout.splitlines()]
    offenders = [str(p) for p in tracked if p.name.startswith(".env") and p.name != ".env.example"]
    assert offenders == []
    assert not [str(p) for p in tracked if p.parts and p.parts[0] == "var"]


def test_env_example_is_backtest_and_placeholder_only() -> None:
    store = parse_env_file(ROOT / ".env.example")
    assert store.get("TRADE_MODE").reveal() == "BACKTEST"
    assert store.keys() == ["TRADE_MODE"]  # 실제로 읽히는 키는 모드 하나뿐

    for n, line in enumerate((ROOT / ".env.example").read_text(encoding="utf-8").splitlines(), start=1):
        body = line.lstrip("# ").strip()
        if "=" not in body:
            continue
        key, _, value = body.partition("=")
        if SECRET_KEY.search(key):
            assert PLACEHOLDER.match(value.strip()), f".env.example {n}줄: 비밀 키의 값은 자리표시자여야 한다"
