"""도구들이 함께 쓰는 것: 지금 사용자, 응답 meta, 서비스 예외를 오류 코드로 바꾸기."""

import sqlite3
from collections.abc import Awaitable
from dataclasses import dataclass
from decimal import Decimal

from nh_mcp import envelope
from nh_mcp.auth.access import current_user_id
from nh_mcp.config import ENV
from nh_mcp.domain import Clock, JsonValue, UserId
from nh_mcp.services.queries import BrokerRejected, KeyMissing, Unavailable, UnknownSymbol

DOMESTIC_ONLY = "지금은 국내(domestic)만 지원합니다."


@dataclass(frozen=True)
class ToolContext:
    conn: sqlite3.Connection
    clock: Clock
    keys_url: str

    def user_id(self) -> UserId:
        return current_user_id(self.conn)

    def meta(self) -> envelope.Meta:
        return envelope.meta(ENV, self.clock())

    def invalid(self, message: str) -> envelope.Response:
        return envelope.error("INVALID_INPUT", message, self.meta())

    async def answer(self, work: Awaitable[dict[str, JsonValue]]) -> envelope.Response:
        """서비스 결과를 성공 응답으로, 서비스 예외를 오류 코드로 바꾼다."""
        meta = self.meta()
        try:
            data = await work
        except KeyMissing:
            return envelope.error(
                "KEY_NOT_REGISTERED",
                f"NH 모의 키가 등록돼 있지 않습니다. 등록 화면에서 등록해 주세요: {self.keys_url}",
                meta,
            )
        except UnknownSymbol:
            return envelope.error(
                "INVALID_INPUT", "search_symbol 결과에 있는 종목코드만 쓸 수 있습니다.", meta
            )
        except Unavailable:
            return envelope.error(
                "TEMPORARILY_UNAVAILABLE",
                "NH 모의 서버가 답하지 않습니다. 잠시 뒤 다시 시도해 주세요.",
                meta,
                retryable=True,
            )
        except BrokerRejected as exc:
            return envelope.error("BROKER_REJECTED", f"NH가 거부했습니다: {exc}", meta)
        return envelope.ok(data, meta)


def text(value: Decimal | None) -> str | None:
    """가격과 금액은 문자열로 내보낸다. float로 바꾸지 않는다."""
    return None if value is None else str(value)
