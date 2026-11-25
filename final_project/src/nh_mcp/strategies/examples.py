"""백테스트 예제 전략. 코드는 파일 원문 그대로 문자열로 들고 있다(실행은 격리 컨테이너 몫)."""

from pathlib import Path

from nh_mcp.domain import JsonValue


def _source(name: str) -> str:
    # 이 모듈은 코드를 실행하거나 파싱하지 않는다. 원문만 읽는다.
    return (Path(__file__).with_name("examples") / name).read_text(encoding="utf-8")


MEAN_REVERSION: str = _source("mean_reversion.py")
PAIR_SPREAD: str = _source("pair_spread.py")
TICK_MOMENTUM: str = _source("tick_momentum.py")
SCALE_IN_LIMIT: str = _source("scale_in_limit.py")

# 실수는 JSON 파라미터로 못 쓴다. 비율은 문자열로 넣는다.
MEAN_REVERSION_PARAMS: dict[str, JsonValue] = {"window": 20, "band": "0.02"}
# a·b는 search_symbol로 찾은 두 종목으로 바꿔 쓴다. 여기는 코스피200 선물 2026년 12월·2027년 3월물.
PAIR_SPREAD_PARAMS: dict[str, JsonValue] = {
    "a": "A016C000",
    "b": "A0173000",
    "window": 30,
    "entry_z": "2",
    "exit_z": "0.5",
}
TICK_MOMENTUM_PARAMS: dict[str, JsonValue] = {"window": 5, "imbalance": "0.2"}
# tick은 그 종목의 틱 단위다(여기는 코스피200 선물).
# settings.max_position은 max_steps 이상으로 둔다.
SCALE_IN_LIMIT_PARAMS: dict[str, JsonValue] = {
    "window": 20,
    "step": "0.003",
    "max_steps": 3,
    "tick": "0.05",
}
