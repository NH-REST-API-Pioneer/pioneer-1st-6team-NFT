"""틱·호가 저장 켜고 끄기와, 사용자가 백테스트에 쓸 수 있는 데이터 보기.

- 저장할 (종목, 흐름) 전체를 한 번에 받는다. 같은 집합을 다시 주면 NH에 아무것도 보내지 않는다.
- 구독은 APP Key당 20건이다. 실행 중인 전략의 구독과 합쳐 센다(같은 것은 1건).
- 종목은 종목 마스터에 있는 국내 선물만 받는다(모르면 막는다).
- 키를 지우면 저장을 멈추고 저장 설정도 지운다. 나중에 새 키를 넣어도 사용자가 다시 켜기 전에는
  저절로 켜지지 않는다. 이미 저장한 틱은 보관 기간(30일)까지 남는다.
- 서버를 다시 띄우면 수신기가 저장 설정대로 다시 켠다(`realtime/recorder.py`).
"""

import asyncio
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from nh_mcp.domain import KST, JsonValue, UserId
from nh_mcp.models.store import ModelStore
from nh_mcp.realtime.recorder import MAX_SUBSCRIPTIONS, Recorder, TooManySubscriptions
from nh_mcp.realtime.session import Status
from nh_mcp.services.queries import KeyMissing, QueryService, UnknownSymbol
from nh_mcp.store import nh_keys, recordings
from nh_mcp.store.ticks import Gap, Stored, Sub

type State = Literal["subscribed", "pending", "error", "off"]

# 응답에 싣는 구독·흐름 수와 흐름마다의 빈 구간 수(가장 최근 것들). 전체 수는 따로 적는다.
MAX_LISTED_STREAMS = 40
MAX_LISTED_GAPS = 20
# 켠 뒤 NH의 구독 답을 기다리는 시간(밀리초). 그 안에 답이 없으면 pending으로 돌려준다.
SETTLE_MS = 3000
_POLL_MS = 50
OPEN_GAP = "진행 중"

NOTE = (
    "지금부터 받는 체결·호가만 저장합니다(NH는 지난 틱을 주지 않습니다). "
    "평일 주간장(08:45~15:45)만 저장하고 30일이 지나면 지웁니다. "
    "연결이 끊긴 동안은 빈 구간으로 남습니다."
)
BARS = "1분봉·일봉은 저장하지 않아도 NH에서 바로 받아 backtest_strategy에 씁니다."


