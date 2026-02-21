#!/usr/bin/env python3
"""
Threads 社群媒體自動發布腳本
從 latest_prediction.csv 讀取預測數據，產生折線圖，並發布到 Threads。
"""
import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for CI
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import requests
# ── 常數設定 ───────────────────────────────────────────────
REPO_OWNER = "s0914712"
REPO_NAME = "pla-data-dashboard"
CSV_PATH = "data/predictions/latest_prediction.csv"
CHART_DIR = "data/charts"
CHART_FILENAME = "threads_chart.png"
CHART_REPO_PATH = f"{CHART_DIR}/{CHART_FILENAME}"
WEEKDAY_MAP = {
    "Monday": "一", "Tuesday": "二", "Wednesday": "三",
    "Thursday": "四", "Friday": "五", "Saturday": "六", "Sunday": "日",
}
RISK_EMOJI = {
    "LOW": "🟢",
    "MEDIUM": "🟡",
    "HIGH": "🔴",
    "CRITICAL": "🔴",
}
def parse_csv(csv_path: str) -> pd.DataFrame:
    """讀取並解析 latest_prediction.csv"""
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])
    return df
def split_actual_vs_predicted(df: pd.DataFrame):
    """區分已有實際架次 vs 純預測的列"""
    actual = df[df["actual_sorties"].notna()].copy()
    predicted = df[df["actual_sorties"].isna()].copy()
    return actual, predicted
def generate_chart(df: pd.DataFrame, actual: pd.DataFrame, predicted: pd.DataFrame, output_path: str):
    """產生折線圖：實際架次 + 預測架次 + 信心區間"""
    plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei', 'SimHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    fig, ax = plt.subplots(figsize=(10, 5))
    # 實際架次（藍色實線）
    if not actual.empty:
        ax.plot(
            actual["date"], actual["actual_sorties"],
            color="#2563EB", linewidth=2.5, marker="o", markersize=7,
            label="國防部公布架次", zorder=5,
        )
    # 預測架次（紅色虛線）
    if not predicted.empty:
        # 連接最後一個實際點到第一個預測點
        if not actual.empty:
            bridge = pd.DataFrame({
                "date": [actual["date"].iloc[-1], predicted["date"].iloc[0]],
                "val": [actual["actual_sorties"].iloc[-1], predicted["predicted_sorties"].iloc[0]],
            })
            ax.plot(bridge["date"], bridge["val"], color="#DC2626", linewidth=2, linestyle="--", alpha=0.5)
        ax.plot(
            predicted["date"], predicted["predicted_sorties"],
            color="#DC2626", linewidth=2.5, marker="s", markersize=7,
            linestyle="--", label="AI 預測架次", zorder=5,
        )
        # 信心區間
        ax.fill_between(
            predicted["date"],
            predicted["lower_bound"],
            predicted["upper_bound"],
            color="#DC2626", alpha=0.1, label="預測信心區間",
        )
    # 全部日期的預測線（淺灰色背景）
    ax.plot(
        df["date"], df["predicted_sorties"],
        color="#9CA3AF", linewidth=1, linestyle=":", alpha=0.6,
    )
    # 格式化
    ax.set_title("共軍擾臺架次 — 實際 vs 預測", fontsize=16, fontweight="bold", pad=15)
    ax.set_xlabel("日期", fontsize=12)
    ax.set_ylabel("架次數", fontsize=12)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    plt.xticks(rotation=45, ha="right")
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)
    # 在預測點上標數字
    if not predicted.empty:
        for _, row in predicted.head(3).iterrows():
            ax.annotate(
                f'{row["predicted_sorties"]:.1f}',
                (row["date"], row["predicted_sorties"]),
                textcoords="offset points", xytext=(0, 12),
                fontsize=9, fontweight="bold", color="#DC2626",
                ha="center",
            )
    plt.tight_layout()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"✅ 圖表已儲存：{output_path}")
    return output_path
