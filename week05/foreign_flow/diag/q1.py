import sys, numpy as np, pandas as pd
sys.path.insert(0,"."); import analyze as A
days=["20261117","20261118","20261119"]; P=A.load(days)
for d in days:
    p=P[d]; hm=p.index.strftime("%H:%M"); sess=(hm>="09:00")&(hm<="15:35")
    print(f"\n## {d}  행 {len(p)} (장중 {sess.sum()})")
    for prd in ["011","051","012","013"]:
        tm=pd.to_numeric(p[f"{prd}_frgn_tm"],errors="coerce")
        age=(p.index-(p.index.normalize()+pd.to_timedelta(((tm//10000)*3600+((tm//100)%100)*60+tm%100).values,unit="s"))).total_seconds()
        a=pd.Series(age,index=p.index)[sess]
        ch=(p[f"{prd}_frgn"].diff()!=0)[sess].mean()
        print(f" {prd}: 결측 {p[f'{prd}_frgn'][sess].isna().mean()*100:4.0f}%  지연 p10/50/90 {np.nanpercentile(a,10):5.0f}/{np.nanpercentile(a,50):5.0f}/{np.nanpercentile(a,90):6.0f}s  >120s {(a>120).mean()*100:3.0f}%  분당 값 바뀜 {ch*100:3.0f}%")
    # 늦은 구간 시간표
    tm=pd.to_numeric(p["011_frgn_tm"],errors="coerce")
    age=pd.Series((p.index-(p.index.normalize()+pd.to_timedelta(((tm//10000)*3600+((tm//100)%100)*60+tm%100).values,unit="s"))).total_seconds(),index=p.index)
    hr=age[sess].groupby(p.index[sess].hour).median()
    print("  시간별 지연 중앙값(s):", {int(k):int(v) if np.isfinite(v) else None for k,v in hr.items()})
    print("  MINI 결측", p["MINI_mid"][sess].isna().mean().round(3), "스프레드 평균", p["MINI_spread"][sess].mean().round(3), "MINI 거래량", p["MINI_vol"][sess].dropna().iloc[-1] if "MINI_vol" in p else None, "K200 거래량", p["K200_vol"][sess].dropna().iloc[-1])
