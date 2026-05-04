#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A/B experiment: do strait-passage features (與那國/宮古/大禹/對馬/進/出/艦型)
improve the PLA 7-day predictor on 2022-01-01 ~ 2025-12-31?

Method:
- Same walk-forward multi-step CV as `PLAPredictor.train()` (5 folds, 7-day
  horizon, 7-day embargo).
- Baseline: stock feature set.
- Treatment: stock feature set + 7 rolling-7d strait features.
- Compares MAE and RMSE (overall + per Day-1..Day-7).

Run:
    python experiments/strait_features_ab.py
"""

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pla_7day_predictor import PLAPredictor  # noqa: E402

DATA_DIR = REPO_ROOT / 'data'
SORTIES_CSV = DATA_DIR / 'JapanandBattleship.csv'
POLITICAL_CSV = DATA_DIR / 'merged_comprehensive_data_M.csv'

START_DATE = '2022-01-01'
END_DATE = '2025-12-31'


def load_inputs():
    df_sorties = pd.read_csv(SORTIES_CSV, encoding='utf-8-sig')
    df_sorties['date'] = pd.to_datetime(df_sorties['date'], errors='coerce')
    df_sorties = df_sorties[df_sorties['date'].notna()]
    df_sorties = df_sorties[df_sorties['pla_aircraft_sorties'].notna()]
    mask = (df_sorties['date'] >= START_DATE) & (df_sorties['date'] <= END_DATE)
    df_sorties = df_sorties[mask].sort_values('date').reset_index(drop=True)

    df_political = pd.read_csv(POLITICAL_CSV, encoding='utf-8-sig')
    df_political['date'] = pd.to_datetime(df_political['date'], errors='coerce')
    df_political = df_political[df_political['date'].notna()].copy()

    return df_sorties, df_political


def run_variant(name, use_strait, df_sorties, df_political):
    print('\n' + '#' * 70)
    print(f'# Variant: {name}  (use_strait_features={use_strait})')
    print('#' * 70)
    p = PLAPredictor(use_strait_features=use_strait)
    # bypass network fetch in load_data() — feed inputs directly
    p.political_events = df_political
    p.latest_date = df_sorties['date'].max()
    p.weather_data = None
    p.news_data = p._load_news_data()
    df = p.prepare_features(df_sorties, df_political)
    p.train(df)
    return p


def summarize(errors_by_day):
    """Per-day and overall MAE/RMSE from absolute-error arrays."""
    out = {}
    flat = []
    for day, errs in errors_by_day.items():
        if not errs:
            continue
        arr = np.asarray(errs, dtype=float)
        out[day] = {
            'n': int(arr.size),
            'mae': float(arr.mean()),
            'rmse': float(np.sqrt(np.mean(arr ** 2))),
        }
        flat.extend(errs)
    flat = np.asarray(flat, dtype=float)
    out['overall'] = {
        'n': int(flat.size),
        'mae': float(flat.mean()) if flat.size else 0.0,
        'rmse': float(np.sqrt(np.mean(flat ** 2))) if flat.size else 0.0,
    }
    return out


def print_table(base, treat):
    keys = [1, 2, 3, 4, 5, 6, 7, 'overall']
    print('\n' + '=' * 78)
    print(f"Walk-forward CV on {START_DATE} ~ {END_DATE}")
    print('=' * 78)
    header = f"{'Bucket':<10} {'n':>5} {'Base RMSE':>12} {'+Strait RMSE':>14} {'ΔRMSE':>10} {'Δ%':>8}"
    print(header)
    print('-' * 78)
    for k in keys:
        if k not in base or k not in treat:
            continue
        b = base[k]['rmse']
        t = treat[k]['rmse']
        d = t - b
        pct = (100 * d / b) if b > 0 else 0.0
        label = f'Day-{k}' if isinstance(k, int) else 'Overall'
        print(f"{label:<10} {base[k]['n']:>5d} {b:>12.3f} {t:>14.3f} {d:>+10.3f} {pct:>+7.2f}%")
    print('-' * 78)
    print(f"{'Bucket':<10} {'n':>5} {'Base MAE':>12} {'+Strait MAE':>14} {'ΔMAE':>10} {'Δ%':>8}")
    print('-' * 78)
    for k in keys:
        if k not in base or k not in treat:
            continue
        b = base[k]['mae']
        t = treat[k]['mae']
        d = t - b
        pct = (100 * d / b) if b > 0 else 0.0
        label = f'Day-{k}' if isinstance(k, int) else 'Overall'
        print(f"{label:<10} {base[k]['n']:>5d} {b:>12.3f} {t:>14.3f} {d:>+10.3f} {pct:>+7.2f}%")
    print('=' * 78)


def main():
    df_sorties, df_political = load_inputs()
    print(f"Loaded {len(df_sorties)} sortie rows in [{START_DATE}, {END_DATE}]")
    print(f"Loaded {len(df_political)} political rows (full range)")

    p_base = run_variant('Baseline', False, df_sorties, df_political)
    p_treat = run_variant('+Strait Features', True, df_sorties, df_political)

    base = summarize(p_base.cv_errors_by_day)
    treat = summarize(p_treat.cv_errors_by_day)

    print_table(base, treat)

    delta_rmse = treat['overall']['rmse'] - base['overall']['rmse']
    verdict = 'BETTER (lower RMSE)' if delta_rmse < 0 else (
        'WORSE (higher RMSE)' if delta_rmse > 0 else 'EQUAL')
    print(f"\nOverall verdict for adding strait features: {verdict}")
    print(f"  ΔRMSE = {delta_rmse:+.3f}  ({100 * delta_rmse / base['overall']['rmse']:+.2f}%)")


if __name__ == '__main__':
    main()
