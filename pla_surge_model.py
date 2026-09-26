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

6. 零架次 regime（3.1.0）：2026 的零架次日由往年 <6% 升到 26%，而 2024 以前
   的資料根本沒有記錄零架次。處理方式是零架次特徵 + 點預測時間衰減權重 +
   零架次閘門（見 ZERO_GATE_PROB），並把 Platt 校準窗拉長到 365 天、另加
   相對排名警示（見 CALIBRATION_WINDOW / ALERT_RANK）。前後回測比較見
   docs/zero_regime_backtest.md。

Surge 只在 h=1 有訊號（ROC-AUC 0.764）；h>=2 掉到 0.45-0.57，等同亂猜。
SIGNAL_HORIZON 就是用來標記這件事的，呼叫端應據此決定要不要顯示警報。
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import (
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
)
from sklearn.linear_model import LogisticRegression

SURGE_THRESHOLD = 20
SIGNAL_HORIZON = 1        # 超過這個 horizon 的 surge 機率不具鑑別力
CONFORMAL_WINDOW = 180    # 殘差校準窗口（天）
MIN_TRAIN_ROWS = 400
# 校準窗至少要有幾個正樣本才做校準。
#
# 這裡用 Platt（logistic）而不是 isotonic，是實測後的選擇：校準窗只有約 180 天、
# 正樣本 20 上下，isotonic 會產生大片平坦區間，把不同的預測壓成同一個值。
# 實測 h=1/thr=20 的 PR-AUC 因此由 0.259 掉到 0.168、ROC-AUC 0.636 → 0.565 ——
# 排序能力被校準本身破壞掉了。Platt 是嚴格單調的，AUC 完全不變，
# 只把系統性偏移（class_weight='balanced' 造成的整體推高）拉回來。
MIN_CALIBRATION_POSITIVES = 8
# Platt 的校準窗（天），與 conformal 的 180 天分開。
#
# 180 天窗在 2026 零架次期偏多時（校準窗 24% 是零）把 Platt 斜率壓到 ~0.5，
# 原始 50% 只映成 20%，而且每次重訓斜率都不同 —— 同一天內雖然單調，跨天排序
# 卻被打亂。walk-forward（2024-07 → 2026-09，813 天，h=1）實測：
#     180 天  ROC-AUC 0.587  Brier 0.166
#     365 天  ROC-AUC 0.618  Brier 0.162
# 另試過把 zr28（零架次比例）當 Platt 第二特徵，沒有增益，未採用。
CALIBRATION_WINDOW = 365

# 點預測的時間衰減權重半衰期（天）。2026 的活動型態（零架次日 26%、日均 7.4）
# 與 2022-2025（零架次日 <6%、日均 ~14）明顯不同，等權訓練會讓模型持續高估。
# 實測半衰期 90 / 180 / 365 天，全期 MAE 8.26 / 8.01 / 7.81（等權 8.06），取 365。
# 只加在回歸頭：surge 分類器正樣本本來就少，再降權只會更不穩。
POINT_HALF_LIFE = 365

# 零架次閘門。條件是「原點當天為 0，且近 ZERO_GATE_WINDOW 天內，
# 0 之後 h 天仍為 0 的比例 >= ZERO_GATE_PROB」，成立時點預測直接給 0。
#
# 為什麼不是學一個「明天是否為 0」的分類器：試過（全資料 / 僅 2024+ /
# 時間加權），2026 的 ROC-AUC 只有 0.50-0.61 —— 2024 以前根本沒有記錄零架次，
# 分類器學不到。但轉移機率本身訊號很強：2026 前一天為 0 時，隔天為 0 的機率
# 是 52%（中位數 0），而模型平均預測 6 架次。門檻 0.5 = 條件中位數為 0，
# 此時預測 0 是 MAE 最佳解。
#
# 代價：pin90 由 4.39 → 4.45（略增低估懲罰）。零架次期間 MAE 7.12 → 3.68。
ZERO_GATE_WINDOW = 180
ZERO_GATE_PROB = 0.5
ZERO_GATE_MIN_PAIRS = 5

