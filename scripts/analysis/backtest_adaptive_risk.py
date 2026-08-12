#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SurgeForecaster 3.1 的驗收回測 —— 三個操作點並排。

3.1 沒有動任何機率，所以「MAE 有沒有變好」不是問題（答案必然是「完全一樣」）。
要證明的只有一件事：**在同樣的機率上，換一個發警報的規則，有沒有抓到更多 surge。**

三路並列：

    3.0 absolute      max(0.30, 2.0 x base_rate)          現行線上
    3.1 adaptive      每日取「當日之前」參考窗的第 80 百分位   本次提案
    oracle budget     np.sort(surge_p)[::-1][k-1]          事後切點，只當上界

第三列是 backtest_predictor.score() 內建的那個切點。它在整個測試窗上取分位數，
等於用未來資訊決定今天要不要警示 —— probability_review.py 檔頭已經寫過為什麼不能
拿它當 live 指標。這裡把它印出來只是為了標出天花板：3.1 的因果 recall 本來就該低於
它，看到差距不代表 3.1 退步。**不要拿 3.1 去對它比而判定失敗。**

用法:
    python3 scripts/analysis/backtest_adaptive_risk.py
    python3 scripts/analysis/backtest_adaptive_risk.py --days 730 --json out.json
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from sklearn.metrics import (  # noqa: E402
    average_precision_score,
    brier_score_loss,
    roc_auc_score,
)

from pla_surge_model import (  # noqa: E402
    ALERT_BUDGET,
    MIN_REFERENCE_N,
    REFERENCE_WINDOW_DAYS,
    SURGE_THRESHOLD,
    absolute_alert_threshold,
    risk_percentile,
)

OOS_CSV = "data/predictions/oos_probabilities.csv"

# 驗收閘門。
#
# 核心指標是 recall lift = recall / alert_rate，也就是「相對於無鑑別力模型的倍數」。
# 無鑑別力模型在任何 budget b 下，recall 的期望值就是 b，所以 lift 的無訊號基準
# 恆為 1.0，與實際警示率無關。
#
# 為什麼不直接寫 recall >= 40%：那個數字只在警示率恰好等於 20% 時才等價於
# lift >= 2。自適應門檻的警示率會隨情勢漂移（升溫期 >20%、安靜期 <20%），
# 此時固定的 recall 門檻會在升溫期變得太鬆、安靜期太嚴。
#
# 另外 precision 不是獨立閘門：在固定警示率下
#     precision = recall x base_rate / alert_rate = lift x base_rate
# 也就是「precision >= 2x base rate」與「lift >= 2」是同一條件，分開列會重複計分。
GATE_ALERT_RATE = (ALERT_BUDGET - 0.05, ALERT_BUDGET + 0.05)
GATE_RECALL_LIFT = 2.0


def adaptive_alerts(dates, probs, calibrated, window_days=REFERENCE_WINDOW_DAYS,
                    budget=ALERT_BUDGET, min_n=MIN_REFERENCE_N):
    """逐日因果門檻：第 i 天的切點只由 date < dates[i] 的機率算出。

    這是 3.1 與 score() 的 budget 切點唯一的差別，也是整支腳本存在的理由。
    回傳 (alert, percentile, threshold)，各為長度 n 的陣列；參考不足處為 NaN/False。
    """
    n = len(probs)
    alert = np.zeros(n, dtype=bool)
    pcts = np.full(n, np.nan)
    cuts = np.full(n, np.nan)

    for i in range(n):
        if not calibrated[i]:
            continue        # 未校準的當日機率與參考分布不同尺度，不排名
        # 嚴格小於：當日自己不得進入自己的參考分布
        m = (dates < dates[i]) & (dates > dates[i] - pd.Timedelta(days=window_days))
        ref = probs[m & calibrated]
        if len(ref) < min_n:
            continue
        p = risk_percentile(float(probs[i]), ref, min_n=min_n)
        if p is None:
            continue
        pcts[i] = p
        cuts[i] = float(np.quantile(ref, 1 - budget))
        alert[i] = p >= 1 - budget
    return alert, pcts, cuts


