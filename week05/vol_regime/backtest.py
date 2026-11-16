"""백테스트 — 종목별로 engine.Engine 을 모든 봉에 돌려 거래 목록을 만듭니다.

체결: 신호 봉 다음 봉 시가. 비용(bp) = 왕복 1틱 + 마이크로 편도 $2 × 2.
  cost_bp = tick / 진입가 × 1e4 + 2 × FEE_SIDE / (진입가 × 포인트가치 / 마이크로 비율) × 1e4

  .venv/bin/python backtest.py        # 출처 4개 × 라벨 2개 × 시간대 2개, 학습·검증 요약만 출력

결과 (results/backtest/):
  ungated_<hours>_<brk|mr>.csv           국면과 상관없이 돌린 거래 + 출처·라벨별 진입 국면 열 regime_<src>_<label>
  switch_<src>_<label>_<hours>.csv       국면 전환 전략 (high→돌파, low→평균회귀)
  labels_<src>_<label>.csv               봉마다 국면 (무작위 섞기 검정용)
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

import config
import data
import engine
import regime
import session

SRCS = {"lgbm": "pred_lgbm", "persist": "pred_persist", "har": "pred_har", "oracle": "y"}
SRC_NAMES = list(SRCS) + ["clock"]   # clock = LightGBM 백분위의 같은 시각 직전 28일 평균 (시계만 쓴 비교 대상)
LABELS = {"three": regime.label, "two": regime.label_two}
HOURS = {"mock": True, "full": False}
OUT = config.RESULTS / "backtest"
COLS = ["sym", "strat", "side", "regime", "entry_bar", "entry_fill_ts", "entry_px", "exit_bar", "exit_fill_ts",
        "exit_px", "reason", "gross_bp", "cost_bp", "net_bp"]


def cost_bp(sym: str, px: float) -> float:
    tick, pv, micro = config.SPEC[sym]
    return tick / px * 1e4 + 2 * config.FEE_SIDE / (px * pv / micro) * 1e4


def _flags(index: pd.DatetimeIndex, mock_hours: bool) -> tuple[np.ndarray, np.ndarray]:
    return _flags_cached(tuple(index.asi8), mock_hours)


@lru_cache(maxsize=64)
def _flags_cached(ns: tuple, mock_hours: bool):
    idx = pd.DatetimeIndex(np.array(ns, dtype="datetime64[ns]"))
    bar = pd.Timedelta("5min")
    flat = np.array([session.must_flatten(t, mock_hours) for t in idx])
    can = np.array([session.entry_ok(t + bar, mock_hours) for t in idx])
    return flat, can


def run(bars: pd.DataFrame, regimes: pd.Series, mode: str, sym: str, mock_hours: bool = True) -> pd.DataFrame:
    eng = engine.Engine(mode, mock_hours)
    h, l, c, o = (bars[k].to_numpy() for k in ("h", "l", "c", "o"))
    bad = (bars["gap"] | bars["roll"]).to_numpy()
    lab = regimes.reindex(bars.index).fillna("none").to_numpy()
    flat, can = _flags(bars.index, mock_hours)
    idx = bars.index
    w = engine.WARM
    trades, open_ = [], None
    for i in range(w - 1, len(bars) - 1):
        s = slice(i - w + 1, i + 1)
        r = eng.step_arr(idx[i], h[s], l[s], c[s], bad[s], lab[i], bool(flat[i]), bool(can[i]))
        if r["act"] == "enter":
            open_ = {"sym": sym, "strat": r["strat"], "side": r["side"], "regime": r["regime"],
                     "entry_bar": idx[i], "entry_fill_ts": idx[i + 1], "entry_px": o[i + 1]}
        elif r["act"] == "exit":
            open_.update(exit_bar=idx[i], exit_fill_ts=idx[i + 1], exit_px=o[i + 1], reason=r["reason"])
            trades.append(open_)
            open_ = None
    if open_ is not None:   # 데이터 끝: 마지막 봉 종가로 정리
        open_.update(exit_bar=idx[-1], exit_fill_ts=idx[-1], exit_px=c[-1], reason="end")
        trades.append(open_)
    tr = pd.DataFrame(trades, columns=[k for k in COLS if k not in ("gross_bp", "cost_bp", "net_bp")])
    tr["gross_bp"] = tr["side"] * (tr["exit_px"] / tr["entry_px"] - 1) * 1e4
    tr["cost_bp"] = [cost_bp(sym, p) for p in tr["entry_px"]]
    tr["net_bp"] = tr["gross_bp"] - tr["cost_bp"]
    return tr[COLS]


def tag(trades: pd.DataFrame, labels: pd.Series, col: str = "regime") -> pd.DataFrame:
    """진입 봉의 국면으로 다시 표시 (labels: 그 종목 ts 인덱스)."""
    out = trades.copy()
    out[col] = labels.reindex(pd.DatetimeIndex(trades["entry_bar"])).fillna("none").to_numpy()
    return out


def all_labels(preds: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    """(출처, 라벨) → (sym, ts) 인덱스 국면."""
    pcts = {src: regime.for_all(preds[col]) for src, col in SRCS.items()}
    lg = pcts["lgbm"]
    pcts["clock"] = pd.concat([regime.clock_pct(g.droplevel("sym")).set_axis(g.index)
                               for _, g in lg.groupby(level="sym", sort=False)]).reindex(lg.index)
    return {(src, name): fn(pct) for src, pct in pcts.items() for name, fn in LABELS.items()}


def load_preds() -> pd.DataFrame:
    return pd.read_csv(config.MODELS_DIR / "preds.csv", parse_dates=["ts"]).set_index(["sym", "ts"])


def trainval(tr: pd.DataFrame) -> pd.DataFrame:
    return tr[pd.DatetimeIndex(tr["entry_bar"]) < pd.Timestamp(config.TEST_START)]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bars = {s: data.load_bars(s) for s in config.SYMS}
    labs = all_labels(load_preds())
    for (src, name), lab in labs.items():
        lab.rename("regime").reset_index().to_csv(OUT / f"labels_{src}_{name}.csv", index=False)
    for hours, mh in HOURS.items():
        for mode in ("brk", "mr"):
            parts = []
            for s in config.SYMS:
                tr = run(bars[s], pd.Series("none", index=bars[s].index), mode, s, mh)
                for (src, name), lab in labs.items():
                    tr = tag(tr, lab.loc[s], f"regime_{src}_{name}")
                parts.append(tr)
            ung = pd.concat(parts, ignore_index=True)
            ung.to_csv(OUT / f"ungated_{hours}_{mode}.csv", index=False)
            tv = trainval(ung)
            print(f"[{hours} {mode}] 학습·검증 거래 {len(tv)}건, 거래당 net {tv['net_bp'].mean():.2f}bp")
            print(tv.groupby("regime_lgbm_three")["net_bp"].agg(["count", "mean"]).round(2).to_string())
        for (src, name), lab in labs.items():
            parts = [run(bars[s], lab.loc[s], "switch", s, mh) for s in config.SYMS]
            sw = pd.concat(parts, ignore_index=True)
            sw.to_csv(OUT / f"switch_{src}_{name}_{hours}.csv", index=False)
            tv = trainval(sw)
            print(f"[{hours} switch {src} {name}] 학습·검증 거래 {len(tv)}건, 거래당 net {tv['net_bp'].mean():.2f}bp")


if __name__ == "__main__":
    main()
