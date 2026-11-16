"""판정 — 국면 × 전략 상호작용, 무작위 섞기 검정, 국면 출처 비교. 시험 구간은 한 번만.

시험 전에 미리 정한 판정 (week05-전략-근거.md 2절):
  주 판정 = LightGBM 국면 · 세 구간 라벨(상위 30%/하위 30%) · 모의 체결 시간 규칙. 나머지는 비교·변형.
  상호작용 = (돌파_고 − 돌파_저) − (평균회귀_고 − 평균회귀_저), 거래당 평균 net bp. 가설: > 0.
  무작위 섞기: 하루 단위 블록 — 거래의 국면을 j 거래일 앞 같은 시각의 국면으로 바꿈 (permute 설명).
  종목별 j(처음 정한 방식)와 모든 종목 공통 j(더 보수적) 두 가지 p 를 같이 보고. p = (1 + #{섞은 값 ≥ 실제}) / (1 + n),
  한쪽. 유의수준 0.05.

  .venv/bin/python verdict.py --period trainval            # 언제든
  .venv/bin/python verdict.py --period test --confirm      # 사용자 확인 후 한 번만 (잠금 파일)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import config
import model
from backtest import HOURS, LABELS, OUT as BT, SRC_NAMES, load_preds

VOUT = config.RESULTS / "verdict"
LOCK = VOUT / "TEST_RUN.lock"
PRIMARY = ("lgbm", "three", "mock")
_CODE = {"high": 1, "low": -1}


# ---------------------------------------------------------------- 구간
def period(tr: pd.DataFrame, which: str) -> pd.DataFrame:
    t = pd.DatetimeIndex(tr["entry_bar"])
    m = t < pd.Timestamp(config.TEST_START)
    return tr[m if which == "trainval" else ~m]


def period_labels(lab: pd.Series, which: str) -> pd.Series:
    m = lab.index < pd.Timestamp(config.TEST_START)
    return lab[m if which == "trainval" else ~m]


# ---------------------------------------------------------------- 통계
def _mean(x: pd.Series) -> float:
    return float(x.mean()) if len(x) else float("nan")


def table2x2(brk: pd.DataFrame, mr: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for name, tr in (("brk", brk), ("mr", mr)):
        row = {}
        for reg in ("high", "low"):
            sub = tr[tr["regime"] == reg]
            row[f"n_{reg}"] = len(sub)
            row[f"net_{reg}"] = _mean(sub["net_bp"])
            row[f"gross_{reg}"] = _mean(sub["gross_bp"])
        rows[name] = row
    return pd.DataFrame(rows).T


def interaction(t2: pd.DataFrame, kind: str = "net") -> float:
    return float((t2.loc["brk", f"{kind}_high"] - t2.loc["brk", f"{kind}_low"])
                 - (t2.loc["mr", f"{kind}_high"] - t2.loc["mr", f"{kind}_low"]))


def _slot(ts: pd.DatetimeIndex) -> np.ndarray:
    return (ts.hour * 60 + ts.minute) // 5


def permute(brk: pd.DataFrame, mr: pd.DataFrame, bar_labels: dict[str, pd.Series], n: int = 5000,
            seed: int = 0, common: bool = False) -> dict:
    """하루 단위 블록 섞기: 거래는 그대로 두고, 각 거래의 국면을 j 거래일 앞 같은 시각(HH:MM)의 국면으로
    바꿈 (원형). 시각이 유지되므로 「시간대 효과」는 귀무분포에도 남음 → 시계 이상의 정보만 검정.
    그 날 그 시각에 봉이 없거나 국면이 none 이면 그 거래는 그 회차에서 빠짐.
    common=False: 종목마다 j 를 따로 (처음 정한 방식). common=True: 모든 종목 같은 j (종목 간 상관 보존, 더 보수적).
    """
    import session

    rng = np.random.default_rng(seed)
    syms = list(bar_labels)
    days_all = sorted(set().union(*[set(session.session_day(l.index)) for l in bar_labels.values()]))
    dix = {d: i for i, d in enumerate(days_all)}
    D = len(days_all)
    grid = np.full((len(syms), D, 288), -9, int)          # -9 = 봉 없음/none
    for k, s in enumerate(syms):
        lab = bar_labels[s]
        lab = lab[lab != "none"]
        di = np.array([dix[d] for d in session.session_day(lab.index)], int)
        grid[k, di, _slot(lab.index)] = lab.map(_CODE).fillna(0).to_numpy(int)

    def prep(tr: pd.DataFrame):
        tr = tr[tr["sym"].isin(syms)]
        t = pd.DatetimeIndex(tr["entry_bar"])
        return (np.array([syms.index(x) for x in tr["sym"]], int),
                np.array([dix.get(d, -1) for d in session.session_day(t)], int), _slot(t),
                tr["net_bp"].to_numpy(float))

    bs, bd, bl, bn = prep(brk)
    ms, md, ml, mn = prep(mr)

    def stats(j: np.ndarray) -> tuple[float, float]:
        cb = np.where(bd >= 0, grid[bs, (bd - j[bs]) % D, bl], -9)
        cm = np.where(md >= 0, grid[ms, (md - j[ms]) % D, ml], -9)
        m = lambda v, msk: v[msk].mean() if msk.any() else np.nan
        inter = (m(bn, cb == 1) - m(bn, cb == -1)) - (m(mn, cm == 1) - m(mn, cm == -1))
        sw = np.concatenate([bn[cb == 1], mn[cm == -1]])
        return float(inter), float(sw.mean()) if len(sw) else np.nan

    obs, sw_obs = stats(np.zeros(len(syms), int))
    null_i, null_s = np.empty(n), np.empty(n)
    for i in range(n):
        j = np.full(len(syms), rng.integers(1, D)) if common else rng.integers(1, D, len(syms))
        null_i[i], null_s[i] = stats(j)
    ok_i, ok_s = null_i[~np.isnan(null_i)], null_s[~np.isnan(null_s)]
    pv = lambda null, o: float((1 + np.sum(null >= o)) / (1 + len(null)))
    return {"obs": obs, "p": pv(ok_i, obs), "switch_obs": sw_obs, "switch_p": pv(ok_s, sw_obs),
            "null_q05": float(np.quantile(ok_i, 0.05)), "null_q95": float(np.quantile(ok_i, 0.95)),
            "n_brk": int((bd >= 0).sum()), "n_mr": int((md >= 0).sum())}


# ---------------------------------------------------------------- 잠금
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def acquire_test_lock(lock: Path = LOCK) -> None:
    if lock.exists():
        raise SystemExit(f"시험 구간은 이미 한 번 돌렸습니다 ({lock}). 다시 돌리지 않습니다.\n{lock.read_text()}")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              cwd=config.HERE).stdout.strip()
    except OSError:
        head = ""
    lock.write_text(json.dumps({
        "started": datetime.now().isoformat(timespec="seconds"), "git_head": head,
        "config_sha256": _sha(config.HERE / "config.py"),
        "model_sha256": _sha(config.MODELS_DIR / "vol_lgbm.txt"),
        "engine_sha256": _sha(config.HERE / "engine.py"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 실행
def _labels(src: str, name: str, which: str) -> dict[str, pd.Series]:
    d = pd.read_csv(BT / f"labels_{src}_{name}.csv", parse_dates=["ts"])
    return {s: period_labels(g.set_index("ts")["regime"], which) for s, g in d.groupby("sym")}


def evaluate(which: str, n: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows, per_sym = [], []
    for hours in HOURS:
        ub = pd.read_csv(BT / f"ungated_{hours}_brk.csv", parse_dates=["entry_bar"])
        um = pd.read_csv(BT / f"ungated_{hours}_mr.csv", parse_dates=["entry_bar"])
        ub, um = period(ub, which), period(um, which)
        for src in SRC_NAMES:
            for name in LABELS:
                col = f"regime_{src}_{name}"
                brk, mr = ub.assign(regime=ub[col]), um.assign(regime=um[col])
                t2 = table2x2(brk, mr)
                labs = _labels(src, name, which)
                perm = permute(brk, mr, labs, n=n)
                perm_c = permute(brk, mr, labs, n=n, common=True)
                sw = period(pd.read_csv(BT / f"switch_{src}_{name}_{hours}.csv", parse_dates=["entry_bar"]), which)
                rows.append({"src": src, "label": name, "hours": hours,
                             "primary": (src, name, hours) == PRIMARY,
                             **{f"brk_{k}": v for k, v in t2.loc["brk"].items()},
                             **{f"mr_{k}": v for k, v in t2.loc["mr"].items()},
                             "inter_net": interaction(t2, "net"), "inter_gross": interaction(t2, "gross"),
                             "perm_p": perm["p"], "null_q05": perm["null_q05"], "null_q95": perm["null_q95"],
                             "perm_p_common": perm_c["p"], "null_c_q05": perm_c["null_q05"],
                             "null_c_q95": perm_c["null_q95"],
                             "switch_sel_net": perm["switch_obs"], "switch_sel_p": perm["switch_p"],
                             "switch_bt_n": len(sw), "switch_bt_net": _mean(sw["net_bp"]),
                             "brk_all_net": _mean(brk["net_bp"]), "mr_all_net": _mean(mr["net_bp"])})
                for s in config.SYMS:
                    t2s = table2x2(brk[brk["sym"] == s], mr[mr["sym"] == s])
                    per_sym.append({"src": src, "label": name, "hours": hours, "sym": s,
                                    "asset": config.ASSET[s], "inter_net": interaction(t2s, "net"),
                                    "n_brk": int(t2s.loc["brk", ["n_high", "n_low"]].sum()),
                                    "n_mr": int(t2s.loc["mr", ["n_high", "n_low"]].sum())})
    preds = load_preds()
    t = preds.index.get_level_values("ts")
    pm = preds[(t < pd.Timestamp(config.TEST_START)) if which == "trainval" else (t >= pd.Timestamp(config.TEST_START))]
    pm = pm[pm["y"].notna()]
    mrows = [{"model": c[5:], **model.metrics(pm["y"], pm[c])} for c in ("pred_lgbm", "pred_persist", "pred_har")]
    return pd.DataFrame(rows), pd.DataFrame(per_sym), pd.DataFrame(mrows)


def _fmt(x) -> str:
    return "" if pd.isna(x) else (f"{x:.3f}" if isinstance(x, float) else str(x))


def write_summary(which: str, res: pd.DataFrame, per_sym: pd.DataFrame, mets: pd.DataFrame, out: Path) -> None:
    p = res[res["primary"]].iloc[0]
    ps = per_sym[(per_sym["src"] == PRIMARY[0]) & (per_sym["label"] == PRIMARY[1]) & (per_sym["hours"] == PRIMARY[2])]
    lines = [f"# 판정 요약 — {which}", "",
             f"생성 {datetime.now().isoformat(timespec='seconds')} · 구간 "
             + ("학습·검증 (~09-30)" if which == "trainval" else f"시험 ({config.TEST_START}~)"), "",
             "## 1. 예측력 (로그 MSE · QLIKE)", "",
             "| 모델 | n | MSE | QLIKE |", "| --- | --- | --- | --- |"]
    lines += [f"| {r.model} | {r.n} | {r.mse:.4f} | {r.qlike:.4f} |" for r in mets.itertuples()]
    lines += ["", "## 2. 주 판정 (LightGBM · 상위30%/하위30% · 모의 시간 규칙)", "",
              "| | 고변동 n | 고변동 net bp | 저변동 n | 저변동 net bp |", "| --- | --- | --- | --- | --- |",
              f"| 돌파 | {int(p.brk_n_high)} | {p.brk_net_high:.2f} | {int(p.brk_n_low)} | {p.brk_net_low:.2f} |",
              f"| 평균회귀 | {int(p.mr_n_high)} | {p.mr_net_high:.2f} | {int(p.mr_n_low)} | {p.mr_net_low:.2f} |", "",
              f"- 상호작용 (net) **{p.inter_net:.2f} bp**, 하루 단위 섞기 p = **{p.perm_p:.4f}** (종목별 j, 처음 정한 방식) · "
              f"**{p.perm_p_common:.4f}** (모든 종목 같은 j). 섞은 분포 5~95%: {p.null_q05:.2f} ~ {p.null_q95:.2f} · "
              f"{p.null_c_q05:.2f} ~ {p.null_c_q95:.2f}. 비용 전(gross) {p.inter_gross:.2f} bp.",
              f"- 국면대로 고른 거래(돌파∩고 + 평균회귀∩저) 거래당 {p.switch_sel_net:.2f} bp, 무작위 전환 대비 p = {p.switch_sel_p:.4f}.",
              f"- 국면 전환 전략 실제 백테스트: {int(p.switch_bt_n)}건, 거래당 {p.switch_bt_net:.2f} bp "
              f"(돌파만 {p.brk_all_net:.2f}, 평균회귀만 {p.mr_all_net:.2f}).",
              f"- 판정 (가설: 상호작용 > 0, α=0.05, 종목별 j 기준): **{'유의' if p.perm_p < 0.05 else '유의하지 않음'}**"
              + ("" if (p.perm_p < 0.05) == (p.perm_p_common < 0.05) else " — 공통 j 로는 판정이 달라짐, 같이 보고"), "",
              "## 3. 종목별 상호작용 (주 판정 설정)", "", "| 종목 | 자산군 | 상호작용 net bp | 돌파 n | 평균회귀 n |",
              "| --- | --- | --- | --- | --- |"]
    lines += [f"| {r.sym} | {r.asset} | {_fmt(r.inter_net)} | {r.n_brk} | {r.n_mr} |" for r in ps.itertuples()]
    lines += ["", f"부호 + 종목 {int((ps['inter_net'] > 0).sum())} / {ps['inter_net'].notna().sum()}", "",
              "## 4. 국면 출처 · 라벨 · 시간대 전체", "",
              "| 출처 | 라벨 | 시간 | 상호작용 net | gross | p | p(공통) | 국면선택 net | p | 전환 백테스트 net |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {r.src} | {r.label} | {r.hours} | {r.inter_net:.2f} | {r.inter_gross:.2f} | {r.perm_p:.4f} | "
              f"{r.perm_p_common:.4f} | {r.switch_sel_net:.2f} | {r.switch_sel_p:.4f} | {r.switch_bt_net:.2f} |"
              for r in res.itertuples()]
    lines += ["", "oracle = 사후 실제 변동성으로 나눈 국면 (거래 불가, 상한선). persist·har = 단순 기준선 국면. "
              "clock = LightGBM 백분위의 같은 시각 직전 28일 평균 (시간대만 쓴 국면).", ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["trainval", "test"], required=True)
    ap.add_argument("--confirm", action="store_true", help="시험 구간: 사용자 확인을 받았을 때만")
    ap.add_argument("--n", type=int, default=5000)
    args = ap.parse_args()
    if args.period == "test":
        if not args.confirm:
            raise SystemExit("시험 구간은 사용자 확인 후 --confirm 으로 한 번만 돌립니다.")
        acquire_test_lock()
    out = VOUT / args.period
    out.mkdir(parents=True, exist_ok=True)
    res, per_sym, mets = evaluate(args.period, args.n)
    res.to_csv(out / "results.csv", index=False)
    per_sym.to_csv(out / "per_symbol.csv", index=False)
    mets.to_csv(out / "model_metrics.csv", index=False)
    write_summary(args.period, res, per_sym, mets, out)
    print((out / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
