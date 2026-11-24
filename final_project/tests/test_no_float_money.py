"""가격과 금액을 다루는 패키지에 float가 없게 막는다(안전 불변식 9). Decimal 또는 int를 쓴다."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "nh_mcp"
# 가격 · 수량 · 금액이 지나가는 곳. 시간 간격만 다루는 nh/의 전송 설정은 대상이 아니다.
MONEY_MODULES = ["domain.py", "envelope.py", "markets", "services", "tools", "guard", "strategies"]


def float_uses(source: str) -> list[int]:
    """float 이름(타입 주석·호출)과 실수 리터럴이 나온 줄."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Name) and node.id == "float") or (
            isinstance(node, ast.Constant) and isinstance(node.value, float)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def money_files() -> list[Path]:
    files: list[Path] = []
    for name in MONEY_MODULES:
        target = PACKAGE / name
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    return files


def test_checker_finds_every_kind_of_float() -> None:
    assert float_uses("price: float = 0") == [1]
    assert float_uses("x = float('1.5')") == [1]
    assert float_uses("fee = 0.5") == [1]
    assert float_uses("from decimal import Decimal\nprice = Decimal('350.25')\nqty = 2") == []


def test_money_modules_have_no_float() -> None:
    files = money_files()
    assert PACKAGE / "markets" / "domestic.py" in files
    offenders = [
        f"{path.relative_to(PACKAGE)}:{line}"
        for path in files
        for line in float_uses(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
