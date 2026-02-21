#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate chart image for Threads daily post.

Reads historical sorties data + latest prediction,
produces data/charts/threads_chart.png with English legends
to avoid font-rendering issues on CI.
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── paths ──────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SORTIES_CSV = os.path.join(ROOT, "data", "JapanandBattleship.csv")
PRED_CSV = os.path.join(ROOT, "data", "predictions", "latest_prediction.csv")
OUT_DIR = os.path.join(ROOT, "data", "charts")
OUT_PATH = os.path.join(OUT_DIR, "threads_chart.png")


def load_history(days=30):
    """Load the last N days of actual sortie data."""
    df = pd.read_csv(SORTIES_CSV)
    df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=False)
    df = df.sort_values("date")
    df = df[["date", "pla_aircraft_sorties"]].dropna(subset=["pla_aircraft_sorties"])
    df = df.tail(days).copy()
    return df


def load_predictions():
    """Load the latest 7-day predictions."""
    df = pd.read_csv(PRED_CSV)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    # Keep only future rows (no actual_sorties yet)
    df = df[["date", "predicted_sorties", "lower_bound", "upper_bound", "risk_level"]]
    return df


def generate_chart():
    hist = load_history(days=30)
    pred = load_predictions()

    fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#fafafa")

    # ── historical line ──
    ax.plot(
        hist["date"], hist["pla_aircraft_sorties"],
        color="#667eea", linewidth=1.8, marker="o", markersize=3,
        label="Actual Sorties", zorder=3,
    )

    # ── prediction line ──
    # Connect the last actual point to the first prediction point
    bridge_date = [hist["date"].iloc[-1], pred["date"].iloc[0]]
    bridge_val = [hist["pla_aircraft_sorties"].iloc[-1], pred["predicted_sorties"].iloc[0]]
    ax.plot(bridge_date, bridge_val, color="#f5576c", linewidth=1.5,
            linestyle="--", zorder=2)

    ax.plot(
        pred["date"], pred["predicted_sorties"],
        color="#f5576c", linewidth=1.8, marker="s", markersize=4,
        label="Predicted Sorties", zorder=3,
    )

    # ── confidence interval ──
    ax.fill_between(
        pred["date"],
        pred["lower_bound"].clip(lower=0),
        pred["upper_bound"],
        color="#f5576c", alpha=0.12,
        label="95% CI",
    )

    # ── risk-level dots ──
    risk_colors = {"LOW": "#38ef7d", "MEDIUM": "#ffd200", "HIGH": "#eb3349"}
    for _, row in pred.iterrows():
        c = risk_colors.get(row["risk_level"], "#a0aec0")
        ax.scatter(row["date"], row["predicted_sorties"], color=c,
                   s=60, zorder=4, edgecolors="white", linewidths=0.8)

    # ── formatting ──
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
    plt.xticks(rotation=45, fontsize=8)
    plt.yticks(fontsize=8)

    ax.set_xlabel("Date", fontsize=9)
    ax.set_ylabel("Sorties", fontsize=9)
    ax.set_title("PLA Aircraft Sorties — 30-Day History + 7-Day Forecast", fontsize=11, fontweight="bold")

    ax.legend(fontsize=7.5, loc="upper left", framealpha=0.9)
    ax.grid(axis="y", alpha=0.3, linewidth=0.5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()

    os.makedirs(OUT_DIR, exist_ok=True)
    fig.savefig(OUT_PATH, bbox_inches="tight")
    plt.close(fig)
    print(f"Chart saved to {OUT_PATH}")
    return OUT_PATH


if __name__ == "__main__":
    generate_chart()
