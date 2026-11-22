"""6주차: 5주차 투자자 흐름 신호를 더 긴 보유 시간(10·15·30·60분)으로 다시 고르고, 고정한 규칙을 표본 밖에서 판정.

  .venv/bin/python analyze.py --scan 20261117,20261118,20261119,20261120     # 회귀표 + 날별 이질성 + 규칙 고르기 → results/rules/
  .venv/bin/python analyze.py --freeze 20261117,20261118,20261119,20261120   # scan 통과 후보가 없어 조건을 풀고 고정 (FREEZE)
  .venv/bin/python analyze.py --test 20261123,20261124,...                   # 고정한 규칙으로 표본 밖 판정

신호 (5주차와 같음, 모두 t 행까지 받은 값만):
  frgn_fut_k   직전 k 분 외국인 선물 순매수 변화 (코스피200 계약 + 미니/5)
  frgn_opt_k   직전 k 분 외국인 (콜 순매수 − 풋 순매수) 변화
  ind_fut_k    직전 k 분 개인 선물 순매수 변화
  aggr_k       매수주도 − 매도주도 체결 (참고용. 실행기가 계산하지 않아서 규칙 후보에서 뺌)
  z = 신호 / 학습일 표준편차 ((신호, h) 별로 따로 잼). 시험일에도 학습일 값 그대로.
목표: 미니 중간가 로그수익률 h 분 뒤 (bp). 진입 가능 시각 09:05~(15:35 − h).
회귀: r_h = a + b·z, Newey-West 표준오차 (lag = h).

규칙 고르는 순서 (11/23 새벽, 표를 돌리기 전에 정함):
  1. 후보 = 투자자 신호 9개 × 보유 h 4개 = 36개. 나흘(17~20) 합친 회귀 b·t 와, 하루씩 따로 돌린 b·표준오차.
  2. 날별 이질성: Cochran Q = Σ w_d (b_d − b̄)², w_d = 1/se_d², 자유도 = 날 수 − 1.
     Q 의 p ≥ Q_P_MIN 이고, b 부호가 합친 b 와 같은 날이 SAME_SIGN_MIN 일 이상, 합친 |t| ≥ T_MIN 이면 통과.
  3. 통과한 후보 중 화요일 표에서 미리 찍어 둔 두 개(PRIORITY)를 먼저, 나머지는 |t| 순. 신호가 다른 것으로 최대 두 개.
     첫째 = 모의계좌 주문, 둘째 = 종이 기록. 통과가 하나도 없으면 규칙을 만들지 않고 끝냄.
  4. θ 는 THETAS 중 나흘 합쳐 거래 MIN_TRADES 건 이상인 것에서 비용 후 평균 1등. 방향 = 합친 b 부호.
  5. results/rules/ 에 쓰고 나면 다시 쓰지 않음 (--force 없이는 거부).

판정 (시험 전에 적어 둠): 시험일 합친 회귀 b 부호가 규칙 방향과 같고, 그리고 규칙 거래의 비용 후 평균 bp > 0.
  둘 다면 "표본 밖에서 맞음", 하나라도 아니면 기각. 거래 수가 적어서 유의성까지는 기대하지 않음.
비용: 왕복 = 진입 시점 스프레드 + 수수료 2번 (거래대금 × FEE_RATE). 미니 1pt = 50,000원.
늦은 스냅샷: 서버 시각(tm)이 행 시각보다 STALE_S 초 넘게 늦은 분은 회귀·규칙 모두에서 뺌 (실행기도 진입 안 함).
"""
from __future__ import annotations

import argparse
import json
import math
from itertools import product

import numpy as np
import pandas as pd

from convert import PANEL_DIR
from record import HERE

RESULTS = HERE / "results"
RULE_DIR = RESULTS / "rules"
KS = (1, 5, 15)
HS = (10, 15, 30, 60)
INVESTOR = ("frgn_fut_", "frgn_opt_", "ind_fut_")
PRIORITY = (("frgn_opt_15", 30), ("ind_fut_5", 15))   # 5주차 화요일 표의 단서 (외국인 옵션 t 3.28, 개인 t −3.12)
Q_P_MIN, SAME_SIGN_MIN, T_MIN = 0.10, 3, 2.0
THETAS = (0.5, 1.0, 1.5, 2.0, 3.0)
MIN_TRADES = 10
TICK_PT, PT_KRW = 0.02, 50_000
FEE_RATE = 0.0000145     # NH 미니코스피200선물 HTS·API 편도 0.00145% (5주차 예수금 fee 와 일치)
START, LAST_EXIT = "09:05", "15:35"
STALE_S = 120

