"""하네스는 표준 라이브러리만 쓴다. nh_mcp와 설치 패키지는 전략 이미지에 없다."""

import ast
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "src" / "nh_mcp" / "sandbox" / "harness.py"


def imported_modules(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # 상대 import(level > 0)는 nh_mcp 안을 가리키므로 그대로 걸리게 둔다.
            names.append("." * node.level + (node.module or ""))
    return names


def test_checker_sees_every_import_form() -> None:
    source = "import os\nimport json as j\nfrom typing import IO\nfrom . import x\n"
    assert imported_modules(source) == ["os", "json", "typing", "."]


def test_harness_imports_only_the_standard_library() -> None:
    names = imported_modules(HARNESS.read_text(encoding="utf-8"))
    assert names
    assert not [name for name in names if "nh_mcp" in name]
    offenders = [name for name in names if name.split(".")[0] not in sys.stdlib_module_names]
    assert offenders == []
