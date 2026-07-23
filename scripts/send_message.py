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

# ── 路徑（相對於 repo root）─────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
NEWS_JSON = REPO_ROOT / "data" / "news_classified.json"
NAV_WARN_JSON = REPO_ROOT / "data" / "navigation_warnings" / "military_exercises.json"
JAPAN_MOD_CSV = REPO_ROOT / "data" / "JapanandBattleship.csv"
PRED_CSV = REPO_ROOT / "data" / "predictions" / "latest_prediction.csv"
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

RISK_EMOJI = {"LOW": "🟢", "MEDIUM": "🟡", "MEDIUM-HIGH": "🟠", "HIGH": "🔴", "CRITICAL": "🔴"}


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


def load_forecast(days=FORECAST_DAYS):
    """讀 latest_prediction.csv，回傳未來 N 天（尚無實際值）的預測列。"""
    try:
        df = pd.read_csv(PRED_CSV, encoding="utf-8-sig")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
        future = df[df["actual_sorties"].isna()] if "actual_sorties" in df.columns else df
        if future.empty:
            future = df
        return future.head(days).reset_index(drop=True)
    except Exception as e:
        print(f"⚠️  讀取預測失敗: {e}")
        return pd.DataFrame()


def summarize_forecast(fc):
    """組未來 3 日預測文字。"""
    if fc.empty:
        return "📊 未來 3 日預測：暫無資料"
    lines = ["📊 未來 3 日 PLA 架次預測："]
    for _, r in fc.iterrows():
        emoji = RISK_EMOJI.get(str(r.get("risk_level", "")).upper(), "⚪")
        date_str = r["date"].strftime("%m/%d")
        pred = r.get("predicted_sorties", 0)
        lo = r.get("lower_bound", 0)
        hi = r.get("upper_bound", 0)
        prob = r.get("high_event_probability", "")
        prob_str = f"，高活動機率 {prob:.0f}%" if isinstance(prob, (int, float)) else ""
        lines.append(
            f"  {emoji} {date_str}：約 {pred:.1f} 架次"
            f"（90% 區間 {lo:.0f}–{hi:.0f}{prob_str}）"
        )
    return "\n".join(lines)


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
    head = "⚠️ MSA 火砲射擊/演習航警" + ("（近期）：" if fallback else "（昨日）：")
    lines = [head]
    for w in recent[:MAX_ITEMS_PER_SOURCE]:
        ch = _s(w.get("channel")).strip()
        title = _s(w.get("title")).strip()[:40]
        period = _s(w.get("time_periods")).strip()[:40]
        pub = _s(w.get("publish_date")).strip()
        line = f"  • [{pub}] {ch} {title}"
        if period:
            line += f"（{period}）"
        lines.append(line)
    return "\n".join(lines)


