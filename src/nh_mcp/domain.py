"""여러 계층이 함께 쓰는 타입. 다른 nh_mcp 모듈을 import하지 않는다."""

from typing import Literal

# NH는 모의 서버만 쓴다. 다른 환경 값은 두지 않는다.
type Env = Literal["demo"]