def operating_point(name, alert, labels, note=""):
    """一個警示規則的混淆矩陣衍生指標。"""
    alert = np.asarray(alert, dtype=bool)
    labels = np.asarray(labels, dtype=int)
    hits = int((alert & (labels == 1)).sum())
    fa = int((alert & (labels == 0)).sum())
    misses = int((~alert & (labels == 1)).sum())
    npos = int(labels.sum())
    nneg = int((labels == 0).sum())
    return {
        "name": name,
        "note": note,
        "alerts": int(alert.sum()),
        "hits": hits,
        "false_alarms": fa,
        "misses": misses,
        "alert_rate": float(alert.mean()),
        "recall": hits / npos if npos else None,
        "precision": hits / alert.sum() if alert.sum() else None,
        # 誤報率 = 在「沒發生 surge 的日子」裡有多少被警示。與 precision 不同，
        # 它不受 base rate 影響，是警示疲勞的直接量度。
        "false_alarm_rate": fa / nneg if nneg else None,
    }


def fmt_ops(rows, base):
    print(f"\n{'操作點':<26}{'警示':>6}{'命中':>6}{'誤報':>6}{'漏報':>6}"
          f"{'警示率':>8}{'召回':>8}{'精確':>8}{'誤報率':>8}")
    print("-" * 84)
    for r in rows:
        def pc(v):
            return "     -" if v is None else f"{100 * v:5.0f}%"
        print(f"{r['name']:<26}{r['alerts']:>6}{r['hits']:>6}{r['false_alarms']:>6}"
              f"{r['misses']:>6}{pc(r['alert_rate']):>8}{pc(r['recall']):>8}"
              f"{pc(r['precision']):>8}{pc(r['false_alarm_rate']):>8}")
    print("-" * 84)
    print(f"base rate {100 * base:.1f}%　"
          f"（無鑑別力模型在 {100 * ALERT_BUDGET:.0f}% budget 下：召回≈"
          f"{100 * ALERT_BUDGET:.0f}%、精確≈{100 * base:.0f}%）")
    for r in rows:
        if r["note"]:
            print(f"  * {r['name']}：{r['note']}")


