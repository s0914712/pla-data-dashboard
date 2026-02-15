#!/usr/bin/env python3
"""
===============================================================================
微博獨立爬取腳本 / Standalone Weibo Crawl Script
===============================================================================

獨立執行微博爬蟲，將結果經 Grok 分類後 **合併** 到：
  - data/news_classified.json
  - data/news_relevant.json

用法:
  python scripts/crawl_weibo.py [--days 7] [--no-push]

環境變數:
  WEIBO_COOKIE   — 微博 Mobile 端 Cookie（必要）
  GROK_API_KEY   — Grok 分類器金鑰（必要）
  WEIBO_TARGET_UID — 目標 UID（可選，預設東部戰區）
"""

import argparse
import json
import sys
import os
from pathlib import Path
from datetime import datetime

# 路徑設定
sys.path.insert(0, str(Path(__file__).parent))

from scrapers.weibo_scraper import WeiboScraper
from classifiers.grok_classifier import GrokNewsClassifier


def load_existing_json(filepath: Path) -> list:
    """讀取既有 JSON 檔案，若不存在則回傳空列表"""
    if filepath.exists():
        with filepath.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
    return []


def merge_articles(existing: list, new_articles: list) -> list:
    """
    合併文章，以 url 去重。
    新文章若 url 已存在則跳過，否則附加。
    """
    seen_urls = {a.get("url") for a in existing if a.get("url")}
    merged = list(existing)
    added = 0
    for article in new_articles:
        url = article.get("url", "")
        if url and url not in seen_urls:
            merged.append(article)
            seen_urls.add(url)
            added += 1
    print(f"  Merged: {added} new, {len(existing)} existing → {len(merged)} total")
    return merged


def main():
    parser = argparse.ArgumentParser(description="Standalone Weibo Scraper")
    parser.add_argument("--days", type=int, default=7, help="Days back to scrape")
    parser.add_argument("--max-pages", type=int, default=5, help="Max pages to scrape")
    parser.add_argument("--no-push", action="store_true", help="Skip GitHub push")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Weibo Scraper — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. 爬取微博
    # ------------------------------------------------------------------
    print("\n[1/3] 爬取微博貼文...")

    if not os.environ.get("WEIBO_COOKIE"):
        print("❌ WEIBO_COOKIE not set. Exiting.")
        sys.exit(1)

    with WeiboScraper(max_pages=args.max_pages) as scraper:
        articles = scraper.run(days_back=args.days)

    if not articles:
        print("⚠️  No articles scraped. Exiting.")
        sys.exit(0)

    print(f"✓ Scraped {len(articles)} posts")

    # ------------------------------------------------------------------
    # 2. Grok 去重 + 分類
    # ------------------------------------------------------------------
    print("\n[2/3] Grok 去重 + 分類...")

    api_key = os.environ.get("GROK_API_KEY")
    if not api_key:
        print("❌ GROK_API_KEY not set. Exiting.")
        sys.exit(1)

    with GrokNewsClassifier(api_key) as classifier:
        deduped = classifier.deduplicate_batch(articles)
        classified = classifier.classify_batch(deduped, delay=1.0)
        relevant = classifier.filter_relevant(classified)

    print(f"✓ Deduped: {len(articles)} → {len(deduped)}")
    print(f"✓ Classified: {len(classified)}, Relevant: {len(relevant)}")

    # ------------------------------------------------------------------
    # 3. 合併到既有 JSON
    # ------------------------------------------------------------------
    print("\n[3/3] 合併到既有 JSON...")

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    classified_file = data_dir / "news_classified.json"
    relevant_file = data_dir / "news_relevant.json"

    # 讀取 → 合併 → 寫回
    print(f"  {classified_file}:")
    merged_classified = merge_articles(load_existing_json(classified_file), classified)
    with classified_file.open("w", encoding="utf-8") as f:
        json.dump(merged_classified, f, ensure_ascii=False, indent=2)

    print(f"  {relevant_file}:")
    merged_relevant = merge_articles(load_existing_json(relevant_file), relevant)
    with relevant_file.open("w", encoding="utf-8") as f:
        json.dump(merged_relevant, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Saved: {classified_file}")
    print(f"✓ Saved: {relevant_file}")

    # ------------------------------------------------------------------
    # 4. Git commit & push (由 Action 處理或手動)
    # ------------------------------------------------------------------
    if not args.no_push:
        from updaters.github_updater import GitHubUpdater
        try:
            updater = GitHubUpdater()
            updater.configure_git(name="PLA Data Bot", email="bot@example.com")
            success = updater.commit_and_push_data(
                data_files=[str(classified_file), str(relevant_file)],
                message=f"🤖 Weibo update: {datetime.now():%Y-%m-%d %H:%M}",
            )
            print("✓ Pushed to GitHub" if success else "⚠️  No changes to push")
        except Exception as e:
            print(f"✗ GitHub push error: {e}")

    print("\n" + "=" * 60)
    print("✅ Weibo scrape completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
