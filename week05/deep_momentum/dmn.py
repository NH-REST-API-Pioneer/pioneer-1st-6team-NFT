"""Deep Momentum Network — Lim·Zohren·Roberts (2019) 의 샤프 최적화 LSTM.

  입력   features.FEATS 8개 (정규화 수익률 5개 + MACD 3개), 63일 구간
  모형   LSTM(8 → h) → dropout → Linear(h → 1) → tanh = 포지션 X_t ∈ [−1, 1]   (8종목이 가중치 공유)
  손실   −샤프( R ),  R_{i,t} = X_{i,t} σ_tgt/(σ_{i,t}√252) r_{i,t+1} − c_{i,t} |Δ(X σ_tgt/(σ√252))|     [식 16·35]
         c = 실제 편도 비용률 (backtest.cost_rate) → 회전 벌점이 곧 비용
  학습   블록 시작 전 모든 데이터, 마지막 10% 를 검증. Adam, 최대 100 에폭, 검증 손실 25 에폭 개선 없으면 멈춤.
         하이퍼파라미터 무작위 탐색 N_SEARCH 회 (논문 50회). 논문 방식 = 검증 1등 모형 하나.
         채택 = 검증 상위 TOP_K 설정 × 시드 N_SEEDS 개 평균 (config 주석의 진단 참고).
  추론   날짜 t 마다 t 로 끝나는 63일 창을 넣고 마지막 출력 = X_t (상태를 넘기지 않음)
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np
import pandas as pd
import torch
from torch import nn

import backtest
import config
import features

torch.set_num_threads(4)


class Net(nn.Module):
    def __init__(self, n_in: int, hidden: int, dropout: float):
        super().__init__()
        self.drop_in = nn.Dropout(dropout)
        self.lstm = nn.LSTM(n_in, hidden, batch_first=True)
        self.drop_out = nn.Dropout(dropout)
        self.head = nn.Linear(hidden, 1)

    def forward(self, x):                       # x: (배치, 길이, 특징)
        h, _ = self.lstm(self.drop_in(x))
        return torch.tanh(self.head(self.drop_out(h))).squeeze(-1)


@dataclass
class Arrays:
    """날짜 × 종목 정렬 배열 (NaN 은 0 으로 채우고 mask 로 표시)."""
    dates: pd.DatetimeIndex
    X: np.ndarray       # (날짜, 종목, 특징)
    scale: np.ndarray   # σ_tgt/(σ√252)
    nr: np.ndarray      # 다음날 수익률
    c: np.ndarray       # 편도 비용률
    ok: np.ndarray      # 특징·σ·다음날 수익 모두 있음


def arrays(P) -> Arrays:
    sig = backtest.align(P, "sigma")
    idx, cols = sig.index, sig.columns
    X = np.stack([backtest.align(P, f).reindex(idx)[cols].values for f in features.FEATS], axis=-1)
    scale = (config.SIGMA_TGT / (sig * np.sqrt(252))).values
    nr = backtest.align(P, "next_r").reindex(idx)[cols].values
    c = backtest.cost_rate(backtest.align(P, "px")).reindex(idx)[cols].values
    ok = np.isfinite(X).all(-1) & np.isfinite(scale) & np.isfinite(nr) & np.isfinite(c)
    Xf = np.nan_to_num(np.clip(X, -10, 10))
    return Arrays(idx, Xf, np.nan_to_num(scale), np.nan_to_num(nr), np.nan_to_num(c), ok)


def chunks(A: Arrays, d0: str, d1: str, L: int = config.SEQ_LEN):
    """[d0, d1] 안을 종목별로 L 일씩 겹치지 않게 자름. 반환 (특징, scale, nr, c, ok) 텐서."""
    m = (A.dates >= d0) & (A.dates <= d1)
    ii = np.where(m)[0]
    out = []
    for j in range(A.X.shape[1]):
        good = ii[A.ok[ii, j]]
        if len(good) < L:
            continue
        good = good[good >= good[0]]
        for s in range(0, len(good) - L + 1, L):
            w = good[s:s + L]
            out.append((A.X[w, j], A.scale[w, j], A.nr[w, j], A.c[w, j]))
    if not out:
        return None
    f = lambda k: torch.tensor(np.stack([o[k] for o in out]), dtype=torch.float32)
    return f(0), f(1), f(2), f(3)


def captured(pos, scale, nr, c):
    w = pos * scale
    turn = torch.zeros_like(w)
    turn[:, 1:] = (w[:, 1:] - w[:, :-1]).abs()
    return w * nr - c * turn


def sharpe_loss(R):
    R = R.reshape(-1)
    return -(R.mean() / (R.std() + 1e-9)) * np.sqrt(252)


def train_one(tr, va, hp: dict, seed: int):
    torch.manual_seed(seed); np.random.seed(seed); random.seed(seed)
    net = Net(tr[0].shape[-1], hp["hidden"], hp["dropout"])
    opt = torch.optim.Adam(net.parameters(), lr=hp["lr"])
    n = tr[0].shape[0]
    best, best_state, bad = np.inf, None, 0
    for ep in range(config.MAX_EPOCHS):
        net.train()
        perm = torch.randperm(n)
        for b in range(0, n, hp["batch"]):
            k = perm[b:b + hp["batch"]]
            loss = sharpe_loss(captured(net(tr[0][k]), tr[1][k], tr[2][k], tr[3][k]))
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), hp["max_grad_norm"])
            opt.step()
        net.eval()
        with torch.no_grad():
            vl = float(sharpe_loss(captured(net(va[0]), va[1], va[2], va[3])))
        if vl < best - 1e-6:
            best, bad = vl, 0
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
        else:
            bad += 1
            if bad >= config.PATIENCE:
                break
    net.load_state_dict(best_state)
    return net, best, ep + 1


def sample_hps(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    return [{k: rng.choice(v) for k, v in config.GRID.items()} for _ in range(n)]


def fit_block(A: Arrays, train_start: str, train_end: str, log=print):
    days = A.dates[(A.dates >= train_start) & (A.dates <= train_end)]
    vstart = days[int(len(days) * (1 - config.VAL_FRAC))]
    tr = chunks(A, train_start, (vstart - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    va = chunks(A, vstart.strftime("%Y-%m-%d"), train_end)
    results = []
    # 탐색 시드는 블록 연도로 고정 (파이썬 hash() 는 실행마다 달라서 쓰면 결과가 재현되지 않음)
    for i, hp in enumerate(sample_hps(config.N_SEARCH, config.SEED + int(train_end[:4]))):
        net, vl, ep = train_one(tr, va, hp, seed=config.SEED + i)
        results.append((vl, i, hp, net, ep))
        log(f"    탐색 {i+1:2d}/{config.N_SEARCH} {hp} → 검증 샤프 {-vl:+.2f} ({ep} 에폭)")
    results.sort(key=lambda x: x[0])
    vl, i, hp, net, ep = results[0]
    return net, {"val_sharpe": -vl, "hp": hp, "epochs": ep, "n_train_seq": int(tr[0].shape[0]),
                 "n_val_seq": int(va[0].shape[0]), "val_start": str(vstart.date()),
                 "search": [{"hp": r[2], "val_sharpe": -r[0]} for r in results]}


def fit_ensemble(A: Arrays, info: dict, train_start: str, train_end: str):
    """fit_block 의 탐색 순위로 상위 TOP_K 설정을 시드 N_SEEDS 개씩 다시 학습."""
    vstart = pd.Timestamp(info["val_start"])
    tr = chunks(A, train_start, (vstart - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    va = chunks(A, vstart.strftime("%Y-%m-%d"), train_end)
    nets = []
    for i, r in enumerate(info["search"][:config.TOP_K]):
        for s in range(config.N_SEEDS):
            net, vl, ep = train_one(tr, va, r["hp"], seed=config.SEED + 1000 + 10 * i + s)
            nets.append(net)
    return nets


def predict_ensemble(nets, A: Arrays, d0: str, d1: str) -> pd.DataFrame:
    return sum(predict(n, A, d0, d1) for n in nets) / len(nets)


def predict(net, A: Arrays, d0: str, d1: str, L: int = config.SEQ_LEN) -> pd.DataFrame:
    """[d0, d1] 날짜마다 그날로 끝나는 L 일 창(종목별 유효 날짜만)으로 X_t."""
    net.eval()
    idx = np.where((A.dates >= d0) & (A.dates <= d1))[0]
    out = np.full((len(A.dates), A.X.shape[1]), np.nan)
    feat_ok = np.isfinite(A.scale) & (A.scale > 0) & (np.abs(A.X).sum(-1) > 0)
    with torch.no_grad():
        for j in range(A.X.shape[1]):
            valid = np.where(feat_ok[:, j])[0]
            pos = {d: k for k, d in enumerate(valid)}
            wins, tgt = [], []
            for t in idx:
                if t not in pos or pos[t] < L - 1:
                    continue
                k = pos[t]
                wins.append(A.X[valid[k - L + 1:k + 1], j]); tgt.append(t)
            if not wins:
                continue
            y = net(torch.tensor(np.stack(wins), dtype=torch.float32))[:, -1].numpy()
            out[tgt, j] = y
    return pd.DataFrame(out, index=A.dates, columns=config.SYMS).loc[d0:d1]
