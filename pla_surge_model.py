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
        self.calibrator = None     # Platt，把分類器輸出映回真實機率

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

        # 機率校準。SURGE_PARAMS 用 class_weight='balanced'，那是為了讓分類器
        # 在稀疏正樣本下學得動，代價是輸出機率被整體推高 —— 實測 Brier 0.1230
        # 比「全押 base rate」的 0.0998 還差，也就是那個百分比本身不能當機率讀。
        # 這裡用跟 conformal 同一段樣本外資料配 Platt 把它映射回真實頻率。
        head, held = y_surge[:-n_cal], y_surge[-n_cal:]
        if 0 < head.sum() < len(head) and \
                MIN_CALIBRATION_POSITIVES <= held.sum() < len(held):
            cal_clf = HistGradientBoostingClassifier(**SURGE_PARAMS).fit(
                X[:-n_cal], head)
            raw = cal_clf.predict_proba(X[-n_cal:])[:, 1]
            self.calibrator = LogisticRegression().fit(_logit(raw), held)

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
            surge_raw = float(self.surge.predict_proba(X)[0, 1])
        else:
            surge_raw = self.surge_base_rate

        surge_p = surge_raw
        if self.calibrator is not None:
            surge_p = float(self.calibrator.predict_proba(_logit([surge_raw]))[0, 1])

        # 只監看，不參與上面的機率 —— 理由見 conformal_surge_prob 的 docstring。
        p_point = conformal_surge_prob(point, self.residuals, self.threshold)

        lo_q, hi_q = np.quantile(self.residuals, [0.05, 0.95])
        return {
            "horizon": self.horizon,
            "target_date": row["_target_date"].iloc[0],
            "point": point,
            "lower": float(max(0.0, point + lo_q)),
            "upper": float(point + hi_q),
            "surge_probability": surge_p,
            # 未校準的原始輸出，供上線後監看校準漂移
            "surge_probability_raw": surge_raw,
            # 回歸頭推得的機率，單獨留一欄才看得出兩個訊號何時分歧
            "surge_probability_point": p_point,
            "surge_calibrated": self.calibrator is not None,
            "surge_base_rate": self.surge_base_rate,
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


# 絕對階梯的警示線就是 MEDIUM-HIGH 那一列 —— 推播裡真的出現 🟠/🔴 的那條線。
# 具名常數，讓下游不必自己知道「是哪一階」。
ALERT_LEVEL = "MEDIUM-HIGH"


def absolute_alert_threshold(base_rate):
    """3.0 的警示線。probability_review 與驗收回測都該用這個，不要各自手抄。

    在這之前 probability_review.py 自己寫了一份 ALERT_FLOOR=0.30/ALERT_LIFT=2.0，
    與這裡是兩份定義 —— 改了 RISK_LADDER 而忘了改那邊，兩邊就會靜靜地分家。
    """
    return risk_thresholds(base_rate)[ALERT_LEVEL]


def risk_level(surge_p, signal_valid, base_rate):
    """把 surge 機率轉成等級。

    門檻取自回測操作點：>=2x lift 才叫 HIGH。訊號無效的 horizon
    一律回 UNKNOWN，不要用一個 AUC 0.5 的數字去嚇人。
    """
    if not signal_valid:
        return "UNKNOWN"
    thresholds = risk_thresholds(base_rate)
    for name, _, _ in RISK_LADDER:
        if surge_p >= thresholds[name]:
            return name
    return "LOW"


# ============================================================
# 3.1 自適應風險層（相對百分位）
# ============================================================
#
# 上面那座階梯問的是「這個機率絕對值高不高」。它沒有壞，但在目前的機率尺度下
# 整段不可達：base rate 0.169 時 MEDIUM 切點是 max(0.20, 1.3*0.169) = 0.22，
# 而上線 16 天校準後機率最高只有 0.181 —— 於是模型認為「最近最值得注意」的那天
# 仍然顯示 LOW。這不是模型沒訊號，是決策層與機率尺度不匹配。
#
# 這一層另外問：「今天這個機率，排在歷史 OOS 機率分布的第幾位？」
# 機率本身一個位元都不改 —— 改的是怎麼解讀它。
#
# ## 為什麼這不是「把門檻搬下來」
#
# probability_review.py 的 ladder_unreachable 診斷寫過一條明確的拒絕：
# 「要修的是沉默，不是門檻」。這一層沒有違反它：
#
#   1. 絕對階梯不下修。risk_level() 原封不動，其結果由呼叫端保留成
#      risk_level_absolute，並仍以 combine_risk_levels() 參與最終等級 ——
#      機率真的到 40% 時不必等百分位同意就是 HIGH。
#   2. 百分位錨定 REFERENCE_WINDOW_DAYS 天的參考窗，不是錨定當週。真正的
#      安靜期，當日機率打不過去年的第 80 百分位，警示率會自然掉到 20% 以下。
#      警示率是「參考期平均 20%」，不是「每週固定 20%」—— 這是這個設計不會
#      把沉默偽裝成訊號的關鍵。
#   3. 上線前必須通過回測閘：無鑑別力的模型在 20% budget 下 recall 期望值
#      就是 20%。閘門要求明顯高於它，過不了就不該上。
# ── 上線開關 ──────────────────────────────────────────────
#
# False = shadow 模式：百分位照算、照寫進 CSV、probability_review 照並列比較，
#         但 risk_level 仍由絕對階梯決定，使用者看到的東西與 3.0 完全相同。
#
# 為什麼預設關著（2026-08-11 實測，1519 列 walk-forward OOS）：
#
#   評估窗    警示率   召回   召回倍數   3.0召回   ROC-AUC   Brier技能
#    365天    15.1%  33.9%    2.25x    30.6%    0.674     +4.9%   ✅
#    730天    28.7%  40.1%    1.40x    10.5%    0.608     +2.3%   ❌
#   1095天    19.9%  27.3%    1.37x     6.6%    0.568     +0.6%   ❌
#   1285天    17.9%  24.0%    1.35x     6.5%    0.551     -2.7%   ❌
#
# 4 個窗只有 1 個通過「召回倍數 >= 2x」。擋住的不是決策層 —— 3.1 在每個窗都
# 大幅贏過 3.0 —— 而是模型本身的鑑別力隨窗口拉長由 0.674 掉到 0.551。
#
# 而且在目前這段線上資料上，3.1 的輸出與 3.0 逐日相同：16 天全部零警示。
# 2026-08-08 的 18.1% 對「往前 365 天的 OOS 分布」只排第 46 百分位（警示線 29.6%），
# 因為近一年含有活躍得多的時段，最近幾週是真的安靜。兩次 surge 的機率本身也低
# （07-31 的 16.4% 排 37th、08-05 的 10.8% 排 18th）—— 那是模型沒看到，不是門檻擋掉。
#
# 所以先影子運行累積證據。要上線只需把這個常數改成 True，
# risk_level、LINE 推播的百分位敘述會一起生效，不必動其他地方。
ADAPTIVE_RISK_ENABLED = False

ALERT_BUDGET = 0.20
REFERENCE_WINDOW_DAYS = 365
# 參考分布的最小樣本數。少於這個數，百分位的解析度比雜訊還粗
# （120 天在 base rate 0.17 下約 20 個正樣本，與 CONFORMAL_WINDOW 同量級）。
MIN_REFERENCE_N = 120

# 百分位階梯：(等級, 百分位下限)，由高到低。與 RISK_LADDER 同樣順序 load-bearing。
#
# MEDIUM-HIGH 的切點寫成 1 - ALERT_BUDGET，不另外寫死 0.80。
# backtest_predictor.score() 用 budget=0.20 取「機率排名前 20%」判定模型的 surge
# 偵測能力，但線上發警報用的是完全另一套絕對門檻 —— 3.1 要修的就是這個不一致。
# 兩者必須是同一個常數，否則哪天有人調了 budget，回測與線上又會默默分家。
PERCENTILE_LADDER = (
    ("HIGH", 0.95),
    ("MEDIUM-HIGH", 1 - ALERT_BUDGET),
    ("MEDIUM", 0.60),
)

# 等級由低到高。combine_risk_levels() 用它比大小，UNKNOWN 不在其中 —— 它不是
# 一個「比 LOW 低」的等級，而是「這個問題沒有答案」，所以單獨處理。
_LEVEL_ORDER = ("LOW", "MEDIUM", "MEDIUM-HIGH", "HIGH")


def risk_percentile(surge_p, reference, min_n=MIN_REFERENCE_N):
    """surge_p 在 reference 分布中的百分位（0..1）；樣本不足回 None。

    用 mid-rank（小於的比例 + 一半的相等比例）而不是單純 (ref < p).mean()。
    機率會在少數幾個值上叢集（同一個 refit 窗內特徵相近的日子輸出幾乎相同），
    純 < 比較會把一整叢並列的日子全部壓到叢集底部的百分位。
    """
    if surge_p is None:
        return None
    ref = np.asarray(reference, dtype=float)
    ref = ref[~np.isnan(ref)]
    if len(ref) < min_n:
        return None
    below = float(np.mean(ref < surge_p))
    ties = float(np.mean(ref == surge_p))
    return below + 0.5 * ties


def percentile_threshold(reference, budget=ALERT_BUDGET, min_n=MIN_REFERENCE_N):
    """警示線的絕對機率值：參考分布的第 (1 - budget) 百分位。

    只為了可稽核 —— 讓讀報表的人看得到「今天的 top 20% 到底是幾 %」。
    判定警示與否一律走百分位，不走這個值，兩者在 tie 上的行為才一致。
    """
    ref = np.asarray(reference, dtype=float)
    ref = ref[~np.isnan(ref)]
    if len(ref) < min_n:
        return None
    return float(np.quantile(ref, 1 - budget))


def percentile_risk_level(pct):
    """百分位 → 等級。pct 為 None（樣本不足）時回 UNKNOWN。"""
    if pct is None:
        return "UNKNOWN"
    for name, floor in PERCENTILE_LADDER:
        if pct >= floor:
            return name
    return "LOW"


def combine_risk_levels(a, b):
    """取較高的等級。UNKNOWN 代表「這一側沒有意見」，回另一側。

    絕對階梯與百分位階梯回答的是不同問題，兩邊都有理由單獨觸發：
    機率真的到 40% 就該是 HIGH（即使近期普遍更高而百分位不突出），
    而機率 18% 卻是一年來最高的那天也該被看見。
    """
    if a == "UNKNOWN":
        return b
    if b == "UNKNOWN":
        return a
    return max(a, b, key=lambda x: _LEVEL_ORDER.index(x)
               if x in _LEVEL_ORDER else -1)


def adaptive_risk(surge_p, signal_valid, base_rate, reference=None,
                  calibrated=True, budget=ALERT_BUDGET, min_n=MIN_REFERENCE_N,
                  enabled=None):
    """3.1 的唯一對外入口：絕對階梯 + 相對百分位。

    回傳三個等級，語意不同，不要混用：

        level_absolute  純絕對階梯 —— 恆等於 risk_level()，3.0 的行為
        level_adaptive  max(絕對, 百分位) —— 3.1 開啟後會顯示的東西
        level           實際該用哪一個，由 ADAPTIVE_RISK_ENABLED 決定

    shadow 模式（預設）下 level == level_absolute，兩者並存讓我們可以每天記錄
    「如果開了會怎樣」而不改變任何使用者看到的東西。

    reference 是歷史 walk-forward OOS 機率（見 build_oos_probabilities.py）。
    **只能傳入嚴格早於今天的 OOS 機率**，否則就是用未來資訊決定今天要不要警示。

    calibrated=False 時不算百分位。Platt 校準器在 held-out 正樣本不足時不會啟用
    （MIN_CALIBRATION_POSITIVES），那時的機率是 class_weight='balanced' 的原始
    輸出，實測膨脹約 1.9 倍。把它拿去對校準後的參考分布排名，排出來的名次是
    尺度差異而不是風險差異。

    降級是安靜且安全的：signal_valid=False、reference 缺漏、樣本不足、未校準 ——
    任一成立就退回純 risk_level()，也就是與 3.0 完全相同的行為。
    """
    absolute = risk_level(surge_p, signal_valid, base_rate)

    pct = None
    if signal_valid and calibrated and reference is not None:
        pct = risk_percentile(surge_p, reference, min_n)

    relative = percentile_risk_level(pct)
    cut = (percentile_threshold(reference, budget, min_n)
           if pct is not None else None)

    ref_n = 0
    if reference is not None:
        ref = np.asarray(reference, dtype=float)
        ref_n = int((~np.isnan(ref)).sum())

    adaptive = combine_risk_levels(absolute, relative)
    if enabled is None:
        enabled = ADAPTIVE_RISK_ENABLED

    return {
        "level": adaptive if enabled else absolute,
        "level_absolute": absolute,
        "level_adaptive": adaptive,
        "enabled": bool(enabled),
        "percentile": pct,
        "relative": relative,
        # 警示 = 百分位進入前 budget。與 PERCENTILE_LADDER 的 MEDIUM-HIGH
        # 切點同源，所以 alert 恆等於 relative in (MEDIUM-HIGH, HIGH)。
        "alert": bool(pct is not None and pct >= 1 - budget),
        "alert_threshold_prob": cut,
        "reference_n": ref_n,
    }
