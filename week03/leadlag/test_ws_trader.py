"""ws_trader 시험 — 접속·주문 없이 가짜 시세·가짜 웹소켓을 넣어 본다 (Engine 판단, 재접속 대기).

  python3 -m unittest test_ws_trader -v
"""
import asyncio
import json
import unittest
from unittest import mock

import ws_trader
from ws_trader import TICK, Backoff, Engine, LiveParams


def engine(**kw) -> Engine:
    return Engine(LiveParams(lookback_s=30, move_won=0.5, rho_max=0.5, hold_s=60, sl_ticks=3, cooldown_s=10, stale_s=60, **kw))


class EngineTest(unittest.TestCase):
    def warm(self, e: Engine, krx=1340.0, bid=0.7450, ask=0.7451, t0=0.0):
        e.on_quote(bid, ask, t0, "100000")
        e.on_krx(krx, t0, "100000")

    def test_krx_up_mku_lagging_sells_mku(self):
        e = engine()
        self.warm(e)
        e.on_quote(0.7450, 0.7451, 20, "100020")            # MKU 그대로
        a = e.on_krx(1340.8, 31, "100031")                    # 31초 뒤 KRX +0.8원
        self.assertEqual(a["act"], "enter")
        self.assertEqual(a["side"], -1)                       # KRX 상승 = MKU 매도
        self.assertEqual(a["ref"], 0.7450)                    # 매도는 매수호가에 체결
        self.assertLess(a["rho"], 0.5)

    def test_mku_already_followed_no_entry(self):
        e = engine()
        self.warm(e)
        # KRX +0.8원 ≈ MKU 4.4틱 하락에 해당. MKU 가 4틱 이미 내려옴 → 반영률 0.9
        e.on_quote(0.7446, 0.7447, 20, "100020")
        self.assertIsNone(e.on_krx(1340.8, 31, "100031"))

    def test_small_move_or_wide_spread_no_entry(self):
        e = engine()
        self.warm(e)
        self.assertIsNone(e.on_krx(1340.3, 31, "100031"))     # 0.3원 < 기준
        e2 = engine()
        self.warm(e2, bid=0.7450, ask=0.7454)                 # 호가 4틱
        self.assertIsNone(e2.on_krx(1340.8, 31, "100031"))

    def test_outside_entry_hours(self):
        e = engine()
        e.on_quote(0.7450, 0.7451, 0, "152100")
        e.on_krx(1340.0, 0, "152100")
        self.assertIsNone(e.on_krx(1340.8, 31, "152131"))

    def test_stop_loss_and_cooldown_and_daily_limit(self):
        e = engine(max_stops_per_day=1)
        self.warm(e)
        a = e.on_krx(1340.8, 31, "100031")
        e.filled_entry(a["side"], a["ref"], 31, "100031")
        self.assertIsNone(e.on_quote(0.7451, 0.7452, 35, "100035"))   # 매도 포지션, 청산호가 0.7452 → -2틱
        x = e.on_quote(0.7452, 0.7453, 36, "100036")                   # -3틱 → 손절
        self.assertEqual((x["act"], x["reason"], x["side"]), ("exit", "손절", 1))
        r = e.filled_exit(x["ref"], 36, x["reason"])
        self.assertAlmostEqual(r["gross_ticks"], -3.0)
        self.assertAlmostEqual(r["net_ticks"], -3.0 - 1.6)
        # 손절 1회 한도 → 이후 신호 무시
        e.on_krx(1340.0, 50, "100050")
        self.assertIsNone(e.on_krx(1341.0, 90, "100130"))

    def test_time_exit_and_flatten(self):
        e = engine()
        self.warm(e)
        a = e.on_krx(1340.8, 31, "100031")
        e.filled_entry(a["side"], a["ref"], 31, "100031")
        self.assertIsNone(e.on_timer(60, "100100"))
        self.assertEqual(e.on_timer(92, "100132")["reason"], "시간")
        e2 = engine()
        e2.on_quote(0.7450, 0.7451, 0, "153900")
        e2.filled_entry(1, 0.7451, 0, "153900")
        self.assertEqual(e2.on_timer(5, "154000")["reason"], "마감")

    def test_stale_krx_gap_no_fake_shock(self):
        e = engine()
        self.warm(e)
        e.on_quote(0.7450, 0.7451, 100, "100140")
        # KRX 가 100초 끊겼다가 +0.8원 위에서 다시 들어옴 → 끊기기 전 값과 비교하지 않음
        self.assertIsNone(e.on_krx(1340.8, 100, "100140"))
        self.assertIsNone(e.on_krx(1340.8, 120, "100200"))
        # 다시 lookback 만큼 쌓인 뒤의 진짜 충격은 잡음
        e.on_quote(0.7450, 0.7451, 140, "100220")
        self.assertEqual(e.on_krx(1341.6, 140, "100220")["act"], "enter")

    def test_no_double_entry_while_pending(self):
        e = engine()
        self.warm(e)
        self.assertIsNotNone(e.on_krx(1340.8, 31, "100031"))
        self.assertIsNone(e.on_krx(1341.0, 32, "100032"))              # 주문 대기 중


class BackoffTest(unittest.TestCase):
    def test_waits_grow_then_give_up(self):
        b = Backoff()
        self.assertEqual([b.fail() for _ in range(5)], [10, 30, 60, 120, 300])
        self.assertIsNone(b.fail())                                     # 6번째 연속 실패 → 포기

    def test_ok_resets(self):
        b = Backoff()
        b.fail(); b.fail()
        b.ok()
        self.assertEqual(b.fail(), 10)