# 相對排名警示：今天的原始分類器分數落在校準窗樣本外分數的前 10%，
# 風險等級至少 MEDIUM-HIGH（推播裡的警示線）。
#
# 為什麼需要：校準後的機率在 base rate ~12% 的期間幾乎摸不到 30% 絕對下限，
# 換成 365 天校準窗後更是如此（回測全期只剩 17 次警示、recall 5%）。
# 排名不受校準斜率影響。回測 813 天：
#     現行階梯          87 次警示  命中 31  precision 0.36  recall 0.18
#     365 校準 + 排名   87 次警示  命中 34  precision 0.39  recall 0.20
# 前 5% 與 5-10% 的命中率沒有差異（0.25 vs 0.44，n=24），所以只設一階。
ALERT_RANK = 0.90
RANDOM_STATE = 42

POINT_PARAMS = dict(
    loss="poisson", max_iter=500, max_depth=6,
    learning_rate=0.03, l2_regularization=1.0, random_state=RANDOM_STATE,
)
SURGE_PARAMS = dict(
    max_iter=300, max_depth=4, learning_rate=0.05,
    class_weight="balanced", random_state=RANDOM_STATE,
)


def _logit(p, eps=1e-6):
    """機率 → log-odds，並整形成 sklearn 要的 (n, 1)。

    Platt 要在 log-odds 上做才是標準做法；直接對機率做線性 logistic
    等於再套一層 sigmoid，對已經接近 0/1 的輸出幾乎沒有調整能力。
    """
    a = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    return np.log(a / (1 - a)).reshape(-1, 1)


def conformal_surge_prob(point, residuals, threshold):
    """用 conformal 殘差分布把點預測轉成 P(Y >= threshold)。

    問法是「把校準期的殘差加到今天的點預測上，有幾成會越過門檻？」——
    回歸頭對 surge 這件事的看法，不需要任何新模型。

    ## 這個值只做監看，不參與警報 —— 這是量過之後的決定

    起因是 2026-08-05 的漏報：實際 21 架次，線上點預測 20.5（已達門檻），
    分類器卻只給 10.8%，risk_level 因此是 LOW。看起來像「回歸頭知道、
    分類器不知道，只是沒接線」。

    把它接進 Platt（雙特徵：分類器輸出 + 本函式）實測過，181 天 / 21 個正樣本：

        單特徵(現行)  PR-AUC 0.362  ROC 0.638  Brier 0.1010
        雙特徵        PR-AUC 0.324  ROC 0.637  Brier 0.1009
        純 conformal  PR-AUC 0.148  ROC 0.582  Brier 0.1077

    PR-AUC 掉 10%，Brier 持平 —— 沒過驗收閘，所以沒有上線。原因在最後一列：
    這個訊號本身的鑑別力遠低於分類器，而且與分類器高度相關，當第二個特徵
    只是加噪音（配出來的 Platt 係數是負的）。

    而那個 08-05 的「回歸頭早就知道」其實是倖存者偏誤：同一天在嚴格
    walk-forward 下點預測只有 9.2，conformal 機率 0.094，一樣抓不到。
    線上看到的 20.5 來自每日重訓的模型，不是可重現的訊號。

    欄位留著（CSV 的 high_event_probability_point）是為了讓下次有人重提
    這個想法時，手上直接有並排的紀錄可看，不必再猜。
    """
    residuals = np.asarray(residuals, dtype=float)
    return float(np.mean(residuals >= (threshold - point)))


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

    # 零架次 regime：目前連續幾天為 0、距上次有活動幾天、上次有活動的量。
    # 缺值不算 0，也不延續連續天數。
    is_zero = p == 0
    o["zero_streak"] = is_zero.groupby((~is_zero).cumsum()).cumsum().astype(float)
    pos = np.arange(len(p), dtype=float)
    last_active = pd.Series(np.where(p > 0, pos, np.nan), index=p.index).ffill()
    o["days_since_active"] = pos - last_active.values
    o["last_active_val"] = p.where(p > 0).ffill()
    o["zero3"] = is_zero.astype(float).mask(p.isna()).rolling(3, min_periods=1).sum()

    # 零架次閘門用的轉移統計（以 _ 開頭，不進模型）。
    # 配對 (o-h, o)，o 落在原點前 ZERO_GATE_WINDOW 天內 —— 全是原點當下已知。
    prev_zero = p.shift(horizon) == 0
    pair = prev_zero & p.notna()
    stay = pair & is_zero
    n_pair = pair.astype(float).rolling(ZERO_GATE_WINDOW, min_periods=1).sum()
    o["_zero_pairs"] = n_pair
    o["_zero_persist"] = (stay.astype(float).rolling(ZERO_GATE_WINDOW, min_periods=1).sum()
                          / n_pair.replace(0, np.nan))

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
    # 以 _ 開頭的是輔助欄（目標、閘門統計），不進模型
    return [c for c in frame.columns
            if c not in FEATURE_EXCLUDE and not c.startswith("_")]


