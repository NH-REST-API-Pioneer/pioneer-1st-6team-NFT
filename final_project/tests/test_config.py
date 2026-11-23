from nh_mcp.config import ENV, NH_BASE_URL, NH_STREAM_URL


def test_nh_addresses_are_demo() -> None:
    assert NH_BASE_URL == "https://apidemo.futures.co.kr"
    assert NH_STREAM_URL == "wss://apidemo.futures.co.kr"
    assert ENV == "demo"
