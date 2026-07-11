#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日抓取 MSA 軍事航行警告並累積合併到 data/navigation_warnings/。

關鍵設計：MSA 網站每個海事局只保留最新一頁公告，舊公告會下架，
因此每次抓取結果必須與現有資料「合併去重」（以 url 為鍵），
而不是直接覆蓋——否則歷史資料會隨網站輪替而遺失。

Usage:
    python scripts/scrape_nav_warnings.py [--max-pages 2] [--days-back 365]
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'scripts'))

OUTPUT_DIR = REPO_ROOT / 'data' / 'navigation_warnings'
CSV_PATH = OUTPUT_DIR / 'military_exercises.csv'
JSON_PATH = OUTPUT_DIR / 'military_exercises.json'
STATS_PATH = OUTPUT_DIR / 'statistics.json'

COLUMNS = [
    'publish_date', 'title', 'channel', 'time_periods',
    'coordinate_count', 'coordinates', 'coordinates_raw',
    'content_preview', 'url', 'scraped_at',
]


def load_existing():
    if CSV_PATH.exists():
        try:
            df = pd.read_csv(CSV_PATH, encoding='utf-8-sig')
            print(f'📂 現有資料: {len(df)} 筆')
            return df
        except Exception as e:
            print(f'⚠️  讀取現有 CSV 失敗: {e}')
    return pd.DataFrame(columns=COLUMNS)


def merge_warnings(existing_df, new_warnings):
    """以 url 去重合併：現有資料優先（保留原始 scraped_at）"""
    new_df = pd.DataFrame(new_warnings)
    if new_df.empty:
        return existing_df.copy(), 0
    for col in COLUMNS:
        if col not in new_df.columns:
            new_df[col] = ''
    new_df = new_df[COLUMNS]
    before = len(existing_df)
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset='url', keep='first')
    combined = combined.sort_values('publish_date', na_position='first').reset_index(drop=True)
    return combined, len(combined) - before


def save_outputs(df, n_new):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(CSV_PATH, index=False, encoding='utf-8-sig')
    print(f'💾 已保存: {CSV_PATH} ({len(df)} 筆, 本次新增 {n_new})')

    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(df.to_dict(orient='records'), f, ensure_ascii=False, indent=2)
    print(f'💾 已保存: {JSON_PATH}')

    dates = df['publish_date'].dropna().astype(str)
    dates = dates[dates.str.len() > 0]
    stats = {
        'update_time': datetime.now().isoformat(),
        'total_warnings': len(df),
        'new_this_run': n_new,
        'with_coordinates': int((df['coordinate_count'].fillna(0).astype(float) > 0).sum()),
        'with_time_periods': int(df['time_periods'].fillna('').astype(str).str.len().gt(0).sum()),
        'date_range': {
            'earliest': dates.min() if len(dates) else None,
            'latest': dates.max() if len(dates) else None,
        },
        'by_channel': dict(Counter(df['channel'].dropna())),
    }
    with open(STATS_PATH, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f'💾 已保存: {STATS_PATH}')
    return stats


def main():
    parser = argparse.ArgumentParser(description='MSA 軍事航警累積爬蟲')
    parser.add_argument('--max-pages', type=int, default=2, help='每個海事局抓取頁數')
    parser.add_argument('--days-back', type=int, default=365, help='追溯天數')
    args = parser.parse_args()

    existing_df = load_existing()

    new_warnings = []
    all_channels_failed = False
    try:
        from scrapers.NavigationWarning_scraper import NavigationWarningScraper
        with NavigationWarningScraper(delay=1.0) as scraper:
            new_warnings = scraper.run(days_back=args.days_back, max_pages=args.max_pages)
            all_channels_failed = scraper.ok_channels == 0
        print(f'🎯 本次抓取: {len(new_warnings)} 筆')
    except Exception as e:
        print(f'⚠️  爬取失敗（保留現有資料）: {e}')
        all_channels_failed = True

    merged_df, n_new = merge_warnings(existing_df, new_warnings)
    stats = save_outputs(merged_df, n_new)

    if all_channels_failed:
        # 所有海事局都無法存取（如 2026 年 3 月底起網站對境外 IP 回 403）。
        # 已保留現有資料，但以非零狀態離開讓 workflow 紅燈，避免故障靜默。
        print('\n❌ 所有海事局頻道皆無法存取，資料未更新（現有資料已保留）。')
        print('   請檢查 msa.gov.cn 是否封鎖來源 IP，或確認 MSA_PROXY secret 是否有效。')
        sys.exit(1)

    print(f'\n✅ 完成！總計 {stats["total_warnings"]} 筆（新增 {n_new}）')


if __name__ == '__main__':
    main()
