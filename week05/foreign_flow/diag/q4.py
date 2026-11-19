import sys, numpy as np, pandas as pd
sys.path.insert(0,"."); import analyze as A
days=["20261117","20261118","20261119"]; P=A.load(days)
def ols(X,y):
    X=np.column_stack([np.ones(len(y))]+X); b,*_=np.linalg.lstsq(X,y,rcond=None); e=y-X@b
    V=np.linalg.inv(X.T@X)*e.var(); return b[1:], b[1:]/np.sqrt(np.diag(V)[1:])
for d in days:
    p=P[d]; f=A.features(p.copy()); bad=A.stale(p,"011")|A.stale(p,"051")
    g=lambda c:(p[f"011_{c}"]+p[f"051_{c}"]/5).where(~bad)
    mid=f["mid"]
    X=pd.DataFrame({"fr":g("frgn"),"ind":g("ind"),"inst":g("inst"),"mid":mid})
    hm=X.index.strftime("%H:%M")
    for lab,lo in [("전체","09:05"),("11시후","11:00")]:
        Z=X[(hm>=lo)&(hm<="15:30")].iloc[::5]   # 겹치지 않는 5분
        dZ=Z[["fr","ind","inst"]].diff(); r=np.log(Z["mid"]).diff()*1e4
        T=pd.DataFrame({"fr":dZ.fr,"ind":dZ.ind,"inst":dZ.inst,"r":r,"fr_n":dZ.fr.shift(-1),"ind_n":dZ.ind.shift(-1),"r_n":r.shift(-1)}).dropna()
        if len(T)<20: continue
        sfr=T.fr.std(); 
        lam,tl=ols([T.fr.values/sfr],T.r.values)
        rho=T.fr.corr(T.fr_n); rhoi=T.ind.corr(T.ind_n)
        pred,tp=ols([T.fr.values/sfr],T.r_n.values)
        both,tb=ols([T.fr.values/sfr,T.fr_n.values/sfr],T.r_n.values)
        rev,tr=ols([T.r.values/T.r.std()],T.r_n.values)
        print(f"{d} {lab:4s} n{len(T):3d} | 충격 λ {lam[0]:+.1f}bp/sd (t{tl[0]:+.1f}) | 외국인 흐름 지속 ρ {rho:+.2f}, 개인 ρ {rhoi:+.2f} | λ·ρ {lam[0]*rho:+.1f} vs 실제 다음수익 {pred[0]:+.1f}(t{tp[0]:+.1f}) | 다음흐름 통제 시 현재흐름 {both[0]:+.1f}(t{tb[0]:+.1f}) | 가격 반전 {rev[0]:+.1f}(t{tr[0]:+.1f}) | corr(외,개) {T.fr.corr(T.ind):+.2f} corr(외,기관) {T.fr.corr(T.inst):+.2f} | 외국인 5분 sd {sfr:.0f}")
