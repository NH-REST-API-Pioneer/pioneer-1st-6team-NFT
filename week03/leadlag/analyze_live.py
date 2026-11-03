"""실시간 기록(raw.jsonl) 초 단위 분석 — MKU 가 KRX 충격을 몇 초 뒤에, 얼마나 따라가는지.

사용법 (week03/leadlag/ 에서):
  python3 analyze_live.py                 # 오늘
  python3 analyze_live.py --day 20261103

시각은 두 연결 모두 이 맥이 받은 시각(recv_ts)을 쓴다 — 같은 시계라 지연 비교에 맞다.
두 연결 중 하나라도 30초 넘게 비면 그 구간은 빼고 본다.
결과: results/live/<day>/analysis.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from datetime import datetime
from pathlib import Path

from ws_trader import open_raw

HERE = Path(__file__).parent
TICK = 0.0001
FEE_TICKS = 2.0 / 2.5
GAP_S = 30
HORIZONS = (1, 3, 5, 10, 20, 30, 60, 120, 300)


def load(day: str):
    krx, mku = {}, {}
    last = {}
    bad: list[tuple[int, int]] = []
    for line in open_raw(day):
        r = json.loads(line)
        t = datetime.fromisoformat(r["recv_ts"])
        s = t.hour * 3600 + t.minute * 60 + t.second
        c = r["conn"]
        if c in last and s - last[c] > GAP_S:
            bad.append((last[c], s))
        last[c] = s
        m = r["msg"]
        h, b = m.get("header") or {}, m.get("body") or {}
        code = h.get("action_code")
        if c == "국내" and code == "KA" and b.get("tick_div") == "1" and float(b.get("exec_qty") or 0):
            krx[s] = float(b["last_pric"])
        elif c == "해외" and code == "FB":
            try:
                mku[s] = (float(b["bid_pric_1"]), float(b["ask_pric_1"]))
            except (KeyError, ValueError):
                pass
    return krx, mku, bad


def fill(d: dict, lo: int, hi: int) -> list:
    out, v = [], None
    for s in range(lo, hi + 1):
        v = d.get(s, v)
        out.append(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--lookback", type=int, default=45)
    args = ap.parse_args()
    krx, mku, bad = load(args.day)
    lo, hi = 9 * 3600, 15 * 3600 + 40 * 60
    k = fill(krx, lo, hi)
    q = fill(mku, lo, hi)
    ok = [True] * len(k)
    for a, b in bad:
        for s in range(max(a, lo), min(b + 300, hi) + 1):   # 공백 + 뒤 5분은 제외
            ok[s - lo] = False
    for i in range(len(k)):
        if k[i] is None or q[i] is None:
            ok[i] = False
    mid = [None if x is None else (x[0] + x[1]) / 2 for x in q]
    clean_s = sum(ok)
    print(f"{args.day} · 깨끗한 구간 {clean_s / 60:.0f}분 (09:00~15:40 중) · KRX 체결 {len(krx)}초 · MKU 호가 {len(mku)}초")

    # 1) 1초 수익률 교차상관: KRX 가 먼저면 lag>0 에서 음(-)의 상관 (KRX 상승 = MKU 하락)
    def r1(x, i):
        return x[i] - x[i - 1]
    xc = {}
    for lag in range(-5, 31):
        a, b = [], []
        for i in range(1 + max(0, -lag), len(k) - max(0, lag)):
            j = i + lag
            if ok[i] and ok[i - 1] and ok[j] and ok[j - 1]:
                a.append(r1(k, i))
                b.append(r1(mid, j))
        if len(a) > 100 and st.pstdev(a) and st.pstdev(b):
            xc[lag] = round(st.correlation(a, b), 3)
    print("\n1) KRX 1초 변화 vs MKU 중간가 1초 변화 상관 (lag 초, MKU 가 늦음 = 양수 lag)")
    print("   " + " ".join(f"{l}:{v:+.2f}" for l, v in xc.items() if l in (-2, -1, 0, 1, 2, 3, 5, 10, 20, 30)))

    # 2) 충격 사건 연구: lookback 초 안에 KRX 가 X원 이상 움직인 첫 순간 → 이후 MKU 가 KRX 방향으로 간 틱
    events = {}
    for thr in (0.3, 0.5, 0.7, 1.0):
        rows, last_ev = [], -10**9
        for i in range(args.lookback, len(k) - max(HORIZONS)):
            j = i - args.lookback
            if not (ok[i] and ok[j]) or i - last_ev < 60:
                continue
            dk = k[i] - k[j]
            if abs(dk) < thr:
                continue
            last_ev = i
            d = 1 if dk > 0 else -1
            exp = abs(dk) / (1000 * TICK / mid[i] ** 2)
            rho = -d * (mid[i] - mid[j]) / TICK / exp
            fwd = {}
            for h in HORIZONS:
                if not ok[i + h]:
                    break
                fwd[h] = -d * (mid[i + h] - mid[i]) / TICK          # 중간가 기준 따라간 틱
            # 실제 체결: 지금 반대 호가로 들어가 h 초 뒤 반대 호가로 나옴
            side = -d
            ent = q[i][1] if side > 0 else q[i][0]
            net = {}
            for h in fwd:
                ex = q[i + h][0] if side > 0 else q[i + h][1]
                net[h] = side * (ex - ent) / TICK - 2 * FEE_TICKS
            rows.append({"t": f"{(lo + i) // 3600:02d}:{(lo + i) % 3600 // 60:02d}:{(lo + i) % 60:02d}",
                         "dk": round(dk, 2), "exp": round(exp, 1), "rho": round(rho, 2), "fwd": fwd, "net": net})
        events[thr] = rows

    print(f"\n2) KRX 충격({args.lookback}초 안 X원) 뒤 MKU 가 KRX 방향으로 간 틱 (중간가) · 반영률<0.5 만")
    print("   기준  건수 | " + " ".join(f"{h:>5}s" for h in HORIZONS) + " | 순손익(호가·수수료 포함) 10s 60s 300s")
    summary = {}
    for thr, rows in events.items():
        sel = [r for r in rows if r["rho"] < 0.5]
        line, s_thr = [], {}
        for h in HORIZONS:
            v = [r["fwd"][h] for r in sel if h in r["fwd"]]
            m = st.mean(v) if v else float("nan")
            s_thr[f"fwd_{h}"] = round(m, 2)
            line.append(f"{m:+6.2f}")
        nets = []
        for h in (10, 60, 300):
            v = [r["net"][h] for r in sel if h in r["net"]]
            m = st.mean(v) if v else float("nan")
            s_thr[f"net_{h}"] = round(m, 2)
            nets.append(f"{m:+.1f}")
        exp_m = st.mean(r["exp"] for r in sel) if sel else float("nan")
        s_thr.update(n=len(sel), n_all=len(rows), exp_mean=round(exp_m, 2))
        summary[thr] = s_thr
        print(f"   {thr:.1f}원 {len(sel):>4} | " + " ".join(line) + f" |  {' '.join(nets)}  (기대 {exp_m:.1f}틱)")

    out = HERE / "results" / "live" / args.day / "analysis.json"
    out.write_text(json.dumps({"day": args.day, "clean_minutes": round(clean_s / 60), "xcorr": xc,
                               "shock": summary, "events": {str(k_): v for k_, v in events.items()}},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
