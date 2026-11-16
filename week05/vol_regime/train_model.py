"""8종목 특징 → LightGBM 학습 → models/vol_lgbm.txt, models/preds.csv (전 구간 예측).

학습·검증 지표만 출력합니다. 시험 구간 지표는 verdict.py --period test 에서 한 번만.
  .venv/bin/python train_model.py
"""
from __future__ import annotations

import pandas as pd

import config
import data
import features
import model


def main() -> None:
    f = features.build({s: data.load_bars(s) for s in config.SYMS})
    tr, va, _ = model.split(f)
    b = model.fit(f)
    config.MODELS_DIR.mkdir(exist_ok=True)
    b.save_model(str(config.MODELS_DIR / "vol_lgbm.txt"), num_iteration=b.best_iteration)
    ok = f["norm"].notna()
    preds = pd.DataFrame({
        "pred_lgbm": model.predict(b, f), "pred_persist": model.baseline_persist(f),
        "pred_har": model.baseline_har(tr, f), "y": f["y"],
    }).where(ok, axis=0)
    preds.reset_index().to_csv(config.MODELS_DIR / "preds.csv", index=False)
    rows = []
    for name, part in (("train", tr), ("val", va)):
        p = preds.loc[part.index]
        for col in ("pred_lgbm", "pred_persist", "pred_har"):
            rows.append({"period": name, "model": col[5:], **model.metrics(part["y"], p[col])})
    out = pd.DataFrame(rows)
    config.RESULTS.mkdir(exist_ok=True)
    out.to_csv(config.RESULTS / "model_metrics_trainval.csv", index=False)
    print(f"best_iteration={b.best_iteration}")
    print(out.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    imp = pd.Series(b.feature_importance("gain"), index=b.feature_name()).sort_values(ascending=False)
    print((imp / imp.sum()).round(3).to_string())


if __name__ == "__main__":
    main()
