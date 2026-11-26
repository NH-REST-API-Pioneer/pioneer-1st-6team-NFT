"""주문이 나가는 길은 하나다(안전 불변식 2). `src/nh_mcp` 전체를 AST로 훑는다.

- NH로 신규 주문을 보내는 `send_order`를 쓰는 곳은 `OrderService._submit` 하나.
- `_submit`을 부르는 곳은 `OrderService.place`(토큰 소진과 리스크 재검사 뒤)와
  `OrderService.place_auto`(같은 리스크 검사와 승인 범위 규칙 뒤) 둘(사람 승인 2026-11-25).
- 취소를 보내는 `send_cancel`을 쓰는 곳은 `OrderService.cancel` 하나.
- NH 주문·취소 경로 상수(`ORDER`, `CANCEL`)를 읽는 곳은 `send_order`·`send_cancel` 안뿐.

속성 호출만 세면 `f = market.send_order` 같은 우회를 놓치므로 이름을 읽는 곳을 모두 센다.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "nh_mcp"

type Place = tuple[str, str]  # (src/nh_mcp 아래 파일, 둘러싼 함수의 이름 경로)


class _Uses(ast.NodeVisitor):
    """이름(속성·변수·문자열)을 읽는 곳을 함수 이름 경로와 함께 모은다. 정의와 대입은 빼다."""

    def __init__(self, file: str, names: set[str]) -> None:
        self.file = file
        self.names = names
        self.scope: list[str] = []
        self.found: dict[str, list[Place]] = {name: [] for name in names}

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def _record(self, name: str) -> None:
        if name in self.names:
            self.found[name].append((self.file, ".".join(self.scope) or "<module>"))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._record(node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record(node.id)

    def visit_Constant(self, node: ast.Constant) -> None:
        # getattr(market, "send_order") 같은 우회
        if isinstance(node.value, str):
            self._record(node.value)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # 경로 상수를 다른 모듈로 가져가는 것도 쓰는 곳으로 센다.
        for alias in node.names:
            self._record(alias.name)


def uses(*names: str, root: Path = SRC) -> dict[str, list[Place]]:
    found: dict[str, list[Place]] = {name: [] for name in names}
    for path in sorted(root.rglob("*.py")):
        visitor = _Uses(path.relative_to(root).as_posix(), set(names))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        for name, places in visitor.found.items():
            found[name] += places
    return found


def test_send_order_is_used_only_in_submit() -> None:
    assert uses("send_order")["send_order"] == [("services/orders.py", "OrderService._submit")]


def test_submit_is_called_only_from_place_and_place_auto() -> None:
    assert uses("_submit")["_submit"] == [
        ("services/orders.py", "OrderService.place"),
        ("services/orders.py", "OrderService.place_auto"),
    ]


def test_send_cancel_is_used_only_in_cancel() -> None:
    assert uses("send_cancel")["send_cancel"] == [("services/orders.py", "OrderService.cancel")]


def test_the_nh_order_and_cancel_paths_are_read_only_by_the_two_senders() -> None:
    found = uses("ORDER", "CANCEL")

    assert found["ORDER"] == [("markets/domestic.py", "DomesticMarket.send_order")]
    assert found["CANCEL"] == [("markets/domestic.py", "DomesticMarket.send_cancel")]


def test_the_scan_catches_a_second_caller(tmp_path: Path) -> None:
    """검사 자체가 무는지: 다른 파일에서 부르거나 이름을 빼 두면 잡힌다."""
    (tmp_path / "sneaky.py").write_text(
        "async def go(market, service):\n"
        "    send = market.send_order\n"
        "    await getattr(market, 'send_cancel')(1, 2)\n"
        "    await service._submit(1, 2, {}, 0, run_id=None)\n",
        encoding="utf-8",
    )

    found = uses("send_order", "send_cancel", "_submit", root=tmp_path)

    assert found == {
        "send_order": [("sneaky.py", "go")],
        "send_cancel": [("sneaky.py", "go")],
        "_submit": [("sneaky.py", "go")],
    }
