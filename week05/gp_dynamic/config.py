"""5주차 GP 동적 매매 설정 — 종목·비용·구간·파라미터·시간 규칙을 한곳에.

근거: ../deep_momentum/week05-DMN-전략-근거.md 2절 (장중 실험). 시험 구간을 돌린 뒤에는 바꾸지 않습니다
(verdict.py 가 이 파일과 params.json 의 해시를 시험 잠금 파일에 남깁니다).
모든 시각은 KST, 봉 시각 = 봉 시작.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
TEAM_BARS = HERE.parent / "vol_regime" / "data" / "bars"   # 팀원(은석)이 받은 5분봉 (읽기만)
BARS_DIR = HERE / "data" / "bars"                          # refresh_bars.py 가 최신까지 이어 받은 것
_ALL = ["ES", "NQ", "CL", "GC", "HG", "6E", "6J", "ZN"]
# 종목 묶음은 환경변수 GP_SYMS 로 바꿀 수 있음 (예: GP_SYMS=ES,NQ). 결과는 묶음별 폴더에 따로
SYMS = [s for s in os.environ.get("GP_SYMS", ",".join(_ALL)).split(",") if s]
RESULTS = HERE / "results" / ("all8" if SYMS == _ALL else "_".join(SYMS))

# 기본 종목 8개 = 5주차 vol_regime 과 같은 묶음 (CME 연결선물, 자산군이 겹치지 않게) — 위 _ALL
ASSET = {"ES": "지수", "NQ": "지수", "CL": "에너지", "GC": "금속", "HG": "금속",
         "6E": "통화", "6J": "통화", "ZN": "금리"}
# 매매 계약 (포인트가치 $, 틱). vol_regime/config.SPEC 의 (틱, 포인트가치, 마이크로 비율)에서 포인트가치 ÷ 비율.
# 6J 는 시세가 100엔당 달러(×100)라 표시 단위 기준. ZN 은 마이크로가 없어 일반 계약.
_SPEC = {"ES": (0.25, 50, 10), "NQ": (0.25, 20, 10), "CL": (0.01, 1000, 10), "GC": (0.1, 100, 10),
         "HG": (0.0005, 25000, 10), "6E": (0.00005, 125000, 10), "6J": (0.00005, 125000, 10),
         "ZN": (0.015625, 1000, 1)}
CONTRACT = {s: (pv / ratio, tick) for s, (tick, pv, ratio) in _SPEC.items()}
FEE_SIDE = 2.0            # 편도 계약당 $ (4주차 모의 체결에서 MGC·MSI 모두 $2 확인)
HALF_SPREAD_TICKS = 0.5   # 편도 반 틱 (왕복 1틱)

# 구간 (봉 시각의 KST 날짜) — vol_regime 과 같음
TRAIN = ("2026-05-01", "2026-08-31")
VAL = ("2026-09-01", "2026-09-30")
TEST = ("2026-10-01", "2026-11-14")

# 신호: 종목마다 자기 수익률을 시간 길이별로 표준화 (논문 식 31 의 롤링 샤프 비율과 같은 꼴)
#   fN = 직전 N 봉 수익률 합 / (σ·√N)
GAP_MAX = pd.Timedelta("15min")
VOL_WIN = 288            # 봉당 변동성 창 (하루)
VOL_FLOOR_WIN = 2016     # 변동성 바닥 = 직전 7일 중앙값 × VOL_FLOOR
VOL_FLOOR = 0.3
HORIZONS = {"f3": 3, "f12": 12, "f48": 48, "f288": 288}   # 15분 · 1시간 · 4시간 · 하루
SIGNALS = tuple(HORIZONS)
COV_WIN = 2016           # 공분산 창 (7일)
COV_SHRINK = 0.5         # 상관을 0 쪽으로 50% (논문과 같음)

# GP (논문 식 9·15). gamma = 위험회피(포지션 크기), k = λ/γ (학습 구간에서 고름)
GAMMA = 5e-4
K_GRID = (1, 3, 10, 30, 100, 300, 1000)
K = 30                   # 학습 판정 뒤 확정
BAND = 0.5               # 그림자 포지션과 실제 계약 차이가 이만큼 이상이면 반올림해 매매 (=그냥 반올림)
STATIC_W_GRID = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5)

# 시간 규칙 (체결 시각 기준). 해외 모의계좌는 KST 07:00~24:00 만 체결
TRADE_FILL_START = "07:00"
TRADE_FILL_END = "23:50"
WEEKEND_FLAT_FILL = "23:50"   # 금요일 이 시각 체결로 전부 정리
