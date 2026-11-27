"""국내 선물·옵션의 틱 크기와 거래 구간: 품목별세부정보(`product-detail`)에서 읽는다.

`product-detail`은 종목이 아니라 품목별 행이고 한글 품목명만 있다. 종목 마스터의 기초자산 이름
(`under_name`)과 품목명을 `UNDERLYING_TO_PRODUCT`(선물)·`OPTION_UNDERLYING_TO_PRODUCT`(콜·풋)로
이어서 찾는다. 대응표의 근거는 `docs/nh-api.md`.
짝이 분명하지 않은 기초자산은 넣지 않는다. 표에 없으면 규칙을 모르는 것이고 모르면 막는다.

시간 칸의 뜻은 (제안, 장중 시험에서 확인): `occurs1_cd_val_inf_2`는 시가 단일가 구간,
`occurs1_cd_val_inf_3`은 종가 단일가 구간이다. 시가 단일가는 열지 않는다(사람 결정, 2026-11-24).
"""

import asyncio
import re
from datetime import date, time
from decimal import Decimal, InvalidOperation

from nh_mcp.domain import KST, Clock, Contract, InstrumentRules, Session, TickBand, UserId
from nh_mcp.markets.domestic import JsonObject
from nh_mcp.nh.client import NHError
from nh_mcp.nh.clients import ClientPool, KeyNotRegistered
from nh_mcp.nh.limits import UnknownApi

PRODUCT_DETAIL = "/trade/v1/domestic/product-detail"
NIGHT_PREFIX = "(야간)"

# 종목 마스터 `under_name` → product-detail 주간 행의 `occurs1_dtl_cd_ko_nm`.
# 이름에 더해 승수(마스터 `ctrt_size` = 응답 `occurs1_mltplr`)가 모두 같고,
# 응답에 만기가 있으면 마스터 근월 만기도 같다.
UNDERLYING_TO_PRODUCT: dict[str, str] = {
    "KOSPI": "코스피200선물",
    "MKOSPI": "미니 코스피200선물",
    "KOSDAQ150": "코스닥150선물",
    "XI3": "KRX300선물",
    "VKI": "코스피200변동성지수선물(VKOSPI)",
    "KOSDAQGL": "코스닥 글로벌 지수",
    "NKTB": "3년국채선물(KTB)",
    "N5TB": "5년국채선물(5KTB)",
    "NLKTB": "10년국채선물(10KTB)",
    "NBKTB": "30년국채선물(30KTB)",
    "USD": "미국달러선물(USD)",
    "EUR": "유로선물(EUR)",
    "JPY": "엔선물(JPY)",
    "CNH": "위안선물(CNH)",
}

# 콜·풋이 같은 품목이다. 위클리(`WKI`·`WKM`), 미니, 코스닥150 옵션은 짝이 없어 규칙이 없다.
OPTION_UNDERLYING_TO_PRODUCT: dict[str, str] = {"KOSPI": "코스피200옵션"}

# 품목 행의 틱 하나 대신 쓰는 가격대별 호가 단위. 코스피200옵션은 명세에 없어 KRX 규칙을 따른다.
TICK_LADDERS: dict[str, tuple[TickBand, ...]] = {
    "코스피200옵션": (
        TickBand(Decimal(0), Decimal("0.01")),
        TickBand(Decimal(10), Decimal("0.05")),
    ),
}

_RANGE = re.compile(r"(\d{2}):(\d{2})~(\d{2}):(\d{2})")

type Product = tuple[Decimal, Session]


def _text(row: JsonObject, key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _decimal(row: JsonObject, key: str) -> Decimal | None:
    try:
        return Decimal(_text(row, key))
    except InvalidOperation:
        return None


def _range(text: str) -> tuple[time, time] | None:
    """`HH:MM~HH:MM`만 읽는다. 다른 모양(`18:00~익일05:00` 등)과 없는 시각은 None."""
    match = _RANGE.fullmatch(text)
    if match is None:
        return None
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    try:
        return time(h1, m1), time(h2, m2)
    except ValueError:
        return None


def _product(row: JsonObject) -> Product | None:
    tick = _decimal(row, "occurs1_cd_val_inf_1")
    opening = _range(_text(row, "occurs1_cd_val_inf_2"))
    closing = _range(_text(row, "occurs1_cd_val_inf_3"))
    if tick is None or not tick.is_finite() or tick <= 0 or opening is None or closing is None:
        return None
    opens, closing_auction, closes = opening[1], closing[0], closing[1]
    if not opens < closing_auction < closes:
        return None
    return tick, Session(opens, closing_auction, closes)


def parse_products(body: JsonObject) -> dict[str, Product]:
    """주간 행의 (틱 크기, 거래 구간). 키는 품목명. 읽을 수 없는 행은 건너뛴다.

    같은 이름이 값이 다르게 두 번 나오면 어느 것인지 알 수 없으므로 뺀다.
    """
    products: dict[str, Product] = {}
    ambiguous: set[str] = set()
    rows = body.get("Occurs1")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = _text(row, "occurs1_dtl_cd_ko_nm")
        if not name or name.startswith(NIGHT_PREFIX):
            continue
        product = _product(row)
        if product is None:
            continue
        if name in products and products[name] != product:
            ambiguous.add(name)
        products[name] = product
    return {name: p for name, p in products.items() if name not in ambiguous}


class DomesticProducts:
    """하루(KST)에 한 번 받아 서버 전체가 나눠 쓴다. 받지 못하면 None을 돌려주고 저장하지 않는다."""

    def __init__(self, pool: ClientPool, clock: Clock) -> None:
        self._pool = pool
        self._clock = clock
        self._products: dict[str, Product] = {}
        self._loaded_for: date | None = None
        self._lock = asyncio.Lock()

    async def rules_for(self, user_id: UserId, contract: Contract) -> InstrumentRules | None:
        if contract.kind == "future":
            name = UNDERLYING_TO_PRODUCT.get(contract.underlying)
        elif contract.kind in ("call", "put"):
            name = OPTION_UNDERLYING_TO_PRODUCT.get(contract.underlying)
        else:
            return None
        if name is None:
            return None
        products = await self._load(user_id)
        found = None if products is None else products.get(name)
        if found is None:
            return None
        tick, session = found
        return InstrumentRules(
            contract, TICK_LADDERS.get(name, (TickBand(Decimal(0), tick),)), session
        )

    async def _load(self, user_id: UserId) -> dict[str, Product] | None:
        async with self._lock:
            today = self._clock().astimezone(KST).date()
            if self._loaded_for == today:
                return self._products
            try:
                client = await self._pool.client_for(user_id, "domestic")
                body = await client.post(PRODUCT_DETAIL, {})
            except (NHError, KeyNotRegistered, UnknownApi):
                return None
            products = parse_products(body)
            if not products:
                return None  # 빈 응답을 하루 동안 붙들지 않는다.
            self._products, self._loaded_for = products, today
            return products
