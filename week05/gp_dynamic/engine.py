"""포지션 판단 — 백테스트와 실행기가 같이 씁니다.

봉 t 마감에 신호 f_t^k, 봉당 변동성 σ_t, 공분산 Σ_t, 가격을 받아서 다음 봉 시가에 가질 계약 수를 냅니다.
  μ_t      = Σ_k β_k σ_t f_t^k                  (분수 수익률, 다음 봉)
  mode gp        목표 = (γΣ)⁻¹ Σ_k β_k σ f^k / (1+φ_k a/γ),  그림자 x ← x + (a/λ)(목표 − x)   [논문 식 10·15]
  mode static    목표 = Markowitz,                         그림자 x ← x + w (목표 − x)      [논문 식 29 비교 대상]
  mode markowitz 목표 = Markowitz,                         그림자 x ← 목표
그림자 포지션은 연속값(계약)이고, 실제 계약은 |그림자 − 실제| ≥ band 일 때만 반올림해 바꿉니다.
신호가 하나라도 없으면(NaN) 그 봉은 판단하지 않고 그대로 둡니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

import config
import gp


@dataclass
class Params:
    betas: dict[str, float]
    phis: dict[str, float]
    gamma: float = config.GAMMA
    k: float = config.K
    band: float = config.BAND
    mode: str = "gp"
    static_w: float = 0.05
    signals: tuple[str, ...] = config.SIGNALS

    def rate(self) -> float:
        if self.mode == "gp":
            return gp.rates(self.k)[1]
        if self.mode == "static":
            return self.static_w
        return 1.0

    def weights(self) -> np.ndarray:
        """신호별 (β_k × 할인). Markowitz·static 은 할인 없음."""
        b = np.array([self.betas[s] for s in self.signals])
        if self.mode != "gp":
            return b
        return b * gp.discount([self.phis[s] for s in self.signals], self.k)


@dataclass
class Stepper:
    mem: list[str]
    p: Params
    xs: np.ndarray = field(default=None)     # 그림자 포지션 (계약, 연속)
    pos: np.ndarray = field(default=None)    # 실제 계약

    def __post_init__(self):
        n = len(self.mem)
        self.xs = np.zeros(n) if self.xs is None else np.asarray(self.xs, float)
        self.pos = np.zeros(n) if self.pos is None else np.asarray(self.pos, float)
        self._w = self.p.weights()
        self._rate = self.p.rate()
        self._mult = np.array([config.CONTRACT[s][0] for s in self.mem], float)

    def aim(self, f: dict[str, np.ndarray], sig: np.ndarray, Sigma: np.ndarray, price: np.ndarray):
        """목표 계약 수 (연속). 판단할 수 없으면 None."""
        F = np.array([f[s] for s in self.p.signals])           # (신호 × 종목)
        if not (np.isfinite(F).all() and np.isfinite(sig).all() and np.isfinite(Sigma).all()
                and np.isfinite(price).all()):
            return None
        mu = sig * (self._w @ F)
        S = gp.shrink(Sigma, config.COV_SHRINK)
        try:
            dollars = gp.markowitz(mu, S, self.p.gamma)
        except np.linalg.LinAlgError:
            return None
        return dollars / (price * self._mult)

    def step(self, f, sig, Sigma, price, flat: bool = False) -> np.ndarray:
        """다음 봉에 가질 계약 수를 정하고 상태를 바꿈."""
        if flat:
            self.xs[:] = 0.0
            self.pos[:] = 0.0
            return self.pos.copy()
        tgt = self.aim(f, sig, Sigma, price)
        if tgt is None:
            return self.pos.copy()
        self.xs = self.xs + self._rate * (tgt - self.xs)
        move = np.abs(self.xs - self.pos) >= self.p.band
        self.pos = np.where(move, np.round(self.xs), self.pos)
        return self.pos.copy()
