#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Bot 每日台海動態簡報推播
================================
每天早上 07:00（台灣時間）由 GitHub Actions 觸發，組合並推播一份簡報：

  1. 未來 3 日 PLA 架次預測（文字 + 折線圖：30 日歷史 + 未來 3 日預測）
  2. 昨日「新聞 / notice」摘要：
       - MSA 火砲射擊 / 演習航行警告
       - 日本防衛省（統合幕僚監部）中國艦機動向通報
       - 微博（東部戰區）
       - Google News / 中央社、新華社等新聞

資料一律讀取 repo 內已由各 workflow 每天清晨爬好並 commit 的檔案，本腳本不重爬。

環境變數：
  LINE_CHANNEL_ACCESS_TOKEN  — LINE Channel Access Token（push 用，必要）
  LINE_USER_ID               — 推播對象 user id（必要）
  GITHUB_TOKEN               — 上傳預測圖取得公開網址用（選用；缺少則只送文字）

用法：
  python scripts/send_message.py            # 實際推播
  python scripts/send_message.py --dry-run  # 只印出內容與產圖，不推播
"""

import argparse
import base64
import json
import math
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import requests

import matplotlib
matplotlib.use("Agg")  # CI 無顯示環境
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nav_warning_dates import parse_periods, format_periods_zh  # noqa: E402

# ── 路徑（相對於 repo root）─────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
NEWS_JSON = REPO_ROOT / "data" / "news_classified.json"
NAV_WARN_JSON = REPO_ROOT / "data" / "navigation_warnings" / "military_exercises.json"
JAPAN_MOD_CSV = REPO_ROOT / "data" / "JapanandBattleship.csv"
PRED_CSV = REPO_ROOT / "data" / "predictions" / "latest_prediction.csv"
# 舊 CatBoost 模型的影子輸出與新舊對照指標，由 daily_prediction.yml 每天產生。
# 兩者都是「有就用、沒有就略過」——LINE 推播不能因為對照組缺檔就掛掉。
LEGACY_PRED_CSV = REPO_ROOT / "data" / "predictions" / "legacy_prediction.csv"
COMPARISON_JSON = REPO_ROOT / "data" / "predictions" / "model_comparison.json"
PROB_REVIEW_JSON = REPO_ROOT / "data" / "predictions" / "probability_review.json"
CHART_OUT = REPO_ROOT / "data" / "charts" / "line_forecast_3day.png"
CHART_REPO_PATH = "data/charts/line_forecast_3day.png"
MAP_OUT = REPO_ROOT / "data" / "charts" / "nav_warning_map.png"
MAP_REPO_PATH = "data/charts/nav_warning_map.png"

# ── 設定 ───────────────────────────────────────────────────
REPO_OWNER = "s0914712"
REPO_NAME = "pla-data-dashboard"
TW_TZ = timezone(timedelta(hours=8))
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
FORECAST_DAYS = 3
NEWS_LOOKBACK_DAYS = 2   # 「昨日」彈性：抓昨日與前日，避免單日無資料
MAX_ITEMS_PER_SOURCE = 5

# 來源代碼 → 中文標籤
SOURCE_LABELS = {
    "weibo": "微博（東部戰區）",
    "cna": "中央社 / Google News",
    "xinhua": "新華社",
    "naval_transits": "美/外軍艦動向",
}

RISK_EMOJI = {"LOW": "🟢", "MEDIUM": "🟡", "MEDIUM-HIGH": "🟠", "HIGH": "🔴",
              "CRITICAL": "🔴", "UNKNOWN": "⚪"}

# 「高架次」門檻，與 pla_surge_model.SURGE_THRESHOLD 一致。
# 分類器就是用這個門檻訓練的，顯示時必須講同一個數字，否則「45%」到底是
# 「>=20 架次」還是「>=30 架次」的機率無從得知。
#
# 由 25 改成 20 是實測結果：同一個 160 天評估窗、h=1，thr=25 的 ROC-AUC 只有
# 0.547（正樣本 11 個），與亂猜無法區分；thr=20 是 0.622、PR-AUC 0.238
# 對 base rate 0.113 有 2.1 倍 lift。門檻太高會讓正樣本太稀疏，模型學不動也測不準。
HIGH_SORTIE_THRESHOLD = 20

# 高架次機率只在 D+1 顯示 —— h>=2 實測 ROC-AUC 0.45-0.57，等同亂猜。
# 上游 predict_surge_daily.py 對 h>=2 直接寫 NaN、risk_level 給 UNKNOWN，
# 這裡的過濾是第二道防線。
PROB_SIGNAL_HORIZON = 1

# 計算基準發生率的回看天數（用來讓機率有比較基準：45% 是高是低，
# 取決於平常多久出現一次高架次日）
BASE_RATE_WINDOW_DAYS = 365

# 新舊模型對照的樣本門檻。低於此天數只報「並行第 N 日」，不報指標 ——
# 3 天的 MAE 差距幾乎全是雜訊，但看到數字的人一定會把它當成結論。
# 實際值以 model_comparison.json 的 min_n 為準，這裡只是後備。
COMPARISON_MIN_N = 10


# ============================================================
# 資料讀取 / 摘要
# ============================================================
def _s(v):
    """安全轉字串：None / NaN / float('nan') 一律回空字串。"""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:  # NaN
        return ""
    return str(v)


def _safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  讀取 {path} 失敗: {e}")
        return None


def _load_forecast_csv(path, days, what):
    """讀一份預測 CSV，回傳未來 N 天（尚無實際值）的預測列。"""
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
        future = df[df["actual_sorties"].isna()] if "actual_sorties" in df.columns else df
        if future.empty:
            future = df
        return future.head(days).reset_index(drop=True)
    except Exception as e:
        print(f"⚠️  讀取{what}失敗: {e}")
        return pd.DataFrame()


def load_forecast(days=FORECAST_DAYS):
    """讀 latest_prediction.csv（線上正式模型）的未來 N 天預測。"""
    return _load_forecast_csv(PRED_CSV, days, "預測")


def load_legacy_forecast(days=FORECAST_DAYS):
    """讀 legacy_prediction.csv（舊 CatBoost 影子模型）的未來 N 天預測。

    這份檔案只是對照組，缺檔是預期情況（舊模型當日跑掛、或雙軌尚未上線），
    所以回空 DataFrame 讓呼叫端安靜略過，不影響正式預測的顯示。
    """
    if not LEGACY_PRED_CSV.exists():
        return pd.DataFrame()
    return _load_forecast_csv(LEGACY_PRED_CSV, days, "舊模型預測")


def summarize_forecast(fc):
    """組未來 3 日預測文字。"""
    if fc.empty:
        return "📊 未來 3 日預測：暫無資料"
    lines = ["📊 未來 3 日 PLA 架次預測："]
    for i, (_, r) in enumerate(fc.iterrows()):
        emoji = RISK_EMOJI.get(str(r.get("risk_level", "")).upper(), "⚪")
        date_str = r["date"].strftime("%m/%d")
        pred = r.get("predicted_sorties", 0)
        lo = r.get("lower_bound", 0)
        hi = r.get("upper_bound", 0)
        # 機率不再塞在這一行的括號裡 —— 它是整份簡報唯一有行動意義的數字，
        # 被夾在點預測與區間之間是最不顯眼的位置。改由 summarize_surge_probability()
        # 獨立成一個區塊。這裡只留點預測與區間。
        lines.append(
            f"  {emoji} {date_str}：約 {pred:.1f} 架次"
            f"（90% 區間 {lo:.0f}–{hi:.0f}）"
        )

    errs = load_recent_errors()
    if not errs.empty:
        mae = errs["prediction_error"].abs().mean()
        detail = "、".join(
            f'{r["date"]:%m/%d} 實際 {r["actual_sorties"]:.0f}／預測 '
            f'{r["predicted_sorties"]:.1f}（{r["prediction_error"]:+.1f}）'
            for _, r in errs.iterrows())
        lines.append(f"  📉 過去 {len(errs)} 日誤差 MAE {mae:.1f}：{detail}")
    return "\n".join(lines)


def _review():
    """讀 probability_review.json。缺檔是預期狀態（尚未產生），安靜回 None。"""
    if not PROB_REVIEW_JSON.exists():
        return None
    return _safe_read_json(PROB_REVIEW_JSON)


def surge_threshold_in_use(fc):
    """優先用預測檔裡記的門檻，讀不到才用常數。

    HIGH_SORTIE_THRESHOLD 是跟 pla_surge_model.SURGE_THRESHOLD 手動同步的重複常數，
    上游改了這裡沒改的話，推播會用錯的數字去描述機率在講什麼事件。
    CSV 的 surge_threshold 欄是上游自己寫的，不會漂。
    """
    if not fc.empty and "surge_threshold" in fc.columns:
        v = pd.to_numeric(fc["surge_threshold"], errors="coerce").dropna()
        if not v.empty:
            return int(v.iloc[0])
    return HIGH_SORTIE_THRESHOLD


_OUTCOME_TEXT = {
    "hit": ("✅", "命中"),
    "false_alarm": ("⚠️", "誤報"),
    "miss": ("❌", "漏報"),
    "correct_quiet": ("✅", "相符"),
}


def summarize_surge_probability(fc):
    """D+1 高架次機率 —— 這是整份簡報的主角，獨立成塊。

    只講 D+1。h>=2 的 surge 機率實測 ROC-AUC 0.45-0.57 等同亂猜，
    上游 predict_surge_daily.py 直接寫 NaN，這裡是第二道防線。
    """
    if fc.empty:
        return ""
    r = fc.iloc[0]
    prob = r.get("high_event_probability")
    if not isinstance(prob, (int, float)) or prob != prob:   # NaN 防護
        return ""

    thr = surge_threshold_in_use(fc)
    emoji = RISK_EMOJI.get(_s(r.get("risk_level")).upper(), "⚪")
    risk = _s(r.get("risk_level")).upper() or "UNKNOWN"
    lines = [f"🎯 明日({r['date']:%m/%d})高架次(≥{thr})機率 {prob:.0f}%　{emoji} {risk}"]

    base = high_sortie_base_rate()
    rev = _review()
    cutoff = (rev or {}).get("alert_cutoff")
    bits = []
    if base is not None:
        lift = prob / base if base > 0 else None
        bits.append(f"基準發生率 ~{base:.0f}%"
                    + (f" → {lift:.1f} 倍" if lift is not None else ""))
    if cutoff is not None:
        reached = prob / 100.0 >= cutoff
        bits.append(("已達" if reached else "未達") + f"警示線（{cutoff * 100:.0f}%）")
    bits.append("僅報 D+1")
    lines.append("   " + "，".join(bits))

    # 昨日回顧：單一事實，不是統計量，所以第一天就能顯示
    daily = ((rev or {}).get("daily") or {}).get("new")
    if daily:
        icon, word = _OUTCOME_TEXT.get(daily["outcome"], ("•", daily["outcome"]))
        try:
            d = datetime.strptime(daily["date"], "%Y-%m-%d").strftime("%m/%d")
        except ValueError:
            d = daily["date"]
        lines.append(f"   {icon} {d} 回顧：預告 {daily['prob'] * 100:.0f}%"
                     f"（{'有' if daily['alerted'] else '無'}警示），"
                     f"實際 {daily['actual']:.0f} 架次 → {word}")
    return "\n".join(lines)


def _model_prob_line(label, m):
    """機率檢討裡的單一模型摘要行。"""
    parts = [f"  {label}：Brier {m['brier']:.3f}（基準 {m['brier_base']:.3f}，"
             f"技能 {m['brier_skill'] * 100:+.0f}%）"]
    ratio = m.get("calibration_ratio")
    verdict = ""
    if ratio is not None:
        if ratio > 1.4:
            verdict = f" → 高估 {ratio:.1f} 倍"
        elif ratio < 0.7:
            verdict = f" → 低估 {1 / ratio:.1f} 倍"
        else:
            verdict = " → 校準良好"
    parts.append(f"，平均預告 {m['mean_prob'] * 100:.0f}%／實際 "
                 f"{m['observed_rate'] * 100:.0f}%{verdict}")
    line = "".join(parts)
    hit = (f"    警示 {m['alerts']} 次命中 {m['hits']} 次"
           + (f"（精確率 {m['precision'] * 100:.0f}%）" if m.get("precision") is not None else "")
           + f"，{m['n_positive']} 個高架次日抓到 {m['hits']} 個"
           + (f"（召回率 {m['recall'] * 100:.0f}%）" if m.get("recall") is not None else ""))
    return line + "\n" + hit


def summarize_probability_review():
    """機率檢討區塊：累積校準 + 兩模型對打 + 診斷。"""
    rev = _review()
    if not rev:
        return ""

    gates = rev.get("gates", {})
    models = rev.get("models", {})
    new_m, old_m = models.get("new", {}), models.get("legacy", {})

    if not new_m.get("enough"):
        n, npos = new_m.get("n", 0), new_m.get("n_positive", 0)
        return (f"📋 機率檢討：已累積 {n} 日（高架次 {npos} 日），需 "
                f"{gates.get('min_n', 30)} 日／{gates.get('min_positive', 5)} "
                f"個高架次日才開始評比")

    lines = [f"📋 機率檢討（近 {new_m['n']} 日，其中高架次 {new_m['n_positive']} 日）"]
    lines.append(_model_prob_line("新模型", new_m))
    if old_m.get("enough"):
        lines.append(_model_prob_line("舊模型", old_m))

    for d in rev.get("diagnostics", []):
        if d.get("severity") in ("warn", "alert"):
            icon = "🔴" if d["severity"] == "alert" else "⚠️"
            lines.append(f"  {icon} {d['message']}")
    return "\n".join(lines)


def summarize_digest(period):
    """週報／月報彙整。period: 'weekly' | 'monthly'。"""
    rev = _review()
    if not rev:
        return ""
    block = (rev.get("digest") or {}).get(period) or {}
    d = block.get("new")
    if not d:
        return ""

    title = "📅 週報（近 7 日）" if period == "weekly" else "🗓️ 月報（近 30 日）"
    lines = [f"{title}：高架次 {d['n_positive']}/{d['n']} 日，"
             f"警示 {d['alerts']} 次（命中 {d['hits']}、漏報 {d['misses']}）",
             f"  平均預告 {d['mean_prob'] * 100:.0f}%／實際發生 "
             f"{d['observed_rate'] * 100:.0f}%，"
             f"期間最高預告 {d['max_prob'] * 100:.0f}%、最高實際 {d['max_actual']:.0f} 架次"]

    old = block.get("legacy")
    if old:
        lines.append(f"  舊模型同期：平均預告 {old['mean_prob'] * 100:.0f}%，"
                     f"警示 {old['alerts']} 次（命中 {old['hits']}、漏報 {old['misses']}）")

    if period == "monthly":
        buckets = (rev.get("calibration") or {}).get("new") or []
        if buckets:
            lines.append("  校準分桶（預告區間 → 實際發生率）：")
            for b in buckets:
                lines.append(f"    {b['range']}：{b['n']} 日 → "
                             f"實際 {b['observed_rate'] * 100:.0f}%")
    return "\n".join(lines)


def _fmt_cell(cell):
    """把 model_comparison.json 的一格預測轉成「13.4（0–31）」。缺值回 '—'。"""
    if not isinstance(cell, dict) or cell.get("point") is None:
        return "—"
    lo, hi = cell.get("lower"), cell.get("upper")
    if lo is None or hi is None:
        return f"{cell['point']:.1f}"
    return f"{cell['point']:.1f}（{lo:.0f}–{hi:.0f}）"


def summarize_model_comparison():
    """新舊模型對照區塊。

    資料來自 data/predictions/model_comparison.json（daily_prediction.yml 產生）。
    刻意不在這裡自己算指標：LINE workflow 只裝 requests/pandas/matplotlib/pillow，
    不裝 sklearn，也不該為了算 MAE 去裝。缺檔一律降級成單行，絕不拋例外。
    """
    # 檔案不存在是預期狀態（雙軌尚未上線），不該印警告嚇人
    if not COMPARISON_JSON.exists():
        return ""
    data = _safe_read_json(COMPARISON_JSON)
    if not data:
        return ""

    legacy_label = data.get("models", {}).get("legacy", {}).get("label", "舊模型")
    header = f"🆚 新舊模型對照（舊 {legacy_label} 影子運行）"

    if not data.get("legacy_ok"):
        return "🆚 新舊模型對照：舊模型今日無輸出，暫無對照資料"

    lines = [header]
    footnotes = []
    if data.get("legacy_stale"):
        asof = data.get("legacy_asof") or "不明"
        lines.append(f"  ⚠️ 舊模型已多日未成功執行，以下沿用 {asof} 的版本")

    for row in data.get("forecast", []):
        try:
            date_str = datetime.strptime(row["date"], "%Y-%m-%d").strftime("%m/%d")
        except (KeyError, ValueError):
            continue
        lines.append(f"  {date_str}：新 {_fmt_cell(row.get('new'))}"
                     f"｜舊 {_fmt_cell(row.get('legacy'))}")

    n = data.get("n", 0)
    min_n = data.get("min_n", COMPARISON_MIN_N)
    new_m = data.get("models", {}).get("new", {})
    old_m = data.get("models", {}).get("legacy", {})

    if n <= 0:
        # 雙軌剛上線時的正常狀態：兩邊都預測過、且已有實際值的日期還是 0 天
        lines.append(f"  📐 尚未累積可比對的日數，需 {min_n} 日才開始評比")
    elif n < min_n:
        lines.append(f"  📐 已累積 {n} 日，樣本不足尚不評比（需 {min_n} 日）")
    else:
        def pair(key, fmt="{:.1f}", scale=1.0):
            a, b = new_m.get(key), old_m.get(key)
            if a is None or b is None:
                return None
            return f"新 {fmt.format(a * scale)}／舊 {fmt.format(b * scale)}"

        parts = []
        for label, key, fmt, scale in (
                ("MAE", "MAE", "{:.1f}", 1.0),
                ("90% 區間覆蓋率", "cov90", "{:.0f}%", 100.0),
                # bias 慣例由 JSON 的 bias_convention 指定（預測−實際，正值為高估）。
                # 這裡不做符號轉換，改在說明文字裡講清楚方向。
                ("高估幅度", "bias", "{:+.1f}", 1.0)):
            p = pair(key, fmt, scale)
            if p:
                parts.append(f"{label} {p}")
        if parts:
            lines.append(f"  📐 並行 N={n} 日：" + "，".join(parts))
            footnotes.append("高估幅度為預測−實際，正值代表高估")

    note = data.get("note")
    if note:
        footnotes.append(note)
    if footnotes:
        lines.append("  （" + "；".join(footnotes) + "）")

    # 只有標題沒有任何內容時不佔簡報版面
    return "\n".join(lines) if len(lines) > 1 else ""


# 火砲/演習「公告」判定：需同時命中「射擊/演習類」與「航警/禁區類」關鍵字，
# 才視為劃設禁航／射擊區的公告，藉此排除東部戰區日常實彈操演等宣傳貼文。
_FIRE_TERMS = [
    "實彈射擊", "实弹射击", "實彈演習", "实弹演习", "火砲射擊", "火炮射击",
    "實彈", "实弹", "軍事訓練", "军事训练", "軍事演習", "军事演习",
    "軍演", "军演", "演訓",
]
_ANNOUNCE_TERMS = [
    "航行警告", "航行警報", "航行警报", "海事局", "禁航", "禁航區", "禁航区",
    "禁止進入", "禁止进入", "禁止駛入", "禁止驶入", "禁止船舶", "劃設", "划设",
]


def summarize_fire_announcements(yesterday):
    """從新聞掃描「軍演／實彈射擊公告」，補足 MSA 海事航警可能遺漏者。

    例如中央社轉述海事局航行警告（劃設實彈射擊禁航區）的報導，
    MSA 官網爬蟲未必即時涵蓋，這裡以新聞來源補上。
    需同時命中射擊/演習類與航警/禁區類關鍵字，避免日常操演宣傳誤入。
    """
    data = _safe_read_json(NEWS_JSON)
    if not data:
        return None

    cutoff = (yesterday - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    y_str = yesterday.strftime("%Y-%m-%d")

    def art(a):
        return a.get("original_article") if isinstance(a, dict) else None

    matches = []
    seen_titles = set()
    for a in data:
        oa = art(a)
        if not isinstance(oa, dict):
            continue
        date = _s(oa.get("date")).strip()
        if not (cutoff <= date <= y_str):
            continue
        ed = a.get("extracted_data") if isinstance(a.get("extracted_data"), dict) else {}
        title = _s(oa.get("title")).strip()
        text = f"{title} {_s(oa.get('content'))} {_s(ed.get('Military_exercise'))}"
        if not (any(k in text for k in _FIRE_TERMS) and any(k in text for k in _ANNOUNCE_TERMS)):
            continue
        key = title[:30]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        matches.append((date, title, _s(oa.get("source")), _s(oa.get("url"))))

    if not matches:
        return None

    matches.sort(reverse=True)
    lines = ["🎯 軍演／實彈射擊公告（新聞補充）："]
    for date, title, src, url in matches[:MAX_ITEMS_PER_SOURCE]:
        label = SOURCE_LABELS.get(src, src)
        clean = title.replace("\n", " ").split("|")[0].strip()[:50]
        lines.append(f"  • [{date}] {clean}（{label}）")
    return "\n".join(lines)


def warning_period_text(w):
    """該航警的起訖日顯示字串。

    優先用爬蟲寫下的 time_periods（已是格式化過的短字串，不需截斷）；
    舊資料若還是空的，就當場從 title + content_preview 重解析一次。

    絕對不要對這個字串做切片 —— 起訖日被切斷正是使用者回報的問題：
    舊版 time_periods 是一串重疊的原始比對片段（例如
    "7月20日0600时至22日1200时; 2026年7月20日0600时至22日1200时"），
    再套 [:40] 就會停在「至22日12」，迄日就此消失。
    """
    text = _s(w.get("time_periods")).strip()
    if text:
        return text
    periods = parse_periods(
        f"{_s(w.get('title'))} {_s(w.get('content_preview'))}",
        _s(w.get("publish_date")))
    return format_periods_zh(periods)


def _dedup_cn_en_twins(warnings):
    """同一則航警的中英文兩版只留一則，優先留有起訖日的那則。

    海事局同一份公告會發中文版（浙航警655/26）與英文版（ZJ284/26），編號不同
    但座標完全相同。舊版把兩則都列出來，佔掉 5 則的名額，而英文版的起訖日
    格式（FROM 16 TO 17 MAR）舊 parser 又解不出來，等於用一半版面顯示
    「起訖日未載明」的重複內容。地圖那邊早就用形心去重，文字這邊沒有。
    """
    def twin_key(w):
        # 用第一個頂點當鍵，不用整串座標：英文版的 content_preview 常被截斷，
        # 解出來的頂點數比中文版少，整串比對永遠不會相等。
        pts = _parse_coords(w.get("coordinates"))
        if not pts:
            return ''
        return f"{pts[0][0]:.3f},{pts[0][1]:.3f}"

    best = {}
    order = []
    for w in warnings:
        key = twin_key(w)
        if not key:
            order.append(w)          # 沒座標無從配對，原樣保留
            continue
        prev = best.get(key)
        if prev is None:
            best[key] = w
            order.append(w)
            continue
        # 有起訖日的優先；同分則優先中文標題
        def score(x):
            return (bool(warning_period_text(x)),
                    any('一' <= c <= '鿿' for c in _s(x.get("title"))))
        if score(w) > score(prev):
            order[order.index(prev)] = w
            best[key] = w
    return order


def summarize_nav_warnings(yesterday):
    """MSA 火砲射擊/演習航警：取昨日（無則近期）公告。"""
    data = _safe_read_json(NAV_WARN_JSON)
    if not data:
        return "⚠️ MSA 軍事航警：無資料"
    cutoff = (yesterday - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    y_str = yesterday.strftime("%Y-%m-%d")
    recent = [w for w in data if cutoff <= _s(w.get("publish_date")) <= y_str]
    fallback = False
    if not recent:
        # 退而求其次：顯示資料中最新幾筆
        recent = sorted(data, key=lambda w: _s(w.get("publish_date")), reverse=True)[:3]
        fallback = True
    if not recent:
        return "⚠️ MSA 軍事航警：近日無新增"

    recent = _dedup_cn_en_twins(recent)
    head = "⚠️ MSA 火砲射擊/演習航警" + ("（近期）：" if fallback else "（昨日）：")
    lines = [head]
    for w in recent[:MAX_ITEMS_PER_SOURCE]:
        ch = _s(w.get("channel")).strip()
        title = _s(w.get("title")).strip()
        period = warning_period_text(w)
        pub = _s(w.get("publish_date")).strip()
        lines.append(f"  • [{pub}] {ch} {title}")
        # 起訖日另起一行，永遠完整。整份簡報有 5000 字預算，實際用不到一半，
        # 沒有理由為了省字把日期切掉。
        lines.append(f"      🕒 {period}" if period else "      🕒 起訖日未載明")
    return "\n".join(lines)


# 欄位名 → 顯示名。用於旗標與 remark 文字兩種來源的地點判定。
STRAIT_LABELS = [("宮古", "宮古海峽"), ("對馬", "對馬海峽"),
                 ("大禹", "大隅海峽"), ("與那國", "與那國島附近")]
ACTIVITY_LABELS = [("空中", "空中活動"), ("航母活動", "航母活動"),
                   ("艦通過", "艦艇通過"), ("聯合演訓", "聯合演訓")]


def _on(row, col):
    return str(row.get(col, "")).strip() in ("1", "1.0")


def _japan_locations(row):
    """該筆通報的地點。

    海峽旗標由 scraper_japan_mod 歸檔在「實際通過日」而非報告日，所以後續
    追蹤報告的旗標會全為 0（例：2026/07/20、07/21 的 remark 明寫「經宮古海峽
    向太平洋航行」，四個旗標卻都是 0，通過本身記在 07/16）。只讀旗標的話，
    使用者收到的就只剩「艦艇通過」而沒有地點——正是回報的問題。
    所以旗標與 remark 文字兩邊都取，聯集後輸出。
    """
    found = [name for col, name in STRAIT_LABELS if _on(row, col)]
    text = _s(row.get("remark")) + " " + _s(row.get("備考"))
    for _, name in STRAIT_LABELS:
        # 「與那國島附近」在 remark 裡就是這個寫法，比對前兩字即可
        if name[:3] in text and name not in found:
            found.append(name)
    return found


def _japan_report_line(row, prefix="  • "):
    """單筆防衛省通報：日期｜國家｜艘數｜艦型｜地點｜方向。"""
    date_str = _s(row.get("date")).strip()
    bits = []

    country = _s(row.get("國家")).strip()
    if country and country not in ("nan", "未知"):
        bits.append(country)

    # 艘數只能從 remark 文字取。plan_vessel_sorties 是國防部通報的「共艦」
    # 每日艘數（由 scraper.py 寫入），跟防衛省這一則通報的艦艇數無關 ——
    # 混用會出現「6 艘」配上「1艘艦艇航行」的自相矛盾。
    m = re.search(r'(\d+)\s*艘', _s(row.get("remark")))
    if m:
        bits.append(f"{m.group(1)} 艘")

    acts = [name for col, name in ACTIVITY_LABELS if _on(row, col)]
    if acts:
        bits.append("、".join(acts))

    head = f"{prefix}[{date_str}] " + "｜".join(bits) if bits else f"{prefix}[{date_str}]"

    detail = []
    # 艦型完整輸出，不截斷。多艦編隊的艦型串常超過 40 字，舊版的 [:40]
    # 會從中間切掉，等於後半編隊資訊消失。
    ship = _s(row.get("艦型")).strip()
    if ship and ship not in ("未提及", "nan"):
        detail.append(f"艦型：{ship}")

    locs = _japan_locations(row)
    if locs:
        detail.append(f"地點：{'、'.join(locs)}")

    direction = []
    if _on(row, "進"):
        direction.append("進入東海")
    if _on(row, "出"):
        direction.append("出往太平洋")
    if direction:
        detail.append("航向：" + "、".join(direction))

    lines = [head]
    for d in detail:
        lines.append(f"      {d}")

    remark = _s(row.get("remark")).strip()
    if remark and remark not in ("False", "nan", "True"):
        lines.append(f"      {remark}")
    return "\n".join(lines)


def summarize_japan_mod(recent_days=3):
    """日本防衛省（統合幕僚監部）最新通報摘要。"""
    try:
        if not JAPAN_MOD_CSV.exists():
            return "🇯🇵 日本防衛省：無資料"
        df = pd.read_csv(JAPAN_MOD_CSV, encoding="utf-8-sig")
        df["_date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
        mask = (
            df["_date"].notna()
            & df["remark"].notna()
            & (df["remark"].astype(str).str.strip() != "")
            & (df["remark"].astype(str) != "False")
        )
        valid = df[mask].sort_values("_date")   # 依日期排序，不靠列順序
        if valid.empty:
            return "🇯🇵 日本防衛省：近日無通報"

        latest_date = valid["_date"].max()
        window = valid[valid["_date"] >= latest_date - timedelta(days=recent_days - 1)]
        shown = window.tail(MAX_ITEMS_PER_SOURCE).iloc[::-1]

        lines = ["🇯🇵 日本防衛省（統合幕僚監部）通報："]
        for _, row in shown.iterrows():
            lines.append(_japan_report_line(row))

        # 近日通報常是「單艦航行」這種沒有艦型也沒有海峽的簡短續報。
        # 這時再往回找最近一筆有艦型或地點的通報補上，否則使用者連續好幾天
        # 都只看到「艦艇通過」而不知道是什麼艦、走哪裡 —— 正是回報的問題。
        def has_detail(r):
            ship = _s(r.get("艦型")).strip()
            return (ship not in ("", "未提及", "nan")) or bool(_japan_locations(r))

        if not any(has_detail(r) for _, r in shown.iterrows()):
            shown_dates = set(shown["_date"])
            older = [r for _, r in valid.iloc[::-1].iterrows()
                     if r["_date"] not in shown_dates and has_detail(r)]
            if older:
                lines.append("  ↩ 最近一次有艦型／地點的通報：")
                lines.append(_japan_report_line(older[0], prefix="  • "))
        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️  讀取日本防衛省資料失敗: {e}")
        traceback.print_exc()
        return "🇯🇵 日本防衛省：讀取失敗"


def summarize_news(yesterday):
    """昨日新聞摘要：依來源（微博 / 中央社·Google News / 新華社）分組。"""
    data = _safe_read_json(NEWS_JSON)
    if not data:
        return "📰 昨日新聞：無資料"

    cutoff = (yesterday - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    y_str = yesterday.strftime("%Y-%m-%d")

    def art_date(item):
        return _s((item.get("original_article") or {}).get("date"))

    recent = [a for a in data if cutoff <= art_date(a) <= y_str]
    fallback = False
    if not recent:
        # 退而求其次：用資料中最新一天
        all_dates = sorted({art_date(a) for a in data if art_date(a)}, reverse=True)
        if all_dates:
            latest_day = all_dates[0]
            recent = [a for a in data if art_date(a) == latest_day]
            fallback = True
    if not recent:
        return "📰 昨日新聞：近日無新增"

    # 依來源分組
    grouped = {}
    for a in recent:
        oa = a.get("original_article") or {}
        src = _s(oa.get("source")) or "other"
        grouped.setdefault(src, []).append(a)

    head = "📰 昨日新聞摘要" + ("（近期）：" if fallback else "：")
    lines = [head]
    # 依預期重要性排序來源
    order = ["weibo", "cna", "xinhua", "naval_transits"]
    srcs = order + [s for s in grouped if s not in order]
    for src in srcs:
        items = grouped.get(src)
        if not items:
            continue
        label = SOURCE_LABELS.get(src, src)
        lines.append(f"▍{label}（{len(items)} 則）")
        for a in items[:MAX_ITEMS_PER_SOURCE]:
            oa = a.get("original_article") or {}
            title = _s(oa.get("title")).strip().replace("\n", " ")[:45]
            lines.append(f"  • {title}")
    return "\n".join(lines)


def due_digest(now=None):
    """今天要不要附彙整：週一出週報、每月 1 號出月報（台灣時間）。

    刻意不開新的 workflow —— 週月報只是同一份 JSON 的不同切片，
    為它多養一條排程與一組 secret 不划算。
    """
    now = now or datetime.now(TW_TZ)
    if now.day == 1:
        return "monthly"
    if now.weekday() == 0:      # Monday
        return "weekly"
    return None


def compose_report_text(yesterday, force_digest=None):
    """組整份簡報文字。"""
    today_str = datetime.now(TW_TZ).strftime("%Y/%m/%d")
    fc = load_forecast()
    sections = [
        f"📡【每日台海動態簡報】{today_str}",
        "",
        summarize_forecast(fc),
    ]
    # 機率是這份簡報唯一有行動意義的數字，緊跟在預測之後、其他所有東西之前。
    # 4900 字截斷是尾端截斷，放在前面就不會被切掉。
    for block in (summarize_surge_probability(fc),
                  summarize_probability_review(),
                  summarize_model_comparison()):
        if block:
            sections += ["", block]

    period = force_digest or due_digest()
    if period:
        d = summarize_digest(period)
        if d:
            sections += ["", d]

    sections += [
        "",
        summarize_japan_mod(),
        "",
        summarize_nav_warnings(yesterday),
    ]
    # 軍演／實彈射擊公告（新聞補充）：僅在有命中時，緊接 MSA 航警之後
    fire_ann = summarize_fire_announcements(yesterday)
    if fire_ann:
        sections += ["", fire_ann]
    sections += [
        "",
        summarize_news(yesterday),
        "",
        "🗺️ 火砲射擊/演習公告範圍地圖與 📈 未來 3 日預測圖如下 ⬇️",
    ]
    text = "\n".join(sections)
    # LINE 單則文字上限 5000 字
    if len(text) > 4900:
        text = text[:4900] + "\n…（內容已截斷）"
    return text


# ============================================================
# 預測圖（30 日歷史 + 未來 3 日）
# ============================================================
def high_sortie_base_rate(window_days=BASE_RATE_WINDOW_DAYS):
    """近 N 天實際出現「高架次日」的比例（%）。

    機率沒有基準就無法解讀。實測基準約 10-18%，所以 45% 大約是 3 倍 lift，
    而 15% 其實低於平常 —— 沒有這條參考，兩者看起來都只是「一個百分比」。
    回傳 None 代表算不出來（資料不足），呼叫端應略過基準標註。
    """
    try:
        df = pd.read_csv(JAPAN_MOD_CSV, encoding="utf-8-sig")
        df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
        df = df.dropna(subset=["date", "pla_aircraft_sorties"])
        cutoff = df["date"].max() - timedelta(days=window_days)
        recent = df[df["date"] >= cutoff]["pla_aircraft_sorties"].astype(float)
        if len(recent) < 30:
            return None
        return float((recent >= HIGH_SORTIE_THRESHOLD).mean() * 100)
    except Exception as e:
        print(f"⚠️  計算基準發生率失敗: {e}")
        return None


def load_recent_errors(days=3):
    """取最近 N 天「已有實際值」的預測誤差。

    latest_prediction.csv 每天由 predict_surge_daily.merge_history() 回填 actual_sorties
    與 prediction_error，且同日期的舊預測會被當天的新預測取代，所以留在檔案裡
    的過去日期都是 h=1（前一日對隔天）的預測 —— 這正是誤差最有意義的那個 horizon。
    """
    try:
        df = pd.read_csv(PRED_CSV, encoding="utf-8-sig")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        done = df[df["actual_sorties"].notna() & df["predicted_sorties"].notna()]
        return done.sort_values("date").tail(days).reset_index(drop=True)
    except Exception as e:
        print(f"⚠️  讀取歷史誤差失敗: {e}")
        return pd.DataFrame()


def generate_chart(days_history=30, days_forecast=FORECAST_DAYS, error_days=3):
    """畫預測圖：上為 30 日歷史 + 未來 3 日預測，下為過去 3 日預測誤差。

    D+1 另外標出「高架次機率」。只標 D+1 是刻意的：pla_surge_model.py 的
    回測（見該檔 docstring）顯示 surge 機率只在 h=1 有鑑別力（ROC-AUC 0.764），
    h>=2 掉到 0.45-0.57 等同亂猜。把三天的機率並排畫出來會讓兩個亂數看起來
    跟一個有訊號的數字一樣可信。
    """
    try:
        hist = pd.read_csv(JAPAN_MOD_CSV, encoding="utf-8-sig")
        hist["date"] = pd.to_datetime(hist["date"], format="mixed", errors="coerce")
        hist = (hist.dropna(subset=["date"]).sort_values("date")
                [["date", "pla_aircraft_sorties"]]
                .dropna(subset=["pla_aircraft_sorties"]).tail(days_history))

        pred = load_forecast(days_forecast)
        if pred.empty or hist.empty:
            print("⚠️  歷史或預測資料不足，跳過產圖")
            return None

        errs = load_recent_errors(error_days)

        # 中文可用就用中文；LINEcron.yml 已安裝 fonts-noto-cjk。
        # 找不到 CJK 字型時整張圖退回英文，不要讓標籤變成豆腐字。
        cjk = _setup_cjk_font()
        if cjk:
            plt.rcParams["font.sans-serif"] = [cjk, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            L = {
                "actual": "實際架次", "pred": "預測（未來 3 日）", "ci": "90% 區間",
                "sorties": "架次", "date": "日期",
                "title": "共機架次 — 近 30 日實際 ＋ 未來 3 日預測",
                "err_title": f"過去 {error_days} 日預測誤差（實際 - 預測）",
                "err_y": "誤差", "mae": "MAE",
                "prob": "高架次機率", "no_signal": "D+2/D+3 機率無鑑別力，不顯示",
                "past_pred": f"過去 {error_days} 日當時預測",
                "base": "基準發生率",
                "legacy": "舊模型（對照）",
            }
        else:
            plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
            L = {
                "actual": "Actual", "pred": "Predicted (next 3d)", "ci": "90% CI",
                "sorties": "Sorties", "date": "Date",
                "title": "PLA Aircraft Sorties — 30-Day History + 3-Day Forecast",
                "err_title": f"Prediction Error, Last {error_days} Days (actual - predicted)",
                "err_y": "Error", "mae": "MAE",
                "prob": "P(high)", "no_signal": "D+2/D+3 probability not skillful - omitted",
                "past_pred": f"Predicted, last {error_days}d",
                "base": "base rate",
                "legacy": "Legacy model (shadow)",
            }

        fig, (ax, axe) = plt.subplots(
            2, 1, figsize=(8, 5.6), dpi=150,
            gridspec_kw={"height_ratios": [3, 1.3], "hspace": 0.55})
        fig.patch.set_facecolor("#ffffff")
        ax.set_facecolor("#fafafa")
        axe.set_facecolor("#fafafa")

        # ── 上：歷史 + 預測 ──────────────────────────────
        ax.plot(hist["date"], hist["pla_aircraft_sorties"],
                color="#2563EB", linewidth=1.8, marker="o", markersize=3,
                label=L["actual"], zorder=3)

        # 串接最後實際點 → 第一個預測點
        ax.plot([hist["date"].iloc[-1], pred["date"].iloc[0]],
                [hist["pla_aircraft_sorties"].iloc[-1], pred["predicted_sorties"].iloc[0]],
                color="#DC2626", linewidth=1.5, linestyle="--", zorder=2)

        # 預測線 + 信賴區間
        ax.plot(pred["date"], pred["predicted_sorties"],
                color="#DC2626", linewidth=2.0, marker="s", markersize=5,
                linestyle="--", label=L["pred"], zorder=3)
        ax.fill_between(pred["date"], pred["lower_bound"].clip(lower=0),
                        pred["upper_bound"], color="#DC2626", alpha=0.12,
                        label=L["ci"])

        # 舊模型（影子）的點預測。刻意只畫線不畫第二條區間帶：上面那條
        # fill_between 已經佔掉版面，再疊一層兩條都會看不清楚。缺檔就整條略過。
        legacy = load_legacy_forecast(days_forecast)
        if not legacy.empty:
            ax.plot(legacy["date"], legacy["predicted_sorties"],
                    color="#6B7280", linewidth=1.4, marker="^", markersize=4,
                    linestyle=":", label=L["legacy"], zorder=2)

        # 過去 3 日的預測點疊在實際線上，讓誤差長條圖有對照
        if not errs.empty:
            ax.plot(errs["date"], errs["predicted_sorties"],
                    linestyle="none", marker="x", markersize=6,
                    color="#9CA3AF", markeredgewidth=1.6, zorder=4,
                    label=L["past_pred"])

        for _, r in pred.iterrows():
            ax.annotate(f'{r["predicted_sorties"]:.1f}',
                        (r["date"], r["predicted_sorties"]),
                        textcoords="offset points", xytext=(0, 10),
                        fontsize=9, fontweight="bold", color="#DC2626", ha="center")

        # D+1 高架次機率標註
        d1 = pred.iloc[0]
        prob = d1.get("high_event_probability")
        if isinstance(prob, (int, float)) and prob == prob:
            risk = _s(d1.get("risk_level")).upper()
            box_color = {"HIGH": "#B91C1C", "MEDIUM-HIGH": "#EA580C",
                         "MEDIUM": "#CA8A04"}.get(risk, "#15803D")
            label = f'D+1 {L["prob"]} (≥{HIGH_SORTIE_THRESHOLD}) {prob:.0f}%'
            base = high_sortie_base_rate()
            if base is not None:
                label += f'\n{L["base"]} ~{base:.0f}%'
            ax.annotate(
                label, (d1["date"], d1["predicted_sorties"]),
                textcoords="offset points", xytext=(-6, 34),
                fontsize=8, fontweight="bold", color="white", ha="right",
                bbox=dict(boxstyle="round,pad=0.4", fc=box_color, ec="none", alpha=0.92),
                arrowprops=dict(arrowstyle="-", color=box_color, lw=1.2))
            # 放在標題下方而不是圖面內，避免壓到信賴區間的陰影
            ax.annotate(L["no_signal"], (0.5, 1.015), xycoords="axes fraction",
                        fontsize=7.5, color="#6B7280", ha="center", va="bottom",
                        style="italic")

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
        ax.tick_params(axis="x", rotation=45, labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.set_ylabel(L["sorties"], fontsize=9)
        ax.set_title(L["title"], fontsize=11, fontweight="bold", pad=18)
        ax.legend(fontsize=7.5, loc="upper left", framealpha=0.9)
        ax.grid(axis="y", alpha=0.3, linewidth=0.5)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_ylim(bottom=0)

        # ── 下：過去 3 日誤差 ────────────────────────────
        if errs.empty:
            axe.text(0.5, 0.5, "—", ha="center", va="center", fontsize=12,
                     color="#9CA3AF", transform=axe.transAxes)
            axe.set_xticks([])
            axe.set_yticks([])
        else:
            labels = [d.strftime("%m/%d") for d in errs["date"]]
            values = errs["prediction_error"].astype(float).tolist()
            colors = ["#DC2626" if v > 0 else "#2563EB" for v in values]
            bars = axe.bar(labels, values, color=colors, alpha=0.75, width=0.32)
            span = max(abs(v) for v in values) or 1.0
            for bar, v, (_, r) in zip(bars, values, errs.iterrows()):
                axe.annotate(
                    f'{v:+.1f}\n({r["actual_sorties"]:.0f} vs {r["predicted_sorties"]:.1f})',
                    (bar.get_x() + bar.get_width() / 2, v),
                    textcoords="offset points",
                    xytext=(0, 6 if v >= 0 else -22),
                    fontsize=7.5, ha="center", color="#374151")
            axe.axhline(0, color="#374151", linewidth=0.9)
            axe.set_ylim(-span * 1.9, span * 1.9)
            mae = sum(abs(v) for v in values) / len(values)
            axe.set_title(f'{L["err_title"]}　{L["mae"]} {mae:.1f}',
                          fontsize=9.5, fontweight="bold")
            axe.set_ylabel(L["err_y"], fontsize=8)
            axe.tick_params(labelsize=8)
            axe.grid(axis="y", alpha=0.25, linewidth=0.5)
            axe.spines["top"].set_visible(False)
            axe.spines["right"].set_visible(False)

        CHART_OUT.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(CHART_OUT, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"✅ 圖表已儲存：{CHART_OUT}")
        return CHART_OUT
    except Exception as e:
        print(f"⚠️  產圖失敗: {e}")
        traceback.print_exc()
        return None


def upload_chart_to_github(local_path, github_token, repo_path=CHART_REPO_PATH):
    """上傳 PNG 到 repo（main 分支）指定路徑，回傳公開 raw URL；失敗回 None。"""
    api = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{repo_path}"
    headers = {"Authorization": f"Bearer {github_token}",
               "Accept": "application/vnd.github.v3+json"}
    try:
        with open(local_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode()
        sha = None
        r = requests.get(api, headers=headers, timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
        payload = {
            "message": f"📊 Update LINE brief image {repo_path} — {datetime.now(TW_TZ):%Y-%m-%d}",
            "content": content_b64,
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha
        r = requests.put(api, headers=headers, json=payload, timeout=30)
        if r.status_code not in (200, 201):
            print(f"⚠️  GitHub 上傳圖片失敗：{r.status_code} {r.text[:200]}")
            return None
        ts = int(datetime.now().timestamp())
        raw_url = (f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
                   f"/main/{repo_path}?t={ts}")
        print(f"✅ 圖片已上傳：{raw_url}")
        return raw_url
    except Exception as e:
        print(f"⚠️  上傳圖片發生例外: {e}")
        return None


# ============================================================
# 火砲射擊 / 演習公告範圍地圖
# ============================================================
# 火砲/實彈射擊類公告用紅色，其餘（演習、訓練、禁航）用橙色
_FIRE_KEYWORDS = ("射擊", "射击", "火砲", "火炮", "實彈", "实弹", "火箭")


def _is_fire_warning(title):
    return any(k in _s(title) for k in _FIRE_KEYWORDS)


def _setup_cjk_font():
    """尋找系統可用的 CJK 字型並註冊給 matplotlib，回傳字型家族名；找不到回 None。

    GitHub Actions 需先 apt 安裝 fonts-noto-cjk（見 LINEcron.yml）。
    找不到時圖上中文會顯示為方框，但編號與英文仍可讀。
    """
    from matplotlib import font_manager
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                font_manager.fontManager.addfont(path)
                return font_manager.FontProperties(fname=path).get_name()
            except Exception:
                continue
    for f in font_manager.fontManager.ttflist:
        if any(k in f.name for k in ("CJK", "WenQuanYi", "Heiti", "YaHei", "PingFang")):
            return f.name
    return None


def _parse_coords(coord_str):
    """把 'lat,lon; lat,lon; …' 解析成 [(lat, lon), …]，格式異常回空 list。"""
    pts = []
    for pair in _s(coord_str).split(";"):
        pair = pair.strip()
        if not pair:
            continue
        try:
            lat_s, lon_s = pair.split(",")
            lat, lon = float(lat_s), float(lon_s)
        except (ValueError, TypeError):
            continue
        # 粗略合理性檢查（西太平洋周邊）
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            pts.append((lat, lon))
    return pts


def _zone_centroid(pts):
    """多邊形頂點的平均座標 (lat, lon)。"""
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _zones_close(c1, c2, tol=0.05):
    """兩個形心是否接近（同一射擊區）。"""
    return abs(c1[0] - c2[0]) <= tol and abs(c1[1] - c2[1]) <= tol


def _dedup_zones(zones, tol=0.05):
    """依形心近似跨來源去重，保留先出現者（MSA 官方優先於新聞）。"""
    out, cents = [], []
    for z in zones:
        pts = z.get("points")
        if not pts:
            continue
        c = _zone_centroid(pts)
        if any(_zones_close(c, pc, tol) for pc in cents):
            continue
        cents.append(c)
        out.append(z)
    return out


def _warning_coord_groups(w):
    """回傳該航警的多邊形群組 [[(lat,lon),…], …] 與是否來自自由文字解析。

    優先用結構化 coordinates 欄（單一多邊形）；缺時退回 content_preview
    的度分座標解析（可含多個射擊區）。
    """
    pts = _parse_coords(w.get("coordinates"))
    if pts:
        return [pts], False
    groups = extract_coord_zones_from_text(w.get("content_preview"))
    if groups:
        return groups, True
    return [], False


def select_range_warnings(yesterday, max_zones=8):
    """挑昨日（無則近期）且含座標的火砲射擊／演習航警，回傳解析後清單。

    座標來源：優先結構化 coordinates 欄；若為空則解析 content_preview
    內的度分座標（如福建海事局闽航警只把座標寫在公告文字裡）。
    """
    data = _safe_read_json(NAV_WARN_JSON)
    if not data:
        return []
    cutoff = (yesterday - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    y_str = yesterday.strftime("%Y-%m-%d")

    def has_coords(w):
        groups, _ = _warning_coord_groups(w)
        return len(groups) >= 1

    recent = [w for w in data
              if cutoff <= _s(w.get("publish_date")) <= y_str and has_coords(w)]
    if not recent:
        recent = sorted([w for w in data if has_coords(w)],
                        key=lambda w: _s(w.get("publish_date")), reverse=True)[:5]

    # 中文標題優先（同一公告有中英文兩版時，去重保留中文那筆）
    def _has_cjk(s):
        return any('一' <= ch <= '鿿' for ch in _s(s))
    recent.sort(key=lambda w: (not _has_cjk(w.get("title")), _s(w.get("publish_date"))))

    # 以形心去重（涵蓋中英文重複公告、及同一公告多來源）
    zones, cents = [], []
    for w in recent:
        groups, from_text = _warning_coord_groups(w)
        if not groups:
            continue
        title = _s(w.get("title")).strip()
        preview = _s(w.get("content_preview"))
        # 火砲/實彈判定兼看內文：闽航警等標題寫「军事演习」但內文為「实弹射击」
        is_fire = _is_fire_warning(f"{title} {preview}")
        multi = len(groups) > 1
        for gi, pts in enumerate(groups, 1):
            c = _zone_centroid(pts)
            if any(_zones_close(c, pc) for pc in cents):
                continue
            cents.append(c)
            suffix = f"(區{gi})" if multi else ""
            zones.append({
                "title": f"{title[:18]}{suffix}",
                "channel": _s(w.get("channel")).strip(),
                "period": warning_period_text(w) or _s(w.get("publish_date")).strip(),
                "date": _s(w.get("publish_date")).strip(),
                "points": pts,
                "fire": is_fire,
                # 由 content_preview 文字解析者列入放大子圖（通常是台灣海峽射擊公告）
                "detail": from_text,
            })
            if len(zones) >= max_zones:
                return zones
    return zones


# 度分格式座標，例如「23-41.31N、117-31.49E」= 23°41.31′N, 117°31.49′E
_DMS_TOKEN = re.compile(r'(\d{1,3})[-–](\d{1,2}(?:\.\d+)?)\s*([NSEWnsew])')


def _dms_to_decimal(deg, minutes):
    return float(deg) + float(minutes) / 60.0


def extract_coord_zones_from_text(text, max_gap=25):
    """從新聞內文抽取度分座標，依文字間距切分成多個多邊形群組。

    一篇公告常含多個射擊區（不同日期各一組頂點），以座標間的字元距離
    分群：同一多邊形的頂點僅以「、；」分隔（間距小），不同區之間隔著整句
    敘述（間距大）。回傳 [[(lat,lon),…], …]。
    """
    toks = []
    for m in _DMS_TOKEN.finditer(_s(text)):
        val = _dms_to_decimal(m.group(1), m.group(2))
        hemi = m.group(3).upper()
        if hemi in ("S", "W"):
            val = -val
        toks.append((m.start(), m.end(), "lat" if hemi in ("N", "S") else "lon", val))
    if not toks:
        return []

    groups = [[toks[0]]]
    for prev, tok in zip(toks, toks[1:]):
        if tok[0] - prev[1] <= max_gap:
            groups[-1].append(tok)
        else:
            groups.append([tok])

    result = []
    for g in groups:
        pts, pending_lat = [], None
        for _, _, kind, val in g:
            if kind == "lat":
                pending_lat = val
            elif pending_lat is not None:  # lon，與前一個 lat 配對
                pts.append((pending_lat, val))
                pending_lat = None
        # 僅保留東亞海域合理範圍，排除誤抓
        pts = [(la, lo) for la, lo in pts if 0 < la < 60 and 100 < lo < 150]
        if pts:
            result.append(pts)
    return result


def select_news_range_warnings(yesterday, max_zones=6):
    """從新聞內文（中央社等）抽取含座標的實彈射擊／演習公告範圍。

    補足 MSA 官網海事航警未即時涵蓋者（例如中央社轉述海事局劃設的
    台灣海峽實彈射擊禁航區，內文附有度分座標）。
    """
    data = _safe_read_json(NEWS_JSON)
    if not data:
        return []
    cutoff = (yesterday - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    y_str = yesterday.strftime("%Y-%m-%d")

    zones = []
    for a in data:
        oa = a.get("original_article") if isinstance(a, dict) else None
        if not isinstance(oa, dict):
            continue
        date = _s(oa.get("date")).strip()
        if not (cutoff <= date <= y_str):
            continue
        ed = a.get("extracted_data") if isinstance(a.get("extracted_data"), dict) else {}
        title = _s(oa.get("title")).strip()
        content = _s(oa.get("content"))
        text = f"{title} {content} {_s(ed.get('Military_exercise'))}"
        if not (any(k in text for k in _FIRE_TERMS) and any(k in text for k in _ANNOUNCE_TERMS)):
            continue
        groups = extract_coord_zones_from_text(content)
        if not groups:
            continue
        clean = title.replace("\n", " ").split("|")[0].strip()
        for gi, pts in enumerate(groups, 1):
            suffix = f"(區{gi})" if len(groups) > 1 else ""
            zones.append({
                "title": f"{clean[:16]}{suffix}",
                "channel": _s(oa.get("source")),
                "period": date,
                "date": date,
                "points": pts,
                "fire": True,        # 皆屬實彈射擊／演習公告
                "from_news": True,   # 標記來源，地圖上以虛線區別
            })
            if len(zones) >= max_zones:
                return zones
    return zones


# ── OpenStreetMap 底圖（純 requests + PIL，失敗則退回無底圖）──────────
def _deg2num(lat, lon, z):
    lat_r = math.radians(lat)
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def _num2deg(x, y, z):
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def _fetch_osm_basemap(min_lat, max_lat, min_lon, max_lon, max_tiles=25):
    """抓取涵蓋 bbox 的 OSM 圖磚並拼接。

    回傳 (PIL.Image, extent=(lon_min,lon_max,lat_min,lat_max))；任何失敗回 (None, None)。
    每日僅一張，屬低頻使用；仍設定合規 User-Agent。
    """
    try:
        from PIL import Image
    except Exception:
        return None, None

    # 依 bbox 大小選 zoom：讓橫向圖磚數落在 <= 5 左右
    chosen = None
    for z in range(12, 3, -1):
        x0, _ = _deg2num(min_lat, min_lon, z)
        x1, _ = _deg2num(min_lat, max_lon, z)
        _, y0 = _deg2num(max_lat, min_lon, z)
        _, y1 = _deg2num(min_lat, min_lon, z)
        nx = int(x1) - int(x0) + 1
        ny = int(y1) - int(y0) + 1
        if nx * ny <= max_tiles and nx <= 6 and ny <= 6:
            chosen = z
            break
    if chosen is None:
        chosen = 5
    z = chosen

    xt0, yt0 = int(_deg2num(max_lat, min_lon, z)[0]), int(_deg2num(max_lat, min_lon, z)[1])
    xt1, yt1 = int(_deg2num(min_lat, max_lon, z)[0]), int(_deg2num(min_lat, max_lon, z)[1])
    x_start, x_end = min(xt0, xt1), max(xt0, xt1)
    y_start, y_end = min(yt0, yt1), max(yt0, yt1)

    tile_size = 256
    n_across = x_end - x_start + 1
    n_down = y_end - y_start + 1
    if n_across * n_down > max_tiles:
        return None, None

    mosaic = Image.new("RGB", (n_across * tile_size, n_down * tile_size))
    headers = {"User-Agent": "pla-data-dashboard/1.0 (daily LINE brief map)"}
    try:
        for xi, xt in enumerate(range(x_start, x_end + 1)):
            for yi, yt in enumerate(range(y_start, y_end + 1)):
                url = f"https://tile.openstreetmap.org/{z}/{xt}/{yt}.png"
                r = requests.get(url, headers=headers, timeout=20)
                if r.status_code != 200:
                    return None, None
                tile = Image.open(BytesIO(r.content)).convert("RGB")
                mosaic.paste(tile, (xi * tile_size, yi * tile_size))
    except Exception as e:
        # 網路/圖磚服務不可用時，退回無底圖（仍會畫出範圍多邊形）
        print(f"⚠️  OSM 底圖抓取失敗，改用無底圖：{e}")
        return None, None

    lat_top, lon_left = _num2deg(x_start, y_start, z)
    lat_bottom, lon_right = _num2deg(x_end + 1, y_end + 1, z)
    extent = (lon_left, lon_right, lat_bottom, lat_top)
    return mosaic, extent


def _draw_zones_on_ax(ax, numbered_zones, spread_labels=False):
    """在指定 ax 上畫出各區塊多邊形/線/單點與編號圓圈。

    numbered_zones 為 [(編號, zone), …]，編號沿用總覽圖圖例，確保放大子圖一致。
    spread_labels=True 時，對過度接近的編號圓圈做小幅位移避免重疊。
    """
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    span = max(abs(xlim[1] - xlim[0]), abs(ylim[1] - ylim[0])) or 1.0
    thresh, step = span * 0.02, span * 0.03
    placed = []
    for idx, z in numbered_zones:
        color = "#DC2626" if z["fire"] else "#EA9010"
        ls = "--" if z.get("from_news") else "-"   # 新聞來源虛線；MSA（含文字解析）實線
        pts = z["points"]
        plons = [p[1] for p in pts]
        plats = [p[0] for p in pts]
        if len(pts) >= 3:
            poly = mpatches.Polygon(list(zip(plons, plats)), closed=True,
                                    facecolor=color, edgecolor=color,
                                    alpha=0.30, linewidth=1.8, linestyle=ls, zorder=3)
            ax.add_patch(poly)
            ax.plot(plons + [plons[0]], plats + [plats[0]],
                    color=color, linewidth=1.8, linestyle=ls, zorder=4)
            cx, cy = sum(plons) / len(plons), sum(plats) / len(plats)
        elif len(pts) == 2:
            ax.plot(plons, plats, color=color, linewidth=2.2, linestyle=ls, zorder=4)
            cx, cy = sum(plons) / 2, sum(plats) / 2
        else:  # 單點（如火箭發射）
            ax.plot(plons, plats, marker="*", markersize=16, color=color, zorder=4)
            cx, cy = plons[0], plats[0]
        if spread_labels:
            k = 0
            while k < 8 and any(abs(cx - px) < thresh and abs(cy - py) < thresh
                                for px, py in placed):
                cx += step
                cy += step
                k += 1
        placed.append((cx, cy))
        ax.annotate(str(idx), (cx, cy), fontsize=10, fontweight="bold",
                    color="white", ha="center", va="center", zorder=5,
                    bbox=dict(boxstyle="circle,pad=0.25", fc=color, ec="white", lw=1))


def generate_range_map(yesterday):
    """畫出近日火砲射擊／演習公告範圍地圖，存 PNG，回傳路徑（無資料或失敗回 None）。"""
    try:
        # MSA 官方在前、新聞在後，依形心跨來源去重（同一射擊區保留官方那筆）
        zones = _dedup_zones(select_range_warnings(yesterday)
                             + select_news_range_warnings(yesterday))
        if not zones:
            print("⚠️  無可繪製的航警範圍，跳過地圖")
            return None

        all_pts = [p for z in zones for p in z["points"]]
        lats = [p[0] for p in all_pts]
        lons = [p[1] for p in all_pts]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        # padding（最少 0.5 度，避免單點或極小範圍）
        pad_lat = max((max_lat - min_lat) * 0.25, 0.5)
        pad_lon = max((max_lon - min_lon) * 0.25, 0.5)
        min_lat, max_lat = min_lat - pad_lat, max_lat + pad_lat
        min_lon, max_lon = min_lon - pad_lon, max_lon + pad_lon

        basemap, extent = _fetch_osm_basemap(min_lat, max_lat, min_lon, max_lon)

        cjk_font = _setup_cjk_font()
        if cjk_font:
            plt.rcParams["font.sans-serif"] = [cjk_font, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
        else:
            plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
            print("⚠️  未找到 CJK 字型，地圖中文可能顯示為方框")
        fig, ax = plt.subplots(figsize=(8, 8), dpi=150)

        if basemap is not None:
            import numpy as np
            ax.imshow(np.asarray(basemap), extent=extent, origin="upper", zorder=0)
            ax.set_xlim(min_lon, max_lon)
            ax.set_ylim(min_lat, max_lat)
        else:
            # 無底圖退回：淺藍海域背景
            ax.set_facecolor("#dcecf5")
            ax.set_xlim(min_lon, max_lon)
            ax.set_ylim(min_lat, max_lat)
            ax.grid(alpha=0.3, linewidth=0.5)

        numbered = list(enumerate(zones, 1))
        _draw_zones_on_ax(ax, numbered, spread_labels=True)

        ax.set_xlabel("Longitude", fontsize=9)
        ax.set_ylabel("Latitude", fontsize=9)
        title = "解放軍火砲射擊／演習公告範圍（近日）" if cjk_font \
            else "PLA Firing / Exercise Warning Areas (recent)"
        ax.set_title(title, fontsize=13, fontweight="bold")

        # 放大子圖：聚焦由自由文字解析的射擊公告（新聞或 MSA content_preview），
        # 讓在總覽尺度下被壓成幾像素的台灣海峽射擊區範圍看得清楚
        try:
            detail = [(i, z) for i, z in numbered if z.get("detail") or z.get("from_news")]
            if detail:
                dlats = [p[0] for _, z in detail for p in z["points"]]
                dlons = [p[1] for _, z in detail for p in z["points"]]
                dmin_lat, dmax_lat = min(dlats), max(dlats)
                dmin_lon, dmax_lon = min(dlons), max(dlons)
                overview_w = max_lon - min_lon
                detail_w = dmax_lon - dmin_lon
                # 僅在細節區塊相對總覽夠小時才放大（否則總覽已足夠清楚）
                if overview_w > 0 and (detail_w / overview_w) < 0.4:
                    dpad_lat = max((dmax_lat - dmin_lat) * 0.4, 0.15)
                    dpad_lon = max((dmax_lon - dmin_lon) * 0.4, 0.15)
                    ilat0, ilat1 = dmin_lat - dpad_lat, dmax_lat + dpad_lat
                    ilon0, ilon1 = dmin_lon - dpad_lon, dmax_lon + dpad_lon
                    inset = ax.inset_axes([0.02, 0.58, 0.36, 0.36])
                    ibase, iext = _fetch_osm_basemap(ilat0, ilat1, ilon0, ilon1)
                    if ibase is not None:
                        import numpy as np
                        inset.imshow(np.asarray(ibase), extent=iext,
                                     origin="upper", zorder=0)
                    else:
                        inset.set_facecolor("#dcecf5")
                    inset.set_xlim(ilon0, ilon1)
                    inset.set_ylim(ilat0, ilat1)
                    _draw_zones_on_ax(inset, detail, spread_labels=False)
                    inset.tick_params(labelsize=6)
                    ititle = "台灣海峽射擊區放大" if cjk_font \
                        else "Taiwan Strait firing areas (zoom)"
                    inset.set_title(ititle, fontsize=8, fontweight="bold")
                    ax.indicate_inset_zoom(inset, edgecolor="black", alpha=0.6)
        except Exception as e:
            print(f"⚠️  放大子圖繪製失敗（略過）: {e}")

        # 圖例：編號 → 公告標題（截短），放在地圖下方避免遮擋範圍
        legend_handles = []
        for i, z in enumerate(zones, 1):
            color = "#DC2626" if z["fire"] else "#EA9010"
            src_tag = "〔新聞〕" if z.get("from_news") else ""
            label = f"{i}. {src_tag}{z['title']}"
            # 起訖日不截斷。舊版的 [:16] 是斷字最嚴重的地方——
            # 「07/20 0600 至 07/22 1200」會被切成「07/20 0600 至 07/2」。
            if z["period"]:
                label += f"（{z['period']}）"
            legend_handles.append(mpatches.Patch(color=color, label=label))
        legend_title = "紅＝火砲/實彈射擊　橙＝演習/訓練/禁航　虛線＝新聞來源" if cjk_font \
            else "red = live fire / orange = exercise / dashed = news"
        ax.legend(handles=legend_handles, fontsize=8,
                  loc="upper center", bbox_to_anchor=(0.5, -0.08),
                  ncol=1, framealpha=0.95, title=legend_title, title_fontsize=8.5)

        plt.tight_layout()
        MAP_OUT.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(MAP_OUT, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        note = "（含 OSM 底圖）" if basemap is not None else "（無底圖）"
        print(f"✅ 航警範圍地圖已儲存：{MAP_OUT} {note}")
        return MAP_OUT
    except Exception as e:
        print(f"⚠️  產製航警地圖失敗: {e}")
        traceback.print_exc()
        return None


# ============================================================
# LINE 推播
# ============================================================
def send_line(messages, token, user_id):
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    payload = {"to": user_id, "messages": messages}
    r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=30)
    if r.status_code == 200:
        print("✅ LINE 訊息發送成功！")
        return True
    print(f"❌ LINE 發送失敗：{r.status_code}\n{r.text}")
    return False


def main():
    parser = argparse.ArgumentParser(description="LINE 每日台海動態簡報")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印出內容與產圖，不實際推播")
    parser.add_argument("--force-digest", choices=["weekly", "monthly"],
                        help="強制附上週報／月報（預覽用，不必等到週一或 1 號）")
    args = parser.parse_args()

    yesterday = (datetime.now(TW_TZ) - timedelta(days=1)).date()
    yesterday = datetime(yesterday.year, yesterday.month, yesterday.day)

    # 1. 組文字
    report_text = compose_report_text(yesterday, force_digest=args.force_digest)
    print("─" * 50)
    print(report_text)
    print("─" * 50)

    # 2. 產圖：預測折線圖 + 火砲射擊/演習範圍地圖
    chart_path = generate_chart()
    map_path = generate_range_map(yesterday)

    # 3. 上傳圖片取得公開網址
    chart_url = map_url = None
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        if chart_path:
            chart_url = upload_chart_to_github(chart_path, github_token, CHART_REPO_PATH)
        if map_path:
            map_url = upload_chart_to_github(map_path, github_token, MAP_REPO_PATH)
    elif chart_path or map_path:
        print("⚠️  未設定 GITHUB_TOKEN，略過圖片上傳（只送文字）")

    # 4. 組 LINE 訊息（LINE 單次 push 上限 5 則）
    messages = [{"type": "text", "text": report_text}]
    for url in (map_url, chart_url):
        if url:
            messages.append({
                "type": "image",
                "originalContentUrl": url,
                "previewImageUrl": url,
            })

    if args.dry_run:
        print("\n🏁 Dry-run 模式 — 未實際推播")
        print(f"   訊息則數：{len(messages)}"
              f"（範圍地圖：{'是' if map_url else '否'}、"
              f"預測圖：{'是' if chart_url else '否'}）")
        return

    # 5. 推播
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        print("❌ 缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID 環境變數")
        sys.exit(1)

    ok = send_line(messages, token, user_id)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
