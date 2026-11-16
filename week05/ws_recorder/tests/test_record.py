import asyncio
import csv
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import convert  # noqa: E402
import record  # noqa: E402


# ---------- 시간별 파일 ----------

def read_gz(p: Path) -> list[dict]:
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f]


def test_writer_rotates_by_hour_and_gzips_finished_hour(tmp_path):
    w = record.HourlyWriter(tmp_path)
    w.write(datetime(2026, 11, 16, 10, 59, 59, 500000), '{"a":1}')
    w.write(datetime(2026, 11, 16, 11, 0, 0), '{"a":2}')
    w.close()
    day = tmp_path / "20261116"
    assert read_gz(day / "10.jsonl.gz") == [{"recv_ts": "2026-11-16T10:59:59.500", "msg": {"a": 1}}]
    assert not (day / "10.jsonl").exists()
    # 닫을 때 지금 쓰던 시간도 압축
    assert read_gz(day / "11.jsonl.gz")[0]["msg"] == {"a": 2}


def test_writer_rotates_across_days(tmp_path):
    w = record.HourlyWriter(tmp_path)
    w.write(datetime(2026, 11, 16, 23, 59, 0), '{"a":1}')
    w.write(datetime(2026, 11, 17, 0, 0, 1), '{"a":2}')
    w.close()
    assert (tmp_path / "20261116" / "23.jsonl.gz").exists()
    assert (tmp_path / "20261117" / "00.jsonl.gz").exists()


def test_writer_compresses_leftover_plain_files_on_start(tmp_path):
    """강제 종료로 남은 압축 안 된 파일은 다음 시작 때 압축 (이미 .gz 가 있으면 이어 붙임)."""
    day = tmp_path / "20261116"
    day.mkdir()
    with gzip.open(day / "09.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write('{"recv_ts":"x","msg":{"a":0}}\n')
    (day / "09.jsonl").write_text('{"recv_ts":"y","msg":{"a":1}}\n', encoding="utf-8")
    w = record.HourlyWriter(tmp_path)
    w.close()
    assert not (day / "09.jsonl").exists()
    assert [r["msg"]["a"] for r in read_gz(day / "09.jsonl.gz")] == [0, 1]


def test_writer_same_hour_after_restart_appends(tmp_path):
    w = record.HourlyWriter(tmp_path)
    w.write(datetime(2026, 11, 16, 10, 0, 0), '{"a":1}')
    w.close()
    w = record.HourlyWriter(tmp_path)
    w.write(datetime(2026, 11, 16, 10, 30, 0), '{"a":2}')
    w.close()
    assert [r["msg"]["a"] for r in read_gz(tmp_path / "20261116" / "10.jsonl.gz")] == [1, 2]


# ---------- 재접속 대기 ----------

def test_backoff_grows_caps_and_resets():
    b = record.Backoff(steps=(10, 30, 60))
    assert [b.fail() for _ in range(5)] == [10, 30, 60, 60, 60]   # 기록기는 포기하지 않고 상한에서 계속
    b.ok()
    assert b.fail() == 10


# ---------- 접속 한 번 ----------

class FakeWS:
    def __init__(self, incoming: list[str]):
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.idle = asyncio.Event()      # 다 보내면 조용해짐

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self.incoming:
            return self.incoming.pop(0)
        await self.idle.wait()
        raise AssertionError("unreachable")


class FakeConn:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        return False


class MemWriter:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, ts, raw):
        self.lines.append(raw)


INIT = '{"code":"INIT","msg":"INIT_SUCCESS"}'
PING = '{"code":"pingpong"}'
SUBS = [("FA", "ESZ26"), ("FB", "ESZ26")]


def fb(sym="ESZ26"):
    return json.dumps({"header": {"action_code": "FB", "action_sym": sym},
                       "body": {"sym": sym, "bid_pric_1": "1", "ask_pric_1": "2"}})


def make_rec(ws, **kw):
    kw.setdefault("silence_s", 0.3)
    kw.setdefault("first_msg_s", 0.3)
    kw.setdefault("poll_s", 0.02)
    kw.setdefault("resub_s", (0.05,))
    return record.Recorder(MemWriter(), SUBS, get_key=lambda: "K", get_token=lambda: "T",
                           connect=lambda url, **_: FakeConn(ws), **kw)


def test_session_inits_subscribes_echoes_pingpong_and_writes_everything():
    ws = FakeWS([INIT, '{"code":"RT_INIT","data":{"action":"A","action_code":"FA","action_sym":"ESZ26"}}',
                 PING, fb()])
    rec = make_rec(ws)
    with pytest.raises(ConnectionError):        # 다 받은 뒤 조용해지면 끊김으로 봄
        asyncio.run(rec.session())
    assert ws.sent[0]["body"] == {"action_code": "session_init", "session_token": "T"}
    assert [(s["body"]["action_code"], s["body"]["action_sym"]) for s in ws.sent[1:3]] == SUBS
    assert {"code": "pingpong"} in ws.sent[3:]
    assert rec.writer.lines[-1] == fb()          # 시세는 원문 그대로
    assert rec.counts["FB ESZ26"] == 1
    assert rec.got_data


