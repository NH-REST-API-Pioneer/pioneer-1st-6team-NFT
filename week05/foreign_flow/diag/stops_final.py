"""손절·하루 한도 최종 (11/20 장 후). 그날 실제로 주문한 규칙(18·19 규칙 2, 20 규칙 3)과 규칙 1 종이 기록에, 1분 중간가로 손절·한도를 걸어 본다.
수수료는 거래대금의 0.00145% (analyze.fee_krw). 17일(신호가 맞던 날, 학습 표본)은 해가 되는지만 본다."""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, "."); sys.path.insert(0, "diag")
import analyze as A
import re, types
src = open("diag/stops.py", encoding="utf-8").read()
S = types.SimpleNamespace(); ns = {"A": A, "np": np, "pd": pd, "H": 5, "FEE": 560}
exec(re.search(r"def run\(.*?\n    return out\n", src, re.S).group(0), ns)   # stops.py 의 run 만 (나머지 출력 안 돌림)
S.run = ns["run"]
P = A.load(["20261117", "20261118", "20261119", "20261120"])
rc = lambda d: json.loads((A.COMBO_DIR / f"for_{d}.json").read_text())
r1 = json.loads((A.RESULTS / "rules.json").read_text()); r3 = json.loads(A.REGIME_FILE.read_text())
order = {"20261118": rc("20261118"), "20261119": rc("20261119"), "20261120": r3}
STOPS, LIMS = (0, 1, 1.5, 2, 3), (0, 200_000, 300_000, 500_000)
def total(rules, stop, lim):
    out = {}
    for d, r in rules.items():
        f = A.add_signal(A.features(P[d].copy()), r)
        tr = A.simulate_risk(f, r["signal"], r["h"], r["theta"], r["sign"], r["sd"], stop, lim)
        out[d] = int(sum(x["krw"] for x in tr))
    return out
for name, rules in (("주문한 규칙 (18·19 규칙2, 20 규칙3)", order), ("규칙 1 (18~20)", {d: r1 for d in order})):
    print(f"\n== {name}: 18~20 합계 만원 (행 손절pt, 열 하루한도 만원)")
    rows = {}
    for s in STOPS:
        rows[s] = {l // 10000: round(sum(total(rules, s, l).values()) / 1e4) for l in LIMS}
    print(pd.DataFrame(rows).T.to_string())
    print("  날별 (손절없음/한도없음):", total(rules, 0, 0), "| 손절2·한도30:", total(rules, 2, 300_000))
print("\n== 17일 (신호 맞던 날, 규칙2 for_18) 에 걸면 만원:")
r = rc("20261118"); print({(s, l // 10000): round(total({"20261117": r}, s, l)["20261117"] / 1e4) for s in STOPS for l in (0, 300_000)})
