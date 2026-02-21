#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Post daily OSINT report to Threads.

Simplified content to avoid platform content-policy blocks.
Image is uploaded to GitHub first, then referenced by URL.

Required env vars:
    THREADS_USER_ID   – Threads/IG user ID
    THREADS_TOKEN     – Long-lived Threads API access token
    GITHUB_TOKEN      – (optional) for committing chart image
"""

import os
import sys
import time
import json
import subprocess
import requests
import pandas as pd
from datetime import datetime

# ── paths ──────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRED_CSV = os.path.join(ROOT, "data", "predictions", "latest_prediction.csv")
SORTIES_CSV = os.path.join(ROOT, "data", "JapanandBattleship.csv")
CHART_PATH = os.path.join(ROOT, "data", "charts", "threads_chart.png")

THREADS_API = "https://graph.threads.net/v1.0"
REPO_RAW_BASE = "https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main"


# ── helpers ────────────────────────────────────────────

def get_latest_actual():
    """Return (date_str, weekday_str, sorties) of the most recent actual record."""
    df = pd.read_csv(SORTIES_CSV)
    df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=False)
    df = df.dropna(subset=["pla_aircraft_sorties"])
    df = df.sort_values("date")
    row = df.iloc[-1]
    d = row["date"]
    weekday_zh = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d.weekday()]
    date_str = d.strftime("%m/%d")
    return date_str, weekday_zh, int(row["pla_aircraft_sorties"])


def load_predictions():
    df = pd.read_csv(PRED_CSV)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date")


def build_post_text():
    """Build simplified post text (avoid sensitive keywords)."""
    date_str, weekday, sorties = get_latest_actual()
    pred = load_predictions()

    # Model version & MAE
    model_ver = pred["model_version"].iloc[-1] if "model_version" in pred.columns else "?"
    mae = pred["cv_mae"].iloc[-1] if "cv_mae" in pred.columns else "?"

    lines = [
        f"OSINT Daily Report",
        f"",
        f"Published ({date_str} {weekday}): {sorties} sorties",
    ]

    return "\n".join(lines)


def upload_chart_to_github():
    """Commit + push the chart image so the raw URL is available."""
    cwd = ROOT
    cmds = [
        ["git", "config", "--local", "user.email", "action@github.com"],
        ["git", "config", "--local", "user.name", "GitHub Action"],
        ["git", "add", "data/charts/threads_chart.png"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, cwd=cwd, check=True)

    # Check if there are staged changes
    result = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        cwd=cwd,
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", f"Update threads chart {datetime.now().strftime('%Y-%m-%d')}"],
            cwd=cwd, check=True,
        )
        for attempt in range(1, 5):
            push = subprocess.run(["git", "push"], cwd=cwd)
            if push.returncode == 0:
                break
            print(f"Push failed (attempt {attempt}/4), retrying...")
            subprocess.run(["git", "pull", "--rebase", "--autostash", "-X", "theirs", "origin", "main"], cwd=cwd)
            time.sleep(2 ** attempt)

    cache_bust = int(time.time())
    return f"{REPO_RAW_BASE}/data/charts/threads_chart.png?t={cache_bust}"


def post_to_threads(text, image_url):
    """Publish a single image post to Threads."""
    user_id = os.environ["THREADS_USER_ID"]
    token = os.environ["THREADS_TOKEN"]

    # Step 1: create media container
    print("Creating Threads media container...")
    resp = requests.post(
        f"{THREADS_API}/{user_id}/threads",
        params={
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": text,
            "access_token": token,
        },
    )
    data = resp.json()
    if "id" not in data:
        print(f"Container creation failed: {resp.status_code} {json.dumps(data)}")
        sys.exit(1)
    container_id = data["id"]
    print(f"Container created: {container_id}")

    # Step 2: wait for processing
    time.sleep(10)

    # Step 3: publish
    print("Publishing to Threads...")
    resp = requests.post(
        f"{THREADS_API}/{user_id}/threads_publish",
        params={
            "creation_id": container_id,
            "access_token": token,
        },
    )
    pub = resp.json()
    if "id" not in pub:
        print(f"Publish failed: {resp.status_code} {json.dumps(pub)}")
        sys.exit(1)
    print(f"Published! Post ID: {pub['id']}")
    return pub["id"]


def main():
    text = build_post_text()
    print("=== Post Text ===")
    print(text)
    print("=================")

    # Upload chart to GitHub
    print("Uploading chart to GitHub...")
    image_url = upload_chart_to_github()
    print(f"Image URL: {image_url}")

    # Post to Threads
    print("Posting to Threads...")
    post_to_threads(text, image_url)
    print("Done!")


if __name__ == "__main__":
    main()
