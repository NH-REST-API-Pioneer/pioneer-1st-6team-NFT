"""틱 크기와 거래 구간. 녹화한 product-detail(2026-11-24, 모의)과 종목 마스터 견본으로 확인한다."""

import asyncio
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from fakes import FakeNH, World

from nh_mcp.domain import (
    KST,
    Contract,
    ContractKind,
    InstrumentRules,
    Session,
    UserId,
)
from nh_mcp.markets.domestic import JsonObject
from nh_mcp.markets.domestic_master import parse
from nh_mcp.markets.domestic_products import (
    PRODUCT_DETAIL,
    UNDERLYING_TO_PRODUCT,
    DomesticProducts,
    parse_products,
)
from nh_mcp.nh.client import Credentials
from nh_mcp.store import users

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED_BODY: JsonObject = json.loads(
    (FIXTURES / "nh" / "domestic" / "product-detail.json").read_text(encoding="utf-8")
)["body"]

KOSPI200 = (Decimal("0.05"), Session(time(8, 45), time(15, 35), time(15, 45)))
KTB3 = (Decimal("0.01"), Session(time(9, 0), time(15, 35), time(15, 45)))


def contract(underlying: str = "KOSPI", kind: ContractKind = "future") -> Contract:
    return Contract(
        "domestic",
        "A016C000",
        "코스피200 F 202612",
        underlying,
        kind,
        date(2026, 12, 10),
        Decimal(250000),
        None,
    )


def row(
    name: str, tick: str = "0.05", opening: str = "08:30~08:45", closing: str = "15:35~15:45"
) -> JsonObject:
    return {
        "occurs1_dtl_cd_ko_nm": name,
        "occurs1_cd_val_inf_1": tick,
        "occurs1_cd_val_inf_2": opening,
        "occurs1_cd_val_inf_3": closing,
    }


def rows_body(*rows: JsonObject) -> JsonObject:
    return {"Occurs1": list(rows)}


def test_recorded_day_rows_give_tick_and_session() -> None:
    products = parse_products(RECORDED_BODY)

    assert products["코스피200선물"] == KOSPI200
    assert products["3년국채선물(KTB)"] == KTB3
    assert products["미니 코스피200선물"][0] == Decimal("0.02")
    assert products["코스닥150선물"][0] == Decimal("0.1")


def test_night_rows_are_not_in_the_result() -> None:
    products = parse_products(RECORDED_BODY)

    assert not any(name.startswith("(야간)") for name in products)
    assert "(야간)코스피200선물" not in products
    # 야간 행 10개를 뺀 18행이 모두 읽혔다.
    assert len(products) == 18


@pytest.mark.parametrize(
    "bad",
    [
        row("깨짐1", tick="abc"),
        row("깨짐2", tick="0"),
        row("깨짐3", tick=""),
        row("깨짐NaN", tick="NaN"),
        row("깨짐sNaN", tick="sNaN"),
        row("깨짐Inf", tick="Infinity"),
        row("깨짐4", opening="08:30-08:45"),
        row("깨짐5", opening=""),
        row("깨짐6", closing="15:35~"),
        row("깨짐7", closing="25:35~15:45"),
        row("깨짐8", closing="15:35 ~ 15:45"),
        row("깨짐9", opening="08:30~08:45", closing="15:35~익일05:00"),
        row("깨짐10", opening="08:30~16:00"),  # 시가 단일가가 종가 단일가보다 늦다
        row(""),
    ],
)
def test_broken_rows_are_skipped_and_the_rest_are_kept(bad: JsonObject) -> None:
    products = parse_products(rows_body(bad, row("코스피200선물")))

    assert products == {"코스피200선물": KOSPI200}


def test_rows_without_a_list_give_nothing() -> None:
    assert parse_products({}) == {}
    assert parse_products({"Occurs1": "x"}) == {}


def test_a_name_that_appears_twice_with_different_values_is_dropped() -> None:
    body = rows_body(row("코스피200선물"), row("코스피200선물", tick="0.1"), row("엔선물(JPY)"))

    assert list(parse_products(body)) == ["엔선물(JPY)"]


def test_a_name_that_appears_twice_with_the_same_values_is_kept() -> None:
    body = rows_body(row("코스피200선물"), row("코스피200선물"))

    assert parse_products(body) == {"코스피200선물": KOSPI200}


def master_futures() -> list[Contract]:
    text = "".join((FIXTURES / "master" / f"{name}.mst").read_text("utf-8") for name in MASTERS)
    return [c for c in parse(text)[0] if c.kind == "future"]


MASTERS = ("fut_opt", "stk_fut_opt", "idx_fut_opt")


def test_every_mapped_product_exists_in_the_recorded_day_rows() -> None:
    products = parse_products(RECORDED_BODY)

    assert set(UNDERLYING_TO_PRODUCT.values()) <= set(products)
    # 한 품목에 기초자산 하나. 둘이 한 품목을 가리키면 근거를 다시 본다.
    assert len(set(UNDERLYING_TO_PRODUCT.values())) == len(UNDERLYING_TO_PRODUCT)


def test_required_mappings() -> None:
    assert UNDERLYING_TO_PRODUCT["KOSPI"] == "코스피200선물"
    assert UNDERLYING_TO_PRODUCT["MKOSPI"] == "미니 코스피200선물"
    assert UNDERLYING_TO_PRODUCT["NKTB"] == "3년국채선물(KTB)"


def test_stock_futures_map_to_nothing() -> None:
    stock = {c.underlying for c in master_futures() if c.underlying.startswith("S")}

    assert "S0A" in stock
    assert not stock & set(UNDERLYING_TO_PRODUCT)


