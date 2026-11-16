import pandas as pd

import session

T = pd.Timestamp


def test_entry_window():
    assert not session.entry_ok(T("2026-11-18 07:00"))   # 겨울: KST 07~08 은 CME 일일 휴장
    assert session.entry_ok(T("2026-11-18 08:00"))
    assert session.entry_ok(T("2026-11-18 23:30"))
    assert not session.entry_ok(T("2026-11-18 23:35"))
    assert not session.entry_ok(T("2026-11-19 00:30"))


def test_must_flatten_2345():
    assert session.must_flatten(T("2026-11-18 23:45"))   # 체결 23:50
    assert not session.must_flatten(T("2026-11-18 23:40"))


def test_summer_pre_break_full_hours():
    # 하루 전체 변형(mock_hours=False): 뉴욕 16:50 봉 마감 → 16:55 체결, 그 다음 봉이 휴장이라 정리
    assert session.must_flatten(T("2026-07-15 05:50"), mock_hours=False)
    assert not session.must_flatten(T("2026-07-15 05:40"), mock_hours=False)
    assert not session.must_flatten(T("2026-11-18 23:45"), mock_hours=False)
    assert session.entry_ok(T("2026-11-19 02:00"), mock_hours=False)
    assert session.must_flatten(T("2026-07-15 05:40"))  # 모의 시간 규칙: 05:45 체결은 0~7시라 정리


def test_friday_close():
    assert not session.market_open(T("2026-11-21 08:00"))  # 토요일
    assert not session.market_open(T("2026-11-23 07:30"))  # 월요일 개장(08:00) 전


def test_session_day_boundary():
    a = session.session_day(T("2026-11-18 06:55"))       # 뉴욕 11/17 16:55
    b = session.session_day(T("2026-11-18 08:00"))       # 뉴욕 11/17 18:00 → 다음 거래일
    assert a != b and b == T("2026-11-18")


def test_summer_break():
    assert not session.market_open(T("2026-07-15 06:00"))
    assert session.market_open(T("2026-07-15 07:00"))


def test_session_day_vectorized():
    idx = pd.DatetimeIndex(["2026-11-18 06:55", "2026-11-18 08:00"])
    got = session.session_day(idx)
    assert list(got) == [T("2026-11-17"), T("2026-11-18")]
