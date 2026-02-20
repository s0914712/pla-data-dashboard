"""
Threads 社群媒體自動發布腳本（官方 Graph API 版）
"""
import argparse
import base64
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import requests


# ── 常數設定 ─────────────────────────
REPO_OWNER = "s0914712"
REPO_NAME = "pla-data-dashboard"
CSV_PATH = "data/predictions/latest_prediction.csv"
CHART_DIR = "data/charts"
CHART_FILENAME = "threads_chart.png"
CHART_REPO_PATH = f"{CHART_DIR}/{CHART_FILENAME}"


# ── CSV ─────────────────────────────
def parse_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])
    return df


def split_actual_vs_predicted(df):
    actual = df[df["actual_sorties"].notna()].copy()
    predicted = df[df["actual_sorties"].isna()].copy()
    return actual, predicted


# ── 圖表 ────────────────────────────
def generate_chart(df, actual, predicted, output_path):
    fig, ax = plt.subplots(figsize=(10, 5))

    if not actual.empty:
        ax.plot(actual["date"], actual["actual_sorties"], label="Actual")

    if not predicted.empty:
        ax.plot(predicted["date"], predicted["predicted_sorties"],
                linestyle="--", label="Prediction")

    ax.legend()
    ax.set_title("PLA Sorties Prediction")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

    print(f"✅ 圖表已儲存：{output_path}")
    return output_path


# ── 上傳到 GitHub ───────────────────
def upload_chart_to_github(local_path, github_token):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{CHART_REPO_PATH}"

    with open(local_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    payload = {
        "message": "Update Threads chart",
        "content": content,
        "branch": "main"
    }

    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {github_token}"},
        json=payload
    )

    if r.status_code not in (200, 201):
        print("❌ GitHub 上傳失敗", r.text)
        sys.exit(1)

    raw_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{CHART_REPO_PATH}?t={int(datetime.now().timestamp())}"
    print("✅ 圖片已上傳:", raw_url)
    return raw_url


# ── 發布到 Threads（官方 API） ──────
def publish_to_threads(text, image_url, user_id, access_token):
    base = f"https://graph.facebook.com/v19.0/{user_id}"

    print("🧵 建立 Threads container...")

    payload = {
        "text": text,
        "access_token": access_token
    }

    if image_url:
        payload["media_type"] = "IMAGE"
        payload["image_url"] = image_url

    r = requests.post(f"{base}/threads", data=payload)
    data = r.json()

    print("Container response:", data)

    if "id" not in data:
        print("❌ 建立 container 失敗")
        sys.exit(1)

    container_id = data["id"]

    print("🚀 發布 Threads 貼文...")

    r = requests.post(
        f"{base}/threads_publish",
        data={
            "creation_id": container_id,
            "access_token": access_token
        }
    )

    result = r.json()
    print("Publish response:", result)

    if "id" not in result:
        print("❌ 發布失敗")
        sys.exit(1)

    print("🎉 發布成功！Post ID:", result["id"])


# ── 主程式 ─────────────────────────
def main():
    df = parse_csv(CSV_PATH)
    actual, predicted = split_actual_vs_predicted(df)

    chart_path = generate_chart(
        df, actual, predicted,
        os.path.join(CHART_DIR, CHART_FILENAME)
    )

    post_text = f"📊 PLA Sorties Prediction — {datetime.now().strftime('%Y/%m/%d')}"

    github_token = os.environ["GITHUB_TOKEN"]
    image_url = upload_chart_to_github(chart_path, github_token)

    user_id = os.environ["THREADS_USER_ID"]
    access_token = os.environ["THREADS_ACCESS_TOKEN"]

    publish_to_threads(post_text, image_url, user_id, access_token)


if __name__ == "__main__":
    main()
