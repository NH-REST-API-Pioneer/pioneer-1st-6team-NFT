"""국면 필터 후보 탐색 (11/19 밤, 17~19일 학습 표본 안). 외국인 선물 5분 흐름의 직전 W분 자기상관 ρ_W 가 ρmin 넘을 때만 조합 신호로 진입."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "."); import analyze as A
days = ["20261117", "20261118", "20261119"]
F = {d: A.features(p) for d, p in A.load(days).items()}
rule, _, _ = A.fit_combo(F)          # 조합 sd 는 17~19일로 (금요일용 규칙 2 와 같은 값)

def rho(f, W):
    x = f["frgn_fut_5"]; y = x.shift(5)
    return x.rolling(W, min_periods=W - 10).corr(y)   # 직전 W분 안의 (5분 흐름, 5분 전 5분 흐름) 상관 — t 까지만 씀

def sim(f, th, W, rmin):
    g = f.copy()
    if W:
        r = rho(g, W); g.loc[~(r > rmin).fillna(False), "combo_5"] = np.nan
    return A.simulate(g, "combo_5", 5, th, 1.0, rule["sd"])

rows = []
for W in (0, 60, 90):
    for rmin in ((None,) if W == 0 else (0.0, 0.2, 0.4)):
        for th in A.THETAS:
            per = {d: sim(F[d], th, W, rmin) for d in days}
            tr = [x for v in per.values() for x in v]
            net = [x["net_bp"] for x in tr]
            rows.append(dict(W=W, rmin=rmin, th=th, n=len(tr), mean=np.mean(net) if net else np.nan,
                             **{f"d{d[-2:]}": (len(v), round(np.mean([x["net_bp"] for x in v]), 1) if v else None) for d, v in per.items()}))
R = pd.DataFrame(rows)
pd.set_option("display.width", 200)
print(R.round(2).to_string(index=False))
ok = R[R.n >= A.MIN_TRADES].sort_values("mean", ascending=False)
print("\n상위 5:\n", ok.head(5).round(2).to_string(index=False))
