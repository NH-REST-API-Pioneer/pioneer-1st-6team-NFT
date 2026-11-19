import sys, json, numpy as np, pandas as pd
sys.path.insert(0,"."); import analyze as A
days=["20261117","20261118","20261119"]; P=A.load(days)
r=json.loads((A.COMBO_DIR/"for_20261119.json").read_text())
F={d:A.add_signal(A.features(P[d].copy()),r) for d in days}
rng=np.random.default_rng(0)
for d in days:
    f=F[d]; m=A.fresh(f,5)&(f.index.strftime("%H:%M")>="11:00")
    D=f.loc[m,["combo_5","r_5"]].dropna(); x=(D.combo_5/D.combo_5.std()).values; y=D.r_5.values
    b=np.polyfit(x,y,1)[0]
    # 30분 블록 부트스트랩
    blk=(D.index.hour*60+D.index.minute)//30; ub=np.unique(blk); bs=[]
    for _ in range(2000):
        pick=rng.choice(ub,len(ub)); idx=np.concatenate([np.where(blk==u)[0] for u in pick]); bs.append(np.polyfit(x[idx],y[idx],1)[0])
    lo,hi=np.percentile(bs,[2.5,97.5])
    # 한 블록씩 빼기
    loo={int(u):round(np.polyfit(x[blk!=u],y[blk!=u],1)[0],2) for u in ub}
    # 극단값
    k=np.abs(y)<np.percentile(np.abs(y),95); bt=np.polyfit(x[k],y[k],1)[0]
    sp=pd.Series(x).rank().corr(pd.Series(y).rank())
    # 상위 기여
    contrib=x*y; top=np.argsort(-contrib)[:10]
    print(f"\n## {d} 11시 뒤 combo b {b:+.2f}, 블록 부트 95% [{lo:+.2f},{hi:+.2f}], |r| 상위5% 빼면 {bt:+.2f}, 스피어만 {sp:+.2f}, n {len(y)}")
    print("  30분 블록 하나씩 뺀 b:", {f"{u//2:02d}:{(u%2)*30:02d}":v for u,v in loo.items()})
    print("  x*y 기여 상위 10분:", [D.index[i].strftime("%H:%M") for i in top], f"합 비중 {contrib[top].sum()/contrib.sum()*100:.0f}%")
