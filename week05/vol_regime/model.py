"""LightGBM 변동성 예측과 기준선 2개.

예측 대상은 features.y (다음 12봉 실현 변동성의 로그, 종목별 표준화). 8종목을 한 모델로 학습.
구간: 학습으로 학습, 검증으로 조기 종료·하이퍼파라미터. 시험 구간은 여기서 보지 않습니다.
"""
from __future__ import annotations

from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

import config
from features import FEATURES

PARAMS = {
    "objective": "regression", "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 500,
    "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
    "seed": 0, "deterministic": True, "num_threads": 4, "verbose": -1,
}
ROUNDS, EARLY = 3000, 100
# 기준선 log 의 하한 (표준화 단위). 12봉 동안 가격이 안 움직인 봉(학습 0.01%)에서 log 0 으로
# 기준선이 망가지지 않게 학습 구간 rv12 0.1% 분위수(0.204, 2026-11-16 실측)로 고정.
RV_FLOOR = 0.2


def _ts(df: pd.DataFrame) -> pd.DatetimeIndex:
    return df.index.get_level_values("ts")


def split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """y·norm 이 있는 행만, 봉 시각 날짜로 학습/검증/시험."""
    d = df[df["y"].notna() & df["norm"].notna()]
    t = _ts(d)
    tr = d[(t >= config.TRAIN[0]) & (t < pd.Timestamp(config.TRAIN[1]) + pd.Timedelta("1D"))]
    va = d[(t >= config.VAL[0]) & (t < pd.Timestamp(config.VAL[1]) + pd.Timedelta("1D"))]
    te = d[t >= config.TEST_START]
    return tr, va, te


def _ds(d: pd.DataFrame, ref=None) -> lgb.Dataset:
    return lgb.Dataset(d[FEATURES], label=d["y"], categorical_feature=["sym_code"], reference=ref,
                       free_raw_data=False)


def fit(df: pd.DataFrame) -> lgb.Booster:
    tr, va, _ = split(df)
    dtr = _ds(tr)
    dva = _ds(va, dtr)
    return lgb.train(PARAMS, dtr, num_boost_round=ROUNDS, valid_sets=[dva],
                     callbacks=[lgb.early_stopping(EARLY, verbose=False)])


def load(path: Path | str) -> lgb.Booster:
    return lgb.Booster(model_file=str(path))


def predict(booster: lgb.Booster, df: pd.DataFrame) -> pd.Series:
    return pd.Series(booster.predict(df[FEATURES], num_iteration=booster.best_iteration or None),
                     index=df.index, name="pred_lgbm")


def baseline_persist(df: pd.DataFrame) -> pd.Series:
    """지난 12봉 실현 변동성이 다음 12봉에도 그대로."""
    return np.log(df["rv12"].clip(lower=RV_FLOOR)).rename("pred_persist")


def _har_x(df: pd.DataFrame) -> np.ndarray:
    cols = [np.log(df[c].clip(lower=RV_FLOOR)).values for c in ("rv12", "rv48", "rv288")]
    return np.column_stack([np.ones(len(df))] + cols)


def baseline_har(df_train: pd.DataFrame, df: pd.DataFrame) -> pd.Series:
    """HAR형: y ~ 1 + log rv12 + log rv48 + log rv288, 학습 구간 최소제곱."""
    tr = df_train.dropna(subset=["rv12", "rv48", "rv288", "y"])
    beta, *_ = np.linalg.lstsq(_har_x(tr), tr["y"].values, rcond=None)
    return pd.Series((_har_x(df) * beta).sum(axis=1), index=df.index, name="pred_har")  # @ 는 macOS numpy 에서 가짜 경고


def metrics(y: pd.Series, yhat: pd.Series) -> dict:
    """로그 MSE 와 QLIKE(분산 기준: exp(2d) − 2d − 1, d = y − ŷ). 과소 예측을 더 무겁게 봅니다."""
    m = y.notna() & pd.Series(yhat, index=y.index).notna()
    d = (y[m] - pd.Series(yhat, index=y.index)[m]).values
    return {"n": int(m.sum()), "mse": float(np.mean(d ** 2)), "qlike": float(np.mean(np.exp(2 * d) - 2 * d - 1))}
