#!/usr/bin/env python3
"""Daily live shadow runner for Classifier 4.0.

Runs two challenger surge classifiers on the same latest data used by production:
  B = production features + temporal/regime features
  C = B + naval features

The production D+1 probability is read from latest_prediction.csv; this script never
changes that file or any risk level. It appends one immutable forecast row per target
date to classifier4_shadow.csv, backfills outcomes when actual sorties arrive, and
writes classifier4_comparison.json for LINE/display use.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from pla_surge_model import (
    CONFORMAL_WINDOW,
    MIN_CALIBRATION_POSITIVES,
    SURGE_PARAMS,
    SURGE_THRESHOLD,
    _logit,
    build_features,
    feature_columns,
    to_daily_series,
)
from scripts.analysis.classifier4_features import (
    build_naval_features,
    build_temporal_features,
    to_daily_frame,
)

DATA = Path("data/JapanandBattleship.csv")
PROD = Path("data/predictions/latest_prediction.csv")
SHADOW = Path("data/predictions/classifier4_shadow.csv")
SUMMARY = Path("data/predictions/classifier4_comparison.json")
ALERT_RATE = 0.20
MIN_SCORE_N = 10


def _experiment_frame(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    series = to_daily_series(raw)
    prod = build_features(series, horizon=1, threshold=SURGE_THRESHOLD)
    daily = to_daily_frame(raw)
    temporal = build_temporal_features(daily)
    naval = build_naval_features(daily)
    frame = prod.join(temporal, how="left").join(naval, how="left")
    pcols = feature_columns(prod)
    tcols = list(temporal.columns)
    ncols = list(naval.columns)
    return frame, {
        "temporal": pcols + tcols,
        "temporal_naval": pcols + tcols + ncols,
    }


def _fit_probability(train: pd.DataFrame, features: list[str], latest: pd.DataFrame):
    """Match production's balanced classifier + OOS Platt calibration pattern."""
    train = train[train["_target"].notna()].copy()
    X = train[features].values
    y = (train["_target"].values >= SURGE_THRESHOLD).astype(int)
    if len(train) < 400 or len(np.unique(y)) < 2:
        raise ValueError("insufficient training data/classes")

    n_cal = min(CONFORMAL_WINDOW, len(train) // 4)
    head_x, held_x = X[:-n_cal], X[-n_cal:]
    head_y, held_y = y[:-n_cal], y[-n_cal:]
    calibrator = None
    if len(np.unique(head_y)) == 2:
        cal_model = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(head_x, head_y)
        raw_held = cal_model.predict_proba(held_x)[:, 1]
        if (held_y.sum() >= MIN_CALIBRATION_POSITIVES
                and len(np.unique(held_y)) == 2):
            calibrator = LogisticRegression(random_state=42).fit(_logit(raw_held), held_y)

    model = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(X, y)
    raw = float(model.predict_proba(latest[features].values)[0, 1])
    calibrated = raw
    if calibrator is not None:
        calibrated = float(calibrator.predict_proba(_logit([raw]))[0, 1])
    return calibrated, raw, calibrator is not None


def _production_d1() -> dict:
    df = pd.read_csv(PROD, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    future = df[df.get("actual_sorties", pd.Series(index=df.index, dtype=float)).isna()].copy()
    if "horizon" in future.columns:
        d1 = future[pd.to_numeric(future["horizon"], errors="coerce") == 1]
        if not d1.empty:
            r = d1.sort_values("date").iloc[0]
        else:
            r = future.sort_values("date").iloc[0]
    else:
        r = future.sort_values("date").iloc[0]
    p = pd.to_numeric(pd.Series([r.get("high_event_probability")]), errors="coerce").iloc[0]
    return {
        "date": r["date"].strftime("%Y-%m-%d"),
        "point": float(r.get("predicted_sorties")),
        "lower": float(r.get("lower_bound")),
        "upper": float(r.get("upper_bound")),
        "prob": None if pd.isna(p) else float(p) / 100.0,
        "risk": str(r.get("risk_level", "UNKNOWN")),
        "model_version": str(r.get("model_version", "production")),
    }


def _metrics(rows: pd.DataFrame, prob_col: str) -> dict:
    d = rows.dropna(subset=["actual_sorties", prob_col]).copy()
    if len(d) < MIN_SCORE_N:
        return {"n": int(len(d)), "enough": False, "min_n": MIN_SCORE_N}
    y = (d["actual_sorties"].astype(float).values >= SURGE_THRESHOLD).astype(int)
    p = d[prob_col].astype(float).values
    if len(np.unique(y)) < 2:
        return {"n": int(len(d)), "enough": False, "reason": "single class"}
    k = max(1, int(np.ceil(len(p) * ALERT_RATE)))
    idx = np.argsort(p)[-k:]
    recall = float(y[idx].sum() / y.sum()) if y.sum() else 0.0
    base = float(y.mean())
    brier = float(brier_score_loss(y, p))
    base_brier = float(np.mean((y - base) ** 2))
    return {
        "n": int(len(d)), "enough": True, "positives": int(y.sum()),
        "roc_auc": float(roc_auc_score(y, p)),
        "pr_auc": float(average_precision_score(y, p)),
        "brier": brier,
        "brier_skill": float(1.0 - brier / base_brier) if base_brier > 0 else None,
        "alert_rate": float(k / len(y)),
        "recall_at_20pct": recall,
        "recall_over_alert_rate": float(recall / (k / len(y))),
    }


def run() -> dict:
    raw = pd.read_csv(DATA, encoding="utf-8-sig")
    series = to_daily_series(raw)
    frame, groups = _experiment_frame(raw)
    latest_date = series.dropna().index.max()
    if latest_date not in frame.index:
        raise RuntimeError("latest observed date missing from feature frame")
    latest = frame.loc[[latest_date]]
    train = frame.loc[frame.index < latest_date]

    vals = {}
    for name, cols in groups.items():
        prob, raw_prob, calibrated = _fit_probability(train, cols, latest)
        vals[name] = {"prob": prob, "raw_prob": raw_prob, "calibrated": calibrated}

    prod = _production_d1()
    target_date = prod["date"]
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "date": target_date,
        "generated_at": now,
        "data_latest_date": latest_date.strftime("%Y-%m-%d"),
        "surge_threshold": SURGE_THRESHOLD,
        "actual_sorties": np.nan,
        "production_point": prod["point"],
        "production_lower": prod["lower"],
        "production_upper": prod["upper"],
        "production_probability": prod["prob"],
        "production_risk": prod["risk"],
        "production_model_version": prod["model_version"],
        "temporal_probability": vals["temporal"]["prob"],
        "temporal_raw_probability": vals["temporal"]["raw_prob"],
        "temporal_calibrated": int(vals["temporal"]["calibrated"]),
        "temporal_naval_probability": vals["temporal_naval"]["prob"],
        "temporal_naval_raw_probability": vals["temporal_naval"]["raw_prob"],
        "temporal_naval_calibrated": int(vals["temporal_naval"]["calibrated"]),
    }

    if SHADOW.exists():
        hist = pd.read_csv(SHADOW, encoding="utf-8-sig")
        hist = hist[hist["date"].astype(str) != target_date]
        out = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        out = pd.DataFrame([row])

    actual_map = {d.strftime("%Y-%m-%d"): float(v) for d, v in series.dropna().items()}
    out["actual_sorties"] = out["date"].astype(str).map(actual_map)
    out = out.sort_values("date").reset_index(drop=True)
    SHADOW.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(SHADOW, index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": now,
        "data_latest_date": latest_date.strftime("%Y-%m-%d"),
        "target_date": target_date,
        "threshold": SURGE_THRESHOLD,
        "latest": {
            "production": prod,
            "temporal": vals["temporal"],
            "temporal_naval": vals["temporal_naval"],
        },
        "history_rows": int(len(out)),
        "metrics": {
            "production": _metrics(out, "production_probability"),
            "temporal": _metrics(out, "temporal_probability"),
            "temporal_naval": _metrics(out, "temporal_naval_probability"),
        },
        "note": "Shadow only: challengers do not change production risk_level or alerts.",
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    summary = run()
    l = summary["latest"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nD+1 shadow comparison")
    print(f"production:      {l['production']['prob']!s}")
    print(f"+ temporal:      {l['temporal']['prob']:.3f}")
    print(f"+ temporal+naval:{l['temporal_naval']['prob']:.3f}")


if __name__ == "__main__":
    main()
