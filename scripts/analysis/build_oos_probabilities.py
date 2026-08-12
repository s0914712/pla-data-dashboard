#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建立並維護 walk-forward OOS 機率分布 —— SurgeForecaster 3.1 的參考資料。

3.1 的自適應風險層要回答「今天這個機率排在歷史第幾位」，就需要一份歷史機率分布。
`backtest_predictor.walk_forward()` 本來就在算這個東西，只是算完印個表格就丟掉 ——
全檔沒有任何 to_csv/json.dump。這支腳本把它落地成
data/predictions/oos_probabilities.csv。

只建 h=1。SIGNAL_HORIZON = 1，h>=2 的 surge 機率實測 ROC-AUC 0.45-0.57，本來就不
發警報，建了也沒有消費者。

用法:
    python3 scripts/analysis/build_oos_probabilities.py              # 增量
    python3 scripts/analysis/build_oos_probabilities.py --rebuild    # 全量重建
    python3 scripts/analysis/build_oos_probabilities.py --since 2024-01-01

## 為什麼增量 append 不會造成 leakage

這是整個 3.1 賴以成立的性質，所以寫在這裡而不是只寫在 plan 裡。

walk_forward() 對固定的 target date t：

    train_idx = np.where(target_dates < t - embargo)[0]
    key       = len(train_idx) // REFIT_EVERY

`train_idx` 只由「target_date 早於 t - embargo」的列決定，`len(train_idx)` 因此也
只由 t 之前的資料決定，`key` 跟著只由過去決定。模型參數全部固定（RANDOM_STATE=42、
無 early stopping 的隨機切分），所以：

    今天重建舊日期  ==  當時建那個日期      （逐位元）

也就是說「先建到 D，隔天只補 D+1」與「隔天整個重建」結果相同，增量是精確的而不是
近似的。--verify（預設開啟）會重算既有檔案最後 VERIFY_ROWS 列並比對，不一致就中止並
要求 --rebuild —— 那條比對就是上面這段論證的執行期證明。

## 為什麼預設 --refit 7 而不是 backtest 的 28

