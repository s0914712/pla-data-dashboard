#!/usr/bin/env python3
"""Champion/challenger ablation for Classifier 4.0 Phase 1.

Runs three comparable feature sets on the same walk-forward splits:
  A. production feature set
  B. production + temporal/regime candidates
  C. production + temporal/regime + naval candidates

No production files are written. Results go to stdout unless --output is given.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from pla_surge_model import SURGE_PARAMS, SURGE_THRESHOLD, build_features, feature_columns, to_daily_series
from scripts.analysis.classifier4_features import (
    build_classifier4_features,
    build_naval_features,
    build_temporal_features,
    to_daily_frame,
)

DEFAULT_DATA = Path("data/JapanandBattleship.csv")
RANDOM_STATE = 42


def _metrics(y: np.ndarray, p: np.ndarray, alert_rate: float = 0.20) -> dict:
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    if len(np.unique(y)) < 2:
        return {"n": int(len(y)), "positives": int(y.sum()), "error": "single-class test set"}

    k = max(1, int(np.ceil(len(p) * alert_rate)))
    alert_idx = np.argsort(p)[-k:]
    tp = int(y[alert_idx].sum())
    positives = int(y.sum())
    recall = tp / positives if positives else 0.0
    actual_alert_rate = k / len(y)

    base_rate = float(y.mean())
    brier = float(brier_score_loss(y, p))
    base_brier = float(np.mean((y - base_rate) ** 2))
    brier_skill = 1.0 - brier / base_brier if base_brier > 0 else None

    return {
        "n": int(len(y)),
        "positives": positives,
        "base_rate": base_rate,
        "roc_auc": float(roc_auc_score(y, p)),
        "pr_auc": float(average_precision_score(y, p)),
        "brier": brier,
        "brier_skill": brier_skill,
        "alert_rate": actual_alert_rate,
        "recall_at_alert_budget": recall,
        "recall_over_alert_rate": recall / actual_alert_rate if actual_alert_rate else None,
    }


def _walk_forward(frame: pd.DataFrame, features: list[str], test_days=365, refit_every=30) -> dict:
    trainable = frame[frame["_target"].notna()].copy()
    trainable = trainable[trainable["_target_date"].notna()]
    if len(trainable) <= test_days + 400:
        raise ValueError(f"not enough rows for {test_days}-day OOS window: {len(trainable)}")

    start = len(trainable) - test_days
    probs = []
    labels = []
    model = None

    for i in range(start, len(trainable)):
        if model is None or (i - start) % refit_every == 0:
            history = trainable.iloc[:i]
            X_train = history[features].values
            y_train = (history["_target"].values >= SURGE_THRESHOLD).astype(int)
            model = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(X_train, y_train)

        row = trainable.iloc[[i]]
        prob = float(model.predict_proba(row[features].values)[0, 1])
        probs.append(prob)
        labels.append(int(row["_target"].iloc[0] >= SURGE_THRESHOLD))

    return _metrics(np.asarray(labels), np.asarray(probs))


def build_experiment_frame(raw: pd.DataFrame, horizon=1) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    series = to_daily_series(raw)
    prod = build_features(series, horizon=horizon, threshold=SURGE_THRESHOLD)

    daily = to_daily_frame(raw)
    temporal = build_temporal_features(daily)
    naval = build_naval_features(daily)

    frame = prod.join(temporal, how="left").join(naval, how="left")
    prod_cols = feature_columns(prod)
    temporal_cols = [c for c in temporal.columns if c in frame.columns]
    naval_cols = [c for c in naval.columns if c in frame.columns]

    groups = {
        "A_production": prod_cols,
        "B_plus_temporal": prod_cols + temporal_cols,
        "C_plus_temporal_naval": prod_cols + temporal_cols + naval_cols,
    }
    return frame, groups


def run(data_path: Path, windows=(365, 730, 1285), refit_every=30) -> dict:
    raw = pd.read_csv(data_path)
    frame, groups = build_experiment_frame(raw)
    out = {"surge_threshold": SURGE_THRESHOLD, "windows": {}, "feature_counts": {}}
    for name, cols in groups.items():
        out["feature_counts"][name] = len(cols)

    for days in windows:
        out["windows"][str(days)] = {}
        for name, cols in groups.items():
            try:
                out["windows"][str(days)][name] = _walk_forward(
                    frame, cols, test_days=days, refit_every=refit_every
                )
            except ValueError as exc:
                out["windows"][str(days)][name] = {"error": str(exc)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--windows", default="365,730,1285")
    parser.add_argument("--refit-every", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    windows = tuple(int(x) for x in args.windows.split(",") if x.strip())
    result = run(args.data, windows=windows, refit_every=args.refit_every)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
