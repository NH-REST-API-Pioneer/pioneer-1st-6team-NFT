"""여러 계층이 함께 쓰는 타입. 다른 nh_mcp 모듈을 import하지 않는다."""

from typing import Literal

# 모의 서버인지 아닌지만 가른다. 모의가 아니면 주문 도구를 등록하지 않는다.
type Env = Literal["demo", "not_demo"]