def recency_weights(target_dates, half_life=POINT_HALF_LIFE):
    """以最後一個目標日為基準的指數衰減權重。"""
    td = pd.DatetimeIndex(target_dates)
    age = (td.max() - td).days.values.astype(float)
    return 0.5 ** (age / half_life)


def zero_gate(frame):
    """零架次閘門是否成立（逐列布林）。定義見 ZERO_GATE_PROB。"""
    return ((frame["lag0"] == 0)
            & (frame["_zero_pairs"] >= ZERO_GATE_MIN_PAIRS)
            & (frame["_zero_persist"] >= ZERO_GATE_PROB)).values


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
        self.calibrator = None     # Platt，把分類器輸出映回真實機率
        self.calibration_base_rate = None
        self.raw_reference = None  # 校準窗的樣本外原始分數（已排序），排名用

    def fit(self, series):
        frame = build_features(series, self.horizon, self.threshold)
        frame = frame[frame["ma28"].notna()]
        train = frame[frame["_target"].notna()]
        if len(train) < MIN_TRAIN_ROWS:
            raise ValueError(
                f"h={self.horizon} 訓練資料不足: {len(train)} < {MIN_TRAIN_ROWS}")
        return self.fit_frame(train)

    def fit_frame(self, train):
        """由 build_features 的列訓練。回測直接呼叫這裡，確保與上線同一套作法。"""
        self.features = feature_columns(train)
        X = train[self.features].values
        y = train["_target"].values
        w = recency_weights(train["_target_date"])
        y_surge = (y >= self.threshold).astype(int)
        self.surge_base_rate = float(y_surge.mean())

        # conformal 校準必須用「沒看過」的殘差。先在扣掉最後
        # CONFORMAL_WINDOW 天的資料上訓一個模型，拿它在那段期間的
        # 樣本外殘差當校準集。用樣本內殘差會嚴重低估區間寬度。
        n_cal = min(CONFORMAL_WINDOW, len(train) // 4)
        cal_model = HistGradientBoostingRegressor(**POINT_PARAMS).fit(
            X[:-n_cal], y[:-n_cal], sample_weight=w[:-n_cal])
        self.residuals = y[-n_cal:] - cal_model.predict(X[-n_cal:])

        # 機率校準。SURGE_PARAMS 用 class_weight='balanced'，那是為了讓分類器
        # 在稀疏正樣本下學得動，代價是輸出機率被整體推高 —— 實測 Brier 0.1230
        # 比「全押 base rate」的 0.0998 還差，也就是那個百分比本身不能當機率讀。
        # 這裡用最後 CALIBRATION_WINDOW 天的樣本外輸出配 Platt 把它映射回真實頻率。
        n_pl = min(CALIBRATION_WINDOW, len(train) // 4)
        head, held = y_surge[:-n_pl], y_surge[-n_pl:]
        self.calibrator = None
        if 0 < head.sum() < len(head):
            cal_clf = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(
                X[:-n_pl], head)
            raw = cal_clf.predict_proba(X[-n_pl:])[:, 1]
            self.raw_reference = np.sort(raw)
            self.calibration_base_rate = float(held.mean())
            if MIN_CALIBRATION_POSITIVES <= held.sum() < len(held):
                self.calibrator = LogisticRegression().fit(_logit(raw), held)

        # 最終模型用全部資料重訓（標準 split-conformal 作法）
        self.point = HistGradientBoostingRegressor(**POINT_PARAMS).fit(
            X, y, sample_weight=w)
        # 全零或全一時分類器無法訓練（極短序列才會發生）
        if 0 < y_surge.sum() < len(y_surge):
            self.surge = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(X, y_surge)
        return self

    def predict(self, series):
        """對序列最後一天當原點，預測 origin + horizon。"""
        frame = build_features(series, self.horizon, self.threshold)
        return self.predict_frame(frame.iloc[[-1]])[0]

    def predict_frame(self, rows):
        """對 build_features 的多列逐列預測，回傳 dict 串列。"""
        X = rows[self.features].values
        model_point = np.maximum(0.0, self.point.predict(X))
        gated = zero_gate(rows)
        points = np.where(gated, 0.0, model_point)

        if self.surge is not None:
            surge_raw = self.surge.predict_proba(X)[:, 1]
        else:
            surge_raw = np.full(len(rows), self.surge_base_rate)
        surge_p = surge_raw
        if self.calibrator is not None:
            surge_p = self.calibrator.predict_proba(_logit(surge_raw))[:, 1]
        if self.raw_reference is not None and len(self.raw_reference):
            rank = (np.searchsorted(self.raw_reference, surge_raw, side="right")
                    / len(self.raw_reference))
        else:
            rank = np.full(len(rows), np.nan)
        base = (self.calibration_base_rate if self.calibration_base_rate is not None
                else self.surge_base_rate)

        # 區間以模型點預測為中心（閘門不改變不確定性：0 之後照樣可能反彈），
        # 下緣再往下延伸到閘門點預測，確保點預測落在區間內。
        lo_q, hi_q = np.quantile(self.residuals, [0.05, 0.95])
        out = []
        for i in range(len(rows)):
            point = float(points[i])
            out.append({
                "horizon": self.horizon,
                "target_date": rows["_target_date"].iloc[i],
                "point": point,
                "point_model": float(model_point[i]),
                "zero_gated": bool(gated[i]),
                "lower": float(min(point, max(0.0, model_point[i] + lo_q))),
                "upper": float(model_point[i] + hi_q),
                "surge_probability": float(surge_p[i]),
                # 未校準的原始輸出，供上線後監看校準漂移
                "surge_probability_raw": float(surge_raw[i]),
                # 原始分數在校準窗樣本外分數中的百分位，警示用（見 ALERT_RANK）
                "surge_rank": float(rank[i]),
                # 回歸頭推得的機率，只監看 —— 理由見 conformal_surge_prob 的 docstring。
                "surge_probability_point": conformal_surge_prob(
                    float(model_point[i]), self.residuals, self.threshold),
                "surge_calibrated": self.calibrator is not None,
                "surge_base_rate": float(base),
                "surge_signal_valid": self.horizon <= SIGNAL_HORIZON,
            })
        return out


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


# 風險階梯：(等級, 絕對下限, 相對基準發生率的倍數)，由高到低。
# 這是門檻的唯一定義處 —— probability_review.py 的 ladder_unreachable 診斷
# 也讀這裡，不要在下游再寫一次 max(0.20, 1.3 * br)。
RISK_LADDER = (
    ("HIGH", 0.40, 3.0),
    ("MEDIUM-HIGH", 0.30, 2.0),
    ("MEDIUM", 0.20, 1.3),
)


def risk_thresholds(base_rate):
    """各等級的實際切點：max(絕對下限, 倍數 x 基準發生率)。"""
    return {name: max(floor, lift * base_rate)
            for name, floor, lift in RISK_LADDER}


ALERT_LEVELS = ("HIGH", "MEDIUM-HIGH")   # 推播裡出現 🟠/🔴 的等級


def risk_level(surge_p, signal_valid, base_rate, surge_rank=None):
    """把 surge 機率轉成等級。

    門檻取自回測操作點：>=2x lift 才叫 HIGH。訊號無效的 horizon
    一律回 UNKNOWN，不要用一個 AUC 0.5 的數字去嚇人。
    surge_rank >= ALERT_RANK 時至少 MEDIUM-HIGH（理由見 ALERT_RANK）。
    """
    if not signal_valid:
        return "UNKNOWN"
    thresholds = risk_thresholds(base_rate)
    level = "LOW"
    for name, _, _ in RISK_LADDER:
        if surge_p >= thresholds[name]:
            level = name
            break
    if surge_rank is not None and surge_rank == surge_rank \
            and surge_rank >= ALERT_RANK and level not in ALERT_LEVELS:
        level = "MEDIUM-HIGH"
    return level
