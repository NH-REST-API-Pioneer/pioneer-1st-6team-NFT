"""백테스트 예제 전략. 코드는 파일 원문 그대로 문자열로 들고 있다(실행은 격리 컨테이너 몫)."""

from pathlib import Path

from nh_mcp.domain import JsonValue

# 이 모듈은 코드를 실행하거나 파싱하지 않는다. 원문만 읽는다.
MEAN_REVERSION: str = (Path(__file__).with_name("examples") / "mean_reversion.py").read_text(
    encoding="utf-8"
)

# 실수는 JSON 파라미터로 못 쓴다. 비율은 문자열로 넣는다.
MEAN_REVERSION_PARAMS: dict[str, JsonValue] = {"window": 20, "band": "0.02"}
