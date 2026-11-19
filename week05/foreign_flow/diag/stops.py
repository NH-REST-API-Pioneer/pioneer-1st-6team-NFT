import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "."); import analyze as A
FEE=565; H=5
P=A.load(["20261117","20261118","20261119"])
def run(f, sig, sd, th, sign, stop, limit):
    m=A.fresh(f,H); z=f[sig]/sd; mid=f["mid"]; idx=f.index; pos={t:i for i,t in enumerate(idx)}
    busy=None; cum=0; out=[]
    for t in idx[m]:
        if busy is not None and t<busy: continue
        if limit and cum<=-limit: break
        zt=z.get(t)
        if not np.isfinite(zt) or abs(zt)<=th or not np.isfinite(f.at[t,"r_5"]): continue
        side=int(np.sign(zt)*sign); i=pos[t]; m0=mid.iloc[i]
        sp=f.at[t,"spread"] if "spread" in f and np.isfinite(f.at[t,"spread"]) else A.TICK_PT
        k_exit=H; why="time"
        for k in range(1,H):
            mk=mid.iloc[i+k]
            if stop and np.isfinite(mk) and side*(mk-m0)<=-stop: k_exit=k; why="stop"; break
        mx=mid.iloc[i+k_exit]
        krw=side*(mx-m0)*A.PT_KRW - sp*A.PT_KRW - 2*FEE
        cum+=krw; out.append((t,side,why,krw)); busy=idx[i+k_exit]
    return out
cases=[]
r1=json.loads((A.RESULTS/"rules.json").read_text())
for d in ["20261118","20261119"]:
    rc=json.loads((A.COMBO_DIR/f"for_{d}.json").read_text())
    cases.append(("규칙2 실제",d,rc))
    cases.append(("규칙1 종이",d,r1))
rt=json.loads(open(sys.argv[1]).read()) if len(sys.argv)>1 else None
rows=[]
for name,d,r in cases:
    f=A.features(P[d].copy())
    if "parts" in r: f=A.add_signal(f,r)
    for stop in (0,1,2,3):
        for lim in (0,300_000,500_000,1_000_000):
            tr=run(f,r["signal"],r["sd"],r["theta"],r["sign"],stop,lim)
            rows.append(dict(rule=name,day=d,stop=stop,limit=lim//10000,n=len(tr),stops=sum(x[2]=="stop" for x in tr),krw=int(sum(x[3] for x in tr))))
R=pd.DataFrame(rows)
for name in ["규칙2 실제","규칙1 종이"]:
    S=R[R.rule==name]
    piv=S.pivot_table(index=["stop"],columns="limit",values="krw",aggfunc="sum")
    print(f"\n== {name}: 18·19일 합계 원 (행=손절pt, 열=하루한도 만원, 0=없음)"); print(piv.to_string())
    print(S[S.limit==0][["day","stop","n","stops","krw"]].to_string(index=False))
print("\n== 신호가 맞던 날(17일, 학습 표본 안)에 걸면")
for name,r in [("규칙2 for_18",json.loads((A.COMBO_DIR/"for_20261118.json").read_text())),("규칙1",r1)]:
    f=A.features(P["20261117"].copy())
    if "parts" in r: f=A.add_signal(f,r)
    print(name, {s:int(sum(x[3] for x in run(f,r["signal"],r["sd"],r["theta"],r["sign"],s,0))) for s in (0,1,2,3)}, "한도30:",int(sum(x[3] for x in run(f,r["signal"],r["sd"],r["theta"],r["sign"],0,300000))))
