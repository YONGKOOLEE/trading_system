"""바이낸스 패키지 경계 (CP-024 D-098·D-119, T-M2-10·11 완료 기준).

- 코드에 두는 바이낸스 주소는 테스트넷 도메인만 (실전·Demo 도메인 상수 없음).
- T-M2-10·11은 네트워크 없음: 바이낸스 패키지는 네트워크 라이브러리를 import하지 않는다(전송은 주입).
- 바이낸스 전용 타입은 broker/binance 밖에서 import하지 않는다 (공통 계약 연결은 T-M2-03~05).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "trading_system"
PKG = SRC / "broker" / "binance"
NETWORK_MODULES = {"requests", "websockets", "socket", "ssl", "http", "urllib3", "aiohttp", "httpx"}


def _strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [(n.lineno, n.value) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_package_exists() -> None:
    assert (PKG / "__init__.py").is_file()


def test_only_testnet_binance_hosts_in_code() -> None:
    bad = []
    for p in PKG.rglob("*.py"):
        for line, s in _strings(p):
            low = s.lower()
            if "binance" in low and ("://" in low or "." + "com" in low or ".vision" in low):
                hosts = [part for part in low.replace("/", " ").split() if "binance" in part]
                if any(not h.endswith("testnet.binance.vision") for h in hosts):
                    bad.append(f"{p.name}:{line}")
    assert bad == []


def test_no_network_library_imports() -> None:
    bad = []
    for p in PKG.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else []
            bad += [f"{p.name}:{node.lineno}:{n}" for n in names if n.split(".")[0] in NETWORK_MODULES]
    assert bad == []


def test_binance_types_not_imported_outside_package() -> None:
    bad = []
    for p in SRC.rglob("*.py"):
        if PKG in p.parents:
            continue
        text = p.read_text(encoding="utf-8")
        if "broker.binance" in text or "from .binance" in text:
            bad.append(str(p.relative_to(SRC)))
    assert bad == []
