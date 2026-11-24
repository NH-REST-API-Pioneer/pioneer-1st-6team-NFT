"""종목 찾기."""

from dataclasses import dataclass

from nh_mcp.markets.domestic_master import DomesticMaster, MasterUnavailable, SearchResult
from nh_mcp.services.queries import Unavailable


@dataclass(frozen=True)
class SymbolService:
    domestic: DomesticMaster

    async def search_futures(self, query: str, limit: int) -> SearchResult:
        try:
            return await self.domestic.search(query, "future", limit)
        except MasterUnavailable:
            raise Unavailable() from None
