#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新舊預測模型的每日正面對決（head-to-head）。

2026-07-26 起線上模型換成 SurgeForecaster，舊的 CatBoost 模型停跑但保留為回滾備案。
問題是換完之後沒有任何持續證據能證明新模型在真實資料上比較好 ——
`latest_prediction.csv` 裡 07-27 之前是舊模型、之後是新模型的紀錄混在一起，
算出來的 MAE 誰都不代表（predict_surge_daily.report() 自己也註明了這點）。

解法是雙軌：每天兩個模型都跑同一份輸入，舊模型以影子模式寫到獨立檔案，
再由這支腳本比對「兩邊都預測過、且已有實際值」的日期。

兩個模式：

    # 1) 驗證舊模型的候選輸出，通過才覆蓋正式的影子檔
    python3 scripts/analysis/compare_predictors.py --promote /tmp/legacy_candidate.csv

    # 2) 計算對照指標，輸出給 LINE 端消費的 JSON
    python3 scripts/analysis/compare_predictors.py

## 刻意只比點預測與區間

兩個模型的「高架次」門檻不同 —— 新模型 SURGE_THRESHOLD=20、舊模型 HIGH_THRESHOLD=25。
`high_event_probability` 在兩邊回答的不是同一個問題，所以 PR-AUC / ROC-AUC / Brier
放在一起比會是錯的。要比機率就得改舊模型的門檻，但那會動到回滾備案的行為，
不值得。因此這裡只比 MAE / bias / pin90 / cov90 —— 而 cov90 正是當初換模型的主因
（舊模型名目 90% 的區間實測只蓋到 54-62%）。
"""

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from scripts.analysis.backtest_predictor import score  # noqa: E402

NEW_CSV = "data/predictions/latest_prediction.csv"
LEGACY_CSV = "data/predictions/legacy_prediction.csv"
OUTPUT_JSON = "data/predictions/model_comparison.json"

NEW_LABEL = "SurgeForecaster 3.0.0"
LEGACY_LABEL = "CatBoost 2.8.0"

# 少於這個天數不輸出指標。3 天的 MAE 差距幾乎全是雜訊，但看到數字的人一定會當結論。
MIN_N = 10

# 並排顯示的未來天數，與 send_message.FORECAST_DAYS 一致。
FORECAST_DAYS = 3

# 影子檔多久沒更新就視為過期（天）。舊模型今天跑掛了會沿用昨天的檔，
# 沿用一天可以接受，沿用一週就該講出來。
STALE_AFTER_DAYS = 2

METRIC_KEYS = ["MAE", "bias", "pin90", "cov90", "width"]

# backtest_predictor.score() 算的是 err = point - actual，正值代表「高估」。
# predict_surge_daily.report() 用的是相反的慣例（實際−預測），兩邊的 bias 符號是反的。
# 這個字串會寫進 JSON，消費端（LINE）必須照這個慣例解讀，不要自己猜。
BIAS_CONVENTION = "predicted_minus_actual"


def load_predictions(path):
    """讀預測 CSV，date 轉成 datetime 並以 date 當 index。讀不到回 None。"""
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:
        print(f"⚠️  讀取 {path} 失敗: {e}")
        return None
    if "date" not in df.columns:
        print(f"⚠️  {path} 缺少 date 欄位")
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    # 同一天理論上只會有一列（兩個模型都會 dedupe），保險起見留最後一列
    return df.drop_duplicates(subset=["date"], keep="last").set_index("date").sort_index()


def is_error_output(df):
    """舊模型的 __main__ 在例外時會寫一份 risk_level='ERROR' 的 CSV 到 --output。

    若直接當成正常輸出搬進影子檔，一次失敗就會把整份影子歷史蓋掉，
    而且會在對照裡留下一列假預測。
    """
    if df is None or df.empty:
        return True
    if "risk_level" in df.columns and (
            df["risk_level"].astype(str).str.upper() == "ERROR").any():
        return True
    if "predicted_sorties" not in df.columns:
        return True
    return not df["predicted_sorties"].notna().any()


def promote(candidate_path, target_path=LEGACY_CSV):
    """驗證舊模型的候選輸出，通過才覆蓋正式影子檔。回傳 True 表示已更新。"""
    cand = load_predictions(candidate_path)
    if is_error_output(cand):
        print(f"❌ 候選輸出未通過檢查，保留既有的 {target_path}")
        return False

    # 舊模型自己會 merge 既有紀錄，但它 merge 的是 --output 指向的檔案（暫存路徑，空的），
    # 所以每天的候選檔只有未來 7 天。歷史要在這裡接起來，否則影子檔永遠沒有過去的紀錄可比。
    existing = load_predictions(target_path)
    if existing is not None and not existing.empty:
        keep = existing[~existing.index.isin(cand.index)]
        combined = pd.concat([keep, cand]).sort_index()
    else:
        combined = cand

    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    combined.reset_index().to_csv(target_path, index=False, encoding="utf-8-sig")
    print(f"✅ 影子檔已更新：{target_path}（{len(combined)} 列）")
    return True


def backfill_actuals(df, actual):
    """把實際架次回填進影子檔。

    舊模型原本靠 run() 自己回填，但它 merge 的是暫存路徑，回填不到累積下來的歷史，
    所以這裡要自己補。新模型的檔案由 predict_surge_daily.merge_history() 負責，不動它。
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    df["actual_sorties"] = df.index.map(actual)
    mask = df["actual_sorties"].notna() & df["predicted_sorties"].notna()
    df["prediction_error"] = np.nan
    df.loc[mask, "prediction_error"] = (
        df.loc[mask, "actual_sorties"] - df.loc[mask, "predicted_sorties"])
    return df


