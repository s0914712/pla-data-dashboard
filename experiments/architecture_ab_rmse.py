#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Architecture A vs B RMSE on PLA 7-day sortie prediction.

Reproduces the appendix design:
- Architecture A: AR/MR features + JSO strait-passage features
  (與那國/宮古/大隅[大禹]/對馬, only counting 旅洋級/仁海級 vessels).
- Architecture B: A + foreign vessels through Taiwan Strait
  + US-TW visits + CN exercise news + CN political statements.

Validation: 4-fold walk-forward CV per Table 1 of the appendix
(2022-01-15 ~ 2025-12-31, 7-day embargo, 7-day rolling iterative
forecasts inside each ~3-month test period).

Estimator: XGBoost regressor (log1p target).

Run:
    python experiments/architecture_ab_rmse.py
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DATA = REPO / 'data'
SORTIES_CSV = DATA / 'JapanandBattleship.csv'
POLITICAL_CSV = DATA / 'merged_comprehensive_data_M.csv'
NEWS_JSON = DATA / 'news_classified.json'

TARGET = 'pla_aircraft_sorties'
TRAIN_START = pd.Timestamp('2022-01-15')
HORIZON = 7

FOLDS = [
    {'name': 'Fold-1', 'train_end': '2024-12-31',
     'test_start': '2025-01-08', 'test_end': '2025-03-31'},
    {'name': 'Fold-2', 'train_end': '2025-03-31',
     'test_start': '2025-04-08', 'test_end': '2025-06-30'},
    {'name': 'Fold-3', 'train_end': '2025-06-30',
     'test_start': '2025-07-08', 'test_end': '2025-09-30'},
    {'name': 'Fold-4', 'train_end': '2025-09-30',
     'test_start': '2025-10-08', 'test_end': '2025-12-31'},
]

STRAIT_MAP = {
    '與那國': 'yonaguni',
    '宮古': 'miyako',
    '大禹': 'osumi',     # 大禹 in CSV ↔ 大隅海峽 in appendix text
    '對馬': 'tsushima',
}

AR_COLS = [
    'lag_1', 'lag_2', 'lag_3', 'lag_7', 'lag_14',
    'ma_3', 'ma_5', 'ma_7', 'ma_9', 'ma_14',
    'std_3', 'std_5', 'std_7', 'std_9', 'std_14',
    'diff_3_7',
]
TIME_COLS = ['weekday', 'month', 'is_weekend']
STRAIT_COLS = []
for s in STRAIT_MAP.values():
    STRAIT_COLS += [f'{s}_target_lag1', f'{s}_target_lag2',
                    f'{s}_target_lag3', f'{s}_any_lag1']

ARCH_B_EXTRA = [
    'foreign_vessel_lag1', 'foreign_vessel_7d',
    'us_tw_visit_lag1', 'us_tw_visit_7d',
    'cn_stmt_lag1', 'cn_stmt_7d',
    'news_mil_lag1', 'news_mil_7d',
    'news_us_tw_lag1', 'news_us_tw_7d',
]

ARCH_A_FEATURES = AR_COLS + TIME_COLS + STRAIT_COLS
ARCH_B_FEATURES = ARCH_A_FEATURES + ARCH_B_EXTRA


