#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sentiment ↔ pla_aircraft_sorties correlation diagnostic.

One-shot read-only script that quantifies whether the existing
`news_avg_sentiment` feature (and several alternative aggregations)
carries signal for the prediction target. Prints a stdout report and
writes data/charts/sentiment_correlation.csv.

Run from repo root:
    python scripts/analysis/sentiment_correlation.py
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

REPO_ROOT = Path(__file__).resolve().parents[2]
SORTIES_CSV = REPO_ROOT / 'data' / 'JapanandBattleship.csv'
NEWS_JSON = REPO_ROOT / 'data' / 'news_classified.json'
OUT_CSV = REPO_ROOT / 'data' / 'charts' / 'sentiment_correlation.csv'

TARGET = 'pla_aircraft_sorties'
MIN_N = 30


def load_target(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date']).sort_values('date')
    df = df[['date', TARGET]].copy()
    df[TARGET] = pd.to_numeric(df[TARGET], errors='coerce')
    df = df.dropna(subset=[TARGET])
    # Reindex to dense daily range (gaps filled with NaN target)
    full_idx = pd.date_range(df['date'].min(), df['date'].max(), freq='D')
    df = df.set_index('date').reindex(full_idx).rename_axis('date').reset_index()
    return df


def load_news_daily(path: Path) -> pd.DataFrame:
    with open(path, 'r', encoding='utf-8') as f:
        articles = json.load(f)

    rows = []
    for n in articles:
        try:
            d = pd.to_datetime(n['original_article']['date']).normalize()
        except (KeyError, TypeError, ValueError):
            continue
        score = n.get('sentiment_score')
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        label = n.get('sentiment_label', '')
        rows.append({'date': d, 'sentiment': score, 'label': label})

    if not rows:
        return pd.DataFrame(columns=['date', 'mean_sent', 'n_articles', 'n_pos', 'n_neg', 'n_neu', 'std_sent'])

    nf = pd.DataFrame(rows)
    nf['is_pos'] = (nf['sentiment'] > 0.1).astype(int)
    nf['is_neg'] = (nf['sentiment'] < -0.1).astype(int)
    nf['is_neu'] = ((nf['sentiment'] >= -0.1) & (nf['sentiment'] <= 0.1)).astype(int)

    daily = nf.groupby('date').agg(
        mean_sent=('sentiment', 'mean'),
        n_articles=('sentiment', 'size'),
        n_pos=('is_pos', 'sum'),
        n_neg=('is_neg', 'sum'),
        n_neu=('is_neu', 'sum'),
        std_sent=('sentiment', 'std'),
    ).reset_index()
    return daily


def _shifted_rolling(series: pd.Series, window: int, op: str = 'mean',
                     min_periods: int = 1, skipna: bool = False) -> pd.Series:
    """Reproduce predictor mechanic: shift(1) then rolling op.

    skipna=True: rolling computed only over non-NaN news days (skip-NaN behavior),
    after which we forward-broadcast to all dates via reindex.
    skipna=False: NaN -> 0 fill before rolling (matches current predictor).
    """
    s = series.copy()
    if skipna:
        s_only = s.dropna()
        if op == 'mean':
            r = s_only.shift(1).rolling(window, min_periods=min_periods).mean()
        elif op == 'std':
            r = s_only.shift(1).rolling(window, min_periods=min_periods).std()
        elif op == 'sum':
            r = s_only.shift(1).rolling(window, min_periods=min_periods).sum()
        else:
            raise ValueError(op)
        return r.reindex(series.index)
    else:
        s = s.fillna(0)
        sh = s.shift(1)
        if op == 'mean':
            return sh.rolling(window, min_periods=min_periods).mean()
        if op == 'std':
            return sh.rolling(window, min_periods=min_periods).std()
        if op == 'sum':
            return sh.rolling(window, min_periods=min_periods).sum()
        raise ValueError(op)


def build_features(target: pd.DataFrame, news_daily: pd.DataFrame) -> pd.DataFrame:
    df = target.merge(news_daily, on='date', how='left')

    # Article-count rollings (NaN -> 0 since "no articles" really is zero)
    for col in ['n_articles', 'n_pos', 'n_neg', 'n_neu']:
        df[col] = df[col].fillna(0)

    # Variant 1: current predictor formula (zero-fill + 7d rolling mean of mean_sent)
    df['sent_zero_fill_roll7'] = _shifted_rolling(df['mean_sent'], 7, 'mean', skipna=False)

    # Variant 2: skip-NaN rolling means at multiple windows
    for w in (3, 7, 14):
        df[f'sent_skipna_roll{w}'] = _shifted_rolling(df['mean_sent'], w, 'mean',
                                                     min_periods=3 if w > 3 else 2,
                                                     skipna=True)

    # Variant 3: count-based features
    df['neg_count_7d'] = df['n_neg'].shift(1).rolling(7, min_periods=1).sum()
    df['pos_count_7d'] = df['n_pos'].shift(1).rolling(7, min_periods=1).sum()
    df['art_count_7d'] = df['n_articles'].shift(1).rolling(7, min_periods=1).sum()

    # Variant 4: ratio (negative share) — only defined when there are articles
    df['neg_ratio_7d'] = np.where(
        df['art_count_7d'] > 0,
        df['neg_count_7d'] / df['art_count_7d'].replace(0, np.nan),
        np.nan,
    )

    # Variant 5: rolling sentiment volatility (skip-NaN)
    df['sent_vol_7d'] = _shifted_rolling(df['mean_sent'], 7, 'std',
                                         min_periods=3, skipna=True)

    return df


def _corr_pair(x: pd.Series, y: pd.Series) -> dict:
    paired = pd.concat([x, y], axis=1).dropna()
    n = len(paired)
    if n < MIN_N or paired.iloc[:, 0].nunique() < 2 or paired.iloc[:, 1].nunique() < 2:
        return {'n': n, 'pearson_r': np.nan, 'pearson_p': np.nan,
                'spearman_r': np.nan, 'spearman_p': np.nan, 'note': 'INSUFFICIENT' if n < MIN_N else 'CONST'}
    pr, pp = stats.pearsonr(paired.iloc[:, 0], paired.iloc[:, 1])
    sr, sp = stats.spearmanr(paired.iloc[:, 0], paired.iloc[:, 1])
    return {'n': n, 'pearson_r': pr, 'pearson_p': pp,
            'spearman_r': sr, 'spearman_p': sp, 'note': ''}


def correlations(df: pd.DataFrame, feature_cols: list, lags=(0, 1, 3, 7)) -> pd.DataFrame:
    rows = []
    target = df[TARGET]
    for feat in feature_cols:
        for lag in lags:
            # feature at t-lag predicting target at t
            x = df[feat].shift(lag)
            y = target
            r = _corr_pair(x, y)
            r.update({'feature': feat, 'lag': lag})
            rows.append(r)
    return pd.DataFrame(rows)[['feature', 'lag', 'n', 'pearson_r', 'pearson_p',
                               'spearman_r', 'spearman_p', 'note']]


def regime_split_corr(df: pd.DataFrame, feature_cols: list) -> pd.DataFrame:
    """Stratify on target == 0 vs target > 0; correlate at lag 0."""
    rows = []
    valid = df.dropna(subset=[TARGET])
    masks = {
        'target==0': valid[TARGET] == 0,
        'target>0': valid[TARGET] > 0,
    }
    for regime, mask in masks.items():
        sub = valid[mask]
        for feat in feature_cols:
            r = _corr_pair(sub[feat], sub[TARGET])
            r.update({'feature': feat, 'regime': regime})
            rows.append(r)
    return pd.DataFrame(rows)[['regime', 'feature', 'n', 'pearson_r', 'pearson_p',
                               'spearman_r', 'spearman_p', 'note']]


def coverage_report(target: pd.DataFrame, news_daily: pd.DataFrame,
                    df_feat: pd.DataFrame) -> dict:
    n_target_days = target[TARGET].notna().sum()
    overlap = target.merge(news_daily, on='date', how='inner')
    n_news_days = (overlap['n_articles'] > 0).sum() if 'n_articles' in overlap else len(overlap)

    cur = df_feat['sent_zero_fill_roll7']
    pct_zero = float((cur == 0).sum()) / max(len(cur), 1)

    return {
        'target_days': int(n_target_days),
        'days_with_news': int(n_news_days),
        'pct_news_coverage': float(n_news_days) / max(n_target_days, 1),
        'pct_current_feature_zero': pct_zero,
    }


def quick_importance(df: pd.DataFrame, feature_cols: list) -> pd.DataFrame:
    """Fit a small GradientBoostingRegressor; report importances.

    Uses minimal autoregressive features so sentiment importance is
    measured against a realistic baseline. Only "always-defined" sentiment
    features are passed in to avoid catastrophic NaN drops; news-day-only
    features (e.g. skipna rollings) leave most days NaN and would shrink
    the training set to n<150.
    """
    from sklearn.ensemble import GradientBoostingRegressor

    work = df.copy()
    work['lag_1'] = work[TARGET].shift(1)
    work['lag_7'] = work[TARGET].shift(7)
    work['ma_7'] = work[TARGET].shift(1).rolling(7, min_periods=1).mean()
    work['dow'] = work['date'].dt.dayofweek
    work['dow_sin'] = np.sin(2 * np.pi * work['dow'] / 7)
    work['dow_cos'] = np.cos(2 * np.pi * work['dow'] / 7)

    base_cols = ['lag_1', 'lag_7', 'ma_7', 'dow_sin', 'dow_cos']
    cols = base_cols + feature_cols
    fit_df = work.dropna(subset=cols + [TARGET])
    if len(fit_df) < 200:
        return pd.DataFrame()

    X = fit_df[cols].values
    y = fit_df[TARGET].values
    gbr = GradientBoostingRegressor(n_estimators=200, max_depth=3,
                                    learning_rate=0.05, random_state=42)
    gbr.fit(X, y)
    imp = pd.DataFrame({'feature': cols, 'importance': gbr.feature_importances_,
                        'n_train': len(fit_df)})
    return imp.sort_values('importance', ascending=False).reset_index(drop=True)


def fmt_corr_row(r: pd.Series) -> str:
    if pd.isna(r['pearson_r']):
        return f"  {r['feature']:<28} lag={r['lag']:<2} n={int(r['n']):<5} {r.get('note','')}"
    star_p = '***' if r['pearson_p'] < 0.001 else ('**' if r['pearson_p'] < 0.01 else ('*' if r['pearson_p'] < 0.05 else ' '))
    star_s = '***' if r['spearman_p'] < 0.001 else ('**' if r['spearman_p'] < 0.01 else ('*' if r['spearman_p'] < 0.05 else ' '))
    return (f"  {r['feature']:<28} lag={r['lag']:<2} n={int(r['n']):<5} "
            f"pearson={r['pearson_r']:+.3f} {star_p:<3} "
            f"spearman={r['spearman_r']:+.3f} {star_s:<3}")


def print_section(title):
    print()
    print('-' * 72)
    print(title)
    print('-' * 72)


def main():
    print('=' * 72)
    print(f'SENTIMENT vs {TARGET} — DIAGNOSTIC REPORT')
    print('=' * 72)
    print(f'  Target CSV : {SORTIES_CSV.relative_to(REPO_ROOT)}')
    print(f'  News JSON  : {NEWS_JSON.relative_to(REPO_ROOT)}')

    target = load_target(SORTIES_CSV)
    news_daily = load_news_daily(NEWS_JSON)
    print(f'  Target rows: {target[TARGET].notna().sum()} '
          f'({target["date"].min().date()} → {target["date"].max().date()})')
    print(f'  News articles: {news_daily["n_articles"].sum() if len(news_daily) else 0} '
          f'across {len(news_daily)} dates '
          f'({news_daily["date"].min().date() if len(news_daily) else "—"} → '
          f'{news_daily["date"].max().date() if len(news_daily) else "—"})')

    df = build_features(target, news_daily)

    cov = coverage_report(target, news_daily, df)
    print_section('[1] DATA COVERAGE')
    print(f'  Target days                   : {cov["target_days"]}')
    print(f'  Days with >=1 article         : {cov["days_with_news"]} '
          f'({cov["pct_news_coverage"]*100:.1f}% of target days)')
    print(f'  Current feature == 0 exactly  : {cov["pct_current_feature_zero"]*100:.1f}% of days '
          f'(rolling-mean dilution from fillna(0))')
    if 'label' in news_daily:
        # original labels live in raw articles; recompute from json for label distribution
        with open(NEWS_JSON, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        label_counts = pd.Series([n.get('sentiment_label', '') for n in raw]).value_counts()
        scores = pd.Series([n.get('sentiment_score', np.nan) for n in raw], dtype=float).dropna()
        print(f'  sentiment_label counts        : '
              + '  '.join(f'{k}={v}' for k, v in label_counts.items()))
        if len(scores):
            print(f'  sentiment_score distribution  : '
                  f'mean={scores.mean():+.3f}  std={scores.std():.3f}  '
                  f'min={scores.min():+.3f}  max={scores.max():+.3f}')

    feature_cols = [
        'sent_zero_fill_roll7',     # current predictor formula
        'sent_skipna_roll3',
        'sent_skipna_roll7',
        'sent_skipna_roll14',
        'neg_count_7d',
        'pos_count_7d',
        'neg_ratio_7d',
        'sent_vol_7d',
    ]

    print_section('[2] CORRELATION (feature shifted by `lag` days vs target[t])')
    corr_df = correlations(df, feature_cols, lags=(0, 1, 3, 7))
    for lag in (0, 1, 3, 7):
        print(f'\n  --- lag={lag} ---')
        sub = corr_df[corr_df['lag'] == lag]
        for _, r in sub.iterrows():
            print(fmt_corr_row(r))

    print_section('[3] REGIME-STRATIFIED (lag 0)')
    reg = regime_split_corr(df, feature_cols)
    for regime in ('target==0', 'target>0'):
        print(f'\n  --- {regime} ---')
        sub = reg[reg['regime'] == regime]
        for _, r in sub.iterrows():
            line = (f"  {r['feature']:<28} n={int(r['n']):<5} ")
            if pd.isna(r['pearson_r']):
                line += r.get('note', '')
            else:
                line += (f"pearson={r['pearson_r']:+.3f} (p={r['pearson_p']:.3f})  "
                         f"spearman={r['spearman_r']:+.3f} (p={r['spearman_p']:.3f})")
            print(line)

    # Only "always-defined" features go into GBR (others are NaN on most days)
    importance_features = ['sent_zero_fill_roll7', 'neg_count_7d',
                           'pos_count_7d', 'art_count_7d']
    print_section('[4] QUICK FEATURE IMPORTANCE (GBR baseline + always-defined sent. features)')
    try:
        imp = quick_importance(df, importance_features)
        if imp.empty:
            print('  Skipped (insufficient rows after NaN drop).')
        else:
            print(f'  Trained on n={int(imp["n_train"].iloc[0])} samples')
            for _, r in imp.iterrows():
                print(f"  {r['feature']:<28} {r['importance']:.4f}")
    except Exception as e:
        print(f'  Skipped: {e}')

    # --- VERDICT (mechanical, based on numbers) ---
    print_section('[5] VERDICT')
    cur = corr_df[(corr_df['feature'] == 'sent_zero_fill_roll7') & (corr_df['lag'] == 0)].iloc[0]
    skip = corr_df[(corr_df['feature'] == 'sent_skipna_roll7') & (corr_df['lag'] == 0)].iloc[0]
    neg = corr_df[(corr_df['feature'] == 'neg_count_7d') & (corr_df['lag'] == 0)].iloc[0]

    def _fmt(r):
        return f"|r|={abs(r['pearson_r']):.3f}, p={r['pearson_p']:.3g}, n={int(r['n'])}"

    print(f"  Current  sent_zero_fill_roll7 : {_fmt(cur)}")
    print(f"  Variant  sent_skipna_roll7    : {_fmt(skip)}")
    print(f"  Variant  neg_count_7d         : {_fmt(neg)}")

    # Strongest alternative: filter to features with adequate sample size AND p<0.05
    candidates = corr_df[(corr_df['feature'] != 'sent_zero_fill_roll7')
                         & (corr_df['n'] >= 200)
                         & (corr_df['pearson_p'] < 0.05)].copy()
    candidates['abs_r'] = candidates['pearson_r'].abs()
    print()
    if len(candidates):
        best = candidates.sort_values('abs_r', ascending=False).iloc[0]
        print(f"  Strongest alt. (n>=200, p<0.05): {best['feature']} "
              f"(lag={int(best['lag'])}) |r|={best['abs_r']:.3f}, p={best['pearson_p']:.3g}")
    else:
        print("  No alternative reaches n>=200 AND p<0.05.")
    # Also show best skipna result (small-n, news-day subset) for completeness
    skipna_cands = corr_df[corr_df['feature'].str.startswith('sent_skipna') |
                            corr_df['feature'].isin(['neg_ratio_7d', 'sent_vol_7d'])].copy()
    skipna_cands['abs_r'] = skipna_cands['pearson_r'].abs()
    if len(skipna_cands):
        best_s = skipna_cands.sort_values('abs_r', ascending=False).iloc[0]
        print(f"  Strongest news-day-subset:        {best_s['feature']} "
              f"(lag={int(best_s['lag'])}) |r|={best_s['abs_r']:.3f}, "
              f"p={best_s['pearson_p']:.3g}, n={int(best_s['n'])}")

    if abs(cur['pearson_r']) < 0.05 and cur['pearson_p'] > 0.05:
        msg = ("  -> Current `news_avg_sentiment` is statistically near-noise. "
               "Consider replacing it with the strongest alternative above, "
               "or dropping it entirely.")
    else:
        msg = ("  -> Current `news_avg_sentiment` shows some signal. "
               "Compare against alternatives before changing predictor.")
    print(msg)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    corr_df.to_csv(OUT_CSV, index=False, encoding='utf-8-sig')
    print()
    print(f'  Correlation table written to {OUT_CSV.relative_to(REPO_ROOT)}')
    print('=' * 72)


if __name__ == '__main__':
    sys.exit(main() or 0)
