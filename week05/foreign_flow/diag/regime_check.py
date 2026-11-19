"""고른 후보(W60, ρ>0, θ0.5)가 무작위로 같은 비율 분을 남긴 필터보다 나은지 (17~19일)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "."); sys.path.insert(0, "diag")
import analyze as A
from regime_search import F, rule, rho, days
rng = np.random.default_rng(1)
def run(masks):
    tr = []
    for d in days:
        g = F[d].copy(); g.loc[~masks[d], "combo_5"] = np.nan
        tr += A.simulate(g, "combo_5", 5, 0.5, 1.0, rule["sd"])
    return np.array([x["net_bp"] for x in tr])
real = {d: (rho(F[d], 60) > 0).fillna(False).to_numpy() for d in days}
net = run(real)
print(f"실제: {len(net)}건 평균 {net.mean():+.2f} t {net.mean()/net.std(ddof=1)*np.sqrt(len(net)):+.2f}, 남긴 분 비율", {d: round(real[d].mean(), 2) for d in days})
# 무작위: 같은 날 안에서 ρ 열을 30~120분 원형 이동 (자기상관 구조 유지)
sims = []
for _ in range(500):
    m = {d: np.roll(real[d], rng.integers(30, len(real[d]) - 30)) for d in days}
    sims.append(run(m).mean())
sims = np.array(sims)
print(f"원형 이동 500번: 평균 {sims.mean():+.2f}, 실제 이상 비율 {(sims >= net.mean()).mean():.3f}")
