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
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    brier_score_loss,
    roc_auc_score,
)

from pla_surge_model import (  # noqa: E402
    CONFORMAL_WINDOW,
    MIN_CALIBRATION_POSITIVES,
    MIN_TRAIN_ROWS,
    POINT_PARAMS,
    SURGE_PARAMS,
    SURGE_THRESHOLD,
    _logit,
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
    lowers, uppers, calibrateds = [], [], []
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

            # split-conformal 與 Platt 校準都要用「沒看過」的那一段，
            # 作法必須與 HorizonModel.fit 一致，否則回測量到的不是上線的行為。
            n_cal = min(CONFORMAL_WINDOW, len(train_idx) // 4)
            cal_point = HistGradientBoostingRegressor(**POINT_PARAMS).fit(
                Xtr[:-n_cal], ytr[:-n_cal])
            residuals = ytr[-n_cal:] - cal_point.predict(Xtr[-n_cal:])

            calibrator = None
            head_surge = ytr_surge[:-n_cal]
            held = ytr_surge[-n_cal:]
            if 0 < head_surge.sum() < len(head_surge) and \
                    MIN_CALIBRATION_POSITIVES <= held.sum() < len(held):
                cal_clf = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(
                    Xtr[:-n_cal], head_surge)
                calibrator = LogisticRegression().fit(
                    _logit(cal_clf.predict_proba(Xtr[-n_cal:])[:, 1]), held)

            cache[key] = (
                HistGradientBoostingRegressor(**POINT_PARAMS).fit(Xtr, ytr),
                HistGradientBoostingClassifier(**SURGE_PARAMS).fit(Xtr, ytr_surge),
                np.quantile(residuals, [0.05, 0.95]),
                calibrator,
            )
        point_m, surge_m, (lo_q, hi_q), calibrator = cache[key]

        x = X[i:i + 1]
        pt = max(0.0, point_m.predict(x)[0])
        p = surge_m.predict_proba(x)[0, 1]
        if calibrator is not None:
            p = float(calibrator.predict_proba(_logit([p]))[0, 1])
        points.append(pt)
        lowers.append(max(0.0, pt + lo_q))
        uppers.append(pt + hi_q)
        surge_probs.append(p)
        actuals.append(y[i])
        dates.append(t)
        calibrateds.append(calibrator is not None)

    return {
        "point": np.array(points),
        "lower": np.array(lowers),
        "upper": np.array(uppers),
        "surge_p": np.array(surge_probs),
        "actual": np.array(actuals),
        "dates": pd.DatetimeIndex(dates),
        # Platt 有沒有啟用。未校準的 surge_p 是 class_weight='balanced' 的原始
        # 輸出（實測膨脹約 1.9 倍），與校準後的機率不是同一個尺度 —— 混進同一個
        # 參考分布，百分位排出來的是尺度差異而不是風險差異。
        # build_oos_probabilities.py 用它過濾；既有呼叫端只讀舊 key，不受影響。
        "calibrated": np.array(calibrateds),
    }


def pinball(actual, pred, q):
    """分位數損失。MAE 在零膨脹右尾分布上獎勵低估（見檔頭），
    pinball 在 q>0.5 時反過來懲罰低估，才對得上「想抓到 surge」的目的。"""
    e = actual - pred
    return float(np.mean(np.where(e >= 0, q * e, (q - 1) * e)))


def score(name, point, surge_p, actual, threshold=SURGE_THRESHOLD, budget=0.20,
          lower=None, upper=None):
    err = point - actual
    labels = (actual >= threshold).astype(int)
    row = {
        "model": name,
        "n": len(actual),
        "MAE": np.abs(err).mean(),
        "RMSE": np.sqrt((err ** 2).mean()),
        "bias": err.mean(),
        "pin90": pinball(actual, point, 0.9),
    }
    # 區間校準。這是最容易被忽略卻最容易錯的一項：線上模型實測覆蓋率只有
    # 61.9%（名目 90%），而在這之前 repo 裡沒有任何程式碼會算它。
    if lower is not None and upper is not None:
        row["cov90"] = float(((actual >= lower) & (actual <= upper)).mean())
        row["width"] = float(np.mean(upper - lower))
    if 0 < labels.sum() < len(labels) and surge_p is not None:
        k = max(1, int(budget * len(labels)))
        cutoff = np.sort(surge_p)[::-1][k - 1]
        alert = surge_p >= cutoff
        base = labels.mean()
        row.update({
            "PR_AUC": average_precision_score(labels, surge_p),
            "ROC_AUC": roc_auc_score(labels, surge_p),
            # Brier 要跟「全押 base rate」比才有意義：一個永遠輸出 base rate
            # 的常數模型鑑別力為零，但 Brier 可能比一個未校準的模型還好。
            "Brier": brier_score_loss(labels, np.clip(surge_p, 0, 1)),
            "Brier_base": brier_score_loss(labels, np.full(len(labels), base)),
            "recall": (labels & alert).sum() / labels.sum(),
            "precision": (labels & alert).sum() / max(1, alert.sum()),
        })
    return row


def baseline_scores(series, horizon, dates, actual, threshold, min_n=30):
    """樸素基準線。沒有基準線就無法判斷模型的複雜度是否買到了東西。

    全部只用 target_date - horizon 當天（含）以前的觀測，與模型同樣是因果的。

    min_n 是「幾筆才值得報這條基準線」。回測窗預設 30；
    probability_review 的週報窗只有 7 天，會自己傳較小的值 —— 那裡的用途是
    「這週模型有沒有贏過複製昨天」，是單週敘述而非統計推論，門檻不同是刻意的。
    """
    out = []
    origin = dates - pd.Timedelta(days=horizon)
    cands = {
        "baseline persistence": series.reindex(origin),
        "baseline EMA-14": series.ewm(span=14, min_periods=2).mean().reindex(origin),
        "baseline median-28": series.rolling(28, min_periods=5).median().reindex(origin),
    }
    for name, pred in cands.items():
        p = pred.values.astype(float)
        m = ~np.isnan(p)
        if m.sum() < min_n:
            continue
        out.append(score(name, p[m], None, actual[m], threshold))
    return out


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
        lower=lp["lower_bound"].values,
        upper=lp["upper_bound"].values,
    )


def fmt(rows):
    cols = ["model", "n", "MAE", "pin90", "bias", "cov90", "PR_AUC", "ROC_AUC",
            "Brier", "Brier_base", "recall"]
    print(f"{'model':<22}{'n':>5}{'MAE':>7}{'pin90':>7}{'bias':>8}{'cov90':>7}"
          f"{'PR-AUC':>8}{'ROC-AUC':>9}{'Brier':>8}{'Brier0':>8}{'recall':>8}")
    print("-" * 97)
    for r in rows:
        vals = []
        for c in cols[1:]:
            v = r.get(c)
            if v is None:
                vals.append("     -")
            elif c == "n":
                vals.append(f"{v:>5d}")
            elif c in ("recall", "precision", "cov90"):
                vals.append(f"{100 * v:>6.0f}%")
            elif c in ("Brier", "Brier_base"):
                vals.append(f"{v:>8.4f}")
            elif c == "bias":
                vals.append(f"{v:>+8.2f}")
            else:
                vals.append(f"{v:>7.3f}" if c.endswith("AUC") else f"{v:>7.2f}")
        print(f"{r['model']:<22}" + "".join(vals))


def main():
    # `global` 必須出現在函式內任何一次使用該名稱之前，否則是 SyntaxError。
    # 舊版寫在 add_argument(default=REFIT_EVERY) 之後，整支腳本根本無法載入。
    global REFIT_EVERY

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="回測最近幾天")
    ap.add_argument("--horizons", default="1,3,7")
    ap.add_argument("--threshold", type=int, default=SURGE_THRESHOLD)
    ap.add_argument("--refit", type=int, default=REFIT_EVERY,
                    help="每幾天重訓一次（越小越接近線上每日重訓，也越慢）")
    args = ap.parse_args()

    REFIT_EVERY = args.refit

    raw = pd.read_csv(SORTIES_CSV, encoding="utf-8-sig")
    series = to_daily_series(raw)
    end = series.index.max()
    start = end - pd.Timedelta(days=args.days)
    horizons = [int(h) for h in args.horizons.split(",")]

    print(f"資料: {series.index.min().date()} -> {end.date()} "
          f"({series.notna().sum()} 個觀測日)")
    print(f"回測: {start.date()} -> {end.date()}  surge 門檻 >={args.threshold}\n")

    rows, first_dates, first_actual = [], None, None
    for h in horizons:
        r = walk_forward(series, h, start, end, args.threshold)
        if len(r["actual"]) == 0:
            print(f"h={h}: 無足夠資料，略過")
            continue
        if first_dates is None:
            first_dates, first_actual = r["dates"], r["actual"]
            # 基準線只跑一次（用第一個 horizon），避免輸出被灌爆
            rows.extend(baseline_scores(
                series, h, r["dates"], r["actual"], args.threshold))
        rows.append(score(f"surge model h={h}", r["point"], r["surge_p"],
                          r["actual"], args.threshold,
                          lower=r["lower"], upper=r["upper"]))

    if first_dates is not None:
        deployed = load_deployed(first_dates)
        if deployed:
            rows.append(deployed)

    fmt(rows)

    if first_actual is not None:
        n_surge = int((first_actual >= args.threshold).sum())
        n = len(first_actual)
        print(f"\n窗口內 surge 天數: {n_surge}/{n} ({100 * n_surge / n:.0f}%)")
        print(f"cov90 目標 90%；Brier 要低於 Brier_base（全押 base rate）才算有校準。")
        print("注意: h>=2 的 surge 機率實測 ROC-AUC 約 0.45-0.57，不具鑑別力。")
        print("MAE 在此分布上獎勵低估，判定成敗請看 pin90 / cov90 / PR_AUC。")


if __name__ == "__main__":
    main()
