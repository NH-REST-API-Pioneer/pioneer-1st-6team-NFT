"""옵션 보유 기록 맞추기와, 그것을 붙인 계좌 조회(`docs/options.md` "만기 처리").

계좌를 받을 때마다(조회 도구 `get_account`와 주문 점검 `OrderService._check`) 옵션 잔고를 사용자별
기록에 맞춘다. 다 읽지 못한 계좌로는 아무것도 바꾸지 않는다(`option_holdings.reconcile`).
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from nh_mcp.domain import AccountState, Contract, ContractKind, UserId
from nh_mcp.guard.risk import OPTION_KINDS
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.markets.domestic_master import MasterUnavailable
from nh_mcp.services.queries import call_nh
from nh_mcp.store import option_holdings
from nh_mcp.store.option_holdings import Holding

EXPIRED_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class AccountView:
    """get_account가 보여 줄 것. kinds는 보유 종목 → 종류(마스터에 없는 종목은 빠진다),
    expired는 최근 7일 안에 행사 또는 소멸로 닫힌 옵션이다."""

    account: AccountState
    kinds: Mapping[str, ContractKind]
    expired: list[Holding]


class HoldingService:
    def __init__(self, conn: sqlite3.Connection, domestic: DomesticMarket) -> None:
        self._conn = conn
        self._domestic = domestic

    async def account(self, user_id: UserId) -> AccountView:
        account = await call_nh(self._domestic.account(user_id))
        contracts = await self.record(user_id, account)
        since = account.as_of - EXPIRED_WINDOW
        expired = option_holdings.expired_since(self._conn, user_id, account.market, since)
        return AccountView(account, {s: c.kind for s, c in contracts.items()}, expired)

    async def record(self, user_id: UserId, account: AccountState) -> dict[str, Contract]:
        """기록을 맞추고, 보유 종목 중 마스터에 있는 것을 돌려준다.

        마스터를 받지 못하면 빈 표다. 그래도 잔고에서 사라진 행은 닫는다(만기일은 기록에 있다).
        """
        contracts: dict[str, Contract] = {}
        try:
            for symbol in {p.symbol for p in account.positions}:
                found = await self._domestic.contract(symbol)
                if found is not None:
                    contracts[symbol] = found
        except MasterUnavailable:
            contracts = {}
        expiries = {s: c.expiry for s, c in contracts.items() if c.kind in OPTION_KINDS}
        option_holdings.reconcile(self._conn, user_id, account, expiries)
        return contracts
