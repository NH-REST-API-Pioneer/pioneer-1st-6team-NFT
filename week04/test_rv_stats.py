"""롤링 β 상대가치가 「진짜 평균회귀」인지 통계로 확인 (backtest_rv.py 의 백테스트와 별개).

15:00~18:00 5분봉, 6개월. β·z 는 매 봉 직전 L개 봉만으로 계산 (표본 밖).

  1. z 예측 회귀: 봉 t 의 β 를 고정한 채 h 봉 뒤 스프레드 변화(표준화)를 z_t 에 회귀.
     기울기 < 0 이고 유의하면 「벗어난 만큼 되돌아온다」. 겹치는 표본이라 Newey-West 표준오차.
  2. 회귀 확률: |z|>2 에서 12봉(60분) 안에 |z|<0.5 로 돌아오는 비율.
     대조군 = 날짜를 섞어 y 의 d일과 x 의 다른 날을 짝지은 가짜 페어 (각 시계열 성질은 그대로, 관계만 끊김).
  3. 거래 손익 부트스트랩: L84·진입2.0·청산0.5 (비용 포함) 거래들의 평균이 0 보다 큰지, 10,000번 재표집.

  python3 test_rv_stats.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

import backtest_rv as B

rng = np.random.default_rng(0)
PAIRS = [("GC", "SI"), ("ES", "NQ"), ("SI", "PL"), ("HO", "RB")]
LS = [36, 84]
HS = [6, 12]  # 30분, 60분


def collect(days, L):
    """날마다 (시각, z, β, sd, y, x) 배열."""
    out = []
    for d, g in days:
        yc, xc = g[("y", "c")].values, g[("x", "c")].values
        z, b = B.rolling_z(yc, xc, L)
        # sd 는 z 정의에서 역산 (e_t / z_t) 대신 다시 계산
        sd = np.full(len(yc), np.nan)
        for t in range(L - 1, len(yc)):
            ys, xs = yc[t - L + 1:t + 1], xc[t - L + 1:t + 1]
            if np.isnan(b[t]):
                continue
            e = ys - ys.mean() - b[t] * (xs - xs.mean())
            sd[t] = e.std()
        clock = g.index.strftime("%H:%M").values
        out.append((d, clock, z, b, sd, yc, xc))
    return out


def predict_reg(data, h):
    zs, fw = [], []
    for d, clock, z, b, sd, yc, xc in data:
        for t in range(len(z) - h):
            if clock[t] < "15:00" or clock[t + h] > B.FLAT_AT or np.isnan(z[t]) or sd[t] <= 0:
                continue
            ds = (yc[t + h] - yc[t]) - b[t] * (xc[t + h] - xc[t])
            zs.append(z[t])
            fw.append(ds / sd[t])
    X = sm.add_constant(np.array(zs))
    m = sm.OLS(np.array(fw), X).fit(cov_type="HAC", cov_kwds={"maxlags": h})
    return m.params[1], m.tvalues[1], len(zs)


def revert_rate(data, k_in=2.0, k_out=0.5, horizon=12):
    hit = n = 0
    for d, clock, z, *_ in data:
        t = 0
        while t < len(z):
            if clock[t] >= "15:00" and clock[t] <= "17:00" and not np.isnan(z[t]) and abs(z[t]) > k_in:
                n += 1
                seg = z[t + 1:t + 1 + horizon]
                if np.any(np.abs(seg[~np.isnan(seg)]) < k_out):
                    hit += 1
                t += horizon  # 같은 사건 중복 세지 않기
            else:
                t += 1
    return hit / n if n else np.nan, n


def shuffled_days(days):
    """y 는 그대로, x 를 다른 날 x 로 바꿔 끼운 가짜 페어 (시각 격자는 y 기준으로 맞춤)."""
    xs = [g["x"] for _, g in days]
    perm = rng.permutation(len(days))
    out = []
    for (d, g), j in zip(days, perm):
        xo = xs[j].copy()
        xo.index = xo.index - xo.index[0].normalize() + d  # 시각만 남기고 날짜 바꿈
        gg = pd.concat({"y": g["y"], "x": xo}, axis=1, sort=True).ffill(limit=2).dropna()
        # 다른 날 가격 수준 차이는 로그 상수라 β·z 에 영향 없음
        if len(gg) >= len(g) * 0.9:
            out.append((d, gg))
    return out


def bootstrap_trades(days, L=84, k_in=2.0, k_out=0.5, cy=0.0, cx=0.0, n=10_000):
    pnl = []
    for d, g in days:
        z, b = B.rolling_z(g[("y", "c")].values, g[("x", "c")].values, L)
        pnl += [t[2] for t in B.simulate(g, z, b, k_in, k_out, 99, cy, cx)]
    p = np.array(pnl) / 10  # $ (y 명목 $10만) → bp
    boots = rng.choice(p, size=(n, len(p)), replace=True).mean(axis=1)
    return len(p), p.mean(), np.percentile(boots, [2.5, 97.5]), (boots <= 0).mean()


def main() -> None:
    data_cache = {s: B.load(s) for s in {p for pr in PAIRS for p in pr}}
    print("1) z 예측 회귀 — 기울기<0 이면 되돌아옴 (t<−2 면 유의)")
    print(f"{'페어':8} {'L':>3} " + " ".join(f"{'h=' + str(h) + '(' + str(h * 5) + '분) 기울기 t':>26}" for h in HS)
          + f" {'| 가짜 페어 h=12 기울기 t':>26}")
    store = {}
    for ys, xs in PAIRS:
        days = list(B.day_frames(data_cache[ys], data_cache[xs], max(LS)))
        fake = shuffled_days(days)
        store[(ys, xs)] = days
        for L in LS:
            data = collect(days, L)
            cells = []
            for h in HS:
                c, t, n = predict_reg(data, h)
                cells.append(f"{c:+.3f} t={t:+.1f} (n={n})")
            cf, tf, _ = predict_reg(collect(fake, L), 12)
            print(f"{ys}-{xs:5} {L:>3} " + " ".join(f"{c:>26}" for c in cells) + f" | {cf:+.3f} t={tf:+.1f}")

    print("\n2) 회귀 확률 — |z|>2 에서 60분 안에 |z|<0.5 (진짜 vs 날짜 섞은 가짜)")
    for (ys, xs), days in store.items():
        for L in LS:
            r, n = revert_rate(collect(days, L))
            fr = [revert_rate(collect(shuffled_days(days), L))[0] for _ in range(5)]
            print(f"  {ys}-{xs:5} L{L:<3} 진짜 {r:.0%} ({n}건)  가짜 {np.mean(fr):.0%} (5번 평균, {min(fr):.0%}~{max(fr):.0%})")

    print("\n3) 거래 손익 부트스트랩 — GC-SI L84·진입2.0·청산0.5, 거래당 bp")
    days = store[("GC", "SI")]
    cy, cx = B.cost_bp("GC", 1), B.cost_bp("SI", 1)
    for label, a, b in (("비용 전", 0, 0), ("비용 후", cy, cx)):
        for part, sel in (("전체", lambda d: True), ("검증", lambda d: d >= B.TRAIN_END)):
            n, m, ci, p = bootstrap_trades([x for x in days if sel(x[0])], cy=a, cx=b)
            print(f"  {label} {part}: {n}건, 평균 {m:+.2f}bp, 95% 구간 [{ci[0]:+.2f}, {ci[1]:+.2f}], P(평균≤0) {p:.3f}")


if __name__ == "__main__":
    main()
