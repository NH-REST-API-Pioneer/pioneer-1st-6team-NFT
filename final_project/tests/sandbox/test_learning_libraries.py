"""전략 이미지의 학습 라이브러리(scipy, scikit-learn, statsmodels, lightgbm, PyTorch)가 격리 안에서
돌고, 같은 입력에 같은 출력을 내고, 격리를 넓히지 않는지. 실제 Docker로 돈다.
"""

import asyncio
from decimal import Decimal

import pytest

from nh_mcp.domain import SandboxResult
from nh_mcp.sandbox.container import run_in_container

from .wire import SYMBOL, bars, job

pytestmark = pytest.mark.docker

# 이벤트마다 라이브러리 하나를 작은 데이터(y = 1 + 2x)에 쓰고, 결과값을 가격 칸에 실어 보낸다
# (0.05 틱의 배수, 값에 10^6을 곱해 반올림). 목표 포지션은 지금 포지션(0)이라 주문은 나지 않는다.
# 기대값과 다르면 예외를 내 실행이 exception으로 끝난다.
LEARNING = """
from decimal import Decimal

import numpy as np

X = np.arange(8.0).reshape(-1, 1)
Y = 1.0 + 2.0 * X[:, 0]
SEEN = []


def init(ctx):
    global torch, linear_model, lightgbm, sm, stats
    import torch
    import sklearn.linear_model as linear_model
    import lightgbm
    import statsmodels.api as sm
    import scipy.stats as stats


def fit_torch():
    # 시드는 주지 않는다. torch를 불러올 때(init) 하네스가 0으로 걸었다.
    model = torch.nn.Linear(1, 1)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.02)
    x = torch.tensor(X, dtype=torch.float32)
    y = torch.tensor(Y, dtype=torch.float32).reshape(-1, 1)
    first = None
    for _ in range(200):
        optimizer.zero_grad()
        loss = torch.nn.functional.mse_loss(model(x), y)
        first = loss.item() if first is None else first
        loss.backward()
        optimizer.step()
    assert loss.item() < first / 10, (first, loss.item())
    with torch.no_grad():
        return model(torch.tensor([[10.0]])).item()


def fit_sklearn():
    model = linear_model.LinearRegression().fit(X, Y)
    assert abs(model.coef_[0] - 2.0) < 1e-9 and abs(model.intercept_ - 1.0) < 1e-9
    return float(model.predict([[10.0]])[0])


def fit_lightgbm():
    params = {"objective": "regression", "min_data_in_leaf": 1, "min_data_in_bin": 1,
              "num_leaves": 3, "deterministic": True, "verbose": -1}
    data = lightgbm.Dataset(X, Y, params={"min_data_in_bin": 1, "verbose": -1})
    booster = lightgbm.train(params, data, num_boost_round=2)
    assert booster.num_trees() == 2
    return float(booster.predict(np.array([[7.0]]))[0])


def fit_statsmodels():
    fit = sm.OLS(Y, sm.add_constant(X)).fit()
    assert np.allclose(fit.params, [1.0, 2.0])
    return float(fit.params.sum())


def use_scipy():
    value = float(stats.norm.cdf(1.0))
    assert abs(value - 0.8413447460685429) < 1e-12
    return value


STEPS = [fit_torch, fit_sklearn, fit_lightgbm, fit_statsmodels, use_scipy]


def on_event(event, ctx):
    value = STEPS[len(SEEN)]()
    SEEN.append(value)
    price = max(1, round(abs(value) * 1_000_000)) * Decimal("0.05")
    return {"A": {"position": 0, "price": str(price)}}
"""


def run(code: str, image: str, events: int) -> SandboxResult:
    return asyncio.run(run_in_container(job(code, bars(events)), image))


def prices(reply: SandboxResult) -> list[Decimal | None]:
    return [None if out is None else out[SYMBOL].price for out in reply.outputs]


