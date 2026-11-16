"""5주차 설정 — 종목·비용·구간·파라미터·시간 규칙·경로를 한곳에.

값은 시험 구간을 보기 전에 고정한 것입니다 (근거: week05-전략-근거.md).
시험 구간을 돌리기 전에 바꾸지 않습니다 (verdict.py 가 이 파일의 해시를 시험 잠금 파일에 남깁니다).
모든 시각은 KST, 봉 시각 = 봉 시작.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
BARS_DIR = HERE / "data" / "bars"
MODELS_DIR = HERE / "models"
RESULTS = HERE / "results"

# 검증 묶음 8개 (자산군이 겹치지 않게)
SYMS = ["ES", "NQ", "CL", "GC", "HG", "6E", "6J", "ZN"]
ASSET = {"ES": "지수", "NQ": "지수", "CL": "에너지", "GC": "금속", "HG": "금속",
         "6E": "통화", "6J": "통화", "ZN": "금리"}
# (틱, 포인트가치 $ = tick_val / tick_unt, 마이크로 비율). 출처 week04/results/data/products_all.csv
# 6J 는 시세가 100엔당 달러(×100)로 와서 표시 단위로 환산: 틱 0.0000005×100, 포인트가치 12.5M÷100
SPEC: dict[str, tuple[float, float, int]] = {
    "ES": (0.25, 50, 10), "NQ": (0.25, 20, 10), "CL": (0.01, 1000, 10), "GC": (0.1, 100, 10),
    "HG": (0.0005, 25000, 10), "6E": (0.00005, 125000, 10), "6J": (0.00005, 125000, 10),
    "ZN": (0.015625, 1000, 1),
}
FEE_SIDE = 2.0  # 마이크로 편도 계약당 $

# 구간 (session 이 아니라 봉 시각의 KST 날짜 기준)
TRAIN = ("2026-05-01", "2026-08-31")
VAL = ("2026-09-01", "2026-09-30")
TEST_START = "2026-10-01"

# 특징·예측
H = 12                         # 예측 대상: 다음 12봉(1시간) 실현 변동성
RV_WINDOWS = (6, 12, 48, 288)
RET_LAGS = (1, 3, 6)
NORM_DAYS = 20                 # 표준화·거래량 비교 창 (거래일)
GAP_MAX = pd.Timedelta("15min")  # 이보다 긴 간격의 수익률은 버림

# 국면
PCT_DAYS = 20
HI, LO = 0.70, 0.30

# 전략
BRK_N, BRK_EXIT = 12, 6
MR_N, MR_IN, MR_STOP = 24, 2.0, 3.5
MAX_HOLD = 24

# 시간 규칙 (체결 시각 기준, 해외 모의계좌는 07:00~24:00 만 체결)
ENTRY_FILL_START = "07:00"
ENTRY_FILL_END = "23:30"
FLAT_FILL = "23:50"

# 실행기 (Task 8)
LIVE = {
    "ES": {"sig": ("ESZ26", "FCME"), "trd": ("MESZ26", "MES"), "qty": 1, "mult": 5},
    "GC": {"sig": ("GCZ26", "FCME"), "trd": ("MGCZ26", "MGC"), "qty": 1, "mult": 10},
}