def test_session_init_failure_raises():
    ws = FakeWS(['{"code":"INIT","msg":"FAIL"}'])
    with pytest.raises(RuntimeError):
        asyncio.run(make_rec(ws).session())


def test_rt_fail_resubscribes_on_same_connection():
    fail = '{"code":"RT_FAIL","data":{"action":"A","action_code":"FB","action_sym":"ESZ26"},"msg":"x"}'
    ws = FakeWS([INIT, fail, PING])
    rec = make_rec(ws)
    with pytest.raises(ConnectionError):
        asyncio.run(rec.session())
    fb_subs = [s for s in ws.sent if s.get("body", {}).get("action_code") == "FB"]
    assert len(fb_subs) == 2                     # 처음 + 같은 연결에서 다시


def test_silence_without_any_message_raises_quickly():
    ws = FakeWS([INIT])
    rec = make_rec(ws, first_msg_s=0.1, silence_s=10)
    with pytest.raises(ConnectionError, match="첫 메시지"):
        asyncio.run(asyncio.wait_for(rec.session(), 2))


def test_run_reconnects_after_failure_and_stops_at_deadline():
    calls = []

    def connect(url, **_):
        calls.append(url)
        return FakeConn(FakeWS([INIT, PING]))

    slept = []

    async def sleep(s):
        slept.append(s)

    rec = record.Recorder(MemWriter(), SUBS, get_key=lambda: "K", get_token=lambda: "T", connect=connect,
                          silence_s=0.05, first_msg_s=0.05, poll_s=0.01, sleep=sleep,
                          backoff=record.Backoff(steps=(7, 9)))
    asyncio.run(rec.run(max_sessions=3))
    assert len(calls) == 3
    assert "access_key=K" in calls[0]
    assert slept[:2] == [7, 9]


# ---------- 변환 ----------

def write_raw(p: Path, msgs: list[tuple[str, dict]]):
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for ts, m in msgs:
            f.write(json.dumps({"recv_ts": ts, "msg": m}) + "\n")


def test_convert_splits_trades_and_quotes_by_symbol(tmp_path):
    raw = tmp_path / "raw"
    fa = {"header": {"action_code": "FA", "action_sym": "ESZ26"},
          "body": {"sym": "ESZ26", "ref_dt": "20261116", "exch_tm": "001637", "ko_exec_tm": "151637",
                   "last_pric": "7539.50", "exec_qty": "2", "exec_div": "+", "cum_trd_qty": "100",
                   "bid_best_pric": "7539.25", "ask_best_pric": "7539.75"}}
    fbm = {"header": {"action_code": "FB", "action_sym": "GCZ26"},
           "body": {"sym": "GCZ26", "exch_tm": "001638", "ko_exec_tm": "151638",
                    **{f"{s}_{k}_{i}": str(i) for s in ("bid", "ask") for k in ("pric", "qty", "cnt")
                       for i in range(1, 6)}, "bid_qty": "15", "ask_qty": "16"}}
    write_raw(raw / "20261116" / "15.jsonl.gz", [
        ("2026-11-16T15:16:37.100", {"code": "pingpong"}),
        ("2026-11-16T15:16:37.200", fa),
        ("2026-11-16T15:16:38.300", fbm),
    ])
    out = tmp_path / "csv"
    n = convert.convert_day(raw / "20261116", out / "20261116")
    assert n == {"trades_ESZ26": 1, "quotes_GCZ26": 1}
    with gzip.open(out / "20261116" / "trades_ESZ26.csv.gz", "rt") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["recv_ts"] == "2026-11-16T15:16:37.200"
    assert rows[0]["last_pric"] == "7539.50" and rows[0]["exec_qty"] == "2" and rows[0]["exec_div"] == "+"
    with gzip.open(out / "20261116" / "quotes_GCZ26.csv.gz", "rt") as f:
        q = list(csv.DictReader(f))[0]
    assert q["bid_pric_1"] == "1" and q["ask_qty_5"] == "5" and q["bid_cnt_3"] == "3"


def test_convert_tolerates_truncated_gzip(tmp_path):
    """기록 중에 끊긴 .gz 끝부분은 버리고 앞쪽은 살림."""
    p = tmp_path / "raw" / "20261116" / "15.jsonl.gz"
    fbm = {"header": {"action_code": "FB", "action_sym": "ESZ26"}, "body": {"sym": "ESZ26"}}
    write_raw(p, [("2026-11-16T15:00:00.000", fbm)] * 50)
    data = p.read_bytes()
    p.write_bytes(data[:-6])                     # 꼬리 잘림
    n = convert.convert_day(p.parent, tmp_path / "csv")
    assert n["quotes_ESZ26"] >= 1
