"""장 시간 규칙 — 백테스트와 실행기가 같이 씁니다.

시각은 KST(시간대 없음), 봉 시각 = 봉 시작. 신호는 봉 t 마감, 체결은 t+5분 봉 시가.
CME: 뉴욕 17:00~18:00 매일 휴장, 금요일 17:00 ~ 일요일 18:00 주말 (vol_regime/session.py 와 같은 규칙).
해외 모의계좌: KST 07:00~24:00 에만 체결 → 매매 체결은 07:00~23:50.
GP 포지션은 천천히 움직이는 게 핵심이라 밤(0~7시)·매일 휴장은 들고 넘기고, 주말 전(금 23:50)에만 정리합니다.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

import config

KST, NY = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")
BAR = pd.Timedelta("5min")


def _tod(ts: pd.Timestamp) -> str:
    return ts.strftime("%H:%M")


def market_open(ts: pd.Timestamp) -> bool:
    """봉 ts(시작 시각)에 CME 가 열려 있나."""
    ny = ts.tz_localize(KST).tz_convert(NY)
    if ny.weekday() == 5 or (ny.weekday() == 4 and ny.hour >= 17) or (ny.weekday() == 6 and ny.hour < 18):
        return False
    return ny.hour != 17


def can_trade(fill_ts: pd.Timestamp) -> bool:
    """이 시각(체결 봉 시작)에 모의계좌 체결이 되나."""
    return config.TRADE_FILL_START <= _tod(fill_ts) <= config.TRADE_FILL_END and market_open(fill_ts)


def weekend_flat(fill_ts: pd.Timestamp) -> bool:
    """이 체결로 전부 정리해야 하나 — 금요일(KST) 23:50 이후 체결."""
    return fill_ts.weekday() == 4 and _tod(fill_ts) >= config.WEEKEND_FLAT_FILL
