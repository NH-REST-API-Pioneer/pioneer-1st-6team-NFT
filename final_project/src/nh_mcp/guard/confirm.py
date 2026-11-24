"""확인 토큰 판정. 없음·남의 것·다른 용도·쓴 것·만료는 모두 invalid."""

from datetime import datetime
from typing import Literal

from nh_mcp.domain import TokenPurpose, TokenRow, UserId

type TokenVerdict = Literal["usable", "invalid"]


def judge(
    row: TokenRow | None, user_id: UserId, purpose: TokenPurpose, now: datetime
) -> TokenVerdict:
    if row is None or row.user_id != user_id or row.purpose != purpose:
        return "invalid"
    if row.used_at is not None or now >= row.expires_at:
        return "invalid"
    return "usable"
