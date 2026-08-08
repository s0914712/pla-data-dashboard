#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高架次機率的檢討機制 —— 每日回顧、累積校準、週月彙整、兩模型對打。

「明天有 45% 機率出現高架次」是這份簡報裡唯一有行動意義的數字，但在這支腳本之前，
**沒有任何程式碼回頭檢查過它準不準**。send_message.load_recent_errors() 只看點預測的
MAE，全 repo 沒有 Brier、沒有校準曲線、沒有命中率追蹤。

這支腳本把兩個模型的機率紀錄與實際結果對起來，輸出 data/predictions/probability_review.json
給 LINE 端消費（LINE workflow 不裝 sklearn，所以指標一律在這裡算完）。

用法:
    python3 scripts/analysis/probability_review.py
    python3 scripts/analysis/probability_review.py --dry-run
    python3 scripts/analysis/probability_review.py --window 90

## 為什麼不重用 backtest_predictor.score() 的 recall/precision

score() 的警示切點是 `np.sort(surge_p)[::-1][k-1]` —— 事後在整個測試窗上取分位數，
等於用未來的資訊決定今天要不要警示。那個 recall/precision 會系統性樂觀，
而且不是使用者實際看到的東西。這裡改用固定的 live 規則（見 ALERT_FLOOR），
與新模型 risk_level() 的 MEDIUM-HIGH 切點一致 —— 也就是推播裡真的出現 🟠/🔴 的那條線。
Brier / ROC-AUC / PR-AUC 沒有這個問題，直接用 sklearn。

## 這些數字什麼時候才可信

