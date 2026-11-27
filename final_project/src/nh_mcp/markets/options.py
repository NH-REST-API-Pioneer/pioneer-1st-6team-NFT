"""코스피200 월물 옵션: 종목 마스터의 만기 목록과 옵션 전광판(`price-all-option`).

전광판은 행사가가 높은 것부터 한 쪽에 최대 261행이 온다. 응답의 `nxt_stkr_pric`(그 쪽의 마지막
행사가)을 다음 요청에 넣고, 그 칸이 비면 마지막 쪽이다(2026-11-26 녹화). 칸 이름은 `nh-api.md`
"옵션 전광판".

종목코드는 마스터에 같은 종목(콜·풋, 만기, 행사가)이 있을 때만 넣고, 아니면 그 칸을 비우고 센다.
행사가는 전광판에서 읽는다. 종목코드에서 읽지 않는다(행사가 1000 이상은 코드 끝이 일련번호다).
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from nh_mcp.domain import KST, ChainRow, Clock, Contract, OptionChain, OptionSide, UserId
from nh_mcp.markets.domestic import JsonObject
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.nh.clients import ClientPool

BOARD = "/trade/v1/domestic/price-all-option"
KOSPI200 = "01"  # prd
FIRST_PAGE = "99999999"  # nxt_stkr_pric 첫 쪽
# 2026-12물 567행이 3쪽이었다. 이 쪽 수 안에 끝나지 않으면 받지 못한 것으로 본다.
MAX_PAGES = 10
# 마스터 under_name. 위클리(WKI, WKM)는 전광판이 주지 않는다(nh-api.md).
MONTHLY = "KOSPI"
UNDERLYING = "KOSPI200"

type Kind = Literal["call", "put"]
SIDES: dict[Kind, str] = {"call": "occurs1_c_", "put": "occurs1_p_"}


class BoardUnavailable(Exception):
    """그 만기의 전광판이 비었거나 끝 쪽까지 받지 못했다. 일부만 내보내지 않는다."""


def _text(obj: JsonObject, key: str) -> str:
    value = obj.get(key)
    return value.strip() if isinstance(value, str) else ""


def _dec(obj: JsonObject, key: str) -> Decimal | None:
    try:
        return Decimal(_text(obj, key))
    except InvalidOperation:
        return None


def _count(obj: JsonObject, key: str) -> int:
    value = _dec(obj, key)
    return int(value) if value is not None and value == value.to_integral_value() else 0


def _rows(body: JsonObject) -> list[JsonObject]:
    rows = body.get("Occurs1")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _quote(row: JsonObject, key: str) -> Decimal | None:
    """호가 0.00은 호가 없음이다."""
    value = _dec(row, key)
    return None if value is None or value == 0 else value


def _side(row: JsonObject, kind: Kind) -> OptionSide:
    p = SIDES[kind]
    return OptionSide(
        symbol=_text(row, p + "sym_exch"),
        last=_dec(row, p + "last_pric"),
        bid=_quote(row, p + "bid_pric"),
        ask=_quote(row, p + "ask_pric"),
        volume=_count(row, p + "trd_qty"),
        open_interest=_count(row, p + "opint"),
        iv=_dec(row, p + "vol"),
        delta=_dec(row, p + "delta"),
        theoretical=_dec(row, p + "theo_pric"),
    )


def _listed(
    side: OptionSide, kind: Kind, expiry: date, strike: Decimal, master: Mapping[str, Contract]
) -> OptionSide | None:
    """마스터에 같은 종목(코스피200 월물, 콜·풋, 만기, 행사가)이 있을 때만 그 칸을 둔다."""
    c = master.get(side.symbol)
    if c is None or (c.underlying, c.kind, c.expiry, c.strike) != (MONTHLY, kind, expiry, strike):
        return None
    return side


def parse_chain(
    expiry: date, pages: list[JsonObject], master: Mapping[str, Contract]
) -> OptionChain:
    """받은 쪽들을 행사가 오름차순 OptionChain으로. 행이나 현물가가 없으면 BoardUnavailable.

    위클리·만기 지난 월물처럼 전광판이 모르는 만기는 0행, 현물가 0으로 온다(nh-api.md).
    """
    head = pages[0]
    spot = _dec(head, "spt_pric")
    rows = [row for page in pages for row in _rows(page)]
    chain: list[ChainRow] = []
    for row in rows:
        strike = _dec(row, "occurs1_strk_pric")
        if strike is not None:
            call = _listed(_side(row, "call"), "call", expiry, strike, master)
            put = _listed(_side(row, "put"), "put", expiry, strike, master)
            chain.append(ChainRow(strike, call, put))
    if not chain or spot is None or spot <= 0:
        raise BoardUnavailable()
    # 대응 선물 코드도 마스터에 선물로 있어야 내보낸다.
    board_future = _text(head, "fut_sym")
    known = master.get(board_future)
    future = board_future if known is not None and known.kind == "future" else None
    on_board = sum(bool(_text(row, p + "sym_exch")) for row in rows for p in SIDES.values())
    on_board += bool(board_future)
    kept = sum((r.call is not None) + (r.put is not None) for r in chain) + (future is not None)
    return OptionChain(
        underlying=UNDERLYING,
        expiry=expiry,
        spot=spot,
        future_symbol=future,
        future_price=_dec(head, "fut_last_pric"),
        rows=tuple(sorted(chain, key=lambda r: r.strike)),
        dropped=on_board - kept,
    )


class DomesticOptions:
    def __init__(self, pool: ClientPool, master: DomesticMaster, clock: Clock) -> None:
        self._pool = pool
        self._master = master
        self._clock = clock

    async def expiries(self) -> list[date]:
        """마스터의 코스피200 월물 옵션 만기 중 오늘(KST) 이후, 가까운 것부터."""
        today = self._clock().astimezone(KST).date()
        return sorted(
            {
                c.expiry
                for c in await self._master.contracts()
                if c.underlying == MONTHLY and c.kind in SIDES and c.expiry >= today
            }
        )

    async def chain(self, user_id: UserId, expiry: date) -> OptionChain:
        master = {c.symbol: c for c in await self._master.contracts()}
        client = await self._pool.client_for(user_id, "domestic")
        # 마스터 만기일 8자리 중 뒤 6자리(명세 `domestic_price-all-option.md:23`).
        month = expiry.strftime("%y%m%d")
        pages: list[JsonObject] = []
        strike = FIRST_PAGE
        seen = {strike}
        for _ in range(MAX_PAGES):
            payload = {"prd": KOSPI200, "exp_mon": month, "nxt_stkr_pric": strike}
            body = await client.post(BOARD, payload)
            pages.append(body)
            strike = _text(body, "nxt_stkr_pric")
            if not strike:
                return parse_chain(expiry, pages, master)
            # 다음 쪽이 있다는데 행이 없으면 끝까지 받지 못한 것이다.
            if not _rows(body) or strike in seen:
                break
            seen.add(strike)
        raise BoardUnavailable()
