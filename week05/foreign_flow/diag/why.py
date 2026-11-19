import sys, numpy as np, pandas as pd
sys.path.insert(0,"."); import analyze as A
days=["20261117","20261118","20261119"]; P=A.load(days)
pd.set_option("display.width",200)
def c(a,b):
    m=np.isfinite(a)&np.isfinite(b); return np.corrcoef(a[m],b[m])[0,1] if m.sum()>10 else np.nan
for d in days:
    p=P[d]; f=A.features(p.copy()); mid=f["mid"]
    f["rb5"]=np.log(mid/mid.shift(5))*1e4; f["r1"]=np.log(mid/mid.shift(1))*1e4
    s=A.stale(p,"011"); tm=pd.to_numeric(p["011_frgn_tm"],errors="coerce")
    age=(p.index-(p.index.normalize()+pd.to_timedelta(((tm//10000)*3600+((tm//100)%100)*60+tm%100).values,unit="s"))).total_seconds()
    m=A.fresh(f,5)
    g=f[m]
    print(f"\n######## {d}")
    hm=f.index.strftime("%H:%M")
    o=mid[(hm>="09:05")].dropna(); print(f"시가→종가 {o.iloc[0]:.2f}→{o.iloc[-1]:.2f} ({(np.log(o.iloc[-1]/o.iloc[0]))*1e4:+.0f}bp), 고저 {o.max():.2f}/{o.min():.2f}, 1분 변동 sd {f.r1.std():.2f}bp")
    print(f"쓸 수 있는 분 {m.sum()} (오전<12시 {(m&(hm<'12:00')).sum()}), 늦은 스냅샷 {s.mean()*100:.0f}%, 늦지 않은 분 지연 중앙값 {np.nanmedian(age[~s.values]):.0f}s")
    ff=p["011_frgn"]+p["051_frgn"]/5; ii=p["011_ind"]+p["051_ind"]/5
    print(f"하루 순매수(K200환산 계약) 외국인 {ff.dropna().iloc[-1]-ff.dropna().iloc[0]:+.0f}, 개인 {ii.dropna().iloc[-1]-ii.dropna().iloc[0]:+.0f}")
    print(f"corr(외국인 누적, 가격) {c(ff.where(~s).values, mid.values):+.2f}  corr(개인 누적, 가격) {c(ii.where(~s).values, mid.values):+.2f}")
    # 같은 5분 vs 다음 5분
    for sig in ["frgn_fut_5","ind_fut_5"]:
        print(f" {sig}: 같은 5분 수익과 corr {c(g[sig].values,g.rb5.values):+.2f} | 다음 5분 corr {c(g[sig].values,g.r_5.values):+.2f}")
    print(f" 가격 자기상관(직전5분→다음5분) corr {c(g.rb5.values,g.r_5.values):+.2f}")
    # 직전 수익 통제
    D=g[["frgn_fut_5","ind_fut_5","rb5","r_5"]].dropna()
    X=np.column_stack([np.ones(len(D)),D.frgn_fut_5/D.frgn_fut_5.std(),D.ind_fut_5/D.ind_fut_5.std(),D.rb5/D.rb5.std()])
    beta,*_=np.linalg.lstsq(X,D.r_5.values,rcond=None)
    print(f" 다중회귀 r_5 ~ 외국인z {beta[1]:+.2f}, 개인z {beta[2]:+.2f}, 직전수익z {beta[3]:+.2f} (bp, n {len(D)})")
    # 시간대
    for lo,hi in [("09:05","11:00"),("11:00","13:00"),("13:00","15:30")]:
        q=g[(g.index.strftime("%H:%M")>=lo)&(g.index.strftime("%H:%M")<hi)]
        print(f"  {lo}-{hi} n{len(q):3d} 외국인→다음 {c(q.frgn_fut_5.values,q.r_5.values):+.2f} 개인→다음 {c(q.ind_fut_5.values,q.r_5.values):+.2f} 가격자기 {c(q.rb5.values,q.r_5.values):+.2f}")
    # 1분 리드래그: corr(flow_1(t), r1(t+k))
    fl=f["frgn_fut_1"].where(m); il=f["ind_fut_1"].where(m)
    print("  1분 리드래그 k(분, +면 흐름이 먼저):", " ".join(f"{k:+d}:{c(fl.values,f.r1.shift(-k).values):+.2f}/{c(il.values,f.r1.shift(-k).values):+.2f}" for k in range(-3,4)), "(외국인/개인)")