def test_master_sample_futures_with_a_mapping_find_their_product() -> None:
    products = parse_products(RECORDED_BODY)
    mapped = [c for c in master_futures() if c.underlying in UNDERLYING_TO_PRODUCT]

    assert {c.underlying for c in mapped} >= {"KOSPI", "MKOSPI", "NKTB"}
    for c in mapped:
        assert UNDERLYING_TO_PRODUCT[c.underlying] in products


class Setup:
    def __init__(self) -> None:
        self.nh = FakeNH()
        self.nh.reply(PRODUCT_DETAIL, RECORDED_BODY)
        self.world = World(nh=httpx2.MockTransport(self.nh))
        self.user: UserId = users.get_or_create(
            self.world.conn, "g-admin", "admin@example.com", self.world.clock()
        )
        asyncio.run(
            self.world.store.register(
                self.user, "domestic", Credentials("fake-key-1", "fake-sec-1")
            )
        )
        self.products = DomesticProducts(self.world.pool, self.world.clock)

    def rules(self, target: Contract, user: UserId | None = None) -> InstrumentRules | None:
        return asyncio.run(self.products.rules_for(user or self.user, target))

    def calls(self) -> int:
        return self.nh.paths().count(PRODUCT_DETAIL)


def test_rules_for_the_kospi200_future() -> None:
    setup = Setup()

    rules = setup.rules(contract())

    assert rules == InstrumentRules(contract(), *KOSPI200)
    assert setup.nh.requests[-1] == (PRODUCT_DETAIL, {})


def test_rules_for_the_treasury_future() -> None:
    setup = Setup()

    rules = setup.rules(contract("NKTB"))

    assert rules is not None
    assert (rules.tick_size, rules.session) == KTB3


def test_unmapped_underlying_gives_none_without_calling_nh() -> None:
    setup = Setup()

    assert setup.rules(contract("S0A")) is None
    assert setup.rules(contract("모르는기초자산")) is None
    assert setup.calls() == 0


@pytest.mark.parametrize("kind", ["call", "put", "spread"])
def test_only_futures_have_rules(kind: ContractKind) -> None:
    setup = Setup()

    assert setup.rules(contract(kind=kind)) is None
    assert setup.calls() == 0


def test_a_mapped_product_missing_from_the_response_gives_none() -> None:
    setup = Setup()
    setup.nh.replies[PRODUCT_DETAIL] = [(200, rows_body(row("엔선물(JPY)")))]

    assert setup.rules(contract()) is None


def test_a_broken_row_for_the_product_gives_none() -> None:
    setup = Setup()
    setup.nh.replies[PRODUCT_DETAIL] = [(200, rows_body(row("코스피200선물", tick="abc")))]

    assert setup.rules(contract()) is None


def test_no_response_gives_none_and_is_not_cached() -> None:
    setup = Setup()
    setup.nh.down = True

    assert setup.rules(contract()) is None
    assert setup.calls() == 1

    setup.nh.down = False
    assert setup.rules(contract()) == InstrumentRules(contract(), *KOSPI200)
    assert setup.calls() == 2


def test_rejection_gives_none_and_is_not_cached() -> None:
    setup = Setup()
    setup.nh.replies[PRODUCT_DETAIL] = [
        (500, {"errorCode": "EGW00201", "errorMessage": "x"}),
        (200, RECORDED_BODY),
    ]

    assert setup.rules(contract()) is None
    assert setup.rules(contract()) is not None
    assert setup.calls() == 2


def test_user_without_a_key_gives_none() -> None:
    setup = Setup()
    other = users.get_or_create(
        setup.world.conn, "g-other", "other@example.com", setup.world.clock()
    )

    assert setup.rules(contract(), other) is None
    assert setup.calls() == 0


def test_an_empty_answer_is_not_cached() -> None:
    setup = Setup()
    setup.nh.replies[PRODUCT_DETAIL] = [(200, {}), (200, RECORDED_BODY)]

    assert setup.rules(contract()) is None
    assert setup.rules(contract()) is not None
    assert setup.calls() == 2


def test_same_day_calls_nh_once_for_all_contracts_and_users() -> None:
    setup = Setup()
    other = users.get_or_create(
        setup.world.conn, "g-other", "other@example.com", setup.world.clock()
    )
    asyncio.run(
        setup.world.store.register(other, "domestic", Credentials("fake-key-2", "fake-sec-2"))
    )

    assert setup.rules(contract()) is not None
    assert setup.rules(contract("NKTB")) is not None
    assert setup.rules(contract(), other) is not None
    assert setup.calls() == 1


def test_concurrent_first_calls_share_one_nh_call() -> None:
    setup = Setup()

    async def both() -> list[InstrumentRules | None]:
        return list(
            await asyncio.gather(
                setup.products.rules_for(setup.user, contract()),
                setup.products.rules_for(setup.user, contract("NKTB")),
            )
        )

    assert all(r is not None for r in asyncio.run(both()))
    assert setup.calls() == 1


def test_next_day_calls_nh_again() -> None:
    setup = Setup()
    setup.rules(contract())

    setup.world.clock.now += timedelta(days=1)
    setup.rules(contract())

    assert setup.calls() == 2


def test_the_day_changes_at_midnight_kst() -> None:
    setup = Setup()
    setup.world.clock.now = datetime(2026, 12, 1, 23, 59, tzinfo=KST)
    setup.rules(contract())
    setup.world.clock.now = datetime(2026, 12, 1, 23, 59, 59, tzinfo=KST)
    setup.rules(contract())
    assert setup.calls() == 1

    setup.world.clock.now = datetime(2026, 12, 2, 0, 0, 1, tzinfo=KST)
    setup.rules(contract())
    assert setup.calls() == 2
