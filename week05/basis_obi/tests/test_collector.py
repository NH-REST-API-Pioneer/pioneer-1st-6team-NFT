import asyncio
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector import build, ws  # noqa: E402


# ---------- 원문 → 틱 ----------

def kb(sym="A756C000", bid="1334.20", ask="1334.30", bq="17", aq="25", tm="091500"):
    body = {"sym": sym, "exch_tm": tm, "seq_no": "1", "bid_qty": "900", "ask_qty": "800"}
    for i in range(1, 11):
        body[f"bid_pric_{i}"] = f"{float(bid) - 0.1 * (i - 1):.2f}"
        body[f"ask_pric_{i}"] = f"{float(ask) + 0.1 * (i - 1):.2f}"
        body[f"bid_qty_{i}"] = bq if i == 1 else "100"
        body[f"ask_qty_{i}"] = aq if i == 1 else "100"
    return {"header": {"action_code": "KB", "action_sym": sym}, "body": body}


def ka(sym="A756C000", last="1334.30", qty="10", tm="091500"):
    return {"header": {"action_code": "KA", "action_sym": sym},
            "body": {"sym": sym, "exch_tm": tm, "last_pric": last, "exec_qty": qty,
                     "bid_best_pric": "1334.20", "ask_best_pric": "1334.30"}}


def fb(sym="MKUZ26", bid="0.7495", ask="0.7496", bq="3", aq="5", tm="081500", ko="091500"):
    body = {"sym": sym, "exch_tm": tm, "ko_exec_tm": ko}
    for i in range(1, 6):
        body[f"bid_pric_{i}"], body[f"ask_pric_{i}"] = bid, ask
        body[f"bid_qty_{i}"] = bq if i == 1 else "9"
        body[f"ask_qty_{i}"] = aq if i == 1 else "9"
    return {"header": {"action_code": "FB", "action_sym": sym}, "body": body}


def fa(sym="MKUZ26", last="0.7496", qty="2", ko="091500"):
    return {"header": {"action_code": "FA", "action_sym": sym},
            "body": {"sym": sym, "exch_tm": "081500", "ko_exec_tm": ko, "last_pric": last, "exec_qty": qty,
                     "bid_best_pric": "0.7495", "ask_best_pric": "0.7496"}}