class RecordingInvalid(Exception):
    """저장 요청이 맞지 않는다. 메시지는 AI에게 그대로 보여 준다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _time(moment: datetime) -> str:
    return moment.astimezone(KST).isoformat(timespec="seconds")


def _state(sub: Sub, status: Status | None) -> tuple[State, str | None]:
    """구독 하나의 지금 상태와, NH가 거절했으면 그 사유(비밀값은 수신기가 이미 가렸다)."""
    if status is None:
        return "pending", None
    if sub in status.live:
        return "subscribed", None
    if sub in status.failed:
        return "error", status.failed[sub]
    return "pending", None


def _subscription(sub: Sub, status: Status | None) -> dict[str, JsonValue]:
    state, message = _state(sub, status)
    out: dict[str, JsonValue] = {"symbol": sub.symbol, "stream": sub.stream, "state": state}
    if message is not None:
        out["message"] = message
    return out


def _connection(status: Status | None) -> dict[str, JsonValue]:
    connected = status is not None and status.connected
    # 연결돼 있으면 지난 실패는 지난 일이다.
    error = None if status is None or connected else status.error
    return {"connected": connected, "error": error}


def _gaps(gaps: list[Gap]) -> dict[str, JsonValue]:
    recent: list[JsonValue] = [
        {
            "start": _time(g.started_at),
            "end": OPEN_GAP if g.ended_at is None else _time(g.ended_at),
            "reason": g.reason,
        }
        for g in gaps[-MAX_LISTED_GAPS:]
    ]
    return {"total": len(gaps), "recent": recent}


def _recorded(
    desired: frozenset[Sub], status: Status | None, stored: list[Stored], gaps: list[Gap]
) -> list[dict[str, JsonValue]]:
    """구독·흐름마다 저장 상태, 저장한 범위, 빈 구간.

    지금 저장 중인 것이 먼저, 그다음은 최근에 저장한 것부터.
    """
    by_sub = {s.sub: s for s in stored}
    gaps_of: dict[Sub, list[Gap]] = {}
    for gap in gaps:
        gaps_of.setdefault(gap.sub, []).append(gap)
    subs = set(desired) | set(by_sub) | set(gaps_of)

    def order(sub: Sub) -> tuple[bool, int, Sub]:
        kept = by_sub.get(sub)
        last = 0 if kept is None else int(kept.last_minute.timestamp())
        return sub not in desired, -last, sub

    out: list[dict[str, JsonValue]] = []
    for sub in sorted(subs, key=order):
        state, message = _state(sub, status) if sub in desired else ("off", None)
        kept = by_sub.get(sub)
        row: dict[str, JsonValue] = {
            "symbol": sub.symbol,
            "stream": sub.stream,
            "recording": state,
            "start": None if kept is None else _time(kept.first_minute),
            "end": None if kept is None else _time(kept.last_minute + timedelta(minutes=1)),
            "rows": 0 if kept is None else kept.rows,
            "gaps": _gaps(gaps_of.get(sub, [])),
        }
        if message is not None:
            row["message"] = message
        out.append(row)
    return out


@dataclass(frozen=True)
class RecordingService:
    conn: sqlite3.Connection
    recorder: Recorder
    queries: QueryService
    models: ModelStore
    models_url: str

    async def set(self, user_id: UserId, subs: frozenset[Sub]) -> dict[str, JsonValue]:
        """저장할 집합을 subs로 맞추고, NH의 구독 답을 잠깐 기다린 뒤 지금 상태를 돌려준다.

        비어 있으면 모두 멈춘다(키가 없어도 된다). 20건 넘음, 모르는 종목, 국내 선물이 아닌 종목은
        RecordingInvalid, 국내 키가 없으면 KeyMissing, 종목 마스터를 못 받으면 Unavailable.
        """
        if len(subs) > MAX_SUBSCRIPTIONS:
            raise RecordingInvalid(
                f"구독은 APP Key당 {MAX_SUBSCRIPTIONS}건까지인데 {len(subs)}건을 요청했습니다. "
                "종목 하나의 tick과 book이 각각 1건입니다. 자동 실행 전략이 생기면 그 구독도 이 "
                f"{MAX_SUBSCRIPTIONS}건을 같이 씁니다."
            )
        if subs:
            if "domestic" not in nh_keys.registered_markets(self.conn, user_id):
                raise KeyMissing("domestic")
            await self._check_futures({sub.symbol for sub in subs})
        try:
            desired = await self.recorder.set_recording(user_id, subs)
        except TooManySubscriptions as exc:
            strategies = len(self.recorder.owned(user_id))
            raise RecordingInvalid(
                f"구독은 APP Key당 {MAX_SUBSCRIPTIONS}건까지인데 틱 저장 {len(subs)}건과 실행 "
                f"중인 자동 실행 전략의 구독 {strategies}건을 합치면 {exc.count}건입니다(같은 "
                "종목·흐름은 1건). 저장할 것을 줄이거나 전략을 멈추세요."
            ) from None
        await self._settle(user_id, desired)
        status = self.recorder.status(user_id)
        return {
            "recording": _grouped(desired),
            "subscriptions": [_subscription(sub, status) for sub in sorted(desired)],
            "used": len(desired | self.recorder.owned(user_id)),
            "limit": MAX_SUBSCRIPTIONS,
            "connection": _connection(status),
            "note": NOTE,
        }

    async def stop(self, user_id: UserId) -> None:
        """저장을 모두 멈추고 저장 설정을 지운다. 저장한 틱은 남긴다."""
        await self.recorder.set_recording(user_id, ())

    def data(self, user_id: UserId) -> dict[str, JsonValue]:
        """이 사용자가 백테스트에 쓸 수 있는 것: 저장한 틱·호가와 빈 구간, 모델 파일."""
        desired = recordings.load(self.conn, user_id)
        status = self.recorder.status(user_id)
        store = self.recorder.store
        recorded = _recorded(desired, status, store.stored(user_id), store.gaps(user_id))
        models: list[JsonValue] = [
            {
                "name": m.name,
                "sha256": m.sha256,
                "size": m.size,
                "uploaded_at": _time(m.uploaded_at),
            }
            for m in self.models.files(user_id)
        ]
        listed: list[JsonValue] = [*recorded[:MAX_LISTED_STREAMS]]
        return {
            "recorded": listed,
            "recorded_total": len(recorded),
            "subscriptions": {
                "used": len(desired | self.recorder.owned(user_id)),
                "limit": MAX_SUBSCRIPTIONS,
            },
            "connection": _connection(status),
            "models": models,
            "models_url": self.models_url,
            "bars": BARS,
        }

    async def _check_futures(self, symbols: Iterable[str]) -> None:
        for symbol in sorted(symbols):
            try:
                contract = await self.queries.contract(symbol)
            except UnknownSymbol:
                raise RecordingInvalid(
                    f"{symbol}: 종목 마스터에 없는 종목코드입니다. search_symbol 결과의 코드만 "
                    "쓰세요."
                ) from None
            if contract.kind != "future":
                raise RecordingInvalid(
                    f"{symbol}: 국내 선물만 저장할 수 있습니다. search_symbol 결과에서 kind가 "
                    "future인 종목을 고르세요."
                )

    async def _settle(self, user_id: UserId, desired: frozenset[Sub]) -> None:
        """구독마다 NH가 받았거나 거절할 때까지, 길어도 SETTLE_MS만 기다린다."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_MS / 1000
        while desired and loop.time() < deadline:
            status = self.recorder.status(user_id)
            if status is None or desired <= status.live | set(status.failed):
                return
            await asyncio.sleep(_POLL_MS / 1000)


def _grouped(subs: frozenset[Sub]) -> dict[str, JsonValue]:
    """{종목코드: [흐름, ...]}, 종목·흐름 순."""
    out: dict[str, list[JsonValue]] = {}
    for sub in sorted(subs):
        out.setdefault(sub.symbol, []).append(sub.stream)
    return {symbol: streams for symbol, streams in out.items()}