def main():
    ap = argparse.ArgumentParser(description="3.1 自適應風險驗收回測")
    ap.add_argument("--oos", default=OOS_CSV)
    ap.add_argument("--days", type=int, default=None,
                    help="只評估最近 N 天（預設全部可評估區間）")
    ap.add_argument("--threshold", type=int, default=SURGE_THRESHOLD)
    ap.add_argument("--reference-window", type=int, default=REFERENCE_WINDOW_DAYS,
                    help="參考窗天數（越短越貼近當前情勢，也越容易把安靜期的"
                         "相對高分誤判成警示）")
    ap.add_argument("--json", default=None, help="把結果另存 JSON")
    args = ap.parse_args()

    if not os.path.exists(args.oos):
        raise SystemExit(
            f"找不到 {args.oos}\n"
            "請先執行: python3 scripts/analysis/build_oos_probabilities.py --rebuild")

    df = pd.read_csv(args.oos, encoding="utf-8-sig")
    df["target_date"] = pd.to_datetime(df["target_date"])
    df = df.sort_values("target_date").reset_index(drop=True)
    df = df[df["actual"].notna()]

    dates = pd.DatetimeIndex(df["target_date"])
    probs = df["surge_p"].to_numpy(dtype=float)
    calib = df["surge_calibrated"].to_numpy(dtype=bool)
    actual = df["actual"].to_numpy(dtype=float)

    print(f"OOS 檔: {args.oos}")
    print(f"全部 {len(df)} 列　{dates.min().date()} -> {dates.max().date()}"
          f"　已校準 {int(calib.sum())} 列")

    # 逐日因果門檻要用「全部歷史」當參考，但只在有足夠參考的日子評估。
    alert_31, pcts, cuts = adaptive_alerts(dates, probs, calib,
                                           window_days=args.reference_window)
    evaluable = ~np.isnan(pcts)
    if args.days:
        evaluable &= (dates > dates.max() - pd.Timedelta(days=args.days))
    if evaluable.sum() < 30:
        raise SystemExit(f"可評估日數僅 {int(evaluable.sum())}，不足以判斷")

    e_dates = dates[evaluable]
    e_probs = probs[evaluable]
    e_actual = actual[evaluable]
    labels = (e_actual >= args.threshold).astype(int)
    base = float(labels.mean())

    print(f"評估窗: {e_dates.min().date()} -> {e_dates.max().date()}"
          f"　{len(e_dates)} 天　surge {int(labels.sum())} 天（{100 * base:.1f}%）")
    print(f"參考窗 {args.reference_window} 天　最小參考列數 {MIN_REFERENCE_N}"
          f"　budget {100 * ALERT_BUDGET:.0f}%")

    # ── 三個操作點 ──
    cut30 = absolute_alert_threshold(base)
    ops = [
        operating_point("3.0 absolute", e_probs >= cut30, labels,
                        f"固定切點 {cut30:.3f} = max(0.30, 2.0 x base rate)"),
        operating_point("3.1 adaptive (causal)", alert_31[evaluable], labels,
                        f"逐日切點，中位數 {np.nanmedian(cuts[evaluable]):.3f}"),
    ]
    k = max(1, int(ALERT_BUDGET * len(labels)))
    oracle_cut = np.sort(e_probs)[::-1][k - 1]
    ops.append(operating_point(
        "oracle budget-0.20", e_probs >= oracle_cut, labels,
        "事後在整個窗上取分位數 —— 不可實作的上界，不是比較對象"))

    fmt_ops(ops, base)

    # ── 機率層未動的證明 ──
    # 三個操作點共用同一組 surge_p，所以這些數字對三者完全相同。
    # 印出來是為了讓「3.1 沒有碰機率」這件事在報表上可查，而不是只寫在 commit message。
    prob_metrics = {
        "PR_AUC": float(average_precision_score(labels, e_probs)),
        "ROC_AUC": float(roc_auc_score(labels, e_probs)),
        "Brier": float(brier_score_loss(labels, np.clip(e_probs, 0, 1))),
        "Brier_base": float(brier_score_loss(labels, np.full(len(labels), base))),
    }
    prob_metrics["Brier_skill"] = (
        1 - prob_metrics["Brier"] / prob_metrics["Brier_base"]
        if prob_metrics["Brier_base"] > 0 else None)
    print(f"\n■ 機率層（三個操作點共用，3.1 未改動）")
    print(f"  PR-AUC {prob_metrics['PR_AUC']:.3f}（base {base:.3f}，"
          f"lift {prob_metrics['PR_AUC'] / base:.1f}x）"
          f"　ROC-AUC {prob_metrics['ROC_AUC']:.3f}")
    print(f"  Brier {prob_metrics['Brier']:.4f}（基準 {prob_metrics['Brier_base']:.4f}，"
          f"技能 {100 * prob_metrics['Brier_skill']:+.1f}%）")

    # ── 閘門 ──
    a = ops[1]
    gates = []

    def gate(name, ok, detail):
        gates.append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"  {'✅' if ok else '❌'} {name}: {detail}")

    print("\n■ 驗收閘門（3.1 adaptive）")
    gate("警示率", GATE_ALERT_RATE[0] <= a["alert_rate"] <= GATE_ALERT_RATE[1],
         f"{100 * a['alert_rate']:.1f}% ∈ "
         f"[{100 * GATE_ALERT_RATE[0]:.0f}%, {100 * GATE_ALERT_RATE[1]:.0f}%]")
    lift = (a["recall"] / a["alert_rate"]
            if a["recall"] is not None and a["alert_rate"] > 0 else None)
    gate("召回倍數", lift is not None and lift >= GATE_RECALL_LIFT,
         f"{lift:.2f}x >= {GATE_RECALL_LIFT:.1f}x"
         f"（召回 {100 * a['recall']:.1f}% ÷ 警示率 {100 * a['alert_rate']:.1f}%；"
         f"無鑑別力 = 1.00x）")
    # 只作資訊：precision 由 lift 與 base rate 決定，不是獨立的判準
    print(f"  ·  精確率 {100 * a['precision']:.1f}%"
          f"（= 召回倍數 x base rate {100 * base:.1f}%，非獨立閘門）")
    gate("勝過 3.0", a["recall"] >= (ops[0]["recall"] or 0),
         f"召回 {100 * a['recall']:.1f}% vs 3.0 {100 * (ops[0]['recall'] or 0):.1f}%")
    a["recall_lift"] = lift

    passed = all(g["pass"] for g in gates)
    print(f"\n{'✅ 全部通過' if passed else '❌ 未通過 —— 不應上線'}")

    if args.json:
        out = {
            "window": [str(e_dates.min().date()), str(e_dates.max().date())],
            "n": len(e_dates), "n_positive": int(labels.sum()), "base_rate": base,
            "budget": ALERT_BUDGET, "reference_window_days": REFERENCE_WINDOW_DAYS,
            "operating_points": ops, "probability_metrics": prob_metrics,
            "gates": gates, "passed": passed,
        }
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 已寫入 {args.json}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