def load_actuals(path="data/JapanandBattleship.csv"):
    """觀測到的每日架次，供影子檔回填。"""
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
        df = df.dropna(subset=["date", "pla_aircraft_sorties"])
        return dict(zip(df["date"], df["pla_aircraft_sorties"].astype(float)))
    except Exception as e:
        print(f"⚠️  讀取實際架次失敗: {e}")
        return {}


def _scored(label, df, dates):
    """對交集日期算指標。surge_p 固定給 None —— 見檔頭「刻意只比點預測與區間」。"""
    sub = df.reindex(dates)
    row = score(
        label,
        sub["predicted_sorties"].values.astype(float),
        None,
        sub["actual_sorties"].values.astype(float),
        lower=sub["lower_bound"].values.astype(float),
        upper=sub["upper_bound"].values.astype(float),
    )
    out = {"label": label}
    for k in METRIC_KEYS:
        v = row.get(k)
        out[k] = None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), 4)
    return out


def _forecast_rows(new_df, legacy_df, days=FORECAST_DAYS):
    """未來 N 天的並排點預測。先算好，LINE 端就不必再讀第二份 CSV。"""
    def future(df):
        if df is None or df.empty:
            return pd.DataFrame()
        if "actual_sorties" in df.columns:
            f = df[df["actual_sorties"].isna()]
            if not f.empty:
                return f
        return df

    fnew = future(new_df)
    if fnew.empty:
        return []

    def cell(df, d):
        if df is None or d not in df.index:
            return None
        r = df.loc[d]
        vals = {}
        for src, dst in (("predicted_sorties", "point"), ("lower_bound", "lower"),
                         ("upper_bound", "upper")):
            v = r.get(src)
            vals[dst] = None if v is None or pd.isna(v) else round(float(v), 1)
        return None if vals["point"] is None else vals

    rows = []
    for d in list(fnew.index)[:days]:
        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "new": cell(new_df, d),
            "legacy": cell(legacy_df, d),
        })
    return rows


