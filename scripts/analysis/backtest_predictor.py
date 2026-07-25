#!/usr/bin/env python3
"""Walk-forward 回測，用來比較預測模型。

重點是 surge 偵測能力，不是 MAE。MAE 在零膨脹右尾分布上會獎勵
「永遠不預測 surge」的模型 — 現行線上模型 MAE 5.86 看似不錯，
但 18 次 surge 一次都沒抓到。

用法:
    python3 scripts/analysis/backtest_predictor.py --days 60
    python3 scripts/analysis/backtest_predictor.py --days 365 --horizons 1,3,7
"""

import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
warnings.filterwarnings("ignore")

from sklearn.ensemble import (  # noqa: E402
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
)
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    roc_auc_score,
)

from pla_surge_model import (  # noqa: E402
    MIN_TRAIN_ROWS,
    POINT_PARAMS,
    SURGE_PARAMS,
    SURGE_THRESHOLD,
    build_features,
    feature_columns,
    to_daily_series,
)

SORTIES_CSV = "data/JapanandBattleship.csv"
PREDICTION_CSV = "data/predictions/latest_prediction.csv"
# 天。線上是每天重訓，但回測裡每天重訓太慢，預設 28 天。
# 實測 refit 越密 MAE 越好（60 天窗口 h=1: 28天=6.64 / 14天=6.48 / 7天=6.03），
# 所以預設值對本模型是偏保守的估計。用 --refit 調整。
REFIT_EVERY = 28


def walk_forward(series, horizon, start, end, threshold=SURGE_THRESHOLD):
    """嚴格時序回測：訓練集只含 target_date - horizon - 7 天以前的樣本。

    那 7 天是 embargo，避免 rolling 特徵跨過訓練/測試邊界洩漏。
    """
    frame = build_features(series, horizon, threshold)
    frame = frame[frame["ma28"].notna()]
    frame = frame[frame["_target"].notna()]

    feats = feature_columns(frame)
    X = frame[feats].values
    y = frame["_target"].values
    target_dates = pd.DatetimeIndex(frame["_target_date"])

    points, surge_probs, actuals, dates = [], [], [], []
    cache = {}
    embargo = pd.Timedelta(days=horizon + 7)

    for i, t in enumerate(target_dates):
        if not (start <= t <= end):
            continue
        train_idx = np.where(target_dates < t - embargo)[0]
        if len(train_idx) < MIN_TRAIN_ROWS:
            continue

        key = len(train_idx) // REFIT_EVERY
        if key not in cache:
            cache.clear()
            Xtr, ytr = X[train_idx], y[train_idx]
            ytr_surge = (ytr >= threshold).astype(int)
            cache[key] = (
                HistGradientBoostingRegressor(**POINT_PARAMS).fit(Xtr, ytr),
                HistGradientBoostingClassifier(**SURGE_PARAMS).fit(Xtr, ytr_surge),
            )
        point_m, surge_m = cache[key]

        x = X[i:i + 1]
        points.append(max(0.0, point_m.predict(x)[0]))
        surge_probs.append(surge_m.predict_proba(x)[0, 1])
        actuals.append(y[i])
        dates.append(t)

    return (np.array(points), np.array(surge_probs),
            np.array(actuals), pd.DatetimeIndex(dates))


def score(name, point, surge_p, actual, threshold=SURGE_THRESHOLD, budget=0.20):
    err = point - actual
    labels = (actual >= threshold).astype(int)
    row = {
        "model": name,
        "n": len(actual),
        "MAE": np.abs(err).mean(),
        "RMSE": np.sqrt((err ** 2).mean()),
        "bias": err.mean(),
    }
    if 0 < labels.sum() < len(labels) and surge_p is not None:
        k = max(1, int(budget * len(labels)))
        cutoff = np.sort(surge_p)[::-1][k - 1]
        alert = surge_p >= cutoff
        row.update({
            "PR_AUC": average_precision_score(labels, surge_p),
            "ROC_AUC": roc_auc_score(labels, surge_p),
            "recall": (labels & alert).sum() / labels.sum(),
            "precision": (labels & alert).sum() / max(1, alert.sum()),
        })
    return row