高架次是稀有事件（基準發生率約 11%），30 天大約只有 3 個正樣本。Brier 與 AUC 在正樣本
少於 10 個時毫無意義，所以有 MIN_N / MIN_POSITIVE / MIN_POSITIVE_AUC 三道門檻，
未達標就不輸出指標，只回報已累積的天數。以基準發生率推算，第一組可信的校準數字
大約要三個月才會出現 —— 這是算術，不是保守。
"""

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# 風險階梯的門檻定義。ladder_unreachable 診斷要用，而那條診斷比點預測計分卡
# 重要，所以分開 import —— 不要讓計分卡的相依把診斷一起拖掉。
try:
    from pla_surge_model import risk_thresholds, to_daily_series
    _LADDER_OK = True
except Exception as _e:  # pragma: no cover - 只在依賴缺失時走到
    print(f"⚠️  無法載入 pla_surge_model，階梯診斷將略過: {_e}")
    _LADDER_OK = False

# 點預測的計分沿用回測腳本那一套，不要在這裡重寫一份 MAE/cov90 —— 兩份實作
# 遲早會漂開，而 bias 正負號慣例已經有過一次不一致的前例。
# 載入失敗時只是少了點預測區塊，機率檢討本身照常輸出。
try:
    from scripts.analysis.backtest_predictor import baseline_scores, score
    _POINT_OK = _LADDER_OK
except Exception as _e:  # pragma: no cover - 只在依賴缺失時走到
    print(f"⚠️  點預測計分模組載入失敗，將略過該區塊: {_e}")
    _POINT_OK = False

NEW_CSV = "data/predictions/latest_prediction.csv"
LEGACY_CSV = "data/predictions/legacy_prediction.csv"
SORTIES_CSV = "data/JapanandBattleship.csv"
OUTPUT_JSON = "data/predictions/probability_review.json"

# 高架次門檻。兩個模型都必須用這個數字，否則機率回答的不是同一個問題。
# 舊模型靠 --high-threshold 20 傳入（見 daily_prediction.yml）。
THRESHOLD = 20

# 只採計新模型（3.0.0-surge 起）的紀錄。latest_prediction.csv 裡 07-27 以前的 160 列
# 是舊 CatBoost 回答「P(架次>=25)」，與新模型的「P(架次>=20)」不是同一個隨機變數，
# 池在一起算會得到一個看起來很像數字的錯誤答案。
NEW_VERSION_PREFIX = "3.0"

# 警示線：與 pla_surge_model.risk_level() 的 MEDIUM-HIGH 切點同式，
# 也就是推播裡真的出現 🟠/🔴 的那條線。兩個模型共用同一條線才公平。
ALERT_FLOOR = 0.30
ALERT_LIFT = 2.0

# 顯示門檻。寫進 JSON 讓 LINE 端照做，不要在兩邊各寫一次。
MIN_N = 30              # 累積指標
MIN_POSITIVE = 5        # 累積指標所需的高架次日數
MIN_POSITIVE_AUC = 10   # ROC/PR-AUC 另外要求

BASE_RATE_WINDOW_DAYS = 365
CALIBRATION_BINS = [(0, 10), (10, 20), (20, 30), (30, 50), (50, 100)]

# ladder_unreachable 診斷的觀察窗。這條診斷判的是「階梯有沒有在動」，
# 不是統計推論，所以門檻遠低於 MIN_N —— 連續一週摸不到 MEDIUM 就該講。
LADDER_WINDOW_DAYS = 14
LADDER_MIN_DAYS = 7


def alert_threshold(base_rate):
    """警示線：max(絕對下限, 倍數 × 基準發生率)，與 risk_level() 同式。"""
    return max(ALERT_FLOOR, ALERT_LIFT * base_rate)


def load_actuals(path=SORTIES_CSV):
    if not os.path.exists(path):
        return pd.Series(dtype=float)
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date", "pla_aircraft_sorties"])
    return (df.set_index("date")["pla_aircraft_sorties"].astype(float)
            .groupby(level=0).last().sort_index())


def base_rate(actuals, window_days=BASE_RATE_WINDOW_DAYS, threshold=THRESHOLD):
    """近一年實際出現高架次日的比例。機率沒有基準就無法解讀。"""
    if actuals.empty:
        return None
    recent = actuals[actuals.index >= actuals.index.max() - pd.Timedelta(days=window_days)]
    if len(recent) < 30:
        return None
    return float((recent >= threshold).mean())


def load_model_rows(path, threshold, version_prefix=None):
    """讀一個模型已解決的機率紀錄：同時有機率與實際值的日期。

    只採計 surge_threshold 與本次比較門檻相符的列。舊模型在加上這個欄位之前寫出的
    紀錄沒有這一欄，無從得知是用哪個門檻算的 —— 一律排除，寧可少算也不要比錯。
    """
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:
        print(f"⚠️  讀取 {path} 失敗: {e}")
        return pd.DataFrame()

    need = {"date", "high_event_probability", "actual_sorties"}
    if not need.issubset(df.columns):
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")

    if version_prefix and "model_version" in df.columns:
        df = df[df["model_version"].astype(str).str.startswith(version_prefix)]

    if "surge_threshold" in df.columns:
        df = df[pd.to_numeric(df["surge_threshold"], errors="coerce") == threshold]
    else:
        return pd.DataFrame()

    df = df[df["high_event_probability"].notna() & df["actual_sorties"].notna()]
    if df.empty:
        return pd.DataFrame()

    # 用 .values 而非 Series：帶著自己 index 的 Series 碰上明確的 index= 會被
    # 重新對齊（整數 index vs Timestamp index），結果整欄變成 NaN。
    out = pd.DataFrame({
        "prob": pd.to_numeric(df["high_event_probability"], errors="coerce").values / 100.0,
        "actual": pd.to_numeric(df["actual_sorties"], errors="coerce").values,
    }, index=pd.DatetimeIndex(df["date"]))
    for opt in ("high_event_probability_raw", "prob_calibrated"):
        if opt in df.columns:
            out[opt] = pd.to_numeric(df[opt].values, errors="coerce")
    # 點預測欄位供 digest() 的計分卡使用。已解決的列都是 h=1（未來列沒有
    # actual_sorties，前面已被濾掉），所以這裡拿到的一律是隔日預測。
    for src, dst in (("predicted_sorties", "point"),
                     ("lower_bound", "lower"), ("upper_bound", "upper")):
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src].values, errors="coerce")
    return out.dropna(subset=["prob", "actual"]).sort_index()


def _r(v, nd=4):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    return round(float(v), nd)


def evaluate(df, br, threshold=THRESHOLD):
    """一個模型的累積機率表現。df 需有 prob / actual 欄。"""
    if df.empty:
        return {"n": 0, "n_positive": 0, "enough": False, "enough_auc": False}

    p = df["prob"].values.astype(float)
    y = (df["actual"].values.astype(float) >= threshold).astype(int)
    n, npos = len(y), int(y.sum())
    observed = float(y.mean())
    cut = alert_threshold(br if br is not None else observed)
    alert = p >= cut

    res = {
        "n": n,
        "n_positive": npos,
        "enough": n >= MIN_N and npos >= MIN_POSITIVE,
        "enough_auc": npos >= MIN_POSITIVE_AUC and npos < n,
        "window": [df.index.min().strftime("%Y-%m-%d"),
                   df.index.max().strftime("%Y-%m-%d")],
        "alert_cutoff": _r(cut, 3),
        "mean_prob": _r(p.mean()),
        "observed_rate": _r(observed),
        # 校準比 >1 代表系統性高估。這是最容易解讀、也最早會露餡的一個數字。
        "calibration_ratio": _r(p.mean() / observed) if observed > 0 else None,
        "alerts": int(alert.sum()),
        "hits": int((alert & (y == 1)).sum()),
        "false_alarms": int((alert & (y == 0)).sum()),
        "misses": int((~alert & (y == 1)).sum()),
        "precision": _r((alert & (y == 1)).sum() / alert.sum()) if alert.sum() else None,
        "recall": _r((alert & (y == 1)).sum() / npos) if npos else None,
        "alert_rate": _r(alert.mean()),
    }

    if 0 < npos < n:
        # Brier 要跟「全押基準發生率」比才有意義：一個永遠輸出基準值的常數模型
        # 鑑別力為零，但 Brier 可能比一個未校準的模型還低。
        brier = brier_score_loss(y, np.clip(p, 0, 1))
        brier_base = brier_score_loss(y, np.full(n, observed))
        res["brier"] = _r(brier)
        res["brier_base"] = _r(brier_base)
        res["brier_skill"] = _r(1 - brier / brier_base) if brier_base > 0 else None
        if res["enough_auc"]:
            res["roc_auc"] = _r(roc_auc_score(y, p))
            res["pr_auc"] = _r(average_precision_score(y, p))
    return res


def calibration_buckets(df, threshold=THRESHOLD):
    """分桶校準：說 40-60% 的那幾天，實際發生了幾成？"""
    if df.empty:
        return []
    pct = df["prob"].values * 100
    y = (df["actual"].values >= threshold).astype(int)
    out = []
    for lo, hi in CALIBRATION_BINS:
        m = (pct >= lo) & (pct < hi) if hi < 100 else (pct >= lo)
        if not m.any():
            continue
        out.append({
            "range": f"{lo}-{hi}%",
            "n": int(m.sum()),
            "mean_prob": _r(pct[m].mean() / 100),
            "observed_rate": _r(float(y[m].mean())),
        })
    return out


def daily_review(df, br, threshold=THRESHOLD):
    """最近一個已知結果的日子：四格混淆矩陣。

    這是第一天就有意義的部分 —— 它是單一事實，不是統計量，不需要等樣本累積。
    """
    if df.empty:
        return None
    row = df.iloc[-1]
    d = df.index[-1]
    p = float(row["prob"])
    actual = float(row["actual"])
    happened = actual >= threshold
    alerted = p >= alert_threshold(br if br is not None else 0.11)
    outcome = ("hit" if happened else "false_alarm") if alerted else (
        "miss" if happened else "correct_quiet")
    return {
        "date": d.strftime("%Y-%m-%d"),
        "prob": _r(p, 3),
        "actual": _r(actual, 1),
        "threshold": threshold,
        "alerted": bool(alerted),
        "happened": bool(happened),
        "outcome": outcome,
    }


def consecutive_misses(df, br, threshold=THRESHOLD):
    """由近到遠數，連續幾個高架次日沒被警示。"""
    if df.empty:
        return 0
    cut = alert_threshold(br if br is not None else 0.11)
    k = 0
    for _, r in df.iloc[::-1].iterrows():
        if r["actual"] < threshold:
            continue          # 非高架次日不算，只看抓到沒
        if r["prob"] >= cut:
            break
        k += 1
    return k


def diagnostics(name, metrics, df, br):
    """只出診斷與建議，不動模型。severity: info / warn / alert。"""
    out = []

    def add(sev, code, msg):
        out.append({"model": name, "severity": sev, "code": code, "message": msg})

    if metrics.get("enough"):
        ratio = metrics.get("calibration_ratio")
        if ratio is not None and not (0.7 <= ratio <= 1.4):
            word = "高估" if ratio > 1 else "低估"
            add("warn", "calibration_drift",
                f"{name}機率系統性{word} {ratio:.1f} 倍"
                f"（平均預告 {metrics['mean_prob'] * 100:.0f}%／實際 "
                f"{metrics['observed_rate'] * 100:.0f}%）")
        skill = metrics.get("brier_skill")
        if skill is not None and skill < 0:
            add("alert", "no_skill",
                f"{name}機率不如直接用基準發生率（Brier 技能 {skill * 100:+.0f}%），建議檢視")
        rate = metrics.get("alert_rate")
        if rate is not None and rate > 0.35:
            add("warn", "alert_fatigue",
                f"{name}警示率 {rate * 100:.0f}%，過於頻繁正在稀釋訊號")

    k = consecutive_misses(df, br)
    if k >= 3:
        add("alert", "consecutive_misses", f"{name}連續 {k} 次漏報高架次日")

    # 以下兩條用 predict_surge_daily.py 已寫進 CSV 的漂移監控欄位。
    # 這兩欄留在 schema 裡本來就是為了這個用途，在此之前沒有任何消費者。
    if "prob_calibrated" in df.columns and len(df) >= 10:
        recent = df["prob_calibrated"].tail(10)
        if recent.notna().any() and (recent.fillna(0) == 0).all():
            add("warn", "calibrator_off",
                f"{name} Platt 校準器近期未啟用（held-out 正樣本不足）")
    if "high_event_probability_raw" in df.columns and len(df) >= 10:
        raw = df["high_event_probability_raw"].dropna() / 100.0
        if len(raw) >= 10:
            cal = df.loc[raw.index, "prob"]
            if cal.mean() > 0 and raw.mean() / cal.mean() > 1.5:
                add("info", "raw_gap",
                    f"{name}原始分類器膨脹 {raw.mean() / cal.mean():.1f} 倍，校準器負擔偏重")

    # 風險階梯不可達。
    #
    # 校準後的機率若整段窗口都摸不到 MEDIUM 切點，儀表板與 LINE 日報就會連日
    # 顯示一個平靜的 🟢 LOW —— 讀起來像「模型說今天安全」，實際是「模型說不出話」。
    # 這裡不去調低 risk_level() 的門檻：機率貼著基準發生率是模型確實沒有鑑別力的
    # 誠實表現，把門檻搬下來只會把「沒有訊號」偽裝成「有訊號」。要修的是沉默，
    # 不是門檻。
    if br is not None and _LADDER_OK and len(df) >= LADDER_MIN_DAYS:
        recent = df["prob"].tail(LADDER_WINDOW_DAYS)
        medium_cut = risk_thresholds(br)["MEDIUM"]
        if len(recent) >= LADDER_MIN_DAYS and recent.max() < medium_cut:
            add("warn", "ladder_unreachable",
                f"{name}近 {len(recent)} 日風險等級恆為 LOW"
                f"（最高 {recent.max() * 100:.0f}%，MEDIUM 需 {medium_cut * 100:.0f}%）"
                f"：階梯在此區間不具區分力，不代表低風險")
    return out


def point_scorecard(sub, series, threshold=THRESHOLD):
    """窗內點預測的計分卡：模型 vs 樸素基準線。

    「MAE 2.8」單看沒有任何意義 —— 這個序列零膨脹又右尾，一個永遠預測低值的
    模型也能拿到漂亮的 MAE。「MAE 2.8 對上 persistence 5.9」才是結論。
    在這支腳本之前，pipeline 裡沒有任何地方會把兩者放在一起看。

    bias 沿用 score() 的 predicted - actual 慣例（負值 = 系統性低估），
    與 CSV 的 prediction_error 欄正負相反，那一欄是 actual - predicted。
    """
    if not _POINT_OK or series is None or sub.empty:
        return None
    if not {"point", "lower", "upper"}.issubset(sub.columns):
        return None

    s = sub.dropna(subset=["point", "lower", "upper", "actual"])
    # 三筆以下連敘述都稱不上，不如不報。
    if len(s) < 3:
        return None

    row = score("model", s["point"].values.astype(float), None,
                s["actual"].values.astype(float), threshold,
                lower=s["lower"].values.astype(float),
                upper=s["upper"].values.astype(float))
    out = {
        "n": int(row["n"]),
        "mae": _r(row["MAE"], 2),
        "rmse": _r(row["RMSE"], 2),
        "bias": _r(row["bias"], 2),
        "pin90": _r(row["pin90"], 2),
        "cov90": _r(row.get("cov90"), 3),
        "width": _r(row.get("width"), 1),
        "bias_convention": "predicted_minus_actual",
    }

    # 基準線用與模型同樣因果的資訊（只看 target_date - 1 當天以前）。
    # min_n=1：週窗只有 7 天，回測用的 30 筆門檻在這裡會把所有基準線濾掉。
    try:
        bases = baseline_scores(series, 1, s.index,
                                s["actual"].values.astype(float), threshold, min_n=1)
    except Exception as e:
        print(f"⚠️  基準線計算失敗，略過: {e}")
        bases = []

    out["baselines"] = {b["model"].replace("baseline ", ""): _r(b["MAE"], 2)
                        for b in bases}
    persistence = out["baselines"].get("persistence")
    if persistence:
        # >0 代表模型贏過「直接複製昨天」。這是整張計分卡唯一該先看的數字。
        out["mae_skill_vs_persistence"] = _r(1 - row["MAE"] / persistence, 3)
    return out


def digest(df, days, br, threshold=THRESHOLD, series=None):
    """近 N 日彙整，給週報／月報用。"""
    if df.empty:
        return None
    cutoff = df.index.max() - pd.Timedelta(days=days)
    sub = df[df.index > cutoff]
    if sub.empty:
        return None
    p = sub["prob"].values
    y = (sub["actual"].values >= threshold).astype(int)
    cut = alert_threshold(br if br is not None else 0.11)
    alert = p >= cut
    return {
        "days": days,
        "n": len(sub),
        "n_positive": int(y.sum()),
        "alerts": int(alert.sum()),
        "hits": int((alert & (y == 1)).sum()),
        "misses": int((~alert & (y == 1)).sum()),
        "mean_prob": _r(p.mean()),
        "observed_rate": _r(float(y.mean())),
        "max_prob": _r(p.max(), 3),
        "max_actual": _r(float(sub["actual"].max()), 1),
        "point": point_scorecard(sub, series, threshold),
    }


def build(new_df, legacy_df, br, threshold=THRESHOLD, series=None):
    new_m = evaluate(new_df, br, threshold)
    old_m = evaluate(legacy_df, br, threshold)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "threshold": threshold,
        "base_rate": _r(br),
        "alert_cutoff": _r(alert_threshold(br) if br is not None else None, 3),
        "gates": {"min_n": MIN_N, "min_positive": MIN_POSITIVE,
                  "min_positive_auc": MIN_POSITIVE_AUC},
        "note": "高架次為稀有事件（基準約 11%），正樣本少於 10 個時 Brier/AUC 不可信",
        "daily": {
            "new": daily_review(new_df, br, threshold),
            "legacy": daily_review(legacy_df, br, threshold),
        },
        "models": {
            "new": dict(label="SurgeForecaster 3.0.0", **new_m),
            "legacy": dict(label="CatBoost 2.8.0", **old_m),
        },
        "calibration": {
            "new": calibration_buckets(new_df, threshold),
            "legacy": calibration_buckets(legacy_df, threshold),
        },
        "digest": {
            "weekly": {"new": digest(new_df, 7, br, threshold, series),
                       "legacy": digest(legacy_df, 7, br, threshold, series)},
            "monthly": {"new": digest(new_df, 30, br, threshold, series),
                        "legacy": digest(legacy_df, 30, br, threshold, series)},
        },
        "diagnostics": (diagnostics("新模型", new_m, new_df, br)
                        + diagnostics("舊模型", old_m, legacy_df, br)),
    }


def report(res):
    print("\n" + "=" * 78)
    print(f"高架次機率檢討（門檻 >={res['threshold']}，基準發生率 "
          f"{(res['base_rate'] or 0) * 100:.0f}%，警示線 "
          f"{(res['alert_cutoff'] or 0) * 100:.0f}%）")
    print("=" * 78)

    for key in ("new", "legacy"):
        m = res["models"][key]
        d = res["daily"][key]
        print(f"\n■ {m['label']}  N={m['n']}（高架次 {m['n_positive']} 日）")
        if d:
            print(f"  最近一日 {d['date']}：預告 {d['prob'] * 100:.0f}%"
                  f"（{'有' if d['alerted'] else '無'}警示），實際 {d['actual']:.0f} 架次"
                  f" → {d['outcome']}")
        if not m.get("enough"):
            print(f"  樣本不足（需 {res['gates']['min_n']} 日／"
                  f"{res['gates']['min_positive']} 個高架次日），暫不評比")
            continue
        print(f"  Brier {m.get('brier')}（基準 {m.get('brier_base')}，"
              f"技能 {(m.get('brier_skill') or 0) * 100:+.0f}%）")
        print(f"  平均預告 {m['mean_prob'] * 100:.0f}%／實際 "
              f"{m['observed_rate'] * 100:.0f}%（校準比 {m.get('calibration_ratio')}）")
        print(f"  警示 {m['alerts']} 次｜命中 {m['hits']}｜誤報 {m['false_alarms']}"
              f"｜漏報 {m['misses']}")
        if m.get("roc_auc") is not None:
            print(f"  ROC-AUC {m['roc_auc']}｜PR-AUC {m['pr_auc']}")

    weekly = (res.get("digest") or {}).get("weekly") or {}
    pt = (weekly.get("new") or {}).get("point")
    if pt:
        print(f"\n■ 近 7 日點預測（新模型，n={pt['n']}）")
        print(f"  MAE {pt['mae']}｜RMSE {pt['rmse']}｜bias {pt['bias']:+.2f}"
              f"（predicted−actual，負值為低估）")
        if pt.get("cov90") is not None:
            print(f"  cov90 {pt['cov90'] * 100:.0f}%（名目 90%）｜平均區間寬度 {pt['width']}")
        if pt.get("baselines"):
            print("  基準線 MAE：" + "｜".join(
                f"{k} {v}" for k, v in pt["baselines"].items()))
        skill = pt.get("mae_skill_vs_persistence")
        if skill is not None:
            print(f"  對 persistence 的 MAE 技能 {skill * 100:+.0f}%"
                  f" → {'優於' if skill > 0 else '劣於'}直接複製昨天")

    if res["diagnostics"]:
        print("\n■ 診斷")
        for d in res["diagnostics"]:
            icon = {"info": "ℹ️", "warn": "⚠️", "alert": "🔴"}.get(d["severity"], "•")
            print(f"  {icon} {d['message']}")
    else:
        print("\n■ 診斷：無異常（或樣本不足以判斷）")


def main():
    ap = argparse.ArgumentParser(description="高架次機率檢討")
    ap.add_argument("--new", default=NEW_CSV)
    ap.add_argument("--legacy", default=LEGACY_CSV)
    ap.add_argument("--output", default=OUTPUT_JSON)
    ap.add_argument("--threshold", type=int, default=THRESHOLD)
    ap.add_argument("--window", type=int, default=None,
                    help="只採計最近 N 天（預設全部）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    actuals = load_actuals()
    br = base_rate(actuals, threshold=args.threshold)

    # 基準線要的是「有缺口也照樣連續」的日曆序列（未觀測是 NaN 不是 0），
    # 與回測用的是同一個函式，rolling/ewm 的語意才對得起來。
    series = None
    if _POINT_OK and os.path.exists(SORTIES_CSV):
        try:
            series = to_daily_series(pd.read_csv(SORTIES_CSV, encoding="utf-8-sig"))
        except Exception as e:
            print(f"⚠️  無法建立日曆序列，點預測基準線將略過: {e}")

    new_df = load_model_rows(args.new, args.threshold, NEW_VERSION_PREFIX)
    legacy_df = load_model_rows(args.legacy, args.threshold)

    def trim(d):
        if args.window and not d.empty:
            return d[d.index > d.index.max() - pd.Timedelta(days=args.window)]
        return d

    new_df, legacy_df = trim(new_df), trim(legacy_df)

    res = build(new_df, legacy_df, br, args.threshold, series)
    report(res)

    if args.dry_run:
        print("\n🏁 Dry-run — 未寫檔")
        return

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已寫入 {args.output}")


if __name__ == "__main__":
    main()