# 위험관리 (규칙 파일 risk 에 들어감). 손절은 5주차 1pt 가 5분 보유용이라 우선 끔 — 보유 시간에 맞춰 다시 정할 것.
# 하루 손실 한도 = VOL_K × 하루 손익 σ. 하루 σ = 학습일 거래 한 건 손익 σ × √(하루 평균 거래 수).
# (5주차 고정 20만 원은 5분 보유용이라 15분 보유에선 하루 σ 의 0.27배밖에 안 돼서 평범한 날에도 걸렸음)
VOL_K = 1.0
NO_RISK = {"stop_pt": 0.0, "daily_loss_krw": 0}


def vol_limit(F: dict[str, pd.DataFrame], rule: dict, k: float = VOL_K) -> dict:
    """위험관리 없이 학습일을 돌린 거래 손익(원)으로 하루 손실 한도를 계산."""
    tr = [dict(x, day=d) for d, f in F.items() for x in simulate_rule(f, dict(rule, risk=NO_RISK))]
    krw = np.array([x["krw"] for x in tr])
    per_day = len(tr) / len(F)
    sd_trade = float(krw.std(ddof=1))
    sd_day = sd_trade * math.sqrt(per_day)
    h = int(rule["h"])
    moves = pd.concat([((f["mid"].shift(-h) - f["mid"])[fresh(f, h)].dropna() * PT_KRW) for f in F.values()])
    return {"stop_pt": 0.0, "daily_loss_krw": float(round(k * sd_day, -4)),
            "how": {"k": k, "sd_trade_krw": round(sd_trade), "trades_per_day": round(per_day, 2), "sd_day_krw": round(sd_day),
                    "trades": len(tr), "check_sd_price_move_krw": round(float(moves.std())), "days": sorted(F)}}


def load(days: list[str]) -> dict[str, pd.DataFrame]:
    return {d: pd.read_csv(PANEL_DIR / f"{d}.csv", index_col="t", parse_dates=True) for d in days}


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


def signals(f: pd.DataFrame) -> list[str]:
    return [c for c in f.columns if not c.startswith("r_") and c not in ("mid", "stale", "spread")]


def usable(idx: pd.DatetimeIndex, h: int) -> np.ndarray:
    hm = idx.strftime("%H:%M")
    last = (pd.Timestamp("2000-01-01 " + LAST_EXIT) - pd.Timedelta(minutes=h)).strftime("%H:%M")
    return (hm >= START) & (hm <= last)


def fresh(f: pd.DataFrame, h: int) -> np.ndarray:
    m = usable(f.index, h)
    return m & ~f["stale"].to_numpy(bool) if "stale" in f else m


def stack(F: dict[str, pd.DataFrame], sig: str, h: int) -> pd.DataFrame:
    parts = [f.loc[fresh(f, h), [sig, f"r_{h}"]].assign(day=d) for d, f in F.items()]
    return pd.concat(parts).dropna()


def nw_ols(x, y, lag: int) -> dict:
    """OLS y = a + b x, Newey-West(Bartlett) 표준오차. (이 맥은 scipy 가 안 올라와서 직접 씀)"""
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
    se = float(np.sqrt(V[1, 1]))
    return {"b": float(beta[1]), "se": se, "t": float(beta[1] / se), "r2": float(r2), "n": int(len(yv))}


