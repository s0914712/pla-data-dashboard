#!/usr/bin/env python3
"""Send the latest Classifier 4.0 shadow comparison through the existing LINE bot."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "data" / "predictions" / "classifier4_comparison.json"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _pct(v):
    return "—" if v is None else f"{float(v) * 100:.1f}%"


def _metric_line(label, m):
    if not m or not m.get("enough"):
        n = (m or {}).get("n", 0)
        need = (m or {}).get("min_n", 10)
        return f"  {label}：已累積 {n} 日，需 {need} 日才評比"
    bs = m.get("brier_skill")
    bs_text = "—" if bs is None else f"{bs * 100:+.0f}%"
    return (f"  {label}：AUC {m['roc_auc']:.3f}｜PR-AUC {m['pr_auc']:.3f}｜"
            f"Brier skill {bs_text}｜Recall@20% {m['recall_at_20pct'] * 100:.0f}%")


def build_text(data):
    latest = data.get("latest", {})
    prod = latest.get("production", {})
    temp = latest.get("temporal", {})
    naval = latest.get("temporal_naval", {})
    target = data.get("target_date", "?")
    try:
        target = datetime.strptime(target, "%Y-%m-%d").strftime("%m/%d")
    except ValueError:
        pass

    point = prod.get("point")
    lo, hi = prod.get("lower"), prod.get("upper")
    point_text = "—" if point is None else f"{point:.1f} 架"
    if lo is not None and hi is not None:
        point_text += f"（90% {lo:.0f}–{hi:.0f}）"

    probs = [p for p in (prod.get("prob"), temp.get("prob"), naval.get("prob")) if p is not None]
    spread = (max(probs) - min(probs)) * 100 if len(probs) >= 2 else None

    lines = [
        f"🧪【Classifier 4.0 Shadow】明日 {target}",
        f"正式模型：{point_text}｜高架次機率 {_pct(prod.get('prob'))}｜{prod.get('risk', 'UNKNOWN')}",
        f"Challenger B（+Temporal）：{_pct(temp.get('prob'))}",
        f"Challenger C（+Temporal+Naval）：{_pct(naval.get('prob'))}",
    ]
    if spread is not None:
        if spread >= 15:
            lines.append(f"⚠️ 新舊機率分歧 {spread:.1f} 個百分點，值得關注特徵訊號差異")
        else:
            lines.append(f"↔️ 三模型機率差距 {spread:.1f} 個百分點")

    lines += ["", "📐 累積 live 對照："]
    metrics = data.get("metrics", {})
    lines.append(_metric_line("正式", metrics.get("production")))
    lines.append(_metric_line("B", metrics.get("temporal")))
    lines.append(_metric_line("C", metrics.get("temporal_naval")))
    lines += [
        "",
        f"資料截至：{data.get('data_latest_date', '?')}｜門檻 ≥{data.get('threshold', 20)} 架",
        "※ B/C 僅 shadow，不會改正式 risk_level 或 LINE 警報。",
    ]
    return "\n".join(lines)


def send(text, token, user_id):
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"LINE push failed: {r.status_code} {r.text}")


def main():
    if not SUMMARY.exists():
        print("No classifier4 comparison file; skip LINE shadow message.")
        return
    data = json.loads(SUMMARY.read_text(encoding="utf-8"))
    text = build_text(data)
    print(text)
    if "--dry-run" in sys.argv:
        return
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        raise RuntimeError("missing LINE_CHANNEL_ACCESS_TOKEN or LINE_USER_ID")
    send(text, token, user_id)
    print("Classifier 4 shadow LINE message sent.")


if __name__ == "__main__":
    main()
