#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日預測產生器 — 以 pla_surge_model.SurgeForecaster 為核心。

取代 pla_7day_predictor.py（CatBoost）成為 daily_prediction.yml 的進入點。
舊檔保留不動，可隨時用 --legacy 對照，或把 workflow 改回去回滾。

## 為什麼換

同一個 160 天評估窗（2026-02-17~07-26）、走動式重訓、h=1 實測：

                        線上 CatBoost   SurgeForecaster(thr=20)
    90% 區間覆蓋率            61.9%            95.6%
    bias（實際−預測）          +2.23            -0.44
    ROC-AUC                  0.502            0.636
    PR-AUC (base 0.113)      0.080            0.259
    MAE                       5.72             6.55

MAE 變差是預期的取捨，不是退步。架次分布右偏（mean 7.2 / median 4 / q90 22），
MAE 的最佳解是條件中位數，所以「最小化 MAE」等於「訓練模型系統性低估」——
舊模型 160 天裡點預測從未超過 17.4，實際最高 36，18 次 surge 一次都沒抓到。
判定成敗請看 cov90 / pin90 / PR-AUC，不要看 MAE。

## 刻意不做的事

- 不做遞迴餵回：每個 horizon 各一個模型（direct multi-horizon）
- 不做天氣調整：稽核記錄增益 +0.011 [-0.076,+0.143]，信賴區間跨 0
- 不對區間做事後加寬：conformal 已校準，再加寬會破壞它
- h>=2 不輸出 surge 機率（risk_level 回 UNKNOWN）：實測 AUC 0.45-0.57

## 3.1 Adaptive Risk（2026-08）—— shadow 模式

起因：base rate 0.169 下 RISK_LADDER 的 MEDIUM 切點是 0.22，而上線 16 天校準後機率
最高只有 0.181 —— 絕對階梯整段不可達，連日顯示 LOW，2 次 surge 全數漏報。

3.1 保留全部機率計算不動（Poisson 回歸、Platt、conformal 一行未改），另外算「今天的
機率排在歷史 walk-forward OOS 分布的第幾位」。警示點與 backtest_predictor.score() 的
budget=0.20 統一成同一個常數（ALERT_BUDGET）—— 在這之前，回測判定模型能力的操作點
與線上發警報的操作點是兩套標準。

**但驗收沒過，所以預設是 shadow：**ADAPTIVE_RISK_ENABLED=False，risk_level 仍由絕對
階梯決定，新欄位只記錄「如果開了會怎樣」。4 個評估窗只有近 365 天那個通過
「召回倍數 >= 2x」；而且在目前這段安靜期，3.1 與 3.0 的輸出逐日相同（16 天全部零警示，
08-08 的 18.1% 對前 365 天 OOS 只排第 46 百分位）。完整數字見 pla_surge_model.py 的
ADAPTIVE_RISK_ENABLED 註解與 docs/data_pipeline_review.md §3.2。

參考分布由 scripts/analysis/build_oos_probabilities.py 產生；缺檔時整層安靜降級。

用法:
    python3 predict_surge_daily.py
    python3 predict_surge_daily.py --dry-run    # 只印，不寫檔
