import sys, json, numpy as np, pandas as pd
sys.path.insert(0,"."); import analyze as A
days=["20261117","20261118","20261119"]; P=A.load(days)
p=P["20261119"]; s=p["MINI_spread"]; hm=p.index.strftime("%H:%M")
print("19일 MINI 스프레드 분포", s.describe().round(3).to_dict()); print(" 0.5 넘는 분:", list(p.index[s>0.5].strftime("%H:%M"))[:30])
print(" MINI 결측 분:", list(p.index[p["MINI_mid"].isna()&(hm>='09:00')&(hm<='15:35')].strftime("%H:%M"))[:40])
r=json.loads((A.COMBO_DIR/"for_20261119.json").read_text())
F={d:A.add_signal(A.features(P[d].copy()),r) for d in days}
def b_of(f,mask):
    D=f.loc[mask,["combo_5","frgn_fut_5","ind_fut_5","r_5"]].dropna()
    out={}
    for sig in ["combo_5","frgn_fut_5","ind_fut_5"]:
        x=D[sig]/D[sig].std(); res=A.nw_ols(x,D.r_5,5); out[sig]=(round(res["b"],2),round(res["t"],2))
    return out,len(D)
print("\n== 같은 시각 마스크: 17일 쓸 수 있던 분(시:분)만 남겨 비교")
m17=F["20261117"].index[A.fresh(F["20261117"],5)].strftime("%H:%M")
for d in days:
    f=F[d]; m=A.fresh(f,5)&f.index.strftime("%H:%M").isin(m17)
    print(" ",d,*b_of(f,m))
print("\n== 반대로 11:00 이후만 / 오전만")
for d in days:
    f=F[d]; base=A.fresh(f,5); h=f.index.strftime("%H:%M")
    print(" ",d,"11시후",*b_of(f,base&(h>="11:00")),"| 11시전",*b_of(f,base&(h<"11:00")) if (base&(h<"11:00")).sum()>30 else "")
