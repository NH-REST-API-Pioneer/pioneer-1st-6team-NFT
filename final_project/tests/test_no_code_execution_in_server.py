"""서버 쪽에서는 코드 문자열을 실행하지 않는다(안전 불변식 12).

전략 코드는 해시하고 저장하고 실행기로 넘기기만 한다. 실행하는 곳은 전략 컨테이너 안의
`sandbox/harness.py` 하나다. 이름으로 부르는 `exec`·`eval`·`compile`, 그리고 `ast.parse`·
`ast.literal_eval`·`builtins.exec`·`builtins.eval`·`builtins.compile`을 막는다(`re.compile` 같은
다른 모듈의 메서드 호출은 코드 실행이 아니다).
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "nh_mcp"
HARNESS = PACKAGE / "sandbox" / "harness.py"
FORBIDDEN = {"exec", "eval", "compile"}
# 받는 쪽 이름 → 막는 메서드.
FORBIDDEN_ATTRIBUTES = {
    "ast": {"parse", "literal_eval"},
    "builtins": {"exec", "eval", "compile"},
}


def _called(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name) and func.id in FORBIDDEN:
        return func.id
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.attr in FORBIDDEN_ATTRIBUTES.get(func.value.id, set[str]())
    ):
        return f"{func.value.id}.{func.attr}"
    return None


def forbidden_calls(source: str) -> list[tuple[int, str]]:
    return sorted(
        (node.lineno, name)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and (name := _called(node.func)) is not None
    )


def server_files() -> list[Path]:
    return [path for path in sorted(PACKAGE.rglob("*.py")) if path != HARNESS]


def test_checker_finds_bare_calls() -> None:
    assert forbidden_calls("exec(code)") == [(1, "exec")]
    assert forbidden_calls("x = 1\nresult = eval('1')") == [(2, "eval")]
    assert forbidden_calls("compile(src, 'f', 'exec')") == [(1, "compile")]


def test_checker_finds_ast_and_builtins_calls() -> None:
    assert forbidden_calls("import ast\nast.parse(code)") == [(2, "ast.parse")]
    assert forbidden_calls("ast.literal_eval(text)") == [(1, "ast.literal_eval")]
    source = "import builtins\nbuiltins.exec(c)\nbuiltins.eval(c)\nbuiltins.compile(c, 'f', 'exec')"
    assert forbidden_calls(source) == [
        (2, "builtins.exec"),
        (3, "builtins.eval"),
        (4, "builtins.compile"),
    ]


def test_checker_leaves_other_methods_alone() -> None:
    source = "import re\nre.compile('a')\nobj.exec(1)\nobj.ast.parse(x)\nast.dump(tree)"
    assert forbidden_calls(source) == []


def test_the_scan_covers_the_server_and_skips_only_the_harness() -> None:
    files = server_files()
    assert HARNESS.exists() and HARNESS not in files
    assert PACKAGE / "sandbox" / "executor.py" in files
    assert PACKAGE / "services" / "strategy.py" in files


def test_server_code_never_runs_code_strings() -> None:
    found = {
        str(path.relative_to(PACKAGE)): calls
        for path in server_files()
        if (calls := forbidden_calls(path.read_text(encoding="utf-8")))
    }
    assert found == {}
