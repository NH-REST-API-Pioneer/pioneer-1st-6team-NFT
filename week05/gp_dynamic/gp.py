"""Gârleanu·Pedersen (2013) 최적 동적 매매 — 식 번호는 NBER WP 15205 기준.

모형
  r_{t+1} = B f_t + u,   Δf_{t+1} = −Φ f_t + ε,   비용 TC = ½ Δx' Λ Δx,  가정 A: Λ = λΣ
해 (명제 2·4, Φ 대각)
  a      = [−(γ(1−ρ)+λρ) + √((γ(1−ρ)+λρ)² + 4γλ(1−ρ)²)] / (2(1−ρ))         (9)
  x_t    = (1 − a/λ) x_{t−1} + (a/λ) aim_t                                      (10)
  aim_t  = (γΣ)⁻¹ Σ_k B_k f_t^k / (1 + φ_k a/γ)                                (15)
  Markowitz_t = (γΣ)⁻¹ B f_t                                                    (11)

ρ = 0 이면 a/γ 와 a/λ 는 k = λ/γ 에만 달려서, 포지션 크기(γ)와 매매 속도(k)를 따로 정할 수 있습니다.
"""
from __future__ import annotations

import numpy as np


def a_coef(gamma: float, lam: float, rho: float = 0.0) -> float:
    """식 (9)."""
    g = gamma * (1 - rho) + lam * rho
    return (-g + np.sqrt(g * g + 4 * gamma * lam * (1 - rho) ** 2)) / (2 * (1 - rho))


def rates(k: float, rho: float = 0.0) -> tuple[float, float]:
    """γ=1, λ=k 로 둔 (a/γ, a/λ). 앞은 신호 할인, 뒤는 매매 속도."""
    a = a_coef(1.0, k, rho)
    return a, a / k


def discount(phis, k: float) -> np.ndarray:
    """신호별 할인 1/(1 + φ_k a/γ) — 빨리 사라지는 신호일수록 많이 깎임 (명제 4 iii)."""
    a_g, _ = rates(k)
    return 1.0 / (1.0 + np.asarray(phis, dtype=float) * a_g)


def markowitz(mu: np.ndarray, Sigma: np.ndarray, gamma: float) -> np.ndarray:
    """(γΣ)⁻¹ μ. μ·Σ 가 분수 수익률이면 결과는 달러 노출."""
    return np.linalg.solve(Sigma, mu) / gamma


def shrink(Sigma: np.ndarray, w: float) -> np.ndarray:
    """상관을 0 쪽으로 w 만큼 (대각은 그대로)."""
    d = np.diag(np.diag(Sigma))
    return (1 - w) * Sigma + w * d
