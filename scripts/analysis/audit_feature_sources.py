#!/usr/bin/env python3
"""稽核爬蟲產出，判斷哪些可以當預測特徵。

加特徵前必須通過三關，任何一關沒過就不該進 feature 欄位：

1. 覆蓋率 —— 資料要蓋住訓練窗口。只有 6 個月歷史的來源，放進
   訓練跨度 10 年的模型裡，99% 的列會是「沒爬到的零」，模型學到的
   是「有新聞 = 2026 年」，不是真的訊號。

2. 非零密度 —— 一年只出現 5 次的類別，AUC 再高也是雜訊。

3. 增量價值 —— 這關最容易被跳過，也最會出事。單一特徵的
   AUC 是樣本內數字，會嚴重高估。要問的是「在序列特徵之上還有沒有
   多的資訊」，用時序交叉驗證比較 seq vs seq+新特徵。
   而且點估計還不夠。實測新聞特徵在 n=160 / 18 個 surge 的情況下，
   只要把 n_splits 從 5 改成 6，結論就從「+0.093 值得加入」翻成
   「-0.038 不要加」；bootstrap 95% 區間是 [-0.153, +0.199]，
   差異為正的比例 52%，等同擲銅板。所以本腳本一律輸出 bootstrap
   區間，區間跨 0 就報「無法判定」，不給假的綠燈。

用法:
    python3 scripts/analysis/audit_feature_sources.py
"""

import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
warnings.filterwarnings("ignore")

from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402
from sklearn.model_selection import TimeSeriesSplit  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from pla_surge_model import SURGE_THRESHOLD, to_daily_series  # noqa: E402

SORTIES_CSV = "data/JapanandBattleship.csv"
NEWS_JSON = "data/news_classified.json"
NAVWARN_CSV = "data/navigation_warnings/military_exercises.csv"
NAVAL_CSV = "data/naval_transits.csv"
WEATHER_PIT_CSV = "data/features/weather_pit.csv"

# 台海地理圍欄。修好座標解析後 80 筆有 6 筆通過（修正前 4 筆）。
# 通過數仍低不是圍欄的問題 —— 爬蟲抓的是全中國海事局，遼寧 22 筆在
# 渤海、海南 16 筆在南海、廣西 12 筆在北部灣，本來就與台海無關。
GEOFENCE = {"lat_min": 21.0, "lat_max": 28.5, "lon_min": 117.0, "lon_max": 124.0}

MIN_NONZERO_RATIO = 0.15   # 非零天數低於此比例，AUC 不予採信


def _in_geofence(coord_str):
    if not isinstance(coord_str, str):
        return False
    for point in coord_str.split(";"):
        try:
            lat, lon = [float(v) for v in point.strip().split(",")]
        except ValueError:
            continue
        if (GEOFENCE["lat_min"] <= lat <= GEOFENCE["lat_max"]
                and GEOFENCE["lon_min"] <= lon <= GEOFENCE["lon_max"]):
            return True
    return False


def load_news_daily(index):
    if not os.path.exists(NEWS_JSON):
        return pd.DataFrame(index=index), None
    with open(NEWS_JSON, encoding="utf-8") as fh:
        items = json.load(fh)
    rows = []
    for item in items:
        article = item.get("original_article", {})
        rows.append({
            "date": article.get("date"),
            "source": article.get("source"),
            "category": item.get("category"),
            "relevant": bool(item.get("is_relevant")),
        })
    news = pd.DataFrame(rows)
    news["date"] = pd.to_datetime(news["date"], errors="coerce", format="mixed")
    news = news[news["date"].notna()]

    out = pd.DataFrame(index=index)
    out["news_total"] = news.groupby("date").size().reindex(index).fillna(0)
    out["news_relevant"] = (news[news["relevant"]].groupby("date").size()
                            .reindex(index).fillna(0))
    for cat, grp in news.groupby("category"):
        if not cat:
            continue
        out[f"news_{cat}"] = grp.groupby("date").size().reindex(index).fillna(0)
    for src, grp in news.groupby("source"):
        out[f"src_{src}"] = grp.groupby("date").size().reindex(index).fillna(0)
    span = (news["date"].min(), news["date"].max())
    return out, span