def summarize_japan_mod():
    """日本防衛省（統合幕僚監部）最新通報摘要。"""
    try:
        if not JAPAN_MOD_CSV.exists():
            return "🇯🇵 日本防衛省：無資料"
        df = pd.read_csv(JAPAN_MOD_CSV, encoding="utf-8-sig")
        mask = (
            df["remark"].notna()
            & (df["remark"].astype(str).str.strip() != "")
            & (df["remark"].astype(str) != "False")
        )
        valid = df[mask]
        if valid.empty:
            return "🇯🇵 日本防衛省：近日無通報"
        latest = valid.iloc[-1]

        def on(col):
            return str(latest.get(col, "")).strip() in ("1", "1.0")

        straits = [name for col, name in
                   [("宮古", "宮古海峽"), ("對馬", "對馬海峽"),
                    ("大禹", "大隅海峽"), ("與那國", "與那國")] if on(col)]
        acts = [name for col, name in
                [("空中", "空中活動"), ("航母活動", "航母活動"),
                 ("艦通過", "艦艇通過"), ("聯合演訓", "聯合演訓")] if on(col)]
        parts = []
        if acts:
            parts.append("、".join(acts))
        if straits:
            parts.append("經 " + "、".join(straits))
        flag_summary = "；".join(parts)

        # 主要內容優先採用 remark（爬蟲已產生的繁中詳述），
        # 只有在 remark 缺失時才退回旗標欄位摘要，最後才是「有通報」。
        # （先前只用旗標欄位，遇到旗標全為 0 的單艦航行通報就只剩「有通報」。）
        remark = _s(latest.get("remark")).strip()
        if remark and remark not in ("False", "nan"):
            summary = remark
        elif flag_summary:
            summary = flag_summary
        else:
            summary = "有通報"

        line = f"🇯🇵 日本防衛省（{latest['date']}）：{summary}"

        # 若 remark 未涵蓋旗標資訊，補上活動／海峽標籤，方便快速掃描
        if summary == remark and flag_summary:
            line += f"｜{flag_summary}"

        ship = str(latest.get("艦型", "")).strip()
        if ship and ship not in ("", "未提及", "nan") and ship not in summary:
            line += f"｜艦型：{ship[:40]}"
        return line
    except Exception as e:
        print(f"⚠️  讀取日本防衛省資料失敗: {e}")
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


