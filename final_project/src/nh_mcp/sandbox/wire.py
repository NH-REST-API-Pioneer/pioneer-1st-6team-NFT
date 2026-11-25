"""MCP 서버·실행기·하네스가 주고받는 JSON 모양. 가격은 소수 문자열, 시각은 KST ISO 초 단위다.

MCP 서버(클라이언트)와 실행기가 같이 쓰는 상수·타입·변환은 여기 둔다. 클라이언트가 docker를
띄우는 container.py나 실행기 앱(executor.py)을 import하지 않게 하려는 것이다.
"""

import base64
import json
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast

from nh_mcp.domain import (
    KST,
    MAX_MODEL_BYTES,
    BarEvent,
    BookEvent,
    Candle,
    FillEvent,
    JsonValue,
    MarketEvent,
    Output,
    SandboxFailure,
    SandboxJob,
    SandboxResult,
    StrategyEvent,
    Stream,
    SymbolTerms,
    Target,
    TickEvent,
)

TOKEN_HEADER = "x-sandbox-token"
MAX_DETAIL = 500
# 호가는 최대 10단계다(KB).
MAX_LEVELS = 10
# 모델 파일을 base64로 쓴 글자 수의 상한(50MiB → 69,905,068자). 실행기 본문 상한이 이만큼 크다.
MAX_MODEL_TEXT = 4 * -(-MAX_MODEL_BYTES // 3)

# 부호·지수·NaN·공백·전각 숫자를 받지 않는 소수 문자열. 길이를 묶어 Decimal 계산이 커지지 않게 한다.
_DECIMAL = re.compile(r"-?[0-9]{1,15}(\.[0-9]{1,10})?")
_EVENT_KEYS = {
    "bar": frozenset(
        {"type", "symbol", "interval", "at", "open", "high", "low", "close", "volume"}
    ),
    "tick": frozenset({"type", "symbol", "at", "price", "qty", "bid", "ask"}),
    "book": frozenset({"type", "symbol", "at", "bids", "asks"}),
}
# model은 늘 있다(없으면 null). 키가 정확히 맞는지만 보면 되게 한다.
_JOB_KEYS = frozenset({"code", "params", "streams", "max_position", "terms", "events", "model"})
_TERMS_KEYS = frozenset({"tick", "multiplier", "fee_per_fill"})
_REPLY_KEYS = frozenset({"status", "outputs", "index", "detail"})
_TARGET_KEYS = (frozenset({"position"}), frozenset({"position", "price"}))


class BadOutput(Exception):
    """전략 출력이 계약과 다르다."""


def _price(value: Decimal) -> str:
    return format(value, "f")


def _stamp(at: datetime) -> str:
    return at.astimezone(KST).isoformat(timespec="seconds")


def _levels(levels: tuple[tuple[Decimal, int], ...]) -> list[JsonValue]:
    return [[_price(price), qty] for price, qty in levels]


def event_to_wire(event: StrategyEvent) -> dict[str, JsonValue]:
    """이벤트를 하네스가 받는 모양으로 바꾼다. 가격을 float로 거치지 않는다."""
    match event:
        case BarEvent(symbol=symbol, interval=interval, bar=bar):
            return {
                "type": "bar",
                "symbol": symbol,
                "interval": interval,
                "at": _stamp(bar.at),
                "open": _price(bar.open),
                "high": _price(bar.high),
                "low": _price(bar.low),
                "close": _price(bar.close),
                "volume": bar.volume,
            }
        case TickEvent(symbol=symbol, at=at, price=price, qty=qty, bid=bid, ask=ask):
            return {
                "type": "tick",
                "symbol": symbol,
                "at": _stamp(at),
                "price": _price(price),
                "qty": qty,
                "bid": None if bid is None else _price(bid),
                "ask": None if ask is None else _price(ask),
            }
        case BookEvent(symbol=symbol, at=at, bids=bids, asks=asks):
            return {
                "type": "book",
                "symbol": symbol,
                "at": _stamp(at),
                "bids": _levels(bids),
                "asks": _levels(asks),
            }
        case FillEvent(symbol=symbol, at=at, side=side, qty=qty, price=price):
            return {
                "type": "fill",
                "symbol": symbol,
                "at": _stamp(at),
                "side": side,
                "qty": qty,
                "price": _price(price),
            }


def output_to_wire(output: Output) -> JsonValue:
    if output is None:
        return None
    return {
        symbol: {
            "position": target.position,
            "price": None if target.price is None else _price(target.price),
        }
        for symbol, target in output.items()
    }


def job_to_json(job: SandboxJob) -> dict[str, JsonValue]:
    """실행기에 보내는 본문. 종목과 흐름은 정렬해서 같은 일이면 같은 본문이 되게 한다."""
    streams: dict[str, JsonValue] = {
        symbol: [*sorted(names)] for symbol, names in sorted(job.streams.items())
    }
    terms: dict[str, JsonValue] = {
        symbol: {
            "tick": _price(t.tick),
            "multiplier": _price(t.multiplier),
            "fee_per_fill": _price(t.fee_per_fill),
        }
        for symbol, t in sorted(job.terms.items())
    }
    return {
        "code": job.code,
        "params": job.params,
        "streams": streams,
        "max_position": job.max_position,
        "terms": terms,
        "events": [event_to_wire(e) for e in job.events],
        "model": None if job.model is None else base64.b64encode(job.model).decode("ascii"),
    }


def result_to_json(r: SandboxResult) -> dict[str, JsonValue]:
    return {
        "status": r.status,
        "outputs": [output_to_wire(o) for o in r.outputs],
        "index": r.index,
        "detail": r.detail,
    }


def _int(raw: object) -> int | None:
    # JSON의 true·false는 bool이고, bool은 int의 하위형이라 따로 막는다.
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _decimal(raw: object) -> Decimal | None:
    if not isinstance(raw, str) or _DECIMAL.fullmatch(raw) is None:
        return None
    return Decimal(raw)


def _optional_decimal(raw: object) -> tuple[Decimal | None] | None:
    """null이면 (None,), 소수 문자열이면 (값,), 아니면 None."""
    if raw is None:
        return (None,)
    value = _decimal(raw)
    return None if value is None else (value,)


def _at(raw: object) -> datetime | None:
    """KST ISO 초 단위 시각만 받는다. 다른 모양은 같은 시각이어도 받지 않는다."""
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if at.tzinfo is None or _stamp(at) != raw:
        return None
    return at


def _book_levels(raw: object) -> tuple[tuple[Decimal, int], ...] | None:
    if not isinstance(raw, list):
        return None
    items = cast(list[object], raw)
    if len(items) > MAX_LEVELS:
        return None
    levels: list[tuple[Decimal, int]] = []
    for item in items:
        if not isinstance(item, list):
            return None
        match cast(list[object], item):
            case [price, qty]:
                checked_price, checked_qty = _decimal(price), _int(qty)
            case _:
                return None
        if checked_price is None or checked_qty is None:
            return None
        levels.append((checked_price, checked_qty))
    return tuple(levels)


def _bar(item: dict[str, object], symbol: str, at: datetime) -> BarEvent | None:
    raw_interval = item["interval"]
    if not isinstance(raw_interval, str):
        return None
    match raw_interval:
        case "minute" | "day" as interval:
            pass
        case _:
            return None
    o, h, low, c = (_decimal(item[key]) for key in ("open", "high", "low", "close"))
    volume = item["volume"]
    checked_volume = None if volume is None else _int(volume)
    if o is None or h is None or low is None or c is None:
        return None
    if volume is not None and checked_volume is None:
        return None
    return BarEvent(symbol, interval, Candle(at, o, h, low, c, checked_volume))


def _tick(item: dict[str, object], symbol: str, at: datetime) -> TickEvent | None:
    price, qty = _decimal(item["price"]), _int(item["qty"])
    bid, ask = _optional_decimal(item["bid"]), _optional_decimal(item["ask"])
    if price is None or qty is None or bid is None or ask is None:
        return None
    return TickEvent(symbol, at, price, qty, bid[0], ask[0])


def _book(item: dict[str, object], symbol: str, at: datetime) -> BookEvent | None:
    bids, asks = _book_levels(item["bids"]), _book_levels(item["asks"])
    if bids is None or asks is None:
        return None
    return BookEvent(symbol, at, bids, asks)


def market_event_from_wire(raw: object) -> MarketEvent | None:
    """시장 이벤트 하나를 검사하고 바꾼다. 종류마다 키가 정확히 맞아야 한다. 틀리면 None."""
    if not isinstance(raw, dict):
        return None
    item = cast(dict[str, object], raw)
    kind = item.get("type")
    if not isinstance(kind, str) or kind not in _EVENT_KEYS or frozenset(item) != _EVENT_KEYS[kind]:
        return None
    symbol, at = item["symbol"], _at(item["at"])
    if not isinstance(symbol, str) or at is None:
        return None
    match kind:
        case "bar":
            return _bar(item, symbol, at)
        case "tick":
            return _tick(item, symbol, at)
        case _:
            return _book(item, symbol, at)


def stream_of(event: MarketEvent) -> Stream:
    """이 이벤트가 속한 흐름."""
    match event:
        case BarEvent(interval=interval):
            return interval
        case TickEvent():
            return "tick"
        case BookEvent():
            return "book"


def _stream_names(raw: object) -> frozenset[Stream] | None:
    if not isinstance(raw, list) or not raw:
        return None
    names = cast(list[object], raw)
    checked: set[Stream] = set()
    for name in names:
        if not isinstance(name, str):
            return None
        match name:
            case "tick" | "book" | "minute" | "day" as stream:
                checked.add(stream)
            case _:
                return None
    return frozenset(checked) if len(checked) == len(names) else None


def _streams(raw: object) -> dict[str, frozenset[Stream]] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    streams: dict[str, frozenset[Stream]] = {}
    for symbol, names in cast(dict[str, object], raw).items():
        checked = _stream_names(names)
        if checked is None:
            return None
        streams[symbol] = checked
    return streams


def _symbol_terms(raw: object) -> SymbolTerms | None:
    if not isinstance(raw, dict) or frozenset(cast(dict[str, object], raw)) != _TERMS_KEYS:
        return None
    item = cast(dict[str, object], raw)
    tick, multiplier = _decimal(item["tick"]), _decimal(item["multiplier"])
    fee = _decimal(item["fee_per_fill"])
    if tick is None or multiplier is None or fee is None:
        return None
    if tick <= 0 or multiplier <= 0 or fee < 0:
        return None
    return SymbolTerms(tick, multiplier, fee)


def _terms(raw: object, symbols: frozenset[str]) -> dict[str, SymbolTerms] | None:
    """흐름을 선언한 종목마다 정확히 하나씩."""
    if not isinstance(raw, dict) or frozenset(cast(dict[str, object], raw)) != symbols:
        return None
    terms: dict[str, SymbolTerms] = {}
    for symbol, item in cast(dict[str, object], raw).items():
        checked = _symbol_terms(item)
        if checked is None:
            return None
        terms[symbol] = checked
    return terms


def _events(raw: object, streams: Mapping[str, frozenset[Stream]]) -> list[MarketEvent] | None:
    """이벤트마다 선언한 종목의 선언한 흐름이어야 한다."""
    if not isinstance(raw, list):
        return None
    events: list[MarketEvent] = []
    for item in cast(list[object], raw):
        event = market_event_from_wire(item)
        if event is None or stream_of(event) not in streams.get(event.symbol, frozenset()):
            return None
        events.append(event)
    return events


def _model(raw: object) -> tuple[bytes | None] | None:
    """null이면 (None,), 1바이트~MAX_MODEL_BYTES의 base64면 (내용,), 아니면 None."""
    if raw is None:
        return (None,)
    if not isinstance(raw, str) or len(raw) > MAX_MODEL_TEXT:
        return None
    try:
        model = base64.b64decode(raw, validate=True)
    except ValueError:
        # base64 글자가 아니거나(binascii.Error) ASCII가 아닌 글자가 있다.
        return None
    return (model,) if 0 < len(model) <= MAX_MODEL_BYTES else None


def job_from_json(raw: object) -> SandboxJob | None:
    """실행기가 받은 본문을 일로 바꾼다. 키가 정확히 일곱 개이고 모양이 모두 맞아야 한다."""
    if not isinstance(raw, dict) or frozenset(cast(dict[str, object], raw)) != _JOB_KEYS:
        return None
    body = cast(dict[str, object], raw)
    code, params, max_position = body["code"], body["params"], _int(body["max_position"])
    streams = _streams(body["streams"])
    if not isinstance(code, str) or not isinstance(params, dict) or streams is None:
        return None
    if max_position is None or max_position < 0:
        return None
    terms, events = _terms(body["terms"], frozenset(streams)), _events(body["events"], streams)
    model = _model(body["model"])
    if terms is None or events is None or model is None:
        return None
    return SandboxJob(
        code, cast(dict[str, JsonValue], params), streams, max_position, terms, events, model[0]
    )


def _target(raw: object) -> Target:
    if not isinstance(raw, dict) or frozenset(cast(dict[str, object], raw)) not in _TARGET_KEYS:
        raise BadOutput
    item = cast(dict[str, object], raw)
    position = _int(item["position"])
    if position is None:
        raise BadOutput
    price = _optional_decimal(item.get("price"))
    if price is None or (price[0] is not None and price[0] <= 0):
        raise BadOutput
    return Target(position, price[0])


def output_from_wire(raw: object, job: SandboxJob) -> Output:
    """전략 출력 하나를 검사하고 바꾼다. 어기면 BadOutput.

    null 또는 {종목: {"position": 정수, "price": 소수 문자열(생략·null이면 시장가)}}. 종목은
    선언한 흐름의 종목만, |position| ≤ max_position, 가격은 0보다 크고 그 종목 틱의 배수.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadOutput
    output: dict[str, Target] = {}
    for symbol, item in cast(dict[str, object], raw).items():
        terms = job.terms.get(symbol)
        if terms is None or symbol not in job.streams:
            raise BadOutput
        target = _target(item)
        if abs(target.position) > job.max_position:
            raise BadOutput
        if target.price is not None and target.price % terms.tick != 0:
            raise BadOutput
        output[symbol] = target
    return output


def preview(raw: object) -> str:
    """잘못된 출력을 실패 사유에 담을 짧은 글. 전략 코드가 낸 값이다."""
    try:
        return json.dumps(raw, ensure_ascii=False)[:100]
    except (TypeError, ValueError, RecursionError):
        return ""


def _status(raw: object) -> Literal["ok"] | SandboxFailure | None:
    if not isinstance(raw, str):
        return None
    match raw:
        case (
            "ok"
            | "invalid_code"
            | "invalid_output"
            | "exception"
            | "cpu"
            | "memory"
            | "processes"
            | "output"
            | "timeout" as status
        ):
            return status
        case _:
            return None


def result_from_json(raw: object, job: SandboxJob) -> SandboxResult | None:
    """실행기 응답 본문을 결과로 바꾼다. 출력도 이 일의 계약대로 다시 검사한다. 틀리면 None."""
    if not isinstance(raw, dict):
        return None
    body = cast(dict[str, object], raw)
    if frozenset(body) != _REPLY_KEYS:
        return None
    status, raw_outputs = _status(body["status"]), body["outputs"]
    index, detail = body["index"], body["detail"]
    if status is None or not isinstance(raw_outputs, list) or not isinstance(detail, str):
        return None
    if index is not None and _int(index) is None:
        return None
    try:
        outputs = [output_from_wire(o, job) for o in cast(list[object], raw_outputs)]
    except BadOutput:
        return None
    return SandboxResult(status, outputs, cast(int | None, index), detail)