def upload_chart_to_github(local_path: str, github_token: str) -> str:
    """透過 GitHub API 上傳圖片到 repo，回傳公開 raw URL"""
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{CHART_REPO_PATH}"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    # 讀取檔案內容
    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    # 檢查是否已存在（需要 sha 來更新）
    sha = None
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        sha = resp.json().get("sha")
    # 上傳/更新
    payload = {
        "message": f"📊 Update Threads chart — {datetime.now().strftime('%Y-%m-%d')}",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        print(f"❌ GitHub 上傳失敗：{resp.status_code} {resp.text}")
        sys.exit(1)
    # 加上 timestamp 避免 CDN cache
    raw_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{CHART_REPO_PATH}?t={int(datetime.now().timestamp())}"
    print(f"✅ 圖片已上傳：{raw_url}")
    return raw_url
def compose_post_text(actual: pd.DataFrame, predicted: pd.DataFrame, df: pd.DataFrame) -> str:
    """組合 Threads 發文內容"""
    tw_tz = timezone(timedelta(hours=8))
    today = datetime.now(tw_tz).strftime("%Y/%m/%d")
    lines = [f" OSINT安全動態日報 — {today}", ""]
    # 國防部公布（只保留最新一天）
    if not actual.empty:
        latest_actual = actual.iloc[-1]
        weekday = WEEKDAY_MAP.get(latest_actual["day_of_week"], "")
        date_str = latest_actual["date"].strftime("%m/%d")
        sorties = int(latest_actual["actual_sorties"])
        lines.append(f"🔹 國防部公布（{date_str} {weekday}）：{sorties} 架次")
        lines.append("")
    return "\n".join(lines)
def publish_to_threads(text: str, image_url: str | None, user_id: str, access_token: str, app_secret: str):
    """透過 Threads Graph API 直接發布貼文（不依賴 SDK）"""
    import time
    base_url = "https://graph.threads.net/v1.0"
    # ── 步驟一：建立 media container ──
    create_params = {
        "media_type": "IMAGE" if image_url else "TEXT",
        "text": text,
        "access_token": access_token,
    }
    if image_url:
        create_params["image_url"] = image_url
    resp = requests.post(f"{base_url}/{user_id}/threads", data=create_params)
    if resp.status_code != 200:
        print(f"❌ 建立 container 失敗：{resp.status_code} {resp.text}")
        sys.exit(1)
    container_id = resp.json().get("id")
    print(f"✅ Media container 已建立：{container_id}")
    # 等待讓伺服器處理（圖片需要較長時間）
    wait_sec = 30 if image_url else 5
    print(f"⏳ 等待 {wait_sec} 秒讓 Threads 處理...")
    time.sleep(wait_sec)
    # ── 步驟二：發布 container ──
    publish_params = {
        "creation_id": container_id,
        "access_token": access_token,
    }
    resp = requests.post(f"{base_url}/{user_id}/threads_publish", data=publish_params)
    if resp.status_code != 200:
        print(f"❌ 發布失敗：{resp.status_code} {resp.text}")
        sys.exit(1)
    result = resp.json()
    print(f"✅ 發布成功！Post ID: {result.get('id')}")
    return result
def main():
    parser = argparse.ArgumentParser(description="Publish PLA prediction data to Threads")
    parser.add_argument("--dry-run", action="store_true", help="只輸出內容不實際發布")
    parser.add_argument("--csv", default=CSV_PATH, help="CSV 路徑")
    parser.add_argument("--chart-dir", default=CHART_DIR, help="圖表輸出目錄")
    args = parser.parse_args()
    # ── 1. 解析 CSV ──
    print("📂 讀取 CSV...")
    df = parse_csv(args.csv)
    actual, predicted = split_actual_vs_predicted(df)
    print(f"  已公布：{len(actual)} 天 ｜ 預測：{len(predicted)} 天")
    # ── 2. 產生圖表 ──
    chart_path = os.path.join(args.chart_dir, CHART_FILENAME)
    print("🎨 產生折線圖...")
    generate_chart(df, actual, predicted, chart_path)
    # ── 3. 組合發文內容 ──
    post_text = compose_post_text(actual, predicted, df)
    print("\n📝 發文內容：")
    print("─" * 40)
    print(post_text)
    print("─" * 40)
    if args.dry_run:
        print("\n🏁 Dry-run 模式 — 未實際發布")
        return
    # ── 4. 上傳圖片到 GitHub ──
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        print("⚠️ 未設定 GITHUB_TOKEN，跳過圖片上傳")
        image_url = None
    else:
        print("📤 上傳圖片到 GitHub...")
        image_url = upload_chart_to_github(chart_path, github_token)
    # ── 5. 發布到 Threads ──
    user_id = os.environ.get("THREADS_USER_ID")
    access_token = os.environ.get("THREADS_ACCESS_TOKEN")
    app_secret = os.environ.get("THREADS_APP_SECRET")
    if not all([user_id, access_token, app_secret]):
        print("❌ 缺少 Threads API 環境變數 (THREADS_USER_ID, THREADS_ACCESS_TOKEN, THREADS_APP_SECRET)")
        sys.exit(1)
    print("📤 發布到 Threads...")
    publish_to_threads(post_text, image_url, user_id, access_token, app_secret)
    print("\n🎉 完成！")
if __name__ == "__main__":
    main()
