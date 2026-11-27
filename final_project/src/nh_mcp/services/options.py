"""옵션 조회 유스케이스와 묶음 스위치: 코스피200 월물의 만기 목록, 등가격 주변의 전광판,
"옵션"·"옵션 매도" 켜기·끄기.

종목코드는 전광판에 있고 종목 마스터에도 같은 종목으로 있는 것만 나간다(`markets/options.py`).
"옵션"을 켜지 않은 사용자에게는 전광판도 옵션 종목의 시세·차트도 주지 않는다(`options_off`).
"옵션 매도"는 "옵션"이 켜져 있을 때만 켜고, "옵션"을 끌 때 먼저 끈다. 그래서 "매도만 켜짐"은
생기지 않는다.
"""

import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from nh_mcp.domain import ChainRow, Clock, Contract, JsonValue, OptionChain, UserId
from nh_mcp.guard.risk import OPTION_KINDS, OPTION_SELL_OFF, OPTIONS_OFF
from nh_mcp.markets.domestic_master import MasterUnavailable
from nh_mcp.markets.options import BoardUnavailable, DomesticOptions
from nh_mcp.services.orders import RiskBlocked
from nh_mcp.services.queries import Unavailable, call_nh
from nh_mcp.store import audit, features
from nh_mcp.store.features import Feature

OPTIONS: Feature = "options"
OPTION_SELL: Feature = "option_sell"
# "옵션"·"옵션 매도" 스위치가 꺼져 막은 규칙. 도구가 문구 끝에 등록 화면 주소를 붙인다.
OPTION_SWITCH_RULES = frozenset({OPTIONS_OFF, OPTION_SELL_OFF})
# 도구가 끝에 등록 화면 주소를 붙인다.
OPTIONS_ARE_OFF = (
    "옵션 묶음이 꺼져 있어 옵션 전광판과 옵션 종목의 시세·차트를 볼 수 없습니다. 사용자가 등록 "
    "화면의 '옵션' 스위치를 켜야 합니다(AI는 켤 수 없습니다)."
)


class UnknownExpiry(Exception):
    """마스터에 있는 코스피200 월물 옵션 만기가 아니다."""


@dataclass(frozen=True)
class ChainView:
    """등가격 위아래만 남긴 전광판. strikes는 자르기 전 행사가 수다."""

    chain: OptionChain
    expiries: list[date]
    atm: Decimal
    strikes: int


def near_money(
    rows: tuple[ChainRow, ...], spot: Decimal, around: int
) -> tuple[Decimal, tuple[ChainRow, ...]]:
    """등가격(현물가에 가장 가까운 행사가, 같은 거리면 낮은 쪽)과 그 위아래 around칸까지의 행.

    rows는 행사가 오름차순이고 비어 있지 않다.
    """
    i = min(range(len(rows)), key=lambda k: (abs(rows[k].strike - spot), rows[k].strike))
    return rows[i].strike, rows[max(0, i - around) : i + around + 1]


class OptionService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, options: DomesticOptions) -> None:
        self._conn = conn
        self._clock = clock
        self._options = options

    # ---- "옵션"·"옵션 매도" 묶음 ---------------------------------------------------

    def switches(self, user_id: UserId) -> tuple[bool, bool]:
        """(옵션, 옵션 매도). 기본은 둘 다 꺼짐이다."""
        return (
            features.enabled(self._conn, user_id, OPTIONS),
            features.enabled(self._conn, user_id, OPTION_SELL),
        )

    def set_options(self, user_id: UserId, on: bool) -> None:
        """ "옵션"을 켜거나 끈다. 끄면 "옵션 매도"를 먼저 끈다."""
        if on:
            features.turn_on(self._conn, user_id, OPTIONS, self._clock())
        else:
            features.turn_off(self._conn, user_id, OPTION_SELL)
            features.turn_off(self._conn, user_id, OPTIONS)
        self._log(user_id, {"feature": OPTIONS, "enabled": on})

    def set_option_sell(self, user_id: UserId, on: bool) -> bool:
        """ "옵션 매도"를 켜거나 끈다. "옵션"이 꺼져 있으면 켜지 않고 거짓을 돌려준다."""
        if on and not features.enabled(self._conn, user_id, OPTIONS):
            return False
        if on:
            features.turn_on(self._conn, user_id, OPTION_SELL, self._clock())
        else:
            features.turn_off(self._conn, user_id, OPTION_SELL)
        self._log(user_id, {"feature": OPTION_SELL, "enabled": on})
        return True

    def require_for(self, user_id: UserId, contract: Contract) -> None:
        """옵션 종목이면 "옵션"이 켜져 있어야 한다. 선물은 그대로 지나간다."""
        if contract.kind in OPTION_KINDS:
            self._require_options(user_id)

    def _require_options(self, user_id: UserId) -> None:
        if not features.enabled(self._conn, user_id, OPTIONS):
            raise RiskBlocked(OPTIONS_OFF, OPTIONS_ARE_OFF)

    def _log(self, user_id: UserId, detail: dict[str, JsonValue]) -> None:
        audit.record(self._conn, user_id, "options.switch", detail, self._clock())

    # ---- 전광판 ----------------------------------------------------------------

    async def chain(self, user_id: UserId, expiry: date | None, around: int) -> ChainView:
        """expiry가 None이면 가장 가까운 만기. 마스터에 없는 만기면 UnknownExpiry.

        "옵션"이 꺼져 있으면 NH를 부르기 전에 RiskBlocked(options_off).
        """
        self._require_options(user_id)
        try:
            expiries = await self._options.expiries()
            chosen = expiry or (expiries[0] if expiries else None)
            if chosen is None or chosen not in expiries:
                raise UnknownExpiry()
            chain = await call_nh(self._options.chain(user_id, chosen))
        except (MasterUnavailable, BoardUnavailable):
            raise Unavailable() from None
        atm, rows = near_money(chain.rows, chain.spot, around)
        return ChainView(replace(chain, rows=rows), expiries, atm, len(chain.rows))
