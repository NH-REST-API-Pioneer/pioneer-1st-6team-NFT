"""주문 규칙(order.json)에 손절·분할 진입/청산을 붙여 17~20일 재생으로 비교.

  .venv/bin/python diag/risk_replay.py                       # 학습 나흘
  .venv/bin/python diag/risk_replay.py 20261117,20261118     # 날 지정

진입 신호·θ·보유 15분·하루 한도 75만은 order.json 그대로. 바꾸는 건 청산·수량만.
가격은 1분 표의 미니 중간가 (실행기도 1분마다 봄). 진입·청산 모두 반 스프레드 + 수수료.
비교 기준은 1계약 환산 원 (2계약 변형은 손익 / 계약 수).

변형
  base            15분 시간 청산만 (지금 order.json)
  stop_pt X       중간가가 진입가보다 X pt 불리하면 청산
  stop_vol k      직전 30분 1분 중간가 변화 σ × √15 (= 15분 기대 변동폭) × k 만큼 불리하면 청산
  cut_m           m 분 지났을 때 손실 중이면 청산 (보유 시간 기준 손절)
  scale_out a     2계약 진입, +a × 15분 변동폭에서 1계약 먼저 청산, 나머지는 15분
  scale_in θ2     |z| ≥ θ2 면 2계약, 아니면 1계약 (신호 세기 분할 진입)
  add q mode      1계약 진입 뒤 보유 중 같은 방향 |z| ≥ q 가 되면 1계약 추가 (한 번만).
                  together = 첫 진입 15분에 같이 청산, own = 추가한 때부터 15분 뒤 청산
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze  # noqa: E402

RULE = json.loads((analyze.RULE_DIR / "order.json").read_text())
VOL_WIN = 30


def vol15(mid: pd.Series) -> pd.Series:
    """t 행까지의 직전 30분 1분 변화 σ × √15 (pt). 진입 시점에 아는 값만."""
    return mid.diff().rolling(VOL_WIN, min_periods=20).std() * np.sqrt(15)


def run(f: pd.DataFrame, stop=None, cut=None, scale_out=None, scale_in=None, add_on=None) -> list[dict]:
    r = RULE
    h, theta, sign, sd = int(r["h"]), r["theta"], r["sign"], r["sd"]
    limit = r["risk"]["daily_loss_krw"]
    m = analyze.fresh(f, h)
    z, mid, idx = f[r["signal"]] / sd, f["mid"], f.index
    v15 = vol15(mid)
    pos = {t: i for i, t in enumerate(idx)}
    spr = lambda tt: f.at[tt, "spread"] if np.isfinite(f.at[tt, "spread"]) else analyze.TICK_PT  # noqa: E731

    def leg(t_in, i_out, side, m0):
        mo = mid.iloc[i_out]
        return side * (mo - m0) * analyze.PT_KRW - (spr(t_in) + spr(idx[i_out])) / 2 * analyze.PT_KRW \
            - analyze.fee_krw(m0) - analyze.fee_krw(mo)

    trades, busy, cum = [], None, 0.0
    for t in idx[m]:
        if busy is not None and t < busy:
            continue
        if limit and cum <= -limit:
            break
        zt = z.get(t)
        if not np.isfinite(zt) or abs(zt) <= theta or not np.isfinite(f.at[t, f"r_{h}"]):
            continue
        side, i, m0 = int(np.sign(zt) * sign), pos[t], mid.iloc[pos[t]]
        vol = v15.iloc[i]
        if not np.isfinite(vol):
            vol = v15.dropna().median() if v15.notna().any() else 1.0
        n = 2 if (scale_out is not None or (scale_in is not None and abs(zt) >= scale_in)) else 1
        stop_pt = None
        if stop is not None:
            kind, x = stop
            stop_pt = x if kind == "pt" else x * vol
        target = scale_out * vol if scale_out is not None else None
        out, why, first = h, "time", None          # first = 먼저 판 1계약의 청산 분
        for j in range(1, h):
            mj = mid.iloc[i + j]
            if not np.isfinite(mj):
                continue
            mv = side * (mj - m0)
            if target is not None and first is None and mv >= target:
                first = j
            if stop_pt is not None and mv <= -stop_pt:
                out, why = j, "stop"
                break
            if cut is not None and j == cut and mv < 0:
                out, why = j, "cut"
                break
        krw = leg(t, i + out, side, m0) * (n if first is None else 1)
        if first is not None:
            krw += leg(t, i + first, side, m0)
            why += "+half"
        last = i + out
        if add_on is not None:                     # 보유 중 같은 방향으로 더 세지면 1계약 추가
            q, mode = add_on
            for j in range(1, out):
                tj = idx[i + j]
                zj = z.iloc[i + j]
                if not m[i + j] or not np.isfinite(zj) or abs(zj) < q or int(np.sign(zj) * sign) != side:
                    continue
                if mode == "own" and not np.isfinite(f[f"r_{h}"].iloc[i + j]):
                    continue
                ma = mid.iloc[i + j]
                ex = i + out if mode == "together" else i + j + h
                krw += leg(tj, ex, side, ma)
                n, why, last = 2, why + "+add", max(last, ex)
                break
        cum += krw
        trades.append({"day": t.strftime("%Y%m%d"), "t": t, "side": side, "z": float(zt), "n": n,
                       "exit_t": idx[last], "why": why, "krw": float(krw), "krw_per": float(krw / n),
                       "vol_pt": float(vol)})
        busy = idx[last]
    return trades


VARIANTS = [("base", {})]
VARIANTS += [(f"stop_pt {x:g}", {"stop": ("pt", x)}) for x in (1, 2, 3, 4)]
VARIANTS += [(f"stop_vol {k:g}", {"stop": ("vol", k)}) for k in (0.5, 1.0, 1.5, 2.0)]
VARIANTS += [(f"cut_{c}", {"cut": c}) for c in (5, 10)]
VARIANTS += [(f"scale_out {a:g}", {"scale_out": a}) for a in (0.5, 1.0)]
VARIANTS += [(f"scale_in {q:g}", {"scale_in": q}) for q in (1.0, 1.5)]
VARIANTS += [(f"add {q:g} {md}", {"add_on": (q, md)}) for q in (1.0, 1.5) for md in ("together", "own")]
# 조합: 손절은 첫 진입가 기준, 걸리면 추가 계약까지 전부 청산
VARIANTS += [(f"stop_vol {k:g} + add 1.5", {"stop": ("vol", k), "add_on": (1.5, "together")}) for k in (1.5, 2.0)]
VARIANTS += [("stop_vol 2 + scale_in 1.5", {"stop": ("vol", 2.0), "scale_in": 1.5})]


def summarize(name: str, tr: list[dict], days: list[str]) -> dict:
    d = pd.DataFrame(tr)
    row = {"variant": name, "trades": len(d)}
    if not len(d):
        return row
    contracts = d.n.sum()
    row.update({"contracts": int(contracts), "total_krw": round(d.krw.sum()),
                "per_contract_krw": round(d.krw.sum() / contracts),
                "worst_trade_per": round(d.krw_per.min()),
                "stops": int(d.why.str.startswith(("stop", "cut")).sum()),
                "halves": int(d.why.str.endswith("+half").sum())})
    cum = d.groupby("day").krw.apply(lambda s: s.cumsum().min())
    row["worst_intraday_krw"] = round(min(0.0, cum.min()))
    for day in days:
        row[day[-2:]] = round(d.loc[d.day == day, "krw"].sum())
    return row


def main() -> None:
    days = sys.argv[1].split(",") if len(sys.argv) > 1 else RULE["train_days"]
    F = {d: analyze.features(p) for d, p in analyze.load(days).items()}
    rows, all_tr = [], {}
    for name, kw in VARIANTS:
        tr = [x for f in F.values() for x in run(f, **kw)]
        all_tr[name] = tr
        rows.append(summarize(name, tr, days))
    T = pd.DataFrame(rows)
    out = analyze.RESULTS / f"risk_replay_{'_'.join(days)}"
    T.to_csv(out.with_suffix(".csv"), index=False)
    pd.DataFrame([dict(x, variant=k) for k, v in all_tr.items() for x in v]).to_csv(
        out.with_name(out.name + "_trades.csv"), index=False)
    v = pd.concat([vol15(f["mid"]) for f in F.values()]).dropna()
    print(f"15분 변동폭(직전 30분 σ×√15) 중앙값 {v.median():.2f}pt, 10~90% {v.quantile(.1):.2f}~{v.quantile(.9):.2f}pt")
    print(T.to_string(index=False))


if __name__ == "__main__":
    main()
