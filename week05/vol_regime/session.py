"""장 시간 규칙 — 백테스트와 실행기가 같이 씁니다.

시각은 KST(시간대 없음), 봉 시각 = 봉 시작. 신호는 봉 t 마감에 나고 체결은 t+5분 봉 시가입니다.
CME: 뉴욕 17:00~18:00 매일 휴장, 금요일 17:00 ~ 일요일 18:00 주말 (week04 rv_runner.market_open 과 같은 규칙).
해외 모의계좌: KST 07:00~24:00 에만 체결 → 진입 체결 07:00~23:30, 강제 청산 체결 23:50.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

import config

KST, NY = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")
_BAR = pd.Timedelta("5min")


def _ny(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.tz_localize(KST).tz_convert(NY)


def _tod(ts: pd.Timestamp) -> str:
    return ts.strftime("%H:%M")


def market_open(ts: pd.Timestamp) -> bool:
    """봉 ts(시작 시각)에 CME 가 열려 있나."""
    ny = _ny(ts)
    if ny.weekday() == 5 or (ny.weekday() == 4 and ny.hour >= 17) or (ny.weekday() == 6 and ny.hour < 18):
        return False
    return ny.hour != 17


def entry_ok(fill_ts: pd.Timestamp, mock_hours: bool = True) -> bool:
    """이 시각(체결 봉 시작)에 진입 체결을 해도 되나. mock_hours=False 면 장 열림만 봄 (하루 전체 변형)."""
    if mock_hours and not (config.ENTRY_FILL_START <= _tod(fill_ts) <= config.ENTRY_FILL_END):
        return False
    return market_open(fill_ts)


def must_flatten(signal_bar: pd.Timestamp, mock_hours: bool = True) -> bool:
    """봉 signal_bar 마감에 보유 포지션을 정리해야 하나.

    체결(signal_bar+5분)이 23:50 이후거나 0~7시면 정리. 체결 봉 다음 봉이 휴장·주말로 닫혀도 정리
    (휴장 너머로 들고 가지 않음 — 휴장 직전 마지막 봉 시가에 체결). mock_hours=False 면 휴장 규칙만.
    """
    fill = signal_bar + _BAR
    tod = _tod(fill)
    if mock_hours and (tod >= config.FLAT_FILL or tod < config.ENTRY_FILL_START):
        return True
    return not market_open(fill + _BAR)


def session_day(ts):
    """CME 거래일 (뉴욕 17:00 경계). Timestamp 또는 DatetimeIndex."""
    if isinstance(ts, pd.Timestamp):
        return (_ny(ts) + pd.Timedelta("7h")).tz_localize(None).normalize()
    idx = pd.DatetimeIndex(ts).tz_localize(KST).tz_convert(NY) + pd.Timedelta("7h")
    return idx.tz_localize(None).normalize()