def test_each_library_trains_and_predicts_the_same_way_twice(sandbox_image: str) -> None:
    first = run(LEARNING, sandbox_image, events=5)
    second = run(LEARNING, sandbox_image, events=5)
    assert first.status == "ok", first
    assert first.outputs == second.outputs
    torch_value, sklearn_value, _, statsmodels_value, scipy_value = prices(first)
    # y = 1 + 2x에서 x = 10의 예측은 21, OLS 계수의 합은 1 + 2, 표준정규 CDF(1)는 0.841345.
    assert sklearn_value == 21_000_000 * Decimal("0.05")
    assert statsmodels_value == 3_000_000 * Decimal("0.05")
    assert scipy_value == 841_345 * Decimal("0.05")
    # 학습한 torch 모델도 21 가까이 맞힌다.
    assert torch_value is not None
    assert abs(torch_value / Decimal("0.05") / 1_000_000 - 21) < 1


def test_torch_cannot_download_or_write_outside_tmp(sandbox_image: str) -> None:
    code = """
import torch

URL = "https://download.pytorch.org/models/resnet18-f37072fd.pth"

def on_event(event, ctx):
    try:
        torch.hub.load_state_dict_from_url(URL, model_dir="/tmp/hub", progress=False)
    except OSError:
        pass
    else:
        return {"A": {"position": 1}}
    for path in ("/sandbox/m.pt", "/harness/m.pt", "/m.pt"):
        try:
            torch.save(torch.zeros(1), path)
        except (OSError, RuntimeError):
            continue
        return {"A": {"position": 1}}
    # /tmp에는 모델을 저장하고 다시 읽을 수 있다(모든 쓰기가 막혀서 실패한 것이 아님을 확인).
    torch.save(torch.ones(2), "/tmp/m.pt")
    assert torch.load("/tmp/m.pt", weights_only=True).tolist() == [1.0, 1.0]
    return None
"""
    reply = run(code, sandbox_image, events=1)
    assert reply == SandboxResult("ok", [None], None, "")


# torch는 프로세스마다 다른 시드로 시작한다. 전략이 시드를 주지 않고 모델을 만들어도 하네스가 import
# 순간에 0을 걸었으면 실행마다 같은 가중치가 나온다. 가중치는 가격 칸에 실어 보낸다.
TORCH_AT_TOP = """
from decimal import Decimal

import torch

assert torch.initial_seed() == 0
MODEL = torch.nn.Linear(1, 1)


def on_event(event, ctx):
    value = MODEL.weight.item() + torch.rand(1).item()
    price = max(1, round(abs(value) * 1_000_000)) * Decimal("0.05")
    return {"A": {"position": 0, "price": str(price)}}
"""

TORCH_IN_INIT = """
from decimal import Decimal


def init(ctx):
    global torch, MODEL
    import torch

    assert torch.initial_seed() == 0
    MODEL = torch.nn.Linear(1, 1)


def on_event(event, ctx):
    value = MODEL.weight.item() + torch.rand(1).item()
    price = max(1, round(abs(value) * 1_000_000)) * Decimal("0.05")
    return {"A": {"position": 0, "price": str(price)}}
"""


@pytest.mark.parametrize("code", [TORCH_AT_TOP, TORCH_IN_INIT], ids=["top-level", "init"])
def test_unseeded_torch_model_is_the_same_every_run(sandbox_image: str, code: str) -> None:
    first = run(code, sandbox_image, events=3)
    second = run(code, sandbox_image, events=3)
    assert first.status == "ok", first
    assert first.outputs == second.outputs
    # 이벤트마다 torch.rand가 새 값을 내므로 같은 실행 안의 출력은 서로 다르다.
    assert len(set(prices(first))) == 3


# 하네스는 torch를 불러오지 않는다(불러오는 데 약 0.5초). 불러왔다면 목표 1을 낸다.
NO_TORCH = """
import sys

LOADED = "torch" in sys.modules


def on_event(event, ctx):
    return {"A": {"position": 1}} if LOADED or "torch" in sys.modules else None
"""


def test_harness_does_not_import_torch(sandbox_image: str) -> None:
    assert run(NO_TORCH, sandbox_image, events=3) == SandboxResult(
        "ok", [None, None, None], None, ""
    )
