"""사람 승인 결과. 승인 창을 띄우는 프로토콜은 도구 계층(`tools/approval.py`)이 맡는다."""

from typing import Literal

# approved: 사람이 승인 / declined: 거절·취소·내용이 바뀜
# unsupported: 승인 창을 못 띄우는 클라이언트
type Approval = Literal["approved", "declined", "unsupported"]