def load_navwarn_daily(index):
    if not os.path.exists(NAVWARN_CSV):
        return pd.DataFrame(index=index), None
    nw = pd.read_csv(NAVWARN_CSV, encoding="utf-8-sig")
    nw["publish_date"] = pd.to_datetime(nw["publish_date"], errors="coerce")
    nw = nw[nw["publish_date"].notna()]
    nw["in_fence"] = nw["coordinates"].apply(_in_geofence)

    out = pd.DataFrame(index=index)
    out["navwarn_pub"] = nw.groupby("publish_date").size().reindex(index).fillna(0)
    out["navwarn_pub_fenced"] = (nw[nw["in_fence"]].groupby("publish_date").size()
                                 .reindex(index).fillna(0))
    return out, (nw["publish_date"].min(), nw["publish_date"].max())


def load_naval_daily(index):
    if not os.path.exists(NAVAL_CSV):
        return pd.DataFrame(index=index), None
    nt = pd.read_csv(NAVAL_CSV, encoding="utf-8-sig")
    nt["Date"] = pd.to_datetime(nt["Date"], errors="coerce", format="mixed")
    nt = nt[nt["Date"].notna()]
    out = pd.DataFrame(index=index)
    out["naval_transit"] = nt.groupby("Date").size().reindex(index).fillna(0)
    return out, (nt["Date"].min(), nt["Date"].max())


def load_weather_pit(index, horizon=1):
    """point-in-time 天氣預報。由 build_pit_features.py 從 git 歷史產生。

    這是唯一真正前瞻的來源：as_of 當天就看得到 as_of+horizon 的預報，
    不必等事件發生。用磁碟上那份被覆寫過的檔案回填歷史會造成洩漏。
    """
    if not os.path.exists(WEATHER_PIT_CSV):
        return pd.DataFrame(index=index), None
    pit = pd.read_csv(WEATHER_PIT_CSV, parse_dates=["as_of", "target_date"])
    pit = pit[pit["horizon"] == horizon].set_index("as_of")
    pit = pit.drop(columns=[c for c in ("target_date", "horizon") if c in pit.columns])
    pit.columns = [f"wx_{c}" for c in pit.columns]
    return pit.reindex(index), (pit.index.min(), pit.index.max())


def column_span(series):
    """每個欄位各自的有效區間 = 第一筆到最後一筆非零之間。

    不能用整個檔案的區間。news_classified.json 裡混了 naval_transits
    （2020 年起）和 xinhua/weibo/cna（2026-02 起），用檔案層級的區間會
    讓 2026 年才開始爬的來源被迫在 6 年「沒爬到的零」上評估，AUC 必然
    被稀釋成 0.5。
    """
    nz = series[series != 0]
    if nz.empty:
        return None
    return nz.index.min(), nz.index.max()


def univariate_report(frame, target, label):
    """單一特徵鑑別力。每個欄位在自己的有效區間內評估。"""
    print(f"\n--- {label}")
    print(f"    {'特徵':<26}{'有效區間':>22}{'AUC(當日)':>10}{'AUC(7日)':>10}{'非零天':>8}")
    rows = []
    for col in frame.columns:
        vals = frame[col].fillna(0)
        span = column_span(vals)
        if span is None:
            continue
        lo, hi = span
        mask = target.notna() & (frame.index >= lo) & (frame.index <= hi)
        if mask.sum() < 30:
            continue
        try:
            auc_d0 = roc_auc_score(target[mask], vals[mask])
            auc_7d = roc_auc_score(
                target[mask], vals.rolling(7, min_periods=1).sum()[mask])
        except ValueError:
            continue
        rows.append((col, f"{lo.date()}~{hi.date()}", auc_d0, auc_7d,
                     (vals[mask] != 0).mean(), int(mask.sum())))
    for col, span_s, a0, a7, nz, n in sorted(rows, key=lambda r: -max(r[2], r[3])):
        note = "" if nz >= MIN_NONZERO_RATIO else "  (太稀疏，不予採信)"
        print(f"    {col:<26}{span_s:>22}{a0:>10.3f}{a7:>10.3f}{100 * nz:>7.0f}%{note}")


