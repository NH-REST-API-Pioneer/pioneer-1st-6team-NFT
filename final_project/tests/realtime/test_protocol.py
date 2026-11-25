import json

import pytest

from nh_mcp.config import NH_STREAM_URL
from nh_mcp.realtime.protocol import (
    Data,
    InitReply,
    Pingpong,
    Skip,
    Skipped,
    SubReply,
    parse,
    stream_url,
    subscription,
)
from nh_mcp.store.ticks import Sub

SYM = "A016C000"


def ka(**body: str) -> str:
    fields = {"tick_div": "1", "exch_tm": "122419", "last_pric": "726.10", "exec_qty": "2"}
    return json.dumps({"header": {"action_code": "KA", "action_sym": SYM}, "body": fields | body})


def test_connect_url_is_the_pinned_demo_host() -> None:
    assert stream_url(NH_STREAM_URL, "k") == (
        "wss://apidemo.futures.co.kr/trade/ws-stream?access_key=k"
    )


def test_subscribe_and_unsubscribe_messages() -> None:
    assert json.loads(subscription(Sub(SYM, "book"), on=True)) == {
        "header": {"api_ver": "1", "action": "A"},
        "body": {"action_code": "KB", "action_sym": SYM},
    }
    assert json.loads(subscription(Sub(SYM, "tick"), on=False))["header"]["action"] == "D"


def test_replies_use_the_code_field() -> None:
    assert parse('{"code": "INIT", "msg": "INIT_SUCCESS"}') == InitReply(ok=True)
    assert parse('{"code": "INIT", "msg": "NO"}') == InitReply(ok=False)
    assert parse('{"code": "pingpong", "msg": "pingpong"}') == Pingpong()
    fail = {
        "code": "RT_FAIL",
        "data": {"action": "A", "action_code": "KC", "action_sym": SYM},
        "msg": "허용된 action_code가 아닙니다",
    }
    assert parse(json.dumps(fail)) == SubReply(None, "A", False, "허용된 action_code가 아닙니다")
    fail["data"] = {"action": "A", "action_code": "KB", "action_sym": SYM}
    reply = parse(json.dumps(fail))
    assert isinstance(reply, SubReply) and reply.sub == Sub(SYM, "book")
    # 명세 예제의 rt_cd는 실제로 오지 않는다. 모르는 응답이다.
    assert parse('{"rt_cd": "RT_INIT"}') == Skipped("unknown")


def test_trade_row_is_integers_with_prices_in_hundredths() -> None:
    message = parse(ka(bid_best_pric="726.05", ask_best_pric="", exec_div="2", cum_trd_qty="9"))
    assert message == Data(
        Sub(SYM, "tick"), (12 * 3600 + 24 * 60 + 19, 72610, 2, 72605, None, 2, 9)
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"tick_div": "0"}, "not_trade"),
        ({"last_pric": "726.105"}, "malformed"),  # 자리가 더 많으면 반올림하지 않고 버린다
        ({"last_pric": "NaN"}, "malformed"),
        ({"last_pric": ""}, "malformed"),
        ({"exec_qty": "1.5"}, "malformed"),
        ({"exch_tm": "126019"}, "malformed"),
        ({"exch_tm": "1224"}, "malformed"),
    ],
)
def test_rows_that_are_not_trades_or_do_not_parse_are_skipped(
    body: dict[str, str], reason: Skip
) -> None:
    assert parse(ka(**body)) == Skipped(reason)


def test_night_and_other_codes_are_dropped() -> None:
    night = json.dumps({"header": {"action_code": "KC", "action_sym": SYM}, "body": {}})
    other = json.dumps({"header": {"action_code": "O1", "action_sym": SYM}, "body": {}})
    assert parse(night) == Skipped("night")
    assert parse(other) == Skipped("unknown")
    assert parse("not json") == Skipped("malformed")
    assert parse("[1, 2]") == Skipped("malformed")