def compose_report_text(yesterday):
    """組整份簡報文字。"""
    today_str = datetime.now(TW_TZ).strftime("%Y/%m/%d")
    sections = [
        f"📡【每日台海動態簡報】{today_str}",
        "",
        summarize_forecast(load_forecast()),
        "",
        summarize_japan_mod(),
        "",
        summarize_nav_warnings(yesterday),
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
def generate_chart(days_history=30, days_forecast=FORECAST_DAYS):
    """畫 30 日實際架次 + 未來 3 日預測，存 PNG，回傳路徑（失敗回 None）。"""
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

        plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
        fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
        fig.patch.set_facecolor("#ffffff")
        ax.set_facecolor("#fafafa")

        # 歷史實際
        ax.plot(hist["date"], hist["pla_aircraft_sorties"],
                color="#2563EB", linewidth=1.8, marker="o", markersize=3,
                label="Actual Sorties", zorder=3)

        # 串接最後實際點 → 第一個預測點
        ax.plot([hist["date"].iloc[-1], pred["date"].iloc[0]],
                [hist["pla_aircraft_sorties"].iloc[-1], pred["predicted_sorties"].iloc[0]],
                color="#DC2626", linewidth=1.5, linestyle="--", zorder=2)

        # 預測線 + 信賴區間
        ax.plot(pred["date"], pred["predicted_sorties"],
                color="#DC2626", linewidth=2.0, marker="s", markersize=5,
                linestyle="--", label="Predicted (next 3d)", zorder=3)
        ax.fill_between(pred["date"], pred["lower_bound"].clip(lower=0),
                        pred["upper_bound"], color="#DC2626", alpha=0.12,
                        label="90% CI")

        # 預測數值標註
        for _, r in pred.iterrows():
            ax.annotate(f'{r["predicted_sorties"]:.1f}',
                        (r["date"], r["predicted_sorties"]),
                        textcoords="offset points", xytext=(0, 10),
                        fontsize=9, fontweight="bold", color="#DC2626", ha="center")

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
        plt.xticks(rotation=45, fontsize=8)
        plt.yticks(fontsize=8)
        ax.set_xlabel("Date", fontsize=9)
        ax.set_ylabel("Sorties", fontsize=9)
        ax.set_title("PLA Aircraft Sorties — 30-Day History + 3-Day Forecast",
                     fontsize=11, fontweight="bold")
        ax.legend(fontsize=7.5, loc="upper left", framealpha=0.9)
        ax.grid(axis="y", alpha=0.3, linewidth=0.5)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_ylim(bottom=0)
        plt.tight_layout()

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


def select_range_warnings(yesterday, max_zones=8):
    """挑昨日（無則近期）且含座標的火砲射擊／演習航警，回傳解析後清單。"""
    data = _safe_read_json(NAV_WARN_JSON)
    if not data:
        return []
    cutoff = (yesterday - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    y_str = yesterday.strftime("%Y-%m-%d")

    def has_coords(w):
        return len(_parse_coords(w.get("coordinates"))) >= 1

    recent = [w for w in data
              if cutoff <= _s(w.get("publish_date")) <= y_str and has_coords(w)]
    if not recent:
        recent = sorted([w for w in data if has_coords(w)],
                        key=lambda w: _s(w.get("publish_date")), reverse=True)[:5]

    # 英文重複公告（同一則的英文版）多半座標相同，去重：以座標字串為 key
    seen, zones = set(), []
    for w in recent:
        pts = _parse_coords(w.get("coordinates"))
        key = w.get("coordinates")
        if key in seen:
            continue
        seen.add(key)
        zones.append({
            "title": _s(w.get("title")).strip(),
            "channel": _s(w.get("channel")).strip(),
            "period": _s(w.get("time_periods")).strip(),
            "date": _s(w.get("publish_date")).strip(),
            "points": pts,
            "fire": _is_fire_warning(w.get("title")),
        })
        if len(zones) >= max_zones:
            break
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


def generate_range_map(yesterday):
    """畫出近日火砲射擊／演習公告範圍地圖，存 PNG，回傳路徑（無資料或失敗回 None）。"""
    try:
        zones = select_range_warnings(yesterday)
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

        for i, z in enumerate(zones, 1):
            color = "#DC2626" if z["fire"] else "#EA9010"
            pts = z["points"]
            plons = [p[1] for p in pts]
            plats = [p[0] for p in pts]
            if len(pts) >= 3:
                poly = mpatches.Polygon(list(zip(plons, plats)), closed=True,
                                        facecolor=color, edgecolor=color,
                                        alpha=0.30, linewidth=1.8, zorder=3)
                ax.add_patch(poly)
                ax.plot(plons + [plons[0]], plats + [plats[0]],
                        color=color, linewidth=1.8, zorder=4)
                cx, cy = sum(plons) / len(plons), sum(plats) / len(plats)
            elif len(pts) == 2:
                ax.plot(plons, plats, color=color, linewidth=2.2, zorder=4)
                cx, cy = sum(plons) / 2, sum(plats) / 2
            else:  # 單點（如火箭發射）
                ax.plot(plons, plats, marker="*", markersize=16,
                        color=color, zorder=4)
                cx, cy = plons[0], plats[0]
            ax.annotate(str(i), (cx, cy), fontsize=10, fontweight="bold",
                        color="white", ha="center", va="center", zorder=5,
                        bbox=dict(boxstyle="circle,pad=0.25", fc=color, ec="white", lw=1))

        ax.set_xlabel("Longitude", fontsize=9)
        ax.set_ylabel("Latitude", fontsize=9)
        title = "解放軍火砲射擊／演習公告範圍（近日）" if cjk_font \
            else "PLA Firing / Exercise Warning Areas (recent)"
        ax.set_title(title, fontsize=13, fontweight="bold")

        # 圖例：編號 → 公告標題（截短），放在地圖下方避免遮擋範圍
        legend_handles = []
        for i, z in enumerate(zones, 1):
            color = "#DC2626" if z["fire"] else "#EA9010"
            label = f"{i}. {z['title'][:18]}"
            if z["period"]:
                label += f"（{z['period'][:16]}）"
            legend_handles.append(mpatches.Patch(color=color, label=label))
        legend_title = "紅＝火砲/實彈射擊　橙＝演習/訓練/禁航" if cjk_font \
            else "red = live fire / orange = exercise"
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
    args = parser.parse_args()

    yesterday = (datetime.now(TW_TZ) - timedelta(days=1)).date()
    yesterday = datetime(yesterday.year, yesterday.month, yesterday.day)

    # 1. 組文字
    report_text = compose_report_text(yesterday)
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