def chi2_sf(x: float, k: int) -> float:
    """카이제곱 위쪽 꼬리 확률 (정수 자유도, 닫힌 식)."""
    if x <= 0:
        return 1.0
    if k % 2 == 0:
        return math.exp(-x / 2) * sum((x / 2) ** i / math.factorial(i) for i in range(k // 2))
    s, term = 0.0, 1.0
    for i in range(1, (k - 1) // 2 + 1):
        term = 1.0 if i == 1 else term * x / (2 * i - 1)
        s += term
    return math.erfc(math.sqrt(x / 2)) + math.sqrt(2 * x / math.pi) * math.exp(-x / 2) * s


def cochran_q(b: np.ndarray, se: np.ndarray) -> dict:
    w = 1 / se ** 2
    bw = float((w * b).sum() / w.sum())
    q = float((w * (b - bw) ** 2).sum())
    k = len(b) - 1
    return {"q": q, "q_p": chi2_sf(q, k), "i2": max(0.0, (q - k) / q) if q > 0 else 0.0, "b_fixed": bw}


def cost_bp(mid: float, spread: float = TICK_PT) -> float:
    """왕복 = 진입 반 스프레드 + 청산 반 스프레드(진입 때 스프레드로 근사) + 수수료 2번."""
    if not np.isfinite(spread):
        spread = TICK_PT
    return (spread + 2 * mid * FEE_RATE) / mid * 1e4


def simulate(f: pd.DataFrame, sig: str, h: int, theta: float, sign: float, sd: float) -> list[dict]:
    """하루 안에서 규칙대로 진입·청산 (위험관리 없음, bp). 보유 중엔 새 진입 없음."""
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
                       "net_bp": float(side * r - cost_bp(mid, f.at[t, "spread"]))})
        busy_until = t + pd.Timedelta(minutes=h)
    return trades


def fee_krw(px: float) -> float:
    """미니 1계약 편도 수수료(원) = 체결가 × 50,000 × 0.00145%."""
    return float(px) * PT_KRW * FEE_RATE


def simulate_risk(f: pd.DataFrame, sig: str, h: int, theta: float, sign: float, sd: float,
                  stop_pt: float = 0.0, daily_loss_krw: float = 0) -> list[dict]:
    """simulate 에 손절·하루 한도를 더한 것 (실행기와 같은 순서: 시간 청산 → 손절 → 한도 → 진입). 원 단위 손익."""
    m = fresh(f, h)
    z, mid, idx = f[sig] / sd, f["mid"], f.index
    pos = {t: i for i, t in enumerate(idx)}
    spr = lambda tt: f.at[tt, "spread"] if np.isfinite(f.at[tt, "spread"]) else TICK_PT  # noqa: E731
    trades, busy_until, cum = [], None, 0.0
    for t in idx[m]:
        if busy_until is not None and t < busy_until:
            continue
        if daily_loss_krw and cum <= -daily_loss_krw:
            break
        zt = z.get(t)
        if not np.isfinite(zt) or abs(zt) <= theta or not np.isfinite(f.at[t, f"r_{h}"]):
            continue
        side, i, m0 = int(np.sign(zt) * sign), pos[t], mid.iloc[pos[t]]
        k, why = h, "time"
        for j in range(1, h):
            mj = mid.iloc[i + j]
            if stop_pt and np.isfinite(mj) and side * (mj - m0) <= -stop_pt:
                k, why = j, "stop"
                break
        krw = side * (mid.iloc[i + k] - m0) * PT_KRW - (spr(t) + spr(idx[i + k])) / 2 * PT_KRW \
            - fee_krw(m0) - fee_krw(mid.iloc[i + k])
        cum += krw
        trades.append({"t": t, "side": side, "z": float(zt), "exit_t": idx[i + k], "why": why, "krw": float(krw)})
        busy_until = idx[i + k]
    return trades


def simulate_rule(f: pd.DataFrame, rule: dict) -> list[dict]:
    r = rule.get("risk", NO_RISK)
    return simulate_risk(f, rule["signal"], int(rule["h"]), rule["theta"], rule["sign"], rule["sd"],
                         stop_pt=r.get("stop_pt", 0.0), daily_loss_krw=r.get("daily_loss_krw", 0))


# ---------------------------------------------------------------- 고르기
def scan_table(F: dict[str, pd.DataFrame]) -> pd.DataFrame:
    days = sorted(F)
    rows = []
    for sig, h in product(signals(next(iter(F.values()))), HS):
        D = stack(F, sig, h)
        if len(D) < 30 or D[sig].std() == 0:
            continue
        sd = float(D[sig].std())
        pooled = nw_ols(D[sig] / sd, D[f"r_{h}"], h)
        per = []
        for d in days:
            Dd = D[D["day"] == d]
            per.append(nw_ols(Dd[sig] / sd, Dd[f"r_{h}"], h) if len(Dd) > h + 10 else None)
        ok = [x for x in per if x is not None]
        q = cochran_q(np.array([x["b"] for x in ok]), np.array([x["se"] for x in ok])) if len(ok) >= 2 else \
            {"q": np.nan, "q_p": np.nan, "i2": np.nan, "b_fixed": np.nan}
        same = sum(np.sign(x["b"]) == np.sign(pooled["b"]) for x in ok)
        row = {"signal": sig, "h": h, "sd": sd, **pooled, **q, "same_sign": int(same), "n_days": len(ok),
               "investor": sig.startswith(INVESTOR)}
        for d, x in zip(days, per):
            row[f"b_{d}"] = x["b"] if x else np.nan
            row[f"t_{d}"] = x["t"] if x else np.nan
        row["passes"] = bool(row["investor"] and row["q_p"] >= Q_P_MIN and same >= SAME_SIGN_MIN
                             and abs(pooled["t"]) >= T_MIN)
        rows.append(row)
    return pd.DataFrame(rows)


def pick(T: pd.DataFrame) -> list[pd.Series]:
    ok = T[T["passes"]].copy()
    ok["prio"] = [next((i for i, p in enumerate(PRIORITY) if p == (r.signal, r.h)), len(PRIORITY))
                  for r in ok.itertuples()]
    ok["abs_t"] = ok["t"].abs()
    ok = ok.sort_values(["prio", "abs_t"], ascending=[True, False])
    chosen, used = [], set()
    for _, r in ok.iterrows():
        if r.signal in used:
            continue
        chosen.append(r)
        used.add(r.signal)
        if len(chosen) == 2:
            break
    return chosen


def choose_theta(F: dict[str, pd.DataFrame], sig: str, h: int, sign: float, sd: float) -> tuple[float, list]:
    scores = []
    for th in THETAS:
        tr = [dict(x, day=d) for d, f in F.items() for x in simulate(f, sig, h, th, sign, sd)]
        net = [x["net_bp"] for x in tr]
        scores.append({"theta": th, "n": len(tr), "net": float(np.mean(net)) if tr else np.nan,
                       "by_day": {d: (sum(1 for x in tr if x["day"] == d),
                                      float(np.mean([x["net_bp"] for x in tr if x["day"] == d]))
                                      if any(x["day"] == d for x in tr) else np.nan) for d in F}})
    ok = [s for s in scores if s["n"] >= MIN_TRADES]
    if not ok:
        raise SystemExit(f"{sig} h{h}: θ 후보 모두 학습 거래 {MIN_TRADES}건 미만")
    return max(ok, key=lambda s: s["net"])["theta"], scores


def fmt(x, spec="+.2f"):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else format(x, spec)


def scan(days: list[str], force: bool) -> None:
    F = {d: features(p) for d, p in load(days).items()}
    T = scan_table(F)
    RESULTS.mkdir(exist_ok=True)
    T.sort_values("t", key=np.abs, ascending=False).to_csv(RESULTS / f"scan_{'_'.join(days)}.csv", index=False)
    dcols = " | ".join(f"{d[4:6]}/{d[6:]} b (t)" for d in days)
    L = [f"# 6주차 보유 시간 다시 고르기 — 학습 {','.join(days)}", "",
         f"통과 조건: 투자자 신호, Q 검정 p ≥ {Q_P_MIN}, 같은 부호 날 ≥ {SAME_SIGN_MIN}/{len(days)}, 합친 |t| ≥ {T_MIN}",
         "", "## 미리 찍은 두 후보", ""]
    for sig, h in PRIORITY:
        r = T[(T.signal == sig) & (T.h == h)].iloc[0]
        rest = stack({d: F[d] for d in days[1:]}, sig, h)
        oos = nw_ols(rest[sig] / r.sd, rest[f"r_{h}"], h)
        L += [f"- **{sig} → {h}분**: 나흘 합친 b {r.b:+.2f} (t {r.t:+.2f}), Q {r.q:.2f} (p {r.q_p:.3f}, I² {r.i2:.0%}), "
              f"같은 부호 {r.same_sign}/{r.n_days}일 → {'통과' if r.passes else '탈락'}",
              f"  - 화요일만: b {r[f'b_{days[0]}']:+.2f} (t {r[f't_{days[0]}']:+.2f}) / 18~20일 합친 것 (화요일 단서의 표본 밖): "
              f"b {oos['b']:+.2f} (t {oos['t']:+.2f}, n {oos['n']})"]
    L += ["", "## 전체 회귀표 (투자자 신호, |t| 순)", "",
          f"| 신호 | h | 합친 b | t | Q p | I² | 같은 부호 | {dcols} | 통과 |",
          "| --- | --- | --- | --- | --- | --- | --- | " + " | ".join("---" for _ in days) + " | --- |"]
    for r in T[T.investor].sort_values("t", key=np.abs, ascending=False).itertuples():
        dv = " | ".join(f"{fmt(getattr(r, f'b_{d}'))} ({fmt(getattr(r, f't_{d}'))})" for d in days)
        L.append(f"| {r.signal} | {r.h} | {r.b:+.2f} | {r.t:+.2f} | {fmt(r.q_p, '.3f')} | {fmt(r.i2, '.0%')} | "
                 f"{r.same_sign}/{r.n_days} | {dv} | {'✓' if r.passes else ''} |")
    L += ["", "참고 (규칙 후보 아님): 체결 주도 흐름 aggr", "", "| 신호 | h | 합친 b | t | Q p |", "| --- | --- | --- | --- | --- |"]
    L += [f"| {r.signal} | {r.h} | {r.b:+.2f} | {r.t:+.2f} | {fmt(r.q_p, '.3f')} |"
          for r in T[~T.investor].sort_values("t", key=np.abs, ascending=False).head(6).itertuples()]

    chosen = pick(T)
    L += ["", "## 고른 규칙", ""]
    if not chosen:
        L += ["통과한 후보가 없음 → 규칙을 만들지 않음."]
        (RESULTS / f"scan_{'_'.join(days)}.md").write_text("\n".join(L) + "\n")
        print("\n".join(L))
        return
    rules = []
    for role, r in zip(("order", "paper"), chosen):
        sign = float(np.sign(r.b))
        theta, scores = choose_theta(F, r.signal, int(r.h), sign, float(r.sd))
        best = next(s for s in scores if s["theta"] == theta)
        rule = {"name": f"{r.signal}_h{int(r.h)}", "signal": r.signal, "h": int(r.h), "theta": theta, "sign": sign,
                "sd": float(r.sd), "train_days": days, "train_b": float(r.b), "train_t": float(r.t),
                "train_q_p": float(r.q_p), "train_trades": best["n"], "train_net_bp_mean": best["net"],
                "fee_rate": FEE_RATE, "valid_from": "20261123",
                "chosen_at": pd.Timestamp.now().isoformat(timespec="seconds")}
        rules.append((role, rule))
        L += [f"### {'모의계좌 주문' if role == 'order' else '종이 기록'}: {rule['name']} (방향 {sign:+.0f})", "",
              "| θ | 거래 | 비용 후 평균 bp | " + " | ".join(f"{d[4:6]}/{d[6:]}" for d in days) + " |",
              "| --- | --- | --- | " + " | ".join("---" for _ in days) + " |"]
        for s in scores:
            L.append(f"| {s['theta']}{' ←' if s['theta'] == theta else ''} | {s['n']} | {fmt(s['net'])} | "
                     + " | ".join(f"{s['by_day'][d][0]}건 {fmt(s['by_day'][d][1])}" for d in days) + " |")
        L.append("")
    write_rules(rules, force)
    L += ["규칙 파일: `results/rules/order.json` (주문, 위험관리 켬), `order_norisk.json` (같은 규칙 위험관리 끔, 종이), "
          "`paper.json` (둘째 후보, 종이).", "",
          "판정 (시험 전에 적어 둠): 시험일 합친 회귀 b 부호 = 규칙 방향, 그리고 비용 후 평균 bp > 0. 둘 다면 맞음, 아니면 기각."]
    (RESULTS / f"scan_{'_'.join(days)}.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


# ---------------------------------------------------------------- 조건을 풀어 고정 (11/23 새벽, scan 결과를 본 뒤)
# scan 에서 통과 후보가 없어서, 아래 두 개를 결과를 보고 골랐다. 보고서에 그대로 밝힐 것.
#   frgn_fut_15 → 15분: 투자자 신호 중 날별 이질성 없는 것(Q p 0.33, 3/4일 같은 부호)에서 |t| 1등 (−1.92, 조건 2.0 미달).
#   aggr_5 → 60분: 규칙 후보 범위 밖(참고용)이던 체결 신호. |t| 2.40, Q p 0.135, 3/4일. 실행기를 웹소켓 체결을 읽게 고친 뒤 넣음.
# 주문은 frgn_fut_15 (θ 를 바꿔도 거래 결과 부호가 그대로), aggr_5 는 종이 기록 (θ 따라 −16 ~ +15bp 로 뒤집힘).
FREEZE = (("frgn_fut_15", 15, "order"), ("aggr_5", 60, "paper"))
FREEZE_NOTE = ("scan 통과 후보 없음 → 결과를 보고 조건(|t|≥2, 투자자 신호만)을 풀어 고름. "
               "θ = 거래 MIN_TRADES 건 이상, 비용 후 평균이 플러스인 날 SAME_SIGN_MIN 일 이상 중 평균 1등 (이 θ 규칙도 표를 본 뒤 정함)")


def theta_consistent(scores: list[dict]) -> float:
    ok = [s for s in scores if s["n"] >= MIN_TRADES
          and sum(1 for n, m in s["by_day"].values() if n and m > 0) >= SAME_SIGN_MIN]
    if not ok:
        raise SystemExit("θ 후보 중 조건(거래 수·플러스인 날)을 맞추는 게 없음")
    return max(ok, key=lambda s: s["net"])["theta"]


def freeze(days: list[str], force: bool) -> None:
    F = {d: features(p) for d, p in load(days).items()}
    T = scan_table(F)
    rules, L = [], [f"# 6주차 규칙 고정 (조건 풀어서) — 학습 {','.join(days)}", "", FREEZE_NOTE, ""]
    for sig, h, role in FREEZE:
        r = T[(T.signal == sig) & (T.h == h)].iloc[0]
        sign = float(np.sign(r.b))
        _, scores = choose_theta(F, sig, h, sign, float(r.sd))
        theta = theta_consistent(scores)
        best = next(s for s in scores if s["theta"] == theta)
        rule = {"name": f"{sig}_h{h}", "signal": sig, "h": h, "theta": theta, "sign": sign, "sd": float(r.sd),
                "train_days": days, "train_b": float(r.b), "train_t": float(r.t), "train_q_p": float(r.q_p),
                "train_same_sign": int(r.same_sign), "train_trades": best["n"], "train_net_bp_mean": best["net"],
                "fee_rate": FEE_RATE, "valid_from": "20261123", "note": FREEZE_NOTE,
                "chosen_at": pd.Timestamp.now().isoformat(timespec="seconds")}
        if role == "order":
            rule["risk"] = vol_limit(F, rule)
        rules.append((role, rule))
        L += [f"## {'모의계좌 주문' if role == 'order' else '종이 기록'}: {rule['name']} — 방향 {sign:+.0f}, θ {theta}", "",
              f"나흘 합친 b {r.b:+.2f} (t {r.t:+.2f}), Q p {r.q_p:.3f}, 같은 부호 {r.same_sign}/{r.n_days}일", "",
              "| θ | 거래 | 비용 후 평균 bp | " + " | ".join(f"{d[4:6]}/{d[6:]}" for d in days) + " |",
              "| --- | --- | --- | " + " | ".join("---" for _ in days) + " |"]
        L += [f"| {s['theta']}{' ←' if s['theta'] == theta else ''} | {s['n']} | {fmt(s['net'])} | "
              + " | ".join(f"{s['by_day'][d][0]}건 {fmt(s['by_day'][d][1])}" for d in days) + " |" for s in scores]
        L.append("")
    write_rules(rules, force)
    risk = next(r for role, r in rules if role == "order")["risk"]
    hw = risk["how"]
    L += ["규칙 파일: `results/rules/order.json` (주문, 위험관리 켬), `order_norisk.json` (같은 규칙, 위험관리 끔, 종이), "
          "`paper.json` (종이).", "",
          f"위험관리 (order.json): 하루 손실 한도 {risk['daily_loss_krw']:,.0f}원 = {hw['k']} × 하루 σ {hw['sd_day_krw']:,}원 "
          f"(거래 한 건 σ {hw['sd_trade_krw']:,}원 × √{hw['trades_per_day']}건, 학습 {hw['trades']}건. "
          f"확인용: 진입 가능한 모든 분의 {rules[0][1]['h']}분 가격 변화 σ {hw['check_sd_price_move_krw']:,}원). "
          "손절 끔 (5주차 1pt 는 5분 보유용 — 보유 시간에 맞춘 손절은 따로 정해서 넣음).", "",
          "**판정 (시험 전에 적어 둠, 11/23 새벽):** 시험일(11/23~27 중 실행한 날) 합친 회귀 b 부호 = 규칙 방향, "
          "그리고 규칙 거래의 비용 후 평균 bp > 0. 둘 다면 \"표본 밖에서 맞음\", 하나라도 아니면 기각. "
          "`analyze.py --test 날짜,...` 로 계산. 거래 수가 적어 유의성까지는 기대하지 않음."]
    (RESULTS / f"freeze_{'_'.join(days)}.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


def write_rules(rules: list[tuple[str, dict]], force: bool) -> None:
    RULE_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for role, rule in rules:
        if role == "order":
            out["order.json"] = rule
            out["order_norisk.json"] = dict(rule, risk=NO_RISK)
        else:
            out["paper.json"] = dict(rule, risk=NO_RISK)
    if any((RULE_DIR / k).exists() for k in out) and not force:
        raise SystemExit("results/rules/ 에 규칙이 이미 있음 (고정된 규칙). 덮어쓰려면 --force")
    for k, v in out.items():
        (RULE_DIR / k).write_text(json.dumps(v, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- 판정
def test(days: list[str]) -> None:
    files = sorted(RULE_DIR.glob("*.json"))
    F = {d: features(p) for d, p in load(days).items()}
    L = [f"# 6주차 규칙 표본 밖 판정 — {','.join(days)}", "",
         "| 규칙 파일 | 규칙 | 회귀 b (기대 부호) | t | 거래 | 비용 전 bp | 비용 후 bp | t | 원 (위험관리 반영) | 판정 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for path in files:
        rule = json.loads(path.read_text())
        if max(rule["train_days"]) >= min(days):
            raise SystemExit(f"{path.name}: 시험일이 학습일보다 뒤여야 함")
        sig, h = rule["signal"], rule["h"]
        D = stack(F, sig, h)
        reg = nw_ols(D[sig] / rule["sd"], D[f"r_{h}"], h)
        tr = [dict(x, day=d) for d, f in F.items() for x in simulate(f, sig, h, rule["theta"], rule["sign"], rule["sd"])]
        net = np.array([x["net_bp"] for x in tr])
        tt = net.mean() / net.std(ddof=1) * np.sqrt(len(net)) if len(net) > 1 and net.std() > 0 else np.nan
        krw = sum(x["krw"] for f in F.values() for x in simulate_rule(f, rule))
        ok = np.sign(reg["b"]) == rule["sign"] and len(net) and net.mean() > 0
        L.append(f"| {path.name} | {rule['name']} θ{rule['theta']} | {reg['b']:+.2f} ({rule['sign']:+.0f}) | {reg['t']:+.2f} | "
                 f"{len(net)} | {fmt(np.mean([x['gross_bp'] for x in tr]) if tr else np.nan)} | "
                 f"{fmt(net.mean() if len(net) else np.nan)} | {fmt(tt)} | {krw:+,.0f} | {'맞음' if ok else '기각'} |")
        pd.DataFrame(tr).to_csv(RESULTS / f"test_trades_{path.stem}.csv", index=False)
    L += ["", "판정 기준 (11/23 고정 전에 적어 둠): 회귀 b 부호 = 규칙 방향 AND 비용 후 평균 > 0. "
          "order 와 order_norisk 는 같은 규칙이라 bp 판정은 같고, 원 열에서 위험관리 차이만 봄."]
    (RESULTS / f"test_{'_'.join(days)}.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default="", help="학습일 (쉼표): 회귀·이질성·규칙 고르기")
    ap.add_argument("--test", default="", help="시험일 (쉼표): 고정한 규칙으로 판정")
    ap.add_argument("--freeze", default="", help="학습일 (쉼표): FREEZE 에 적은 후보로 규칙 고정 (scan 통과 후보가 없을 때)")
    ap.add_argument("--force", action="store_true", help="이미 있는 규칙 파일을 덮어씀 (고정 뒤엔 쓰지 말 것)")
    a = ap.parse_args()
    if a.freeze:
        return freeze(a.freeze.split(","), a.force)
    if a.scan:
        return scan(a.scan.split(","), a.force)
    if a.test:
        return test(a.test.split(","))
    raise SystemExit("--scan 또는 --test")


if __name__ == "__main__":
    main()