def load_deployed(dates):
    """線上模型同期的實際表現，當作比較基準。"""
    if not os.path.exists(PREDICTION_CSV):
        return None
    lp = pd.read_csv(PREDICTION_CSV, encoding="utf-8-sig")
    lp["date"] = pd.to_datetime(lp["date"])
    lp = lp[lp["actual_sorties"].notna()].set_index("date")
    common = dates.intersection(lp.index)
    if len(common) < 10:
        return None
    lp = lp.reindex(common)
    return score(
        "deployed (線上現行)",
        lp["predicted_sorties"].values,
        lp["high_event_probability"].values / 100.0,
        lp["actual_sorties"].values,
    )


def fmt(rows):
    cols = ["model", "n", "MAE", "RMSE", "bias", "PR_AUC", "ROC_AUC", "recall", "precision"]
    print(f"{'model':<22}{'n':>5}{'MAE':>7}{'RMSE':>7}{'bias':>8}"
          f"{'PR-AUC':>8}{'ROC-AUC':>9}{'recall':>8}{'prec':>7}")
    print("-" * 81)
    for r in rows:
        vals = []
        for c in cols[1:]:
            v = r.get(c)
            if v is None:
                vals.append("     -")
            elif c == "n":
                vals.append(f"{v:>5d}")
            elif c in ("recall", "precision"):
                vals.append(f"{100 * v:>6.0f}%")
            elif c == "bias":
                vals.append(f"{v:>+8.2f}")
            else:
                vals.append(f"{v:>7.3f}" if c.endswith("AUC") else f"{v:>7.2f}")
        print(f"{r['model']:<22}" + "".join(vals))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="回測最近幾天")
    ap.add_argument("--horizons", default="1,3,7")
    ap.add_argument("--threshold", type=int, default=SURGE_THRESHOLD)
    ap.add_argument("--refit", type=int, default=REFIT_EVERY,
                    help="每幾天重訓一次（越小越接近線上每日重訓，也越慢）")
    args = ap.parse_args()

    global REFIT_EVERY
    REFIT_EVERY = args.refit

    raw = pd.read_csv(SORTIES_CSV, encoding="utf-8-sig")
    series = to_daily_series(raw)
    end = series.index.max()
    start = end - pd.Timedelta(days=args.days)
    horizons = [int(h) for h in args.horizons.split(",")]

    print(f"資料: {series.index.min().date()} -> {end.date()} "
          f"({series.notna().sum()} 個觀測日)")
    print(f"回測: {start.date()} -> {end.date()}  surge 門檻 >={args.threshold}\n")

    rows, first_dates = [], None
    for h in horizons:
        point, surge_p, actual, dates = walk_forward(
            series, h, start, end, args.threshold)
        if len(actual) == 0:
            print(f"h={h}: 無足夠資料，略過")
            continue
        if first_dates is None:
            first_dates = dates
        rows.append(score(f"surge model h={h}", point, surge_p, actual, args.threshold))

    if first_dates is not None:
        deployed = load_deployed(first_dates)
        if deployed:
            rows.append(deployed)

    fmt(rows)

    if rows:
        n_surge = int((rows[0]["n"] * 0))  # 佔位，實際數字下面單獨算
        _, _, actual, _ = walk_forward(series, horizons[0], start, end, args.threshold)
        n_surge = int((actual >= args.threshold).sum())
        print(f"\n窗口內 surge 天數: {n_surge}/{len(actual)} "
              f"({100 * n_surge / len(actual):.0f}%)")
        print("注意: h>=2 的 surge 機率實測 ROC-AUC 約 0.45-0.57，不具鑑別力。")


if __name__ == "__main__":
    main()
