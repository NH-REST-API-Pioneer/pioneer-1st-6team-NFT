"""외국인 흐름 → 미니코스피200 다음 몇 분 수익률. 학습일로 규칙을 고르고, 시험일에서 그대로 판정.

  .venv/bin/python analyze.py --train 20261117                              # 화요일 밤: 규칙 고르기 → results/rules.json
  .venv/bin/python analyze.py --train 20261117 --test 20261118,20261119,20261120   # 금요일: 표본 밖 판정

두 번째 규칙 (화요일 결과를 보고 추가, 2026-11-17 밤 고정) — 조합, 매일 밤 갱신:
  .venv/bin/python analyze.py --combo-train 20261117 --for 20261118                 # 수요일용
  .venv/bin/python analyze.py --combo-train 20261117,20261118 --for 20261119        # 목요일용 (수요일 밤)
  .venv/bin/python analyze.py --combo-train 20261117,20261118,20261119 --for 20261120
  .venv/bin/python analyze.py --combo-test 20261118,20261119,20261120               # 금요일: 날마다 그날용 규칙으로 판정
  신호 = 외국인 5분 z − 개인 5분 z (COMBO_PARTS, 고정), 보유 5분, 방향 +1 (고정).
  밤마다 바뀌는 것: 두 흐름의 sd, 조합 sd, θ (학습 거래 MIN_TRADES 이상 중 비용 후 평균 1등). 그 밖엔 안 바꿈.

신호 (모두 t 행까지 받은 값만, convert.py 의 시각 규칙):
  frgn_fut_k   직전 k 분 외국인 선물 순매수 변화 (코스피200 계약 + 미니/5 → 코스피200 계약 환산)
  frgn_opt_k   직전 k 분 외국인 (콜 순매수 − 풋 순매수) 변화
  ind_fut_k    직전 k 분 개인 선물 순매수 변화 (반대 방향 신호인지 보려고)
  aggr_k       직전 k 분 매수주도 − 매도주도 체결 (코스피200 + 미니/5) — 투자자 구분 없는 비교 신호
  각 신호는 학습일의 표준편차로 나눠 z 로 씀 (시험일에도 학습일 값 그대로).
목표: 미니 중간가 로그수익률 h 분 뒤 (bp). 진입 가능 시각 09:05~(15:35 − h) — 시가 동시호가 직후와 종가 단일가 전 구간 제외.
회귀: r_h = a + b·z, Newey-West 표준오차 (lag = h, numpy 로 직접 계산).
규칙: 학습일에서 |t| 가 가장 큰 (신호, k, h) 하나 + 문턱 θ (학습일 비용 후 평균 1등). 방향 = 학습 β 부호.
       |z| > θ 이면 그 방향으로 1계약, h 분 보유, 보유 중 새 진입 없음.
비용: 왕복 = 진입 시점 스프레드(시장가로 사고팔 때 반씩 두 번) + 수수료 2 × FEE_KRW. 미니 1pt = 50,000원.
늦은 스냅샷: 서버 시각(tm)이 행 시각보다 STALE_S 초 넘게 늦은 분은 회귀·규칙 모두에서 뺌 (실행기도 그런 분엔 진입 안 함).
"""
from __future__ import annotations

import argparse
import json
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from convert import PANEL_DIR
from record import HERE

RESULTS = HERE / "results"
KS = (1, 5, 15)
HS = (5, 15, 30)
THETAS = (0.5, 1.0, 1.5, 2.0, 3.0)
MIN_TRADES = 10          # θ 는 학습일 거래가 이만큼 이상인 것 중에서만 고름 (1건짜리 운 배제)
TICK_PT, PT_KRW = 0.02, 50_000
FEE_KRW = 1_500          # 미니 1계약 편도 수수료 가정 — 첫 모의 체결의 예수금 변화로 확인해서 고칠 것
START, LAST_EXIT = "09:05", "15:35"


def load(days: list[str]) -> dict[str, pd.DataFrame]:
    return {d: pd.read_csv(PANEL_DIR / f"{d}.csv", index_col="t", parse_dates=True) for d in days}


STALE_S = 120            # 투자자 스냅샷 서버 시각이 행 시각보다 이만큼 넘게 늦으면 그 분 흐름은 버림 (NH 504 등)