def load_and_prepare():
    df = pd.read_csv(SORTIES_CSV, encoding='utf-8-sig')
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df[df['date'].notna()].copy()
    # de-duplicate dates (article notes 17 dupes around 2025/01)
    df = df.sort_values('date').drop_duplicates('date', keep='last')
    full = pd.date_range(df['date'].min(), df['date'].max(), freq='D')
    df = df.set_index('date').reindex(full).rename_axis('date').reset_index()
    df[TARGET] = df[TARGET].fillna(0).astype(float)

    for src, dst in STRAIT_MAP.items():
        if src in df.columns:
            raw = df[src].fillna(0).astype(str).str.strip()
            df[f'_{dst}_any'] = (
                (raw != '') & (raw != '0') & (raw != '0.0') & (raw != 'nan')
            ).astype(int)
        else:
            df[f'_{dst}_any'] = 0

    if '艦型' in df.columns:
        st = df['艦型'].fillna('').astype(str)
        # 旅洋級 (incl. 旅陽 typo / 旅洋+補) and 仁海級
        df['_target_class'] = (
            st.str.contains('旅洋') | st.str.contains('旅陽') | st.str.contains('仁海')
        ).astype(int)
    else:
        df['_target_class'] = 0

    for dst in STRAIT_MAP.values():
        df[f'_{dst}_target'] = df[f'_{dst}_any'] * df['_target_class']

    dfp = pd.read_csv(POLITICAL_CSV, encoding='utf-8-sig')
    dfp['date'] = pd.to_datetime(dfp['date'], errors='coerce')
    dfp = dfp[dfp['date'].notna()].copy()

    fb = (dfp[dfp['Foreign_battleship'].notna()]
          .groupby('date').size().reset_index(name='_foreign_vessel'))
    us = (dfp[dfp['US_Taiwan_interaction'].notna()]
          .groupby('date').size().reset_index(name='_us_tw_visit'))
    cn_mask = dfp['Political_statement'].astype(str).str.contains(
        '中共|中國|中方|國台辦', na=False)
    cn = dfp[cn_mask].groupby('date').size().reset_index(name='_cn_stmt')

    for daily in (fb, us, cn):
        df = df.merge(daily, on='date', how='left')
    for c in ['_foreign_vessel', '_us_tw_visit', '_cn_stmt']:
        df[c] = df[c].fillna(0)

    if NEWS_JSON.exists():
        with open(NEWS_JSON, encoding='utf-8') as f:
            news = json.load(f)
        rows = []
        for n in news:
            try:
                nd = pd.to_datetime(n['original_article']['date']).normalize()
                cat = n.get('category', '')
                rows.append({
                    'date': nd,
                    '_news_mil': int(cat == 'Military_Exercise'),
                    '_news_us_tw': int(cat == 'US_TW_Interaction'),
                })
            except (KeyError, ValueError, TypeError):
                continue
        if rows:
            ndf = (pd.DataFrame(rows)
                   .groupby('date')[['_news_mil', '_news_us_tw']]
                   .sum().reset_index())
            df = df.merge(ndf, on='date', how='left')
    for c in ['_news_mil', '_news_us_tw']:
        if c not in df.columns:
            df[c] = 0
        df[c] = df[c].fillna(0)

    return df


def add_static_features(df):
    df = df.copy()
    df['weekday'] = df['date'].dt.dayofweek
    df['month'] = df['date'].dt.month
    df['is_weekend'] = (df['weekday'] >= 5).astype(int)

    for s in STRAIT_MAP.values():
        for L in (1, 2, 3):
            df[f'{s}_target_lag{L}'] = df[f'_{s}_target'].shift(L).fillna(0)
        df[f'{s}_any_lag1'] = df[f'_{s}_any'].shift(1).fillna(0)

    for src, dst in [('_foreign_vessel', 'foreign_vessel'),
                     ('_us_tw_visit', 'us_tw_visit'),
                     ('_cn_stmt', 'cn_stmt'),
                     ('_news_mil', 'news_mil'),
                     ('_news_us_tw', 'news_us_tw')]:
        df[f'{dst}_lag1'] = df[src].shift(1).fillna(0)
        df[f'{dst}_7d'] = (df[src].shift(1)
                           .rolling(7, min_periods=1).sum().fillna(0))
    return df


def add_ar_features_for_training(df):
    df = df.copy()
    for lag in (1, 2, 3, 7, 14):
        df[f'lag_{lag}'] = df[TARGET].shift(lag)
    for w in (3, 5, 7, 9, 14):
        df[f'ma_{w}'] = df[TARGET].shift(1).rolling(w, min_periods=1).mean()
        df[f'std_{w}'] = (df[TARGET].shift(1)
                          .rolling(w, min_periods=1).std(ddof=0).fillna(0))
    df['diff_3_7'] = df['ma_3'] - df['ma_7']
    return df


def ar_features_from_series(series, idx):
    feats = {}
    feats['lag_1'] = series[idx - 1] if idx >= 1 else 0.0
    feats['lag_2'] = series[idx - 2] if idx >= 2 else 0.0
    feats['lag_3'] = series[idx - 3] if idx >= 3 else 0.0
    feats['lag_7'] = series[idx - 7] if idx >= 7 else 0.0
    feats['lag_14'] = series[idx - 14] if idx >= 14 else 0.0
    for w in (3, 5, 7, 9, 14):
        window = series[max(0, idx - w):idx]
        feats[f'ma_{w}'] = float(np.mean(window)) if len(window) else 0.0
        feats[f'std_{w}'] = float(np.std(window, ddof=0)) if len(window) else 0.0
    feats['diff_3_7'] = feats['ma_3'] - feats['ma_7']
    return feats


