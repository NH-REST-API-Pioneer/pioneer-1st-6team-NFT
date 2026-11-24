"""국내 종목 마스터: 받기, 읽기, 찾기.

개발자센터가 하루에 한 번 바꾸는 정적 파일이다(gzip, UTF-8, 칸 구분 ';', 한 행 26칸).
실전 거래 서버가 아니라 개발자센터 주소다.
칸의 뜻은 NH선물 헤더정보 문서를 옮긴 팀 앱 자료를 따랐다.
모든 행이 26칸인 것은 실제 파일로 확인했다.
야간 모의는 지원되지 않아 주간(day) 행만 쓴다.
"""

import asyncio
import gzip
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx2

from nh_mcp.domain import KST, Clock, Contract, ContractKind

MASTER_URL = "https://apidev.futures.co.kr/guide/master/{name}.mst.gz"
FILES = ("fut_opt", "stk_fut_opt", "idx_fut_opt")
TIMEOUT_SECONDS = 30
COLUMNS = 26
DAY_SESSION = "day"

# 칸 위치(0부터).
OPT_TP, UNDER_NAME, EXPIRY, SESSION, SERIES, NAME_KOR, STRIKE, MULTIPLIER = (
    2,
    3,
    4,
    5,
    8,
    10,
    14,
    17,
)
KINDS: dict[str, ContractKind] = {"1": "future", "2": "spread", "3": "call", "4": "put"}


class MasterUnavailable(Exception):
    """종목 마스터를 받지 못했고 받아 둔 것도 없다."""


def parse(text: str) -> tuple[list[Contract], int]:
    """(주간 종목, 읽지 못해 건너뛴 행 수). 야간 행은 건너뛴 것으로 세지 않는다."""
    contracts: list[Contract] = []
    skipped = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        cells = line.split(";")
        if len(cells) != COLUMNS:
            skipped += 1
            continue
        if cells[SESSION] != DAY_SESSION:
            continue
        contract = _contract(cells)
        if contract is None:
            skipped += 1
        else:
            contracts.append(contract)
    return contracts, skipped


def _contract(cells: list[str]) -> Contract | None:
    kind = KINDS.get(cells[OPT_TP])
    if kind is None or not cells[SERIES]:
        return None
    try:
        expiry = datetime.strptime(cells[EXPIRY], "%Y%m%d").date()
        multiplier = Decimal(cells[MULTIPLIER])
        strike = Decimal(cells[STRIKE]) if kind in ("call", "put") else None
    except (ValueError, InvalidOperation):
        return None
    return Contract(
        market="domestic",
        symbol=cells[SERIES],
        name=" ".join(cells[NAME_KOR].split()),
        underlying=cells[UNDER_NAME],
        kind=kind,
        expiry=expiry,
        multiplier=multiplier,
        strike=strike,
    )


def _normalize(text: str) -> str:
    return "".join(text.split()).casefold()


@dataclass(frozen=True)
class SearchResult:
    contracts: list[Contract]
    truncated: bool
    master_date: date


class DomesticMaster:
    """하루에 한 번 받아 메모리에 둔다. 받지 못하면 전에 받은 것을 쓴다."""

    def __init__(self, clock: Clock, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._clock = clock
        self._transport = transport
        self._contracts: list[Contract] = []
        self._loaded_for: date | None = None
        self._lock = asyncio.Lock()

    async def find(self, symbol: str) -> Contract | None:
        """종목코드가 정확히 같은 주간 종목. 없으면 None."""
        contracts, _ = await self._load()
        return next((c for c in contracts if c.symbol == symbol), None)

    async def search(self, query: str, kind: ContractKind, limit: int) -> SearchResult:
        contracts, loaded_for = await self._load()
        wanted = _normalize(query)
        found = sorted(
            (
                c
                for c in contracts
                if c.kind == kind
                and (
                    _normalize(c.symbol) == wanted
                    or wanted in _normalize(c.name)
                    or wanted in _normalize(c.underlying)
                )
            ),
            key=lambda c: (c.underlying, c.expiry, c.symbol),
        )
        return SearchResult(found[:limit], len(found) > limit, loaded_for)

    async def _load(self) -> tuple[list[Contract], date]:
        async with self._lock:
            today = self._clock().astimezone(KST).date()
            if self._loaded_for != today:
                try:
                    self._contracts = await self._download()
                    self._loaded_for = today
                except (httpx2.HTTPError, OSError, UnicodeDecodeError):
                    pass  # 전에 받은 것이 있으면 그것을 쓴다.
            if self._loaded_for is None:
                raise MasterUnavailable("종목 마스터를 받지 못했습니다.")
            return self._contracts, self._loaded_for

    async def _download(self) -> list[Contract]:
        contracts: list[Contract] = []
        # 호출 제한이 이 정적 파일에도 걸릴 수 있어 한 번에 하나씩 받는다.
        async with httpx2.AsyncClient(transport=self._transport, timeout=TIMEOUT_SECONDS) as http:
            for name in FILES:
                response = await http.get(MASTER_URL.format(name=name))
                response.raise_for_status()
                rows, _ = parse(gzip.decompress(response.content).decode("utf-8"))
                contracts.extend(rows)
        return contracts
