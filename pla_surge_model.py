#!/usr/bin/env python3
"""PLA 架次預測 — surge 導向模型

設計取捨（依 2026-07 回測結果）：

1. 主回歸用 Poisson loss 直接對原始 count 訓練，不做 log1p + MAE。
   log1p+MAE 的最佳解是條件中位數，對零膨脹右尾分布會系統性低估
   （實測 bias -2.26，剛好等於序列的 mean-median gap）。

2. Surge 機率用獨立的 class_weight='balanced' 分類器，而不是從回歸值
   反推。門檻用 20（近 12 個月 base rate 17.8%），不用 25 — 25 太稀疏，
   PR-AUC 掉一半。

3. 每個 horizon 各訓一個模型（direct multi-horizon），不做遞迴餵回。
   遞迴會讓預測變異數逐日塌陷。

4. 區間用 split-conformal，不用分位數回歸直出 — 後者實測覆蓋率只有 47%。

5. 特徵一律按日曆對齊。原始資料有 11% 的相鄰紀錄間隔 >1 天，
   直接 shift(1) 會讓 lag_1 不是「昨天」。缺失日保留 NaN，
   由 HistGradientBoosting 原生處理，不補零 — 沒回報 ≠ 零架次。

Surge 只在 h=1 有訊號（ROC-AUC 0.764）；h>=2 掉到 0.45-0.57，等同亂猜。
SIGNAL_HORIZON 就是用來標記這件事的，呼叫端應據此決定要不要顯示警報。
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import (
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
)

SURGE_THRESHOLD = 20
SIGNAL_HORIZON = 1        # 超過這個 horizon 的 surge 機率不具鑑別力
CONFORMAL_WINDOW = 180    # 殘差校準窗口（天）
MIN_TRAIN_ROWS = 400
RANDOM_STATE = 42

POINT_PARAMS = dict(
    loss="poisson", max_iter=500, max_depth=6,
    learning_rate=0.03, l2_regularization=1.0, random_state=RANDOM_STATE,
)
SURGE_PARAMS = dict(
    max_iter=300, max_depth=4, learning_rate=0.05,
    class_weight="balanced", random_state=RANDOM_STATE,
)


def to_daily_series(df, date_col="date", value_col="pla_aircraft_sorties"):
    """整理成連續日曆序列。沒有紀錄的日子留 NaN（未觀測），不是 0。"""
    d = df[[date_col, value_col]].copy()
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce", format="mixed")
    d = d[d[date_col].notna() & d[value_col].notna()]
    # 同日多筆取最大值（同一天可能有多來源回報）
    d = d.groupby(date_col, as_index=False)[value_col].max().sort_values(date_col)
    idx = pd.date_range(d[date_col].min(), d[date_col].max(), freq="D")
    s = d.set_index(date_col)[value_col].reindex(idx)
    s.index.name = "date"
    return s.rename("y")


def build_features(series, horizon, threshold=SURGE_THRESHOLD):
    """列 = 預測原點 t（含當日觀測），目標 = y[t + horizon]。

    所有特徵只用 t 當下已知的資訊，不含任何未來值。
    """
    p = series
    o = pd.DataFrame(index=series.index)

    for lag in [0, 1, 2, 3, 4, 6, 13, 20, 27]:
        o[f"lag{lag}"] = p.shift(lag)
    for w in [3, 7, 14, 28, 56, 112]:
        o[f"ma{w}"] = p.rolling(w, min_periods=2).mean()
        o[f"mx{w}"] = p.rolling(w, min_periods=2).max()
    for w in [7, 28, 56]:
        o[f"sd{w}"] = p.rolling(w, min_periods=3).std()
        o[f"zr{w}"] = p.rolling(w, min_periods=3).apply(
            lambda a: np.nanmean(a == 0), raw=True)
        o[f"sr{w}"] = p.rolling(w, min_periods=3).apply(
            lambda a: np.nanmean(a >= threshold), raw=True)
        o[f"q75_{w}"] = p.rolling(w, min_periods=3).quantile(0.75)
        o[f"q90_{w}"] = p.rolling(w, min_periods=3).quantile(0.90)

    o["ema7"] = p.ewm(span=7, min_periods=3).mean()
    o["ema28"] = p.ewm(span=28, min_periods=3).mean()
    o["ema56"] = p.ewm(span=56, min_periods=3).mean()
    o["trend"] = o["ema7"] - o["ema28"]
    o["trend2"] = o["ema28"] - o["ema56"]
    o["accel"] = p.diff().rolling(3, min_periods=1).mean()

    # surge 專用：surge 會叢集（實測 P(surge | 前一天有活動) = 25%）
    is_surge = p >= threshold
    o["days_since_surge"] = (~is_surge).groupby(is_surge.cumsum()).cumcount()
    o["surge_run"] = is_surge.astype(float).mask(p.isna()).rolling(3, min_periods=1).sum()
    o["ratio_7_56"] = o["ma7"] / (o["ma56"] + 1)
    o["mx7_vs_ma28"] = o["mx7"] / (o["ma28"] + 1)
    # 回報密度：低密度期間的 lag 特徵較不可信，讓模型自己學到這點
    o["obs28"] = p.notna().rolling(28, min_periods=1).mean()

    target_dates = series.index + pd.Timedelta(days=horizon)
    o["dow"] = target_dates.dayofweek
    o["dow_sin"] = np.sin(2 * np.pi * target_dates.dayofweek / 7)
    o["dow_cos"] = np.cos(2 * np.pi * target_dates.dayofweek / 7)
    o["moy_sin"] = np.sin(2 * np.pi * target_dates.month / 12)
    o["moy_cos"] = np.cos(2 * np.pi * target_dates.month / 12)

    o["_target"] = series.reindex(target_dates).values
    o["_target_date"] = target_dates
    return o


FEATURE_EXCLUDE = ("_target", "_target_date")


def feature_columns(frame):
    return [c for c in frame.columns if c not in FEATURE_EXCLUDE]


class HorizonModel:
    """單一 horizon 的模型：點預測 + surge 機率 + conformal 區間。"""

    def __init__(self, horizon, threshold=SURGE_THRESHOLD):
        self.horizon = horizon
        self.threshold = threshold
        self.point = None
        self.surge = None
        self.features = None
        self.residuals = None      # 供 conformal 區間使用
        self.surge_base_rate = None

    def fit(self, series):
        frame = build_features(series, self.horizon, self.threshold)
        frame = frame[frame["ma28"].notna()]
        train = frame[frame["_target"].notna()]
        if len(train) < MIN_TRAIN_ROWS:
            raise ValueError(
                f"h={self.horizon} 訓練資料不足: {len(train)} < {MIN_TRAIN_ROWS}")

        self.features = feature_columns(train)
        X = train[self.features].values
        y = train["_target"].values
        y_surge = (y >= self.threshold).astype(int)
        self.surge_base_rate = float(y_surge.mean())

        # conformal 校準必須用「沒看過」的殘差。先在扣掉最後
        # CONFORMAL_WINDOW 天的資料上訓一個模型，拿它在那段期間的
        # 樣本外殘差當校準集。用樣本內殘差會嚴重低估區間寬度。
        n_cal = min(CONFORMAL_WINDOW, len(train) // 4)
        cal_model = HistGradientBoostingRegressor(**POINT_PARAMS).fit(
            X[:-n_cal], y[:-n_cal])
        self.residuals = y[-n_cal:] - cal_model.predict(X[-n_cal:])

        # 最終模型用全部資料重訓（標準 split-conformal 作法）
        self.point = HistGradientBoostingRegressor(**POINT_PARAMS).fit(X, y)
        # 全零或全一時分類器無法訓練（極短序列才會發生）
        if 0 < y_surge.sum() < len(y_surge):
            self.surge = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(X, y_surge)
        return self

    def predict(self, series):
        """對序列最後一天當原點，預測 origin + horizon。"""
        frame = build_features(series, self.horizon, self.threshold)
        row = frame.iloc[[-1]]
        X = row[self.features].values

        point = float(max(0.0, self.point.predict(X)[0]))
        if self.surge is not None:
            surge_p = float(self.surge.predict_proba(X)[0, 1])
        else:
            surge_p = self.surge_base_rate

        lo_q, hi_q = np.quantile(self.residuals, [0.05, 0.95])
        return {
            "horizon": self.horizon,
            "target_date": row["_target_date"].iloc[0],
            "point": point,
            "lower": float(max(0.0, point + lo_q)),
            "upper": float(point + hi_q),
            "surge_probability": surge_p,
            "surge_signal_valid": self.horizon <= SIGNAL_HORIZON,
        }


class SurgeForecaster:
    """多 horizon 容器。每個 horizon 一個獨立模型，不做遞迴。"""

    def __init__(self, horizons=range(1, 8), threshold=SURGE_THRESHOLD):
        self.horizons = list(horizons)
        self.threshold = threshold
        self.models = {}

    def fit(self, series):
        for h in self.horizons:
            self.models[h] = HorizonModel(h, self.threshold).fit(series)
        return self

    def predict(self, series):
        return [self.models[h].predict(series) for h in self.horizons]


def risk_level(surge_p, signal_valid, base_rate):
    """把 surge 機率轉成等級。

    門檻取自回測操作點：>=2x lift 才叫 HIGH。訊號無效的 horizon
    一律回 UNKNOWN，不要用一個 AUC 0.5 的數字去嚇人。
    """
    if not signal_valid:
        return "UNKNOWN"
    if surge_p >= max(0.40, 3 * base_rate):
        return "HIGH"
    if surge_p >= max(0.30, 2 * base_rate):
        return "MEDIUM-HIGH"
    if surge_p >= max(0.20, 1.3 * base_rate):
        return "MEDIUM"
    return "LOW"