def stale(p: pd.DataFrame, prd: str) -> pd.Series:
    """{prd}_frgn_tm (HHMMSS) 이 t 보다 STALE_S 초 넘게 늦은 행. 열이 없으면 전부 False."""
    c = f"{prd}_frgn_tm"
    if c not in p:
        return pd.Series(False, index=p.index)
    s = pd.to_numeric(p[c], errors="coerce")
    day = p.index.normalize()
    hh, mm, ss = s // 10000, (s // 100) % 100, s % 100
    tm = day + pd.to_timedelta((hh * 3600 + mm * 60 + ss).values, unit="s")
    age = (p.index - tm).total_seconds()
    return pd.Series(~np.isfinite(age) | (age > STALE_S), index=p.index)


def features(p: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=p.index)
    bad = {prd: stale(p, prd) for prd in ("011", "051", "012", "013")}
    zero = pd.Series(0.0, index=p.index)
    g = lambda c: p[c].where(~bad[c[:3]]) if c in p else zero   # noqa: E731  — 늦은 스냅샷은 NaN → diff 도 NaN
    fut = g("011_frgn") + g("051_frgn") / 5
    opt = g("012_frgn") - g("013_frgn")
    ind = g("011_ind") + g("051_ind") / 5
    f["stale"] = bad["011"] | bad["051"]
    aggr = p.get("K200_aggr", zero) + p.get("MINI_aggr", zero) / 5
    for k in KS:
        f[f"frgn_fut_{k}"] = fut.diff(k)
        f[f"frgn_opt_{k}"] = opt.diff(k)
        f[f"ind_fut_{k}"] = ind.diff(k)
        f[f"aggr_{k}"] = aggr.rolling(k).sum()
    mid = p["MINI_mid"].where(p["MINI_spread"] > 0)
    for h in HS:
        f[f"r_{h}"] = (np.log(mid.shift(-h) / mid)) * 1e4
    f["mid"] = mid
    f["spread"] = p["MINI_spread"].where(p["MINI_spread"] > 0)
    return f


def usable(idx: pd.DatetimeIndex, h: int) -> np.ndarray:
    hm = idx.strftime("%H:%M")
    last = (pd.Timestamp("2000-01-01 " + LAST_EXIT) - pd.Timedelta(minutes=h)).strftime("%H:%M")
    return (hm >= START) & (hm <= last)


def fresh(f: pd.DataFrame, h: int) -> np.ndarray:
    """진입 가능 시각 중 투자자 스냅샷이 늦지 않은 분 (모든 신호를 같은 표본에서 비교)."""
    m = usable(f.index, h)
    return m & ~f["stale"].to_numpy(bool) if "stale" in f else m


def stack(F: dict[str, pd.DataFrame], sig: str, h: int) -> pd.DataFrame:
    parts = []
    for d, f in F.items():
        m = fresh(f, h)
        parts.append(f.loc[m, [sig, f"r_{h}"]].assign(day=d))
    return pd.concat(parts).dropna()


def nw_ols(x: pd.Series, y: pd.Series, lag: int) -> dict:
    """OLS y = a + b x, Newey-West(Bartlett) 표준오차. (statsmodels 는 이 맥에서 scipy 바이너리가 안 올라와서 직접 씀)"""
    X = np.column_stack([np.ones(len(x)), np.asarray(x, float)])
    yv = np.asarray(y, float)
    XtXi = np.linalg.inv(X.T @ X)
    beta = XtXi @ X.T @ yv
    e = yv - X @ beta
    u = X * e[:, None]
    S = u.T @ u
    for l in range(1, lag + 1):
        w = 1 - l / (lag + 1)
        G = u[l:].T @ u[:-l]
        S += w * (G + G.T)
    V = XtXi @ S @ XtXi
    r2 = 1 - e.var() / yv.var() if yv.var() > 0 else 0.0
    return {"b": float(beta[1]), "t": float(beta[1] / np.sqrt(V[1, 1])), "r2": float(r2), "n": int(len(yv))}


def cost_bp(mid: float, spread: float = TICK_PT) -> float:
    """왕복 = 진입 반 스프레드 + 청산 반 스프레드(진입 때 스프레드로 근사) + 수수료 2번."""
    if not np.isfinite(spread):
        spread = TICK_PT
    rt_pt = spread + 2 * FEE_KRW / PT_KRW
    return rt_pt / mid * 1e4


