"""의존 방향 검사 (04 V-93, 03-design §7·§13, D-042)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.import_rules import INTERNAL_RULES, ROOT_PACKAGE, check_source, check_tree

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def test_src_has_no_dependency_violations() -> None:
    violations = check_tree(SRC_ROOT)
    assert violations == [], "\n".join(str(v) for v in violations)


def test_every_rule_package_exists() -> None:
    package_root = SRC_ROOT / ROOT_PACKAGE
    missing = [name for name in INTERNAL_RULES if not (package_root / name / "__init__.py").is_file()]
    assert missing == [], f"03 §13 구조에 없는 패키지: {missing}"


def test_every_existing_package_has_rule() -> None:
    package_root = SRC_ROOT / ROOT_PACKAGE
    existing = {p.name for p in package_root.iterdir() if (p / "__init__.py").is_file()}
    assert existing - set(INTERNAL_RULES) == set()


# --- 검사기 자체가 위반을 잡아내는지 확인 (오류 경로) ---

@pytest.mark.parametrize(
    ("module", "source", "reason_part"),
    [
        ("trading_system.strategy.s1", "from trading_system.broker import base\n", "의존 금지"),
        ("trading_system.strategy.s1", "import trading_system.storage.repo\n", "의존 금지"),
        ("trading_system.strategy.s1", "from ..gate import checks\n", "의존 금지"),
        ("trading_system.strategy.s1", "import requests\n", "표준 라이브러리만"),
        ("trading_system.strategy.s1", "import sqlite3\n", "금지된 모듈"),
        ("trading_system.strategy.s1", "from time import monotonic\n", "금지된 모듈"),
        ("trading_system.strategy.s1", "import datetime\nx = datetime.datetime.now()\n", "현재 시각"),
        ("trading_system.domain.money", "import websockets\n", "표준 라이브러리만"),
        ("trading_system.domain.money", "from trading_system.gate import g\n", "의존 금지"),
        ("trading_system.broker.kiwoom.client", "from trading_system.gate import g\n", "의존 금지"),
        ("trading_system.broker.fake.broker", "from trading_system.strategy import s\n", "의존 금지"),
        ("trading_system.gate.checks", "from trading_system.execution import e\n", "의존 금지"),
        ("trading_system.execution.executor", "from trading_system.strategy import s\n", "의존 금지"),
        ("trading_system.storage.repo", "from trading_system.app import main\n", "app을 import"),
        ("trading_system.unknown.mod", "x = 1\n", "규칙 미정 패키지"),
    ],
)
def test_checker_detects_violation(module: str, source: str, reason_part: str) -> None:
    violations = check_source(source, module)
    assert any(reason_part in v.reason for v in violations), violations


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("trading_system.strategy.s1", "from decimal import Decimal\nfrom trading_system.domain import types\n"),
        ("trading_system.strategy.s1", "from ..domain import types\n"),
        ("trading_system.gate.checks", "from trading_system.storage import repo\nfrom trading_system.domain import t\n"),
        ("trading_system.execution.executor", "from trading_system.broker import base\n"),
        ("trading_system.broker.kiwoom.client", "from trading_system.broker import base\nimport json\n"),
        ("trading_system.app.main", "from trading_system.gate import g\nfrom trading_system.execution import e\n"),
    ],
)
def test_checker_allows_permitted_imports(module: str, source: str) -> None:
    assert check_source(source, module) == []
