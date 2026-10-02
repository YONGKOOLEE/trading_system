"""모듈 의존 방향 검사기 (03-design §7, D-042, 04 V-93).

표준 라이브러리 ast로 import와 금지 호출을 읽어 규칙 위반을 찾는다.
규칙을 바꾸려면 03-design §7을 먼저 CP로 개정한다.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_PACKAGE = "trading_system"

# None = 내부 패키지 제약 없음 (03 §7에 규칙이 없는 패키지, app import 금지만 적용)
INTERNAL_RULES: dict[str, frozenset[str] | None] = {
    "app": None,
    "domain": frozenset(),
    "strategy": frozenset({"domain"}),
    "gate": frozenset({"domain", "storage"}),
    "execution": frozenset({"domain", "broker", "storage"}),
    # 03 §7은 broker.kiwoom만 명시. fake도 같은 인터페이스(D-062)이므로 broker 전체에 적용한다.
    "broker": frozenset({"domain", "broker"}),
    # 03 §7 v0.5 (CP-018 D-5)
    "evaluation": frozenset({"domain", "strategy", "marketdata"}),
    "marketdata": frozenset({"domain", "broker"}),
    "reconciliation": frozenset({"domain", "storage", "broker"}),
    "halt": frozenset({"domain", "gate", "execution", "storage"}),
    "ops": frozenset({"domain", "gate", "execution", "halt", "storage", "config"}),
    "monitoring": frozenset({"domain", "storage"}),
    "reporting": frozenset({"domain", "storage"}),
    "storage": frozenset({"domain"}),
    "config": frozenset({"domain", "storage"}),
}

# 표준 라이브러리만 허용하는 패키지
STDLIB_ONLY = frozenset({"domain", "strategy"})

# 전략에서 금지하는 표준 라이브러리 (네트워크·저장소·시계·난수·프로세스·파일, 03 §7)
STRATEGY_FORBIDDEN_STDLIB = frozenset({
    "socket", "ssl", "http", "urllib", "sqlite3", "time", "random", "asyncio",
    "subprocess", "threading", "multiprocessing", "os", "pathlib", "io", "shutil",
    "tempfile", "secrets",
})

# 전략에서 금지하는 호출: 현재 시각 조회 (D-002)
FORBIDDEN_CLOCK_ATTRS = frozenset({"now", "utcnow", "today"})


@dataclass(frozen=True)
class Violation:
    module: str
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line}: {self.reason}"


def module_name(path: Path, src_root: Path) -> str:
    rel = path.relative_to(src_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def subpackage_of(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != ROOT_PACKAGE:
        return None
    return parts[1]


def _resolve_relative(current: str, is_package: bool, level: int, target: str | None) -> str:
    base = current.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([target] if target else []))


def imported_modules(tree: ast.AST, current: str, is_package: bool) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(current, is_package, node.level, node.module)
            else:
                base = node.module or ""
            found.append((base, node.lineno))
            # `from trading_system import gate` 형태는 하위 모듈 import로도 본다
            if base == ROOT_PACKAGE:
                found.extend((f"{base}.{alias.name}", node.lineno) for alias in node.names)
    return found


def _clock_calls(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in FORBIDDEN_CLOCK_ATTRS
        ):
            lines.append(node.lineno)
    return lines


def check_source(source: str, module: str, is_package: bool = False) -> list[Violation]:
    """한 모듈의 소스를 규칙으로 검사한다."""
    tree = ast.parse(source)
    owner = subpackage_of(module)
    violations: list[Violation] = []
    if owner is None:
        return violations
    if owner not in INTERNAL_RULES:
        violations.append(Violation(module, 1, f"규칙 미정 패키지 '{owner}' (03 §7·import_rules 갱신 필요)"))
        return violations

    allowed = INTERNAL_RULES[owner]
    for name, line in imported_modules(tree, module, is_package):
        top = name.split(".")[0]
        target = subpackage_of(name)
        if top == ROOT_PACKAGE:
            if target is None or target == owner:
                continue
            if target == "app" and owner != "app":
                violations.append(Violation(module, line, f"'{owner}'는 app을 import할 수 없음"))
            elif allowed is not None and target not in allowed:
                violations.append(Violation(module, line, f"'{owner}' → '{target}' 의존 금지"))
            continue
        if owner in STDLIB_ONLY and top not in sys.stdlib_module_names and top != "__future__":
            violations.append(Violation(module, line, f"'{owner}'는 표준 라이브러리만 사용 가능: '{top}'"))
        if owner == "strategy" and top in STRATEGY_FORBIDDEN_STDLIB:
            violations.append(Violation(module, line, f"전략에서 금지된 모듈: '{top}'"))

    if owner == "strategy":
        for line in _clock_calls(tree):
            violations.append(Violation(module, line, "전략에서 현재 시각 조회 금지 (D-002)"))
    return violations


def check_tree(src_root: Path) -> list[Violation]:
    """src 아래 trading_system 패키지 전체를 검사한다."""
    violations: list[Violation] = []
    for path in sorted((src_root / ROOT_PACKAGE).rglob("*.py")):
        module = module_name(path, src_root)
        source = path.read_text(encoding="utf-8")
        violations.extend(check_source(source, module, is_package=path.name == "__init__.py"))
    return violations