def predict_fold(df_static, fold, feat_cols):
    df_full = add_ar_features_for_training(df_static)
    df_full = df_full.reset_index(drop=True)

    train_end = pd.Timestamp(fold['train_end'])
    test_start = pd.Timestamp(fold['test_start'])
    test_end = pd.Timestamp(fold['test_end'])

    train_mask = (df_full['date'] >= TRAIN_START) & (df_full['date'] <= train_end)
    train = df_full[train_mask].dropna(subset=['lag_14']).copy()
    X_train = train[feat_cols].values
    y_train = train[TARGET].values

    model = xgb.XGBRegressor(
        n_estimators=400, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbosity=0,
        objective='reg:squarederror',
    )
    model.fit(X_train, np.log1p(y_train))

    series = df_full[TARGET].astype(float).tolist()
    test_idx = df_full[(df_full['date'] >= test_start) &
                       (df_full['date'] <= test_end)].index.tolist()

    preds, acts, dates = [], [], []
    extra_cols = [c for c in feat_cols if c not in AR_COLS]

    i = 0
    while i < len(test_idx):
        chunk = test_idx[i:i + HORIZON]
        actuals_backup = [series[idx] for idx in chunk]
        for idx in chunk:
            ar = ar_features_from_series(series, idx)
            row = df_full.iloc[idx]
            feat = dict(ar)
            for c in extra_cols:
                feat[c] = float(row[c])
            x = np.array([[feat[c] for c in feat_cols]])
            pred = max(0.0, float(np.expm1(model.predict(x)[0])))
            preds.append(pred)
            acts.append(float(actuals_backup[chunk.index(idx)]))
            dates.append(row['date'])
            series[idx] = pred  # within-chunk iterative substitution
        # restore actuals so the next chunk uses real history for its lags
        for j, idx in enumerate(chunk):
            series[idx] = actuals_backup[j]
        i += HORIZON

    return np.array(preds), np.array(acts), dates


def rmse(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.sqrt(np.mean((p - y) ** 2)))


def mae(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean(np.abs(p - y)))


def run_arch(name, feat_cols, df_static):
    print(f"\n=== {name} ===")
    print(f"  feature count: {len(feat_cols)}")
    all_p, all_a = [], []
    per_fold = []
    for fold in FOLDS:
        p, a, _ = predict_fold(df_static, fold, feat_cols)
        r, m = rmse(p, a), mae(p, a)
        per_fold.append((fold['name'], r, m, len(p)))
        print(f"  {fold['name']:7s} n={len(p):3d}  RMSE={r:7.3f}  MAE={m:6.3f}")
        all_p.extend(p)
        all_a.extend(a)
    overall_rmse = rmse(all_p, all_a)
    overall_mae = mae(all_p, all_a)
    print(f"  Overall  n={len(all_p):3d}  RMSE={overall_rmse:7.3f}  MAE={overall_mae:6.3f}")
    return {'overall_rmse': overall_rmse, 'overall_mae': overall_mae,
            'per_fold': per_fold, 'n': len(all_p)}


def main():
    print("Loading data...")
    df = load_and_prepare()
    df_static = add_static_features(df)
    in_window = df_static[(df_static['date'] >= TRAIN_START) &
                          (df_static['date'] <= pd.Timestamp('2025-12-31'))]
    print(f"  daily rows after reindex: {len(df_static)}  "
          f"(in modeling window 2022-01-15..2025-12-31: {len(in_window)})")

    a = run_arch('架構A (AR + JSO 海峽)', ARCH_A_FEATURES, df_static)
    b = run_arch('架構B (架構A + 外艦過台 + 新聞 + 政治論述)',
                 ARCH_B_FEATURES, df_static)

    print("\n" + "=" * 64)
    print("最終結果")
    print("=" * 64)
    print(f"預測模型架構A  RMSE = {a['overall_rmse']:.3f}  (MAE = {a['overall_mae']:.3f})")
    print(f"預測模型架構B  RMSE = {b['overall_rmse']:.3f}  (MAE = {b['overall_mae']:.3f})")
    d = b['overall_rmse'] - a['overall_rmse']
    print(f"\nΔRMSE (B − A) = {d:+.3f}  ({100 * d / a['overall_rmse']:+.2f}%)")
    print("=" * 64)


if __name__ == '__main__':
    main()