def simulate(f: pd.DataFrame, sig: str, h: int, theta: float, sign: float, sd: float) -> list[dict]:
    """하루 안에서 규칙대로 진입·청산. 보유 중엔 새 진입 없음."""
    m = fresh(f, h)
    z = f[sig] / sd
    trades, busy_until = [], None
    for t in f.index[m]:
        if busy_until is not None and t < busy_until:
            continue
        zt, r, mid = z.get(t), f.at[t, f"r_{h}"], f.at[t, "mid"]
        if not np.isfinite(zt) or not np.isfinite(r) or abs(zt) <= theta:
            continue
        side = np.sign(zt) * sign
        trades.append({"t": t, "side": int(side), "z": float(zt), "gross_bp": float(side * r),
                       "net_bp": float(side * r - cost_bp(mid, f.at[t, "spread"] if "spread" in f else TICK_PT))})
        busy_until = t + pd.Timedelta(minutes=h)
    return trades



# ---------------------------------------------------------------- 두 번째 규칙: 조합 (매일 갱신)
COMBO_PARTS = (("frgn_fut_5", 1.0), ("ind_fut_5", -1.0))   # 외국인 z − 개인 z. 고정
COMBO_SIGNAL, COMBO_H, COMBO_SIGN = "combo_5", 5, 1.0
COMBO_DIR = RESULTS / "rules_combo"
COMBO_CURRENT = RESULTS / "rules_combo.json"            # 실행기가 주문에 쓰는 것 = 가장 최근 for_ 파일


def add_signal(f: pd.DataFrame, rule: dict) -> pd.DataFrame:
    """rule 에 parts 가 있으면 조합 신호 열(rule['signal'])을 만듦. filter 가 있으면 국면이 아닌 분의 신호를 NaN 으로."""
    if "parts" in rule:
        f[rule["signal"]] = sum(q["w"] * f[q["signal"]] / q["sd"] for q in rule["parts"])
    if "filter" in rule:
        ok = regime_ok(f, rule["filter"])
        f[rule["signal"]] = f[rule["signal"]].where(ok)
    return f


# ---------------------------------------------------------------- 세 번째 규칙: 국면 필터 (11/19 밤 고정, 금요일 시험)
REGIME_FILE = RESULTS / "rules_regime.json"
REGIME_FILTER = {"signal": "frgn_fut_5", "lag": 5, "window": 60, "min_periods": 50, "rho_min": 0.0}
REGIME_THETA = 0.5


def flow_rho(f: pd.DataFrame, flt: dict) -> pd.Series:
    """직전 window 분 안에서 (5분 흐름, lag 분 전 5분 흐름) 상관. t 행은 t 까지 값만 씀."""
    x = f[flt["signal"]]
    return x.rolling(flt["window"], min_periods=flt["min_periods"]).corr(x.shift(flt["lag"]))


def regime_ok(f: pd.DataFrame, flt: dict) -> pd.Series:
    return (flow_rho(f, flt) > flt["rho_min"]).fillna(False)


def regime_freeze(days: list[str], for_day: str) -> None:
    """조합 sd 는 학습일로, 필터·θ 는 위 상수 (diag/regime_search.py 에서 학습일 보고 고른 것). 한 번만 씀."""
    if REGIME_FILE.exists():
        raise SystemExit(f"{REGIME_FILE.name} 이 이미 있음 (고정된 규칙)")
    F = {d: features(p) for d, p in load(days).items()}
    rule, _, _ = fit_combo(F)
    rule = {k: rule[k] for k in ("signal", "parts", "h", "sign", "sd")}
    rule.update(theta=REGIME_THETA, filter=REGIME_FILTER, train_days=sorted(days), for_day=for_day, fee_krw=FEE_KRW,
                chosen_at=pd.Timestamp.now().isoformat(timespec="seconds"),
                note="학습일 17~19 안에서 W·ρmin·θ 30여 조합 중 1등 (80건 +3.70bp, 무작위 원형 이동 대비 p≈0.10). 시험: for_day 하루")
    tr = [x for d in days for x in simulate(add_signal(F[d], rule), rule["signal"], rule["h"], rule["theta"], rule["sign"], rule["sd"])]
    rule.update(train_trades=len(tr), train_net_bp_mean=float(np.mean([x["net_bp"] for x in tr])))
    REGIME_FILE.write_text(json.dumps(rule, indent=2, ensure_ascii=False))
    print(json.dumps(rule, indent=2, ensure_ascii=False))