def incremental_value(sorties, extra, cols, span, label):
    """關鍵檢驗：在序列特徵之上，這些欄位還有沒有增量價值。"""
    lo, hi = span
    index = pd.date_range(max(lo, sorties.index.min()), min(hi, sorties.index.max()), freq="D")
    y = sorties.reindex(index)

    d = pd.DataFrame(index=index)
    d["target"] = (y.shift(-1) >= SURGE_THRESHOLD).astype(float)
    d["lag1"] = y
    d["ma7"] = y.shift(1).rolling(7, min_periods=2).mean()
    d["mx7"] = y.shift(1).rolling(7, min_periods=2).max()
    d["surge_rate28"] = y.shift(1).rolling(28, min_periods=3).apply(
        lambda a: np.nanmean(a >= SURGE_THRESHOLD), raw=True)
    for c in cols:
        if c in extra.columns:
            d[c] = extra[c].reindex(index)
    d = d.dropna()
    usable = [c for c in cols if c in d.columns]
    if len(d) < 60 or not usable or d["target"].nunique() < 2:
        print(f"\n=== {label} 增量檢驗: 樣本不足，略過")
        return

    seq = ["lag1", "ma7", "mx7", "surge_rate28"]

    def cv(feature_cols):
        X = StandardScaler().fit_transform(d[feature_cols].values)
        t = d["target"].values
        scores = []
        for tr, te in TimeSeriesSplit(n_splits=5).split(X):
            if len(set(t[tr])) < 2 or len(set(t[te])) < 2:
                continue
            model = LogisticRegression(max_iter=2000, class_weight="balanced")
            prob = model.fit(X[tr], t[tr]).predict_proba(X[te])[:, 1]
            scores.append(roc_auc_score(t[te], prob))
        return float(np.mean(scores)) if scores else float("nan")

    n_pos = int(d["target"].sum())
    print(f"\n=== {label} 增量檢驗  (n={len(d)}, surge {n_pos} 天) ===")
    base, only, both = cv(seq), cv(usable), cv(seq + usable)
    print(f"    只有序列特徵      CV AUC = {base:.3f}")
    print(f"    只有新來源        CV AUC = {only:.3f}")
    print(f"    序列 + 新來源     CV AUC = {both:.3f}")
    print(f"    -> 點估計差異 {both - base:+.3f}")

    # 點估計本身不足以下結論。實測在 n=160 / 18 個正樣本下，只要改
    # n_splits（3~10）結論就會在「值得加入」和「不要加」之間翻面。
    # 一定要看 bootstrap 區間有沒有跨過 0。
    lo_ci, hi_ci, p_pos = _bootstrap_delta(d, seq, usable)
    print(f"    bootstrap 95% 區間 [{lo_ci:+.3f}, {hi_ci:+.3f}]，"
          f"差異為正的比例 {100 * p_pos:.0f}%")

    per_fold_pos = n_pos / 5.0
    if lo_ci > 0:
        verdict = "有增量價值，可以加入"
    elif hi_ci < 0:
        verdict = "會讓模型變差，不要加"
    else:
        verdict = (f"無法判定 —— 區間跨過 0。每折平均只有 {per_fold_pos:.0f} "
                   f"個正樣本，樣本量不足以支持任何結論")
    print(f"    -> {verdict}")