def test_parse_krx_quote_uses_level1_and_keeps_5_levels():
    r = build.parse({"recv_ts": "2026-11-17T09:15:00.123", "conn": "dom", "msg": kb()})
    assert r["kind"] == "quote" and r["symbol"] == "A756C000"
    assert r["recv_ts_local_ms"] == int(pd.Timestamp("2026-11-17 09:15:00.123", tz="Asia/Seoul").value // 10**6)
    assert (r["bid"], r["ask"], r["bid_qty"], r["ask_qty"]) == (1334.20, 1334.30, 17.0, 25.0)
    assert r["exch_ts"] == "091500"
    assert r["bid_pric_5"] == pytest.approx(1333.80) and r["ask_qty_5"] == 100.0
    assert pd.isna(r["last"])


def test_parse_krx_trade():
    r = build.parse({"recv_ts": "2026-11-17T09:15:00.500", "conn": "dom", "msg": ka()})
    assert r["kind"] == "trade" and r["last"] == 1334.30 and r["last_qty"] == 10.0
    assert (r["bid"], r["ask"]) == (1334.20, 1334.30)
    assert pd.isna(r["bid_qty"])          # 체결 메시지에는 잔량이 없음


def test_parse_mku_quote_uses_korean_time_as_exch_ts():
    r = build.parse({"recv_ts": "2026-11-17T09:15:00.900", "conn": "ovs", "msg": fb()})
    assert r["symbol"] == "MKUZ26" and r["exch_ts"] == "091500" and r["exch_tm_raw"] == "081500"
    assert (r["bid"], r["ask"], r["bid_qty"], r["ask_qty"]) == (0.7495, 0.7496, 3.0, 5.0)


def test_parse_skips_pingpong_and_responses():
    assert build.parse({"recv_ts": "2026-11-17T09:15:00.000", "msg": {"code": "pingpong"}}) is None
    assert build.parse({"recv_ts": "2026-11-17T09:15:00.000",
                        "msg": {"code": "RT_INIT", "data": {"action_code": "KB"}}}) is None


def test_parse_week03_line_format():
    """3주차 기록은 conn 이름이 한국어(국내/해외) — 같은 함수로 읽힘."""
    r = build.parse({"recv_ts": "2026-11-03T09:24:49.693", "conn": "국내", "msg": ka(sym="A756B000")})
    assert r["symbol"] == "A756B000" and r["kind"] == "trade"


# ---------- 품질 ----------

def ticks(rows):
    return pd.DataFrame([build.parse(x) for x in rows])


def line(ts, msg, conn="dom"):
    return {"recv_ts": ts, "conn": conn, "msg": msg}


def test_quality_counts_duplicates_and_out_of_order():
    df = ticks([
        line("2026-11-17T09:00:00.000", kb(tm="090000")),
        line("2026-11-17T09:00:00.100", kb(tm="090000")),               # 완전 중복
        line("2026-11-17T09:00:01.000", kb(tm="090001", bid="1334.10")),
        line("2026-11-17T09:00:01.500", kb(tm="085959", bid="1334.00")),  # 거래소 시각 역순
    ])
    q = build.quality(df, events=[], day="20261117")
    s = q["symbols"]["A756C000"]["quote"]
    assert s["n"] == 4 and s["duplicates"] == 1 and s["out_of_order"] == 1


def test_quality_quote_gaps_only_inside_krx_hours():
    df = ticks([
        line("2026-11-17T08:00:00.000", kb()),        # 장 전 — 공백 계산에서 뺌
        line("2026-11-17T09:00:00.000", kb()),
        line("2026-11-17T09:00:03.000", kb(bid="1334.10")),
        line("2026-11-17T09:00:10.000", kb(bid="1334.00")),   # 7초 공백
    ])
    q = build.quality(df, events=[], day="20261117", gap_s=5)
    g = q["symbols"]["A756C000"]["quote_gaps"]
    assert g["count"] == 1 and g["max_s"] == pytest.approx(7.0)
    assert g["list"][0]["start"] == "09:00:03.000"


def test_quality_leg_lag_is_age_of_other_leg_quote():
    df = ticks([
        line("2026-11-17T09:00:00.000", fb(), "ovs"),
        line("2026-11-17T09:00:01.000", kb()),                         # MKU 호가가 1초 묵음
        line("2026-11-17T09:00:01.500", fb(bid="0.7494"), "ovs"),      # KRX 호가가 0.5초 묵음
        line("2026-11-17T09:00:04.500", kb(bid="1334.10")),            # MKU 3초
    ])
    q = build.quality(df, events=[], day="20261117", krx="A756C000", mku="MKUZ26")
    lag = q["leg_lag_s"]
    assert lag["n"] == 3
    assert lag["max"] == pytest.approx(3.0)
    assert lag["p50"] == pytest.approx(1.0)


def test_quality_disconnects_from_events():
    ev = [{"ts": "2026-11-17T10:00:00.000", "conn": "dom", "kind": "disconnect", "note": "60초 무응답"},
          {"ts": "2026-11-17T10:00:40.000", "conn": "dom", "kind": "connect", "note": ""}]
    q = build.quality(ticks([line("2026-11-17T09:00:00.000", kb())]), events=ev, day="20261117")
    d = q["disconnects"]["dom"]
    assert d["count"] == 1 and d["list"][0]["down_s"] == pytest.approx(40.0)


def test_build_day_writes_parquet_per_symbol_and_quality(tmp_path):
    raw = tmp_path / "jsonl" / "20261117"
    raw.mkdir(parents=True)
    with gzip.open(raw / "09.jsonl.gz", "wt", encoding="utf-8") as f:
        for x in [line("2026-11-17T09:00:00.000", kb()), line("2026-11-17T09:00:00.500", fb(), "ovs"),
                  line("2026-11-17T09:00:01.000", ka()), line("2026-11-17T09:00:01.000", {"code": "pingpong"})]:
            f.write(json.dumps(x) + "\n")
    out = build.build_day(tmp_path / "jsonl", "20261117", tmp_path / "raw", tmp_path / "quality")
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == ["20261117_A756C000.parquet",
                                                                    "20261117_MKUZ26.parquet"]
    k = pd.read_parquet(tmp_path / "raw" / "20261117_A756C000.parquet")
    assert list(k["kind"]) == ["quote", "trade"]
    assert k["recv_ts_local_ms"].is_monotonic_increasing
    assert json.loads((tmp_path / "quality" / "20261117.json").read_text())["symbols"]["MKUZ26"]["quote"]["n"] == 1
    assert out["rows"] == 3


# ---------- 접속 ----------

class FakeWS:
    def __init__(self, incoming):
        self.incoming, self.sent, self.idle = list(incoming), [], asyncio.Event()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.incoming:
            return self.incoming.pop(0)
        await self.idle.wait()


class FakeConn:
    def __init__(self, ws_):
        self.ws = ws_

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        return False


class Mem:
    def __init__(self):
        self.lines, self.events = [], []

    def write(self, ts, conn, raw):
        self.lines.append((conn, raw))

    def event(self, conn, kind, note=""):
        self.events.append((conn, kind, note))


INIT = '{"code":"INIT","msg":"INIT_SUCCESS"}'
PING = '{"code":"pingpong"}'


def stream(w, sock, **kw):
    kw.setdefault("silence_s", 0.3)
    kw.setdefault("first_msg_s", 0.3)
    kw.setdefault("poll_s", 0.02)
    kw.setdefault("resub_s", (0.05,))
    return ws.Stream("dom", [("KA", "A756C000"), ("KB", "A756C000")], w, get_key=lambda: "K",
                     get_token=lambda: "T", connect=lambda url, **_: FakeConn(sock), **kw)


def test_stream_subscribes_echoes_pingpong_and_logs():
    sock = FakeWS([INIT, PING, json.dumps(kb())])
    w = Mem()
    with pytest.raises(ConnectionError):
        asyncio.run(stream(w, sock).session())
    assert [s["body"].get("action_code") for s in sock.sent[1:3]] == ["KA", "KB"]
    assert {"code": "pingpong"} in sock.sent
    assert ("dom", "connect", "구독 2개") in w.events
    assert w.lines[-1][0] == "dom"


def test_stream_resubscribes_when_data_stops_but_pingpong_continues():
    """3주차: pingpong 은 오는데 KA 만 안 오던 구간 → 같은 연결에서 다시 구독."""
    sock = FakeWS([INIT, json.dumps(kb())] + [PING] * 30)
    w = Mem()
    s = stream(w, sock, silence_s=10, data_silence_s=0.05, poll_s=0.01, is_open=lambda: True)

    async def go():
        try:
            await asyncio.wait_for(s.session(), 0.5)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    assert any(k == "resub_data" for _, k, _ in w.events)
    assert sum(1 for m in sock.sent if m.get("body", {}).get("action_code") == "KB") >= 2


def test_writer_hourly_with_conn(tmp_path):
    w = ws.HourlyWriter(tmp_path)
    w.write(datetime(2026, 11, 17, 9, 59, 59), "dom", '{"a":1}')
    w.write(datetime(2026, 11, 17, 10, 0, 0), "ovs", '{"a":2}')
    w.event("dom", "connect", "x")
    w.close()
    with gzip.open(tmp_path / "20261117" / "09.jsonl.gz", "rt") as f:
        assert json.loads(f.readline()) == {"recv_ts": "2026-11-17T09:59:59.000", "conn": "dom", "msg": {"a": 1}}
    ev = [json.loads(x) for x in (tmp_path / "20261117" / "events.jsonl").read_text().splitlines()]
    assert ev[0]["kind"] == "connect" and ev[0]["conn"] == "dom"


def test_summary_lines_are_short_and_cover_each_check():
    q = build.quality(ticks([line("2026-11-17T09:00:00.000", kb()), line("2026-11-17T09:00:01.000", fb(), "ovs"),
                             line("2026-11-17T09:00:09.000", kb(bid="1334.00"))]),
                      events=[{"ts": "2026-11-17T09:30:00.000", "conn": "ovs", "kind": "disconnect", "note": "x"}],
                      day="20261117")
    text = build.summary(q)
    for word in ("A756C000", "MKUZ26", "중복", "역순", "공백", "끊김", "p95"):
        assert word in text
    assert len(text.splitlines()) <= 15


def test_trades_same_second_same_price_are_not_duplicates_unless_cum_volume_same():
    a = ka(); b = ka(); c = ka()
    a["body"]["cum_trd_qty"], b["body"]["cum_trd_qty"], c["body"]["cum_trd_qty"] = "100", "110", "110"
    df = ticks([line("2026-11-17T09:00:00.000", a), line("2026-11-17T09:00:00.100", b),
                line("2026-11-17T09:00:00.200", c)])
    assert build.quality(df, [], "20261117")["symbols"]["A756C000"]["trade"]["duplicates"] == 1


def test_leg_lag_uses_trades_when_a_leg_has_no_quotes():
    df = ticks([line("2026-11-17T09:00:00.000", fb(), "ovs"), line("2026-11-17T09:00:02.000", ka())])
    assert build.quality(df, [], "20261117")["leg_lag_s"]["max"] == pytest.approx(2.0)


# ---------- 웹소켓 접속키: 다른 프로세스가 토큰을 새로 받아 401 이 날 때 ----------

class _Res:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class _Session:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def get(self, url, timeout=None, headers=None):
        self.seen.append(headers["Authorization"])
        return self.replies.pop(0)


def _client(tmp_path, monkeypatch, file_token, replies, issue="ISSUED"):
    import nh_client
    from collector import collect

    monkeypatch.setattr(nh_client, "HOME_DIR", tmp_path)
    c = nh_client.NHClient.__new__(nh_client.NHClient)
    c.profile, c.env, c.base, c.timeout, c.scope = "overseas", "demo", "https://x", 5, None
    c.app_key, c.app_secret = "key-abcd", "secret"
    c.throttles = {"auth": type("T", (), {"wait": lambda self: None})()}
    c._token, c._token_until = "OLD", 9e18
    c.session = _Session(replies)
    c.issued = []

    def post(url, data=None, timeout=None):
        c.issued.append(1)
        return _Res(200, {"access_token": issue, "expires_in": 86400, "scopes": "S"})
    c.session.post = post
    if file_token:
        c._token_file().write_text(json.dumps({"access_token": file_token, "until": 9e18,
                                               "app_key_tail": "abcd", "scopes": "S"}))
    return c, collect


EXPIRED = _Res(401, {"errorMessage": "토큰 재발급이 필요합니다.", "errorCode": "EGW052"})
OK = _Res(200, {"access_key": "WSKEY"})


def test_ws_key_uses_newer_token_another_process_saved(tmp_path, monkeypatch):
    c, collect = _client(tmp_path, monkeypatch, file_token="NEWER", replies=[EXPIRED, OK])
    assert collect.ws_key(c) == "WSKEY"
    assert c.session.seen == ["Bearer OLD", "Bearer NEWER"]
    assert c.issued == []                                   # 새로 발급하지 않음 → 다른 프로세스 토큰을 죽이지 않음


def test_ws_key_reissues_only_when_saved_token_is_the_failed_one(tmp_path, monkeypatch):
    c, collect = _client(tmp_path, monkeypatch, file_token="OLD", replies=[EXPIRED, OK])
    assert collect.ws_key(c) == "WSKEY"
    assert c.session.seen == ["Bearer OLD", "Bearer ISSUED"]
    assert c.issued == [1]
    assert json.loads(c._token_file().read_text())["access_token"] == "ISSUED"