"""

import argparse
import os
from datetime import datetime

import numpy as np
import pandas as pd

from pla_surge_model import (
    ADAPTIVE_RISK_ENABLED,
    REFERENCE_WINDOW_DAYS,
    SIGNAL_HORIZON,
    SURGE_THRESHOLD,
    SurgeForecaster,
    adaptive_risk,
    to_daily_series,
)

MODEL_VERSION = "3.1.0-shadow"
PREDICTION_DAYS = 7

SORTIES_LOCAL = "data/JapanandBattleship.csv"
SORTIES_URL = ("https://raw.githubusercontent.com/s0914712/pla-data-dashboard/"
               "main/data/JapanandBattleship.csv")
HOLIDAYS_LOCAL = "data/cn_holidays.csv"
OUTPUT_PATH = "data/predictions/latest_prediction.csv"
# 3.1 的參考分布。由 scripts/analysis/build_oos_probabilities.py 產生並每日增量。
# 缺檔不是錯誤 —— adaptive_risk() 會安靜降級成 3.0 的純絕對階梯。
OOS_REFERENCE_PATH = "data/predictions/oos_probabilities.csv"

# 輸出欄位順序必須與舊版一致 —— prediction.html、scripts/send_message.py
# 與 daily_prediction.yml 都直接讀這些欄名。新欄位一律加在尾端。
LEGACY_COLUMNS = [
    "date", "day_of_week", "predicted_sorties", "lower_bound", "upper_bound",
    "high_event_probability", "risk_level", "is_cn_holiday", "weather_adjustment",
    "cn_stmt_7d", "ema_7", "ema_14", "generated_at", "model_version",
    "data_latest_date", "cv_mae", "actual_sorties", "prediction_error",
]
NEW_COLUMNS = [
    "horizon", "surge_threshold", "prob_signal_valid",
    "high_event_probability_raw", "prob_calibrated",
    # 回歸頭由 conformal 殘差推得的 P(>=門檻)。純監看，不參與 high_event_probability
    # 的計算 —— 實測把它併進校準器會讓 PR-AUC 掉 10%，理由見
    # pla_surge_model.conformal_surge_prob 的 docstring。留這一欄是為了讓下次
    # 重提這個想法的人手上直接有並排紀錄。
    "high_event_probability_point",
]
# 3.1 自適應風險層（shadow）。一律加在尾端（見上）。
#
# ADAPTIVE_RISK_ENABLED = False 時，risk_level 仍由絕對階梯決定 —— 這一組欄位
# 純粹是紀錄「如果開了會怎樣」，好讓 probability_review 每天把兩組操作點並列。
# risk_level_adaptive 就是開啟後 risk_level 會變成的值；兩欄相同代表當天
# 百分位沒有把等級推高。
ADAPTIVE_COLUMNS = [
    "risk_level_adaptive", "risk_percentile", "relative_risk",
    "alert", "alert_threshold_prob", "reference_n",
]


def load_sorties():
    """優先讀本機 checkout，讀不到才回退到 raw URL。

    舊版無條件讀 raw.githubusercontent.com，即使 workflow 已經 checkout 了 repo。
    那會受 CDN 快取影響，且本機重現不了 CI 的結果。
    """
    if os.path.exists(SORTIES_LOCAL):
        src = SORTIES_LOCAL
    else:
        print(f"⚠️  找不到 {SORTIES_LOCAL}，改用遠端")
        src = SORTIES_URL
    df = pd.read_csv(src, encoding="utf-8-sig")
    print(f"[1] 讀取架次資料: {src} ({len(df)} 列)")
    return df


def load_holidays():
    """回傳中國假日日期集合；讀不到就回空集合（欄位仍會輸出 0）。"""
    if not os.path.exists(HOLIDAYS_LOCAL):
        return set()
    try:
        h = pd.read_csv(HOLIDAYS_LOCAL, encoding="utf-8-sig")
        col = "date" if "date" in h.columns else h.columns[0]
        return set(pd.to_datetime(h[col], errors="coerce").dropna().dt.date)
    except Exception as e:
        print(f"⚠️  讀取假日表失敗: {e}")
        return set()


def load_oos_reference(latest_date, path=OOS_REFERENCE_PATH):
    """讀 3.1 的參考分布：h=1、已校準、且**嚴格早於今天**的 OOS 機率。

    三道過濾各有理由，少一道就會出錯：

      * horizon == SIGNAL_HORIZON —— 只有 h=1 會發警報，h>=2 的機率不具鑑別力。
      * surge_calibrated == 1 —— Platt 沒啟用時機率是原始的 balanced 輸出，
        實測膨脹約 1.9 倍，混進參考分布排出來的是尺度差異不是風險差異。
      * target_date <= latest_date 且在 REFERENCE_WINDOW_DAYS 內 —— 前者防
        leakage（參考分布不得含有預測目標日之後的資訊），後者讓門檻跟得上
        模型行為的長期漂移。

    缺檔／格式不符一律安靜回 None，adaptive_risk() 會退回 3.0 的行為。
    """
    if not os.path.exists(path):
        print(f"    參考分布 {path} 不存在 —— 風險等級退回絕對階梯（3.0 行為）")
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:
        print(f"⚠️  讀取參考分布失敗: {e}")
        return None

    need = {"target_date", "horizon", "surge_p", "surge_calibrated"}
    if not need.issubset(df.columns):
        print(f"⚠️  參考分布缺欄位，需要 {sorted(need)}")
        return None

    df["target_date"] = pd.to_datetime(df["target_date"], errors="coerce")
    window_start = latest_date - pd.Timedelta(days=REFERENCE_WINDOW_DAYS)
    df = df[(df["horizon"] == SIGNAL_HORIZON)
            & (df["surge_calibrated"] == 1)
            & df["target_date"].notna()
            & (df["target_date"] <= latest_date)
            & (df["target_date"] > window_start)]

    ref = pd.to_numeric(df["surge_p"], errors="coerce").dropna().to_numpy()
    if len(ref) == 0:
        print("⚠️  參考分布在窗內沒有可用列")
        return None
    print(f"    參考分布 {len(ref)} 列（{window_start.date()} .. {latest_date.date()}）"
          f"　p80={np.quantile(ref, 0.8):.3f}")
    return ref


def build_rows(series, holidays, reference=None):
    """訓練 + 產生未來 PREDICTION_DAYS 天的預測列。"""
    print(f"[2] 訓練 SurgeForecaster (門檻 >={SURGE_THRESHOLD}, "
          f"horizons 1..{PREDICTION_DAYS})")
    fc = SurgeForecaster(horizons=range(1, PREDICTION_DAYS + 1),
                         threshold=SURGE_THRESHOLD).fit(series)
    results = fc.predict(series)

    ema7 = series.ewm(span=7, min_periods=2).mean().iloc[-1]
    ema14 = series.ewm(span=14, min_periods=2).mean().iloc[-1]
    # conformal 校準集的平均絕對殘差，語意上對應舊欄位 cv_mae
    cal_mae = float(np.mean(np.abs(fc.models[1].residuals)))
    latest_date = series.dropna().index.max()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for r in results:
        d = pd.Timestamp(r["target_date"])
        valid = bool(r["surge_signal_valid"])
        prob = round(r["surge_probability"] * 100, 1)
        # 3.1：機率一個位元都沒改，只是多問一句「這個機率排在歷史第幾位」。
        risk = adaptive_risk(r["surge_probability"], valid, r["surge_base_rate"],
                             reference=reference,
                             calibrated=bool(r["surge_calibrated"]))
        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "day_of_week": d.strftime("%A"),
            "predicted_sorties": round(r["point"], 1),
            "lower_bound": round(r["lower"], 1),
            "upper_bound": round(r["upper"], 1),
            # h>=2 的機率不具鑑別力，寧可留空也不要輸出一個會被當真的數字
            "high_event_probability": prob if valid else np.nan,
            "risk_level": risk["level"],
            "is_cn_holiday": int(d.date() in holidays),
            "weather_adjustment": 1.0,      # 不再做天氣調整，保留欄位相容性
            "cn_stmt_7d": 0,                # 同上：特徵已移除，欄位保留
            "ema_7": round(float(ema7), 1),
            "ema_14": round(float(ema14), 1),
            "generated_at": now,
            "model_version": MODEL_VERSION,
            "data_latest_date": latest_date.strftime("%Y-%m-%d"),
            "cv_mae": round(cal_mae, 2),
            "actual_sorties": np.nan,
            "prediction_error": np.nan,
            "horizon": r["horizon"],
            "surge_threshold": SURGE_THRESHOLD,
            "prob_signal_valid": int(valid),
            "high_event_probability_raw": round(r["surge_probability_raw"] * 100, 1),
            "prob_calibrated": int(r["surge_calibrated"]),
            "high_event_probability_point": round(
                r["surge_probability_point"] * 100, 1),
            "risk_level_adaptive": risk["level_adaptive"],
            "risk_percentile": (np.nan if risk["percentile"] is None
                                else round(risk["percentile"] * 100, 1)),
            "relative_risk": risk["relative"],
            "alert": int(risk["alert"]),
            "alert_threshold_prob": (np.nan if risk["alert_threshold_prob"] is None
                                     else round(risk["alert_threshold_prob"] * 100, 1)),
            "reference_n": risk["reference_n"],
        })
    return pd.DataFrame(rows)


def merge_history(predictions, series, output_path):
    """合併既有紀錄並回填實際值。

    同日期的舊預測會被當天的新預測取代，所以留在檔案裡的過去日期都是 h=1，
    誤差統計因此一律是 h=1 的誤差 —— 這正是最有意義的那個 horizon。
    """
    actual = {d.strftime("%Y-%m-%d"): float(v)
              for d, v in series.dropna().items()}

    existing = pd.DataFrame()
    if os.path.exists(output_path):
        try:
            existing = pd.read_csv(output_path, encoding="utf-8-sig")
            print(f"[3] 既有紀錄 {len(existing)} 列")
        except Exception as e:
            print(f"⚠️  讀取既有預測失敗: {e}")

    if not existing.empty:
        overlap = set(existing["date"]) & set(predictions["date"])
        existing = existing[~existing["date"].isin(overlap)]
        combined = pd.concat([existing, predictions], ignore_index=True)
    else:
        combined = predictions.copy()

    combined["actual_sorties"] = combined["date"].map(actual)
    mask = combined["actual_sorties"].notna() & combined["predicted_sorties"].notna()
    # 慣例：prediction_error = actual - predicted（正值 = 實際比預測高）。
    #
    # 這與 backtest_predictor.score() / model_comparison.json 的 bias 欄剛好相反，
    # 那邊是 predicted - actual。兩者都沒錯，但混用過就會出事，所以記在這裡：
    #   * CSV 這一欄是「給人看的誤差」，LINE 推播與 prediction.html 的誤差圖
    #     都直接顯示它，翻號等於把所有既有畫面的正負對調 —— 不要為了統一而翻。
    #   * 計分程式一律用 predicted - actual，並在輸出裡帶 bias_convention 欄標明。
    # MAE/RMSE 對正負不敏感，兩邊算出來一致，會分歧的只有 bias。
    combined.loc[mask, "prediction_error"] = (
        combined.loc[mask, "actual_sorties"] - combined.loc[mask, "predicted_sorties"])

    for col in LEGACY_COLUMNS + NEW_COLUMNS + ADAPTIVE_COLUMNS:
        if col not in combined.columns:
            combined[col] = np.nan
    ordered = LEGACY_COLUMNS + NEW_COLUMNS + ADAPTIVE_COLUMNS
    extra = [c for c in combined.columns if c not in ordered]
    return combined[ordered + extra].sort_values("date").reset_index(drop=True)


def report(predictions, combined):
    print("\n" + "=" * 78)
    print(f"[{PREDICTION_DAYS}-Day Prediction] SurgeForecaster {MODEL_VERSION}")
    print("=" * 78)
    print(f"{'Date':<12}{'Day':<11}{'Pred':>7}{'90% 區間':>16}{'P(high)':>10}"
          f"{'百分位':>8}  Risk")
    print("-" * 78)
    for _, r in predictions.iterrows():
        ci = f"[{r['lower_bound']:.0f} - {r['upper_bound']:.0f}]"
        p = ("     -" if pd.isna(r["high_event_probability"])
             else f"{r['high_event_probability']:5.1f}%")
        pct = ("      -" if pd.isna(r["risk_percentile"])
               else f"{r['risk_percentile']:5.0f}th")
        flag = " 🟠 ALERT" if r["alert"] else ""
        print(f"{r['date']:<12}{r['day_of_week']:<11}{r['predicted_sorties']:>7.1f}"
              f"{ci:>16}{p:>10}{pct:>8}  {r['risk_level']}{flag}")

    d1 = predictions.iloc[0]
    if not pd.isna(d1["risk_percentile"]):
        # 兩個數字回答不同問題：絕對機率答「明天實際發生的機率多大」，
        # 百分位答「和平常相比今天是不是異常值得注意」。兩者不矛盾。
        state = "已啟用" if ADAPTIVE_RISK_ENABLED else "shadow，未套用"
        print(f"\nD+1 相對百分位 第 {d1['risk_percentile']:.0f} 百分位"
              f"（警示線 {d1['alert_threshold_prob']:.1f}%／參考 "
              f"{int(d1['reference_n'])} 列）　相對階梯 {d1['relative_risk']}")
        print(f"    3.1 若啟用會顯示 {d1['risk_level_adaptive']}"
              f"（目前 {d1['risk_level']}）—— {state}")

    done = combined[combined["actual_sorties"].notna()
                    & combined["prediction_error"].notna()]
    if len(done) >= 10:
        err = done["prediction_error"].astype(float)
        cov = ((done["actual_sorties"] >= done["lower_bound"])
               & (done["actual_sorties"] <= done["upper_bound"])).mean()
        print(f"\n歷史回填 N={len(done)}  MAE={err.abs().mean():.2f}  "
              f"bias={err.mean():+.2f}  90% 區間覆蓋率={cov:.1%}")
        print("（此統計混合了新舊模型的紀錄，換模型後需累積一段時間才具代表性）")


def main():
    ap = argparse.ArgumentParser(description="每日 PLA 架次預測（SurgeForecaster）")
    ap.add_argument("--dry-run", action="store_true", help="只印出結果，不寫檔")
    ap.add_argument("--output", default=OUTPUT_PATH)
    args = ap.parse_args()

    series = to_daily_series(load_sorties())
    print(f"    日曆序列 {series.index.min().date()} .. {series.index.max().date()}"
          f"  ({series.notna().sum()} 個觀測日 / {len(series)} 日曆日)")

    holidays = load_holidays()
    reference = load_oos_reference(series.dropna().index.max())
    predictions = build_rows(series, holidays, reference)
    combined = merge_history(predictions, series, args.output)
    report(predictions, combined)

    if args.dry_run:
        print("\n🏁 Dry-run — 未寫檔")
        return

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    combined.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"\n✅ 已寫入 {args.output}（{len(combined)} 列）")


if __name__ == "__main__":
    main()