def head_to_head(new_df, legacy_df, start=None, min_n=MIN_N):
    """組出給 LINE 端消費的對照結果。"""
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n": 0,
        "min_n": min_n,
        "legacy_ok": legacy_df is not None and not legacy_df.empty,
        "legacy_stale": False,
        "legacy_asof": None,
        "bias_convention": BIAS_CONVENTION,
        # 消費端（LINE）會把這句包在全形括號裡，所以本身不要再帶括號
        "note": "高架次門檻不同：新 ≥20、舊 ≥25，故不比較機率類指標",
        "models": {"new": {"label": NEW_LABEL}, "legacy": {"label": LEGACY_LABEL}},
        "forecast": _forecast_rows(new_df, legacy_df),
    }

    if legacy_df is not None and not legacy_df.empty and "generated_at" in legacy_df.columns:
        gen = pd.to_datetime(legacy_df["generated_at"], errors="coerce").dropna()
        if not gen.empty:
            asof = gen.max()
            result["legacy_asof"] = asof.strftime("%Y-%m-%d")
            result["legacy_stale"] = (datetime.now() - asof).days > STALE_AFTER_DAYS

    if new_df is None or legacy_df is None:
        return result

    def evaluable(df):
        cols = {"predicted_sorties", "actual_sorties", "lower_bound", "upper_bound"}
        if not cols.issubset(df.columns):
            return df.iloc[0:0]
        return df[df["actual_sorties"].notna() & df["predicted_sorties"].notna()]

    a, b = evaluable(new_df), evaluable(legacy_df)
    # 交集自動排除了雙軌上線之前的日期：那些日期只存在於 latest_prediction.csv
    # （而且 07-27 以前那些列本來就是舊模型產生的，拿來當「新模型」比會是錯的）。
    dates = a.index.intersection(b.index)
    if start:
        dates = dates[dates >= pd.Timestamp(start)]
    dates = dates.sort_values()

    result["n"] = len(dates)
    result["window"] = ([dates.min().strftime("%Y-%m-%d"),
                         dates.max().strftime("%Y-%m-%d")] if len(dates) else None)
    if len(dates) == 0:
        return result

    result["models"]["new"] = _scored(NEW_LABEL, a, dates)
    result["models"]["legacy"] = _scored(LEGACY_LABEL, b, dates)
    return result


def report(res):
    print("\n" + "=" * 72)
    print(f"新舊模型對照  N={res['n']}"
          + (f"  ({res['window'][0]} .. {res['window'][1]})" if res.get("window") else ""))
    print("=" * 72)
    if res["n"] == 0:
        print("尚無兩邊都有預測且已有實際值的日期 —— 雙軌剛上線時這是正常的。")
        return
    print(f"{'model':<26}{'MAE':>8}{'bias':>9}{'pin90':>8}{'cov90':>8}{'width':>8}"
          "   (bias = 預測−實際，正值為高估)")
    print("-" * 72)
    for key in ("new", "legacy"):
        m = res["models"][key]

        def cell(k, fmt="{:>8.2f}", scale=1.0):
            v = m.get(k)
            return "       -" if v is None else fmt.format(v * scale)

        print(f"{m['label']:<26}"
              f"{cell('MAE')}{cell('bias', '{:>+9.2f}')}{cell('pin90')}"
              f"{cell('cov90', '{:>7.0f}%', 100)}{cell('width')}")
    print(f"\n註：{res['note']}")
    if res["n"] < res["min_n"]:
        print(f"⚠️  樣本不足（需 {res['min_n']} 日），這組數字還不能當結論。")


def main():
    ap = argparse.ArgumentParser(description="新舊預測模型正面對決")
    ap.add_argument("--promote", metavar="CSV",
                    help="驗證舊模型候選輸出並更新影子檔，然後結束")
    ap.add_argument("--new", default=NEW_CSV)
    ap.add_argument("--legacy", default=LEGACY_CSV)
    ap.add_argument("--output", default=OUTPUT_JSON)
    ap.add_argument("--start", default=None, help="只計入此日期之後（YYYY-MM-DD）")
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--dry-run", action="store_true", help="只印，不寫 JSON")
    args = ap.parse_args()

    if args.promote:
        sys.exit(0 if promote(args.promote, args.legacy) else 1)

    actual = load_actuals()
    new_df = load_predictions(args.new)
    legacy_df = backfill_actuals(load_predictions(args.legacy), actual)

    # 回填後的影子檔寫回去，讓 prediction_error 也進版控（與新模型的檔案一致）
    if legacy_df is not None and not legacy_df.empty and not args.dry_run:
        legacy_df.reset_index().to_csv(args.legacy, index=False, encoding="utf-8-sig")

    res = head_to_head(new_df, legacy_df, start=args.start, min_n=args.min_n)
    report(res)

    if args.dry_run:
        print("\n🏁 Dry-run — 未寫檔")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已寫入 {args.output}")


if __name__ == "__main__":
    main()