線上 predict_surge_daily.py 每天重訓；backtest_predictor 預設 28 天重訓一次。
用 28 天 cadence 的分布去給每日重訓的機率排名，兩邊尺度未必一致，警示率會偏掉。
7 是成本與一致性的折衷。實際落差由 --check-live 量測（見該旗標）。
"""

import argparse
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from pla_surge_model import (  # noqa: E402
    MIN_TRAIN_ROWS,
    SIGNAL_HORIZON,
    SURGE_THRESHOLD,
    to_daily_series,
)
from scripts.analysis import backtest_predictor as bp  # noqa: E402

SORTIES_CSV = "data/JapanandBattleship.csv"
LIVE_CSV = "data/predictions/latest_prediction.csv"
OUTPUT_CSV = "data/predictions/oos_probabilities.csv"

# 線上每日重訓，回測預設 28 天一次。7 是折衷 —— 見檔頭。
REFIT_EVERY = 7

# 增量時重算並比對的既有列數。5 列足以抓到「模型行為變了」而成本可忽略
# （這 5 列多半落在同一個 refit 窗，只要一次重訓）。
VERIFY_ROWS = 5

# 浮點比對容差。同一台機器、同一組參數應該是逐位元相同，留一點餘裕是為了
# 跨 numpy/sklearn 版本的最後一位差異 —— 真正的行為改變會遠大於這個值。
VERIFY_TOL = 1e-9

COLUMNS = [
    "target_date", "horizon", "surge_p", "surge_calibrated",
    "point", "lower", "upper", "actual", "threshold",
    "refit_every", "built_at",
]


def load_series(path=SORTIES_CSV):
    if not os.path.exists(path):
        raise SystemExit(f"找不到 {path}")
    return to_daily_series(pd.read_csv(path, encoding="utf-8-sig"))


def load_existing(path=OUTPUT_CSV):
    """讀既有檔案。缺檔／損壞一律當成空 —— 呼叫端會走全量重建。"""
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:
        print(f"⚠️  讀取 {path} 失敗，將全量重建: {e}")
        return pd.DataFrame(columns=COLUMNS)
    if "target_date" not in df.columns:
        return pd.DataFrame(columns=COLUMNS)
    df["target_date"] = pd.to_datetime(df["target_date"], errors="coerce")
    return df.dropna(subset=["target_date"]).sort_values("target_date")


def run_window(series, start, end, horizon, threshold, refit):
    """跑一段 walk-forward，回傳標準欄位的 DataFrame。"""
    bp.REFIT_EVERY = refit          # walk_forward 讀模組層變數
    r = bp.walk_forward(series, horizon, start, end, threshold)
    if len(r["dates"]) == 0:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame({
        "target_date": r["dates"],
        "horizon": horizon,
        "surge_p": r["surge_p"],
        "surge_calibrated": r["calibrated"].astype(int),
        "point": r["point"],
        "lower": r["lower"],
        "upper": r["upper"],
        "actual": r["actual"],
        "threshold": threshold,
        "refit_every": refit,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def verify_tail(series, existing, horizon, threshold, refit, n=VERIFY_ROWS):
    """重算既有檔案最後 n 列並比對 —— 增量正確性的執行期證明。

    回傳 (ok, message)。不一致代表模型行為已經改變（改了特徵、超參數、
    sklearn 版本…），此時舊列與新列不是同一個分布，混用會讓百分位失真。
    """
    tail = existing.tail(n)
    if tail.empty:
        return True, "既有檔案為空，略過驗證"

    recomputed = run_window(series, tail["target_date"].min(),
                            tail["target_date"].max(), horizon, threshold, refit)
    if recomputed.empty:
        return False, "重算不出任何列"

    merged = tail.merge(recomputed, on="target_date", suffixes=("_old", "_new"))
    if len(merged) != len(tail):
        return False, f"重算列數不符（既有 {len(tail)}，對得上 {len(merged)}）"

    worst, worst_col = 0.0, None
    for col in ("surge_p", "point", "lower", "upper"):
        d = float(np.max(np.abs(merged[f"{col}_old"] - merged[f"{col}_new"])))
        if d > worst:
            worst, worst_col = d, col
    if worst > VERIFY_TOL:
        return False, f"{worst_col} 最大差異 {worst:.3e} > 容差 {VERIFY_TOL:.0e}"
    return True, f"最後 {len(merged)} 列逐位元相同（最大差異 {worst:.1e}）"


def check_live_scale(oos, live_path=LIVE_CSV, threshold=SURGE_THRESHOLD):
    """把線上機率與同日期的 OOS 機率並排，量測尺度落差。

    這是可能推翻 3.1 的檢查。線上每天重訓、OOS 每 refit 天重訓；若兩者有系統性
    平移，拿 OOS 分布去給線上機率排名，警示率就會偏離 budget。

    百分位是 rank-based，所以單調的重新縮放無害，會出事的是平移／壓縮。
    這裡報平均差與 Spearman 相關，判讀留給人。
    """
    if not os.path.exists(live_path):
        print("⚠️  找不到線上預測檔，略過尺度檢查")
        return None
    live = pd.read_csv(live_path, encoding="utf-8-sig")
    need = {"date", "high_event_probability", "model_version", "prob_signal_valid"}
    if not need.issubset(live.columns):
        print("⚠️  線上預測檔缺欄位，略過尺度檢查")
        return None

    live["date"] = pd.to_datetime(live["date"], errors="coerce")
    live = live[live["model_version"].astype(str).str.startswith(("3.0", "3.1"))]
    live = live[(live["prob_signal_valid"] == 1)
                & live["high_event_probability"].notna()]
    if live.empty:
        print("⚠️  線上尚無可比對的 h=1 機率紀錄，略過尺度檢查")
        return None

    live = live.drop_duplicates(subset=["date"], keep="last")
    m = live.merge(oos, left_on="date", right_on="target_date", how="inner")
    m = m[m["surge_calibrated"] == 1]
    if len(m) < 5:
        print(f"⚠️  可比對日期僅 {len(m)} 天，不足以判斷尺度一致性")
        return None

    a = m["high_event_probability"].to_numpy(dtype=float) / 100.0
    b = m["surge_p"].to_numpy(dtype=float)
    rho = pd.Series(a).corr(pd.Series(b), method="spearman")
    res = {
        "n": len(m),
        "live_mean": float(a.mean()),
        "oos_mean": float(b.mean()),
        "mean_shift": float(a.mean() - b.mean()),
        "max_abs_diff": float(np.max(np.abs(a - b))),
        "spearman": None if pd.isna(rho) else float(rho),
    }
    print(f"\n■ 線上 vs OOS 尺度一致性（n={res['n']}）")
    print(f"  線上平均 {res['live_mean']:.3f}／OOS 平均 {res['oos_mean']:.3f}"
          f"　平移 {res['mean_shift']:+.3f}")
    print(f"  最大絕對差 {res['max_abs_diff']:.3f}"
          f"　Spearman {res['spearman'] if res['spearman'] is None else round(res['spearman'], 3)}")
    if abs(res["mean_shift"]) > 0.03:
        print("  ⚠️  平移 >3pp：OOS 分布與線上機率不同尺度，警示率會偏離 budget。"
              "考慮 --refit 1 重建，或在報表揭露此偏移。")
    else:
        print("  ✅ 平移在 3pp 內，可用 OOS 分布為線上機率排名")
    return res


def main():
    ap = argparse.ArgumentParser(description="建立 walk-forward OOS 機率分布")
    ap.add_argument("--output", default=OUTPUT_CSV)
    ap.add_argument("--rebuild", action="store_true", help="全量重建（忽略既有檔案）")
    ap.add_argument("--since", default=None, help="全量重建的起始日 YYYY-MM-DD")
    ap.add_argument("--refit", type=int, default=REFIT_EVERY,
                    help=f"每幾天重訓一次（預設 {REFIT_EVERY}，越小越貼近線上也越慢）")
    ap.add_argument("--horizon", type=int, default=SIGNAL_HORIZON)
    ap.add_argument("--threshold", type=int, default=SURGE_THRESHOLD)
    ap.add_argument("--no-verify", action="store_true",
                    help="跳過增量前的尾列一致性驗證（不建議）")
    ap.add_argument("--check-live", action="store_true",
                    help="額外量測線上機率與 OOS 機率的尺度落差")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    series = load_series()
    data_end = series.dropna().index.max()
    print(f"資料: {series.index.min().date()} -> {data_end.date()} "
          f"({series.notna().sum()} 個觀測日)")

    existing = pd.DataFrame(columns=COLUMNS) if args.rebuild else load_existing(args.output)
    existing = existing[existing.get("horizon", pd.Series(dtype=float)) == args.horizon] \
        if not existing.empty else existing

    if existing.empty:
        # 全量：從最早可能有足夠訓練列的日子開始。MIN_TRAIN_ROWS=400 會自動把
        # 更早的日期濾掉，這裡給一個寬鬆的下界即可。
        start = (pd.Timestamp(args.since) if args.since
                 else series.index.min() + pd.Timedelta(days=MIN_TRAIN_ROWS))
        print(f"模式: 全量重建　起點 {start.date()}　refit={args.refit}")
        print("（依資料量約需數分鐘）")
        fresh = run_window(series, start, data_end, args.horizon,
                           args.threshold, args.refit)
        combined = fresh
    else:
        last = existing["target_date"].max()
        print(f"模式: 增量　既有 {len(existing)} 列，最後 {last.date()}　refit={args.refit}")

        if not args.no_verify:
            ok, msg = verify_tail(series, existing, args.horizon,
                                  args.threshold, args.refit)
            print(f"  一致性驗證: {'✅' if ok else '❌'} {msg}")
            if not ok:
                raise SystemExit(
                    "既有列無法重現 —— 模型行為已改變，舊列與新列不是同一個分布。\n"
                    "請用 --rebuild 全量重建（或 --no-verify 明確接受混用風險）。")

        start = last + pd.Timedelta(days=1)
        if start > data_end:
            print("沒有新的日期可補，結束。")
            return
        fresh = run_window(series, start, data_end, args.horizon,
                           args.threshold, args.refit)
        combined = pd.concat([existing, fresh], ignore_index=True)

    if combined.empty:
        raise SystemExit("沒有產生任何列 —— 檢查資料量是否達 MIN_TRAIN_ROWS")

    combined = (combined.drop_duplicates(subset=["target_date"], keep="last")
                .sort_values("target_date").reset_index(drop=True))
    combined = combined[COLUMNS]

    n_new = len(combined) - (0 if existing.empty else len(existing))
    cal = int(combined["surge_calibrated"].sum())
    pos = int((combined["actual"] >= args.threshold).sum())
    print(f"\n總計 {len(combined)} 列（本次新增 {n_new}）"
          f"　{combined['target_date'].min().date()} -> {combined['target_date'].max().date()}")
    print(f"  已校準 {cal} 列（{cal / len(combined):.0%}）"
          f"　surge {pos} 天（{pos / len(combined):.0%}）")
    ref = combined.loc[combined["surge_calibrated"] == 1, "surge_p"]
    if len(ref):
        print(f"  校準後機率分位: p50 {ref.quantile(0.5):.3f}"
              f"　p80 {ref.quantile(0.8):.3f}"
              f"　p95 {ref.quantile(0.95):.3f}"
              f"　max {ref.max():.3f}")

    if args.check_live:
        check_live_scale(combined, threshold=args.threshold)

    if args.dry_run:
        print("\n🏁 Dry-run — 未寫檔")
        return

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    combined.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"\n✅ 已寫入 {args.output}")


if __name__ == "__main__":
    main()
