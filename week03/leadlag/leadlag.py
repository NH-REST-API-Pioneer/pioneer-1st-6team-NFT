"""KRX 달러선물 → MKU 선행 검증 (근거 문서 4절).

사용법 (week03/leadlag/ 에서):
  python3 leadlag.py                                   # 10/13~10/30, 09:00~15:30
  python3 leadlag.py --start 20261015 --move 0.3

봉 시각 기준: 두 시장 모두 「그 분의 시작」입니다 (2026-11-02 확인 — MKU 15:38:35 체결이 153800 봉,
15:40:08 체결이 154000 봉에 들어감. 국내는 week02 에서 확인). 그래서 같은 시각 봉끼리 바로 비교합니다.

검증 세 가지:
  A. 빈 분을 직전 가격으로 채운 상관 (근거 문서 4-1절)
  B. MKU 가 이번 분·앞 분 모두 체결된 경우만 쓴 상관 — 비동시 거래 착시 확인
  C. KRX 1분 움직임이 --move 원 이상인 「충격」 뒤 MKU 가 KRX 방향으로 몇 틱 더 가는지 — 틱 크기 보정 + 비용 비교
MKU 는 원/달러(1000/가격)로 바꿔 비교합니다. C 의 틱은 MKU 틱(0.0001)입니다.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as st
from pathlib import Path

HERE = Path(__file__).parent
KRX_FILES = [HERE.parent / "week02" / "results" / "data" / f
             for f in ("bars_1m_A756B000.csv", "bars_1m_A756B000_20261030.csv")]
MKU_TICK = 0.0001


def load(paths) -> dict[tuple[str, str], tuple[float, float]]:
    """(날짜, HHMM) → (시가, 종가). 체결이 없던 분은 없습니다."""
    out = {}
    for p in paths:
        for r in csv.DictReader(Path(p).open(encoding="utf-8")):
            out[(r["ref_dt"], r["exch_tm"][:4])] = (float(r["open_pric"]), float(r["close_pric"]))
    return out


def minutes(a: str = "0900", b: str = "1530") -> list[str]:
    ts = []
    for m in range(int(a[:2]) * 60 + int(a[2:]), int(b[:2]) * 60 + int(b[2:]) + 1):
        ts.append(f"{m // 60:02d}{m % 60:02d}")
    return ts


def corr(xs, ys) -> float:
    mx, my = st.mean(xs), st.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))


def tstat(v) -> float:
    return st.mean(v) / (st.stdev(v) / math.sqrt(len(v))) if len(v) > 2 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20261013")
    ap.add_argument("--end", default="20261030")
    ap.add_argument("--move", type=float, default=0.3, help="KRX 충격 기준 (원). MKU 1틱 ≈ 0.18원")
    args = ap.parse_args()

    krx = load(KRX_FILES)
    mku = load([HERE / "results" / "data" / "bars_1m_MKUX26.csv"])
    days = sorted({d for d, _ in krx if args.start <= d <= args.end} & {d for d, _ in mku})
    ts = minutes()
    lags = range(-3, 4)
    a_x, a_y = {k: [] for k in lags}, {k: [] for k in lags}
    b_x, b_y = {k: [] for k in lags}, {k: [] for k in lags}
    horizons = (0, 1, 2, 3, 5)
    follow = {h: [] for h in horizons}   # 충격 뒤 MKU 가 KRX 방향으로 간 틱 (h=0 은 같은 분)
    n_events = 0

    for d in days:
        k_close, m_close, m_traded = [], [], []
        lk = lm = None
        for t in ts:
            if (d, t) in krx:
                lk = krx[(d, t)][1]
            if (d, t) in mku:
                lm = mku[(d, t)][1]
            k_close.append(lk)
            m_close.append(lm)
            m_traded.append((d, t) in mku)
        rk = [None] + [math.log(k_close[i] / k_close[i - 1]) if k_close[i] and k_close[i - 1] else None
                       for i in range(1, len(ts))]
        # MKU 를 원/달러로: log(1000/p) 차분 = -log(p) 차분
        rm = [None] + [-math.log(m_close[i] / m_close[i - 1]) if m_close[i] and m_close[i - 1] else None
                       for i in range(1, len(ts))]
        for i in range(len(ts)):
            for k in lags:
                j = i - k
                if not (0 <= j < len(ts)) or rk[j] is None or rm[i] is None:
                    continue
                a_x[k].append(rk[j]); a_y[k].append(rm[i])
                if m_traded[i] and i > 0 and m_traded[i - 1]:
                    b_x[k].append(rk[j]); b_y[k].append(rm[i])
        # C. 충격: 분 i 동안 KRX 가 move 원 이상 움직임 (종가 i-1 → i)
        for i in range(1, len(ts) - max(horizons)):
            if k_close[i] is None or k_close[i - 1] is None or m_close[i - 1] is None:
                continue
            dk = k_close[i] - k_close[i - 1]
            if abs(dk) < args.move:
                continue
            n_events += 1
            sign = 1 if dk > 0 else -1          # KRX 상승(원화 약세) → MKU 하락 기대
            for h in horizons:
                base = m_close[i - 1] if h == 0 else m_close[i]
                tgt = m_close[i + h] if h else m_close[i]
                follow[h].append(-sign * (tgt - base) / MKU_TICK)

    print(f"세션 {len(days)}개 ({days[0]} ~ {days[-1]}), 09:00~15:30")
    print("\nA. 빈 분 채움 상관  /  B. MKU 체결 분만 상관   (lag +k = KRX 가 k분 먼저)")
    res = {"days": days, "A": {}, "B": {}, "C": {}}
    for k in lags:
        ca, cb = corr(a_x[k], a_y[k]), corr(b_x[k], b_y[k])
        res["A"][k], res["B"][k] = round(ca, 4), round(cb, 4)
        print(f"  lag {k:+d}:  A {ca:+.3f} (n={len(a_x[k])})   B {cb:+.3f} (n={len(b_x[k])})")

    print(f"\nC. KRX 1분 충격 |Δ| ≥ {args.move}원 → MKU 가 KRX 방향으로 간 틱 (MKU 1틱 = $2.50)")
    print(f"   충격 {n_events}건")
    for h in horizons:
        v = follow[h]
        label = "같은 분(충격 분 안)" if h == 0 else f"충격 뒤 {h}분"
        hit = sum(x > 0 for x in v) / len(v) * 100
        res["C"][h] = {"mean_ticks": round(st.mean(v), 3), "t": round(tstat(v), 2), "hit_pct": round(hit, 1), "n": len(v)}
        print(f"  {label:<14} 평균 {st.mean(v):+.2f}틱  t={tstat(v):+.2f}  같은 방향 {hit:.0f}%")
    out = HERE / "results" / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    (out / "leadlag.json").write_text(json.dumps({**res, "move": args.move, "events": n_events},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  저장: {(out / 'leadlag.json').relative_to(HERE)}")


if __name__ == "__main__":
    main()
