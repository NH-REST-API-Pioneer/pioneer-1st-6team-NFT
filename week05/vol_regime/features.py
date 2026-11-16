"""변동성 특징과 예측 대상 — 백테스트와 실행기가 같이 씁니다.

입력: data.clean 형식 봉 (o h l c v r gap roll). r 의 NaN(간격·롤)은 제곱합에서 0 으로 칩니다.
모든 특징은 봉 t 까지의 정보만 씁니다.

  norm     그 종목 직전 NORM_DAYS 거래일(당일 제외)의 「하루 rv12 중앙값」들의 중앙값 (표준화 기준)
  rvk      √(직전 k봉 r² 합) ÷ norm,  k ∈ RV_WINDOWS
  ark      |직전 k봉 r 합| ÷ norm,     k ∈ RET_LAGS
  vratio   v ÷ 같은 HH:MM 봉들의 직전 28일(≈ NORM_DAYS 거래일, 당일 제외) 평균 v. 개수가 아니라 기간으로 세서
           가끔만 있는 시각(06:00 봉 등)도 받아 온 기간 길이와 상관없이 같은 값 (실행기 = 백테스트)
  hour minute dow   KST 시각
  es_rv12  같은 시각 ES 의 rv12 (15분 안에 없으면 NaN)
  sym_code config.SYMS 안의 번호 (범주형)
  y        log(√(다음 H봉 r² 합) ÷ norm). 다음 H봉 안에 gap·roll 이 있거나 H봉이 안 남았으면 NaN
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
import session

FEATURES = [f"rv{k}" for k in config.RV_WINDOWS] + [f"ar{k}" for k in config.RET_LAGS] + \
    ["vratio", "hour", "minute", "dow", "es_rv12", "sym_code"]


VRATIO_WIN = pd.Timedelta("28D")


def _rv_raw(r2: pd.Series, k: int) -> pd.Series:
    return np.sqrt(r2.rolling(k, min_periods=k).sum())


def _norm(rv12: pd.Series, day: pd.Series) -> pd.Series:
    daily = rv12.groupby(day).median()
    prior = daily.shift(1).rolling(config.NORM_DAYS, min_periods=config.NORM_DAYS).median()
    return day.map(prior)


def one(df: pd.DataFrame) -> pd.DataFrame:
    """종목 하나의 특징 (es_rv12·sym_code 제외) + y + norm."""
    r = df["r"]
    r2 = r.fillna(0.0) ** 2
    day = pd.Series(session.session_day(df.index), index=df.index)
    rv12 = _rv_raw(r2, 12)
    norm = _norm(rv12, day)
    out = pd.DataFrame(index=df.index)
    for k in config.RV_WINDOWS:
        out[f"rv{k}"] = (rv12 if k == 12 else _rv_raw(r2, k)) / norm
    for k in config.RET_LAGS:
        out[f"ar{k}"] = r.fillna(0.0).rolling(k, min_periods=k).sum().abs() / norm
    tod = df.index.strftime("%H:%M")
    prev_v = df["v"].groupby(tod).transform(
        lambda v: v.rolling(VRATIO_WIN, closed="left", min_periods=5).mean())
    out["vratio"] = df["v"] / prev_v
    out["hour"], out["minute"], out["dow"] = df.index.hour, df.index.minute, df.index.dayofweek
    h = config.H
    bad = (df["gap"] | df["roll"]).astype(float)
    fwd_bad = bad[::-1].rolling(h, min_periods=h).sum()[::-1].shift(-1)
    fwd_r2 = r2[::-1].rolling(h, min_periods=h).sum()[::-1].shift(-1)
    with np.errstate(divide="ignore"):
        y = np.log(np.sqrt(fwd_r2) / norm)
    y[(fwd_bad != 0) | fwd_bad.isna() | (fwd_r2 <= 0)] = np.nan
    out["y"], out["norm"] = y, norm
    return out


def build(bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = {s: one(df) for s, df in bars.items()}
    es = parts["ES"]["rv12"] if "ES" in parts else None
    for s, f in parts.items():
        if es is None:
            f["es_rv12"] = np.nan
        elif s == "ES":
            f["es_rv12"] = f["rv12"]
        else:
            f["es_rv12"] = es.reindex(f.index, method="ffill", tolerance=config.GAP_MAX)
        f["sym_code"] = config.SYMS.index(s) if s in config.SYMS else -1
    out = pd.concat(parts, names=["sym", "ts"])
    return out[FEATURES + ["y", "norm"]]
