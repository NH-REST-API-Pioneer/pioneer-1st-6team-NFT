import sys, json, numpy as np, pandas as pd
sys.path.insert(0,"."); import analyze as A
days=["20261117","20261118","20261119"]; P=A.load(days)
r=json.loads((A.COMBO_DIR/"for_20261119.json").read_text())
rows=[]
for d in days:
    p=P[d]; f=A.add_signal(A.features(p.copy()),r); bad=A.stale(p,"011")|A.stale(p,"051")
    fr=(p["011_frgn"]+p["051_frgn"]/5).where(~bad); mid=f["mid"]; hm=f.index.strftime("%H:%M")
    S=(hm>="09:05")&(hm<="15:30")
    r1=np.log(mid).diff()*1e4
    print(f"\n## {d}")
    # 시간별 국면
    for h in range(9,16):
        q=S&(f.index.hour==h); 
        if q.sum()<20: continue
        m=mid[q].dropna(); eff=abs(np.log(m.iloc[-1]/m.iloc[0]))*1e4/ r1[q].abs().sum()
        fz=fr[q].dropna(); z5=fz.iloc[::5].diff().dropna()
        rho=z5.autocorr() if len(z5)>4 else np.nan
        mm=A.fresh(f,5)&q; D=f.loc[mm,["combo_5","r_5"]].dropna()
        c=D.combo_5.corr(D.r_5) if len(D)>15 else np.nan
        bas=p["K200_basis"][q].dropna()
        rows.append(dict(day=d,h=h,eff=eff,rho=rho,corr=c,vol=r1[q].std(),ret=np.log(m.iloc[-1]/m.iloc[0])*1e4,frnet=(fz.iloc[-1]-fz.iloc[0]) if len(fz) else np.nan,n=len(D)))
        print(f" {h:02d}시 수익 {rows[-1]['ret']:+6.0f}bp 효율 {eff:.2f} 1분sd {r1[q].std():5.1f} 외국인 순 {rows[-1]['frnet']:+6.0f} 흐름지속 {rho:+.2f} | 베이시스 {bas.iloc[0]:+.2f}→{bas.iloc[-1]:+.2f} | 조합→다음5분 corr {c:+.2f} (n{len(D)})")
    # 베이시스·옵션
    dfz=fr.diff(5); db=p["K200_basis"].diff(5); opt=f["frgn_opt_5"]
    print(f" corr(외국인5분, 베이시스5분변화) {dfz.corr(db):+.2f}, corr(외국인 선물, 외국인 옵션델타) {dfz.corr(opt):+.2f}, 옵션흐름→다음5분 {opt[A.fresh(f,5)].corr(f.r_5[A.fresh(f,5)]):+.2f}")
R=pd.DataFrame(rows).dropna()
print("\n== 시간(일×시) 단위 18칸: 조합 예측 corr 과 국면 변수의 상관")
for v in ["eff","rho","vol","ret","frnet"]:
    print(f"  {v}: {R['corr'].corr(R[v]):+.2f}" + (f"  (|값|: {R['corr'].corr(R[v].abs()):+.2f})" if v in ('ret','frnet') else ""))