def regime_test(days: list[str]) -> None:
    """국면 규칙을 시험일에 — 같은 sd·θ 로 필터 없는 것과 나란히."""
    rule = json.loads(REGIME_FILE.read_text())
    if max(rule["train_days"]) >= min(days):
        raise SystemExit("시험일이 학습일보다 뒤여야 함")
    bare = {k: v for k, v in rule.items() if k != "filter"}
    lines = [f"# 국면 필터 규칙 표본 밖 판정 — {','.join(days)}", "",
             "| 규칙 | 거래 | 비용 전 bp | 비용 후 bp | t | 승률 |", "| --- | --- | --- | --- | --- | --- |"]
    out = {}
    for name, r in (("필터 (주문)", rule), ("필터 없음 (같은 sd·θ)", bare)):
        tr = [dict(x, day=d) for d in days
              for x in simulate(add_signal(features(load([d])[d]), r), r["signal"], r["h"], r["theta"], r["sign"], r["sd"])]
        net = np.array([x["net_bp"] for x in tr])
        t = net.mean() / net.std(ddof=1) * np.sqrt(len(net)) if len(net) > 1 and net.std() > 0 else np.nan
        g = np.mean([x["gross_bp"] for x in tr]) if tr else np.nan
        lines.append(f"| {name} | {len(net)} | {g:+.2f} | {net.mean() if len(net) else np.nan:+.2f} | {t:+.2f} | "
                     f"{(net > 0).mean() * 100 if len(net) else np.nan:.0f}% |")
        out[name] = tr
    pd.DataFrame(out["필터 (주문)"]).to_csv(RESULTS / "test_trades_regime.csv", index=False)
    lines += ["", "판정 기준 (11/19 밤 미리 정함): 필터 규칙 비용 후 평균 > 0 AND 필터 없는 것보다 큼. 하루라 강한 근거는 못 됨."]
    (RESULTS / f"analysis_regime_test_{'_'.join(days)}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def fit_combo(F: dict[str, pd.DataFrame]) -> tuple[dict, list, dict]:
    parts = []
    for sig, w in COMBO_PARTS:
        D = stack(F, sig, COMBO_H)
        parts.append({"signal": sig, "w": w, "sd": float(D[sig].std())})
    rule = {"signal": COMBO_SIGNAL, "parts": parts, "h": COMBO_H, "sign": COMBO_SIGN}
    for f in F.values():
        add_signal(f, rule)
    D = stack(F, COMBO_SIGNAL, COMBO_H)
    rule["sd"] = float(D[COMBO_SIGNAL].std())
    reg = nw_ols(D[COMBO_SIGNAL] / rule["sd"], D[f"r_{COMBO_H}"], COMBO_H)
    scores = []
    for th in THETAS:
        tr = [x for f in F.values() for x in simulate(f, COMBO_SIGNAL, COMBO_H, th, COMBO_SIGN, rule["sd"])]
        net = [x["net_bp"] for x in tr]
        scores.append((float(np.mean(net)) if len(tr) >= MIN_TRADES else -np.inf, th, len(tr)))
    m, theta, n = max(scores)
    if not np.isfinite(m):
        raise SystemExit(f"θ 후보 모두 학습 거래 {MIN_TRADES}건 미만")
    rule.update(theta=theta, train_days=sorted(F), train_b=reg["b"], train_t=reg["t"], train_net_bp_mean=m,
                train_trades=n, fee_krw=FEE_KRW)
    return rule, scores, reg


def combo_train(days: list[str], for_day: str, force: bool) -> None:
    if for_day <= max(days):
        raise SystemExit(f"--for {for_day} 는 학습일({max(days)}) 뒤여야 함")
    out = COMBO_DIR / f"for_{for_day}.json"
    if out.exists() and not force:
        raise SystemExit(f"{out.name} 이 이미 있음 (그날 규칙은 한 번만 정함). 덮어쓰려면 --force")
    F = {d: features(p) for d, p in load(days).items()}
    rule, scores, reg = fit_combo(F)
    rule.update(for_day=for_day, chosen_at=pd.Timestamp.now().isoformat(timespec="seconds"))
    COMBO_DIR.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(rule, indent=2, ensure_ascii=False)
    out.write_text(txt)
    COMBO_CURRENT.write_text(txt)
    lines = [f"# 조합 규칙 — 학습 {','.join(days)} → {for_day} 매매용", "",
             f"조합 회귀 (5분): b {reg['b']:+.2f} bp/표준편차, t {reg['t']:+.2f}, R² {reg['r2']:.4f}, n {reg['n']}", "",
             "| θ | 거래 수 | 비용 후 평균 bp |", "| --- | --- | --- |"]
    lines += [f"| {th} | {k} | {mm:+.2f} |" for mm, th, k in scores]
    lines += ["", f"→ {txt}"]
    (RESULTS / f"analysis_combo_for_{for_day}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def combo_test(days: list[str]) -> None:
    """각 시험일을 그날용(for_날짜) 규칙으로만 판정. 학습에 그날 이후 데이터는 안 들어감."""
    zs, rs, trades, lines = [], [], [], [f"# 조합 규칙 표본 밖 판정 — {','.join(days)}", "",
                                         "| 날 | 규칙 학습일 | θ | 거래 | 비용 후 평균 bp |", "| --- | --- | --- | --- | --- |"]
    for d in days:
        rule = json.loads((COMBO_DIR / f"for_{d}.json").read_text())
        if max(rule["train_days"]) >= d:
            raise SystemExit(f"{d} 규칙에 그날 이후 학습일이 섞임")
        f = add_signal(features(load([d])[d]), rule)
        D = stack({d: f}, rule["signal"], rule["h"])
        zs.append(D[rule["signal"]] / rule["sd"])
        rs.append(D[f"r_{rule['h']}"])
        tr = [dict(x, day=d) for x in simulate(f, rule["signal"], rule["h"], rule["theta"], rule["sign"], rule["sd"])]
        trades += tr
        mean = np.mean([x["net_bp"] for x in tr]) if tr else float("nan")
        lines.append(f"| {d} | {','.join(rule['train_days'])} | {rule['theta']} | {len(tr)} | {mean:+.2f} |")
    res = nw_ols(pd.concat(zs), pd.concat(rs), COMBO_H)
    net = np.array([x["net_bp"] for x in trades])
    tstat = net.mean() / net.std(ddof=1) * np.sqrt(len(net)) if len(net) > 1 and net.std() > 0 else np.nan
    lines += ["", f"회귀 (날마다 그날 규칙의 sd 로 z): b {res['b']:+.2f} bp, t {res['t']:+.2f}, n {res['n']} (기대 부호 +)",
              f"규칙: 거래 {len(net)}건, 비용 전 평균 {np.mean([x['gross_bp'] for x in trades]) if trades else float('nan'):+.2f} bp, "
              f"비용 후 평균 {net.mean() if len(net) else float('nan'):+.2f} bp, t {tstat:+.2f}, "
              f"승률 {(net > 0).mean() * 100 if len(net) else float('nan'):.0f}%"]
    pd.DataFrame(trades).to_csv(RESULTS / "test_trades_combo.csv", index=False)
    (RESULTS / f"analysis_combo_test_{'_'.join(days)}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="")
    ap.add_argument("--test", default="")
    ap.add_argument("--combo-train", default="", help="조합 규칙 학습일 (쉼표)")
    ap.add_argument("--for", dest="for_day", default="", help="조합 규칙을 쓸 매매일")
    ap.add_argument("--combo-test", default="", help="조합 규칙 시험일 (쉼표)")
    ap.add_argument("--force", action="store_true", help="이미 있는 규칙 파일을 덮어씀 (고정 뒤엔 쓰지 말 것)")
    ap.add_argument("--regime-freeze", default="", help="국면 필터 규칙 고정: 학습일 (쉼표), --for 매매일")
    ap.add_argument("--regime-test", default="", help="국면 필터 규칙 시험일 (쉼표)")
    a = ap.parse_args()
    if a.regime_freeze:
        if not a.for_day:
            raise SystemExit("--regime-freeze 에는 --for 매매일이 필요")
        return regime_freeze(a.regime_freeze.split(","), a.for_day)
    if a.regime_test:
        return regime_test(a.regime_test.split(","))
    if a.combo_train:
        if not a.for_day:
            raise SystemExit("--combo-train 에는 --for 매매일이 필요")
        return combo_train(a.combo_train.split(","), a.for_day, a.force)
    if a.combo_test:
        return combo_test(a.combo_test.split(","))
    if not a.train:
        raise SystemExit("--train, --combo-train, --combo-test 중 하나")
    return main_single(a)


def main_single(a) -> None:
    tr_days = a.train.split(",")
    te_days = [d for d in a.test.split(",") if d]
    Ftr = {d: features(p) for d, p in load(tr_days).items()}
    sigs = [c for c in next(iter(Ftr.values())).columns if not c.startswith("r_") and c not in ("mid", "stale", "spread")]
    RESULTS.mkdir(exist_ok=True)
    lines = [f"# 외국인 흐름 분석 — 학습 {','.join(tr_days)}" + (f" · 시험 {','.join(te_days)}" if te_days else ""), ""]

    # 1) 학습일 회귀표
    rows, sd = [], {}
    for sig, h in product(sigs, HS):
        D = stack(Ftr, sig, h)
        if len(D) < 30 or D[sig].std() == 0:
            continue
        sd[(sig, h)] = float(D[sig].std())     # 보유 길이마다 표본이 달라서 (신호, h) 별로 따로 잼
        res = nw_ols(D[sig] / sd[(sig, h)], D[f"r_{h}"], h)
        rows.append({"signal": sig, "h": h, **res})
    R = pd.DataFrame(rows).sort_values("t", key=np.abs, ascending=False)
    lines += ["## 학습일 회귀 (r_h bp = a + b·z, Newey-West t)", "", "| 신호 | h분 | b (bp/표준편차) | t | R² | n |",
              "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {r.signal} | {r.h} | {r.b:+.2f} | {r.t:+.2f} | {r.r2:.4f} | {r.n} |" for r in R.itertuples()]

    # 2) 규칙 고르기 (학습일만)
    best = R.iloc[0]
    sign = float(np.sign(best.b))
    scores = []
    for th in THETAS:
        tr = [x for f in Ftr.values() for x in simulate(f, best.signal, int(best.h), th, sign, sd[(best.signal, int(best.h))])]
        mean = np.mean([x["net_bp"] for x in tr]) if len(tr) >= MIN_TRADES else -np.inf
        scores.append((mean, th, len(tr)))
    m, theta, n = max(scores)
    rule = {"signal": best.signal, "h": int(best.h), "theta": theta, "sign": sign, "sd": sd[(best.signal, int(best.h))],
            "train_days": tr_days, "train_t": float(best.t), "train_net_bp_mean": float(m), "train_trades": n,
            "fee_krw": FEE_KRW, "chosen_at": pd.Timestamp.now().isoformat(timespec="seconds")}
    lines += ["", "## 규칙 (학습일에서 고름)", "", "| θ | 거래 수 | 비용 후 평균 bp |", "| --- | --- | --- |"]
    lines += [f"| {th} | {k} | {mm:+.2f} |" for mm, th, k in scores]
    lines += ["", f"→ {json.dumps(rule, ensure_ascii=False)}"]
    if not te_days:
        if (RESULTS / "rules.json").exists() and not a.force:
            lines += ["", "results/rules.json 이 이미 있어 그대로 둠 (고정된 규칙). 덮어쓰려면 --force."]
        else:
            (RESULTS / "rules.json").write_text(json.dumps(rule, indent=2, ensure_ascii=False))
            lines += ["", "results/rules.json 에 고정. 시험일에는 이 파일을 바꾸지 않음."]

    # 3) 시험일 판정 (규칙은 rules.json 그대로)
    if te_days:
        rule = json.loads((RESULTS / "rules.json").read_text())
        Fte = {d: features(p) for d, p in load(te_days).items()}
        D = stack(Fte, rule["signal"], rule["h"])
        res = nw_ols(D[rule["signal"]] / rule["sd"], D[f"r_{rule['h']}"], rule["h"])
        te = [x for f in Fte.values() for x in simulate(f, rule["signal"], rule["h"], rule["theta"], rule["sign"], rule["sd"])]
        net = np.array([x["net_bp"] for x in te])
        tstat = net.mean() / net.std(ddof=1) * np.sqrt(len(net)) if len(net) > 1 and net.std() > 0 else np.nan
        lines += ["", "## 시험일 (표본 밖)", "", f"회귀: b {res['b']:+.2f} bp, t {res['t']:+.2f}, n {res['n']} (학습 b 부호 {rule['sign']:+.0f})",
                  f"규칙: 거래 {len(te)}건, 비용 전 평균 {np.mean([x['gross_bp'] for x in te]) if te else float('nan'):+.2f} bp, "
                  f"비용 후 평균 {net.mean() if len(net) else float('nan'):+.2f} bp, t {tstat:+.2f}, 승률 {(net > 0).mean() * 100 if len(net) else float('nan'):.0f}%"]
        pd.DataFrame(te).to_csv(RESULTS / "test_trades.csv", index=False)
    tag = "_".join(tr_days) + (("__" + "_".join(te_days)) if te_days else "")
    (RESULTS / f"analysis_{tag}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
