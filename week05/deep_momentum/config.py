"""5주차 Deep Momentum Network 설정 — 종목·비용·구간·특징·학습 범위를 한곳에.

근거: week05-DMN-전략-근거.md. 재현 대상 = Lim·Zohren·Roberts (2019) "Enhancing Time Series Momentum
Strategies Using Deep Neural Networks" (arXiv 1904.04912). 비교 기준 = 변동성 조정 TSMOM·MACD(논문 기준선),
Gârleanu·Pedersen (2013) 동적 매매.
시험 구간을 돌린 뒤에는 바꾸지 않습니다 (verdict.py 가 해시를 잠금 파일에 남깁니다).
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "daily"
RESULTS = HERE / "results"

# 종목 8개 — 5주차 vol_regime 과 같은 묶음 (CME 연결선물). 조회용 대표 월물
SYMS = ["ES", "NQ", "CL", "GC", "HG", "6E", "6J", "ZN"]
SIG = {"ES": "ESZ26", "NQ": "NQZ26", "CL": "CLF27", "GC": "GCZ26", "HG": "HGZ26",
       "6E": "6EZ26", "6J": "6JZ26", "ZN": "ZNZ26"}
ASSET = {"ES": "지수", "NQ": "지수", "CL": "에너지", "GC": "금속", "HG": "금속",
         "6E": "통화", "6J": "통화", "ZN": "금리"}
# 매매 계약 (포인트가치 $, 틱) — 마이크로 (ZN 은 마이크로가 없어 일반). 출처 week04 products_all.csv
# M6E·MJY 틱은 0.0001 ($1.25). 6J 시세는 100엔당 달러(×100 표시) → MJY 포인트가치 12,500 (표시 단위)
CONTRACT = {"ES": (5, 0.25), "NQ": (2, 0.25), "CL": (100, 0.01), "GC": (10, 0.1), "HG": (2500, 0.0005),
            "6E": (12500, 0.0001), "6J": (12500, 0.0001), "ZN": (1000, 0.015625)}
FEE_SIDE = 2.0             # 편도 계약당 $ (4주차 모의 체결 실측)
HALF_SPREAD_TICKS = 0.5    # 편도 반 틱

# 수익률·위험 (논문 식 1)
SIGMA_TGT = 0.15           # 종목별 연 변동성 목표
VOL_SPAN = 60              # 사전 변동성 = 일 수익률 EWM 표준편차 (span 60일)
WINSOR_K, WINSOR_HL = 5, 252   # 수익률을 EWM 평균 ± 5 EWM 표준편차로 자름 (반감기 252일)

# 특징 (논문 V-B): 정규화 수익률 + MACD
RET_WINDOWS = (1, 21, 63, 126, 252)
MACD_PAIRS = ((8, 24), (16, 48), (32, 96))

# 구간 — 5년 블록 전진 학습 (논문처럼 블록 시작마다 그때까지 데이터로 처음부터 다시 학습)
START = "2002-01-01"                     # 특징 준비(1년+)를 위해 그 전 데이터는 창으로만 씀
BLOCKS = [("2011-01-01", "2015-12-31"), ("2016-01-01", "2020-12-31")]   # 개발용 표본 밖 블록
TEST = ("2021-01-01", "2026-11-13")                                        # 시험 (한 번만)

# DMN 학습 (논문 부록 B 범위에서 줄인 무작위 탐색)
SEQ_LEN = 63
MAX_EPOCHS = 100
PATIENCE = 25
VAL_FRAC = 0.1
N_SEARCH = 12              # 논문은 50회. 노트북 시간 때문에 줄임 (근거 문서에 적음)
GRID = {
    "dropout": [0.1, 0.2, 0.3, 0.4, 0.5],
    "hidden": [5, 10, 20, 40, 80],
    "batch": [64, 128, 256],             # 논문 256~2048 은 표본 단위가 달라서 (우리는 63일 구간 단위)
    "lr": [1e-4, 1e-3, 1e-2],
    "max_grad_norm": [1e-2, 1e-1, 1.0, 10.0],
}
SEED = 7
# 최종 DMN = 검증 상위 TOP_K 설정 × 시드 N_SEEDS 개 모형의 포지션 평균 (2026-11-16 진단: 8종목이라 검증 1등
# 하나는 시드만 바꿔도 표본 밖 샤프가 −0.37~+0.20 으로 흔들림. 검증 구간이 7개월~1년 반이라 고르는 게 잡음)
TOP_K, N_SEEDS = 3, 3
