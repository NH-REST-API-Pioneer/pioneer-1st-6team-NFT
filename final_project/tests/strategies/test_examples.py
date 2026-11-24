import ast
import runpy
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import cast

from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS

type OnBar = Callable[[dict[str, object], list[dict[str, object]], dict[str, object]], str]


def bar(close: str) -> dict[str, object]:
    c = Decimal(close)
    return {"open": str(c), "high": str(c), "low": str(c), "close": str(c), "volume": 1}


def load_on_bar(tmp_path: Path) -> OnBar:
    path = tmp_path / "strategy.py"
    path.write_text(MEAN_REVERSION, encoding="utf-8")
    namespace = runpy.run_path(str(path))
    on_bar = namespace["on_bar"]
    assert callable(on_bar)
    return cast(OnBar, on_bar)


def test_example_code_has_on_bar_with_three_parameters() -> None:
    tree = ast.parse(MEAN_REVERSION)
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "on_bar"]
    assert len(found) == 1
    assert [a.arg for a in found[0].args.args] == ["bar", "history", "params"]


def test_example_params_are_json_safe() -> None:
    assert MEAN_REVERSION_PARAMS == {"window": 20, "band": "0.02"}


def test_example_runs_on_plain_python(tmp_path: Path) -> None:
    on_bar = load_on_bar(tmp_path)
    params = dict[str, object](MEAN_REVERSION_PARAMS)
    # 평균 100인 봉 20개 뒤에 종가 97 → 평균보다 2% 넘게 아래라 사라는 신호
    history = [bar("99"), bar("101")] * 10
    assert on_bar(bar("97"), history, params) == "buy"
    # 평균 위로 돌아오면 청산
    assert on_bar(bar("100"), history, params) == "exit"
    assert on_bar(bar("103"), history, params) == "exit"
    # 평균 아래지만 밴드 안이면 아무것도 안 한다
    assert on_bar(bar("99"), history, params) == "none"
    # 20개 미만이면 판단하지 않는다
    assert on_bar(bar("97"), history[:19], params) == "none"