def _bootstrap_delta(d, seq, extra, n_boot=200, seed=0):
    """重抽樣估計「加了新特徵之後 AUC 差異」的不確定性。

    小樣本下單一 CV 點估計極不穩定，這個區間才是該看的東西。
    """
    rng = np.random.default_rng(seed)
    t = d["target"].values
    X_base = StandardScaler().fit_transform(d[seq].values)
    X_both = StandardScaler().fit_transform(d[seq + extra].values)
    deltas = []
    for _ in range(n_boot):
        boot = np.sort(rng.choice(len(d), len(d), replace=True))
        base_scores, both_scores = [], []
        for tr, te in TimeSeriesSplit(n_splits=5).split(boot):
            i_tr, i_te = boot[tr], boot[te]
            if len(set(t[i_tr])) < 2 or len(set(t[i_te])) < 2:
                continue
            for X, bucket in ((X_base, base_scores), (X_both, both_scores)):
                model = LogisticRegression(max_iter=1000, class_weight="balanced")
                prob = model.fit(X[i_tr], t[i_tr]).predict_proba(X[i_te])[:, 1]
                bucket.append(roc_auc_score(t[i_te], prob))
        if base_scores and both_scores:
            deltas.append(np.mean(both_scores) - np.mean(base_scores))
    if not deltas:
        return float("nan"), float("nan"), float("nan")
    deltas = np.array(deltas)
    return (float(np.percentile(deltas, 2.5)), float(np.percentile(deltas, 97.5)),
            float((deltas > 0).mean()))


def main():
    raw = pd.read_csv(SORTIES_CSV, encoding="utf-8-sig")
    sorties = to_daily_series(raw)
    index = pd.date_range(sorties.index.min(), sorties.index.max(), freq="D")
    target = (sorties.reindex(index).shift(-1) >= SURGE_THRESHOLD).astype(float)
    target[sorties.reindex(index).shift(-1).isna()] = np.nan

    print("=" * 74)
    print(f"目標: 隔日 surge (>={SURGE_THRESHOLD} 架次)")
    print(f"架次序列: {sorties.index.min().date()} ~ {sorties.index.max().date()}"
          f"  ({int(sorties.notna().sum())} 個觀測日)")
    print("=" * 74)

    news, news_span = load_news_daily(index)
    weather, weather_span = load_weather_pit(index)
    navwarn, navwarn_span = load_navwarn_daily(index)
    naval, naval_span = load_naval_daily(index)

    for frame, span, label in [
        (news, news_span, "新聞 (xinhua / weibo / cna)"),
        (navwarn, navwarn_span, "MSA 航行警告"),
        (naval, naval_span, "海軍過境"),
        (weather, weather_span, "天氣預報 (point-in-time, h=1)"),
    ]:
        if span is not None and not frame.empty:
            univariate_report(frame, target, label)

    dense_news_span = column_span(news["src_xinhua"]) if "src_xinhua" in news else None
    if dense_news_span:
        incremental_value(sorties, news, ["news_relevant", "news_total", "src_xinhua"],
                          dense_news_span, "新聞")
    if weather_span and not weather.empty:
        # 一次只加一欄。184 列硬塞 7 個天氣特徵會稀釋掉序列特徵，
        # 實測全加會讓 CV AUC 從 0.735 掉到 0.637。
        best_wx = [c for c in ("wx_wind_max", "wx_precip_total") if c in weather.columns]
        if best_wx:
            incremental_value(sorties, weather, best_wx[:1], weather_span, "天氣預報")
    navwarn_span_eff = column_span(navwarn["navwarn_pub"]) if "navwarn_pub" in navwarn else None
    if navwarn_span_eff:
        incremental_value(sorties, navwarn, ["navwarn_pub"],
                          navwarn_span_eff, "航行警告")

    print("\n" + "=" * 74)
    print("提醒: 單一特徵 AUC 是樣本內數字，會高估。以增量檢驗的結論為準。")
    print("=" * 74)


if __name__ == "__main__":
    main()
