"""5주차 GP 동적 매매 설정 — 종목·비용·구간·파라미터·시간 규칙을 한곳에.

근거: week05-GP-전략-근거.md. 값은 시험 구간(10/01~)을 보기 전에 고정했고, 시험 구간을 돌린 뒤에는
바꾸지 않습니다 (verdict.py 가 이 파일의 해시를 시험 잠금 파일에 남깁니다).
모든 시각은 KST, 봉 시각 = 봉 시작.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
W4 = HERE.parent.parent / "week04"
BARS_DIR = HERE / "data" / "bars"          # refresh_bars.py 가 최신까지 이어 받은 파일
RESULTS = HERE / "results"

# 원본 5분봉 (팀 저장소에 이미 있는 것). data/bars/ 에 같은 이름이 있으면 그쪽을 먼저 씀
SRC = {
    **{s: W4 / "results" / "data" / "bars_5m_cont" / f"{s}.csv" for s in ["ES", "NQ", "YM", "RTY", "GC", "SI"]},
    **{s: W4 / "coint_fullday" / "data" / f"{s}.csv" for s in ["6A", "6B", "6C", "6E", "6J", "6S"]},
}
# 묶음. 계수 추정은 세 묶음 12종목을 한데 묶어서(논문처럼 pooled), 매매는 MAIN 묶음만
GROUPS = {"met": ["GC", "SI"], "idx": ["ES", "NQ", "YM", "RTY"], "fx": ["6A", "6B", "6C", "6E", "6J", "6S"]}
MAIN = "met"
COMPARE = "idx"      # 같은 규칙을 그대로 적용해 보는 비교 묶음 (매매 안 함)

# 마이크로 계약: (포인트가치 $, 틱). 출처 week04/results/data/products_all.csv
# 6J 시세는 100엔당 달러(×100 표시) → MJY 1,250,000엔 = 표시 단위 포인트가치 12,500, 틱 0.000001×100
MICRO = {
    "GC": (10, 0.1), "SI": (1000, 0.005),
    "ES": (5, 0.25), "NQ": (2, 0.25), "YM": (0.5, 1.0), "RTY": (5, 0.1),
    "6A": (10000, 0.0001), "6B": (6250, 0.0001), "6C": (10000, 0.0001), "6E": (12500, 0.0001),
    "6J": (12500, 0.0001), "6S": (12500, 0.0001),
}
FEE_SIDE = 2.0     # 마이크로 편도 계약당 $ (5주차 vol_regime 과 같은 가정)
HALF_SPREAD_TICKS = 0.5   # 편도 반 틱 (왕복 1틱)

# 구간 (봉 시각의 KST 날짜)
TRAIN = ("2026-05-01", "2026-08-31")
VAL = ("2026-09-01", "2026-09-30")
TEST = ("2026-10-01", "2026-11-14")

# 신호
GAP_MAX = pd.Timedelta("15min")
VOL_WIN = 288          # 봉당 변동성 창 (하루)
VOL_FLOOR_WIN = 2016   # 변동성 바닥 = 직전 7일 중앙값 × VOL_FLOOR
VOL_FLOOR = 0.3
FAST_N = 3             # 빠른 신호: 직전 15분 수익률 (표준화)
RESID_L = 72           # 느린 신호: 직전 6시간 창 잔차 z
SIGNALS = ("f3", "z72")
COV_WIN = 2016         # 공분산 창 (7일)
COV_SHRINK = 0.5       # 상관을 0 쪽으로 50% (논문과 같음)

# GP (논문 식 9·15). gamma = 위험회피(포지션 크기), k = λ/γ (비용 대비 위험회피, 학습 구간에서 고름)
GAMMA = 5e-4           # 처음에 정한 크기. 금 1~3계약 수준
K_GRID = (1, 3, 10, 30, 100, 300, 1000)
K = 30                 # 학습 구간 1등 (근거 문서 5절)
BAND = 0.5             # 그림자 포지션과 실제 계약 차이가 이만큼 이상이면 반올림해 매매 (=그냥 반올림)
STATIC_W_GRID = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5)

# 시간 규칙 (체결 시각 기준). 해외 모의계좌는 KST 07:00~24:00 만 체결
TRADE_FILL_START = "07:00"
TRADE_FILL_END = "23:50"
WEEKEND_FLAT_FILL = "23:50"   # 금요일 이 시각 체결로 전부 정리 (주말 너머로 안 들고 감)

# 실행기
LIVE = {
    "GC": {"sig": ("GCZ26", "FCME"), "trd": ("MGCZ26", "MGC")},
    "SI": {"sig": ("SIZ26", "FCME"), "trd": ("MSIZ26", "MSI")},
}