# ---- stream() 재접속 시험: 가짜 웹소켓 ----
INIT = json.dumps({"code": "INIT", "msg": "INIT_SUCCESS"})
RT_INIT = json.dumps({"code": "RT_INIT", "data": {"action": "A", "action_code": "KA", "action_sym": "A756B000"}, "msg": ""})
PING = json.dumps({"code": "pingpong", "msg": "pingpong"})


class FakeWS:
    """script 의 메시지를 차례로 주고, 다 떨어지면 조용해진다 (11/03 처럼 소켓은 안 닫힘)."""

    def __init__(self, script):
        self.script, self.sent = list(script), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        self.sent.append(raw)

    async def recv(self):
        if self.script:
            return self.script.pop(0)
        await asyncio.sleep(3600)


class FakeRec:
    def __init__(self):
        self.events = []

    def message(self, conn, raw):
        pass

    def event(self, kind, **kw):
        self.events.append((kind, kw.get("note", "")))


class StreamTest(unittest.TestCase):
    def run_stream(self, scripts, key_errors=0, max_fails=3, handler=None, ws_cls=FakeWS, silence=0.5):
        t = ws_trader.Trader.__new__(ws_trader.Trader)
        t.rec, t.eng, t.stop = FakeRec(), Engine(LiveParams()), None
        conns, keys = [], {"n": 0}

        def fake_key(client):
            keys["n"] += 1
            if keys["n"] <= key_errors:
                raise SystemExit("웹소켓 키 발급 실패: EGW020")
            return "k"

        def fake_connect(url, **kw):
            ws = ws_cls(scripts[min(len(conns), len(scripts) - 1)])
            conns.append(ws)
            return ws

        async def noop(code, body):
            pass

        client = mock.Mock(env=next(iter(ws_trader.WS_URLS)), token=lambda: "tok")

        async def go():
            t.stop = asyncio.Event()
            await asyncio.wait_for(t.stream("국내", client, [("KA", "A756B000")], handler or noop), 10)

        with mock.patch.object(ws_trader, "ws_key", fake_key), \
             mock.patch.object(ws_trader.websockets, "connect", fake_connect), \
             mock.patch.object(ws_trader, "Backoff", lambda: Backoff(steps=(0.01, 0.02), max_fails=max_fails)), \
             mock.patch.object(ws_trader, "FIRST_MSG_S", 0.3), mock.patch.object(ws_trader, "SILENCE_S", silence), \
             mock.patch.object(ws_trader, "RESUB_S", (0.05,)):
            asyncio.run(go())
        return t.rec.events, conns, keys["n"]

    def test_rt_init_then_silence_gives_up(self):
        # 11/03 국내: 접속·RT_INIT 은 되는데 그 뒤 아무것도 없음 → 대기 늘려 가다 상한에서 중단 (무한 반복 안 함)
        events, conns, nkeys = self.run_stream([[INIT, RT_INIT]], max_fails=3)
        kinds = [k for k, _ in events]
        self.assertEqual(kinds.count("끊김"), 3)
        self.assertEqual(kinds[-1], "중단")
        self.assertEqual(len(conns), 3)
        self.assertEqual(nkeys, 3)
        self.assertIn("첫 메시지 없음", events[1][1])

    def test_key_error_backs_off_not_exit(self):
        # 키 발급 실패(SystemExit)도 프로그램을 끄지 않고 같은 대기 규칙으로 처리
        events, conns, nkeys = self.run_stream([[INIT, RT_INIT]], key_errors=3, max_fails=3)
        self.assertEqual([k for k, _ in events][-1], "중단")
        self.assertEqual(nkeys, 3)
        self.assertEqual(len(conns), 0)

    def test_pingpong_echoed(self):
        events, conns, _ = self.run_stream([[INIT, RT_INIT, PING]], max_fails=1)
        self.assertIn(PING, conns[0].sent)
        self.assertIn("0.5초 무응답", events[1][1])                    # 첫 메시지 뒤엔 SILENCE_S 기준

    def test_handler_error_does_not_reconnect(self):
        data = json.dumps({"header": {"action_code": "KA"}, "body": {}})

        async def bad(code, body):
            raise KeyError("last_pric")

        events, conns, _ = self.run_stream([[INIT, RT_INIT, data, data]], max_fails=1, handler=bad)
        kinds = [k for k, _ in events]
        self.assertEqual(kinds.count("처리오류"), 2)
        self.assertEqual(len(conns), 1)                                  # 처리 오류로는 다시 접속 안 함


    def test_rt_fail_resubscribes_on_same_connection(self):
        # 11/05 08:40 해외: 구독이 RT_FAIL(종목정보 없음)로 빠졌는데 pingpong 만 오면서 하루 종일 그대로였음
        # → 같은 연결에서 구독만 다시 (새 키·재접속 없이)
        fail = json.dumps({"code": "RT_FAIL", "data": {"action": "A", "action_code": "KA", "action_sym": "A756B000"},
                           "msg": "해당 종목에 대한 종목정보를 찾을 수 없습니다."})

        class ResubWS(FakeWS):
            async def send(self, raw):
                self.sent.append(raw)
                subs = [x for x in self.sent if '"action": "A"' in x]
                if '"action": "A"' in raw and len(subs) == 2:
                    self.script += [RT_INIT, PING]

        events, conns, nkeys = self.run_stream([[INIT, PING, fail]], max_fails=1, ws_cls=ResubWS, silence=2)
        self.assertEqual(len(conns), 1)
        self.assertEqual(nkeys, 1)
        self.assertEqual(sum('"action": "A"' in x for x in conns[0].sent), 2)
        notes = [n for k, n in events if k == "재구독"]
        self.assertTrue(any("성공" in n for n in notes), events)

if __name__ == "__main__":
    unittest.main()
