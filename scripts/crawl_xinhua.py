#!/usr/bin/env python3
"""
===============================================================================
新華社獨立爬取腳本 / Standalone Xinhua Crawl Script
===============================================================================

獨立執行新華社爬蟲，將結果經 Grok 分類後 **合併** 到：
  - data/news_classified.json
  - data/news_relevant.json

用法:
  python scripts/crawl_xinhua.py [--days 7] [--no-push]

環境變數:
  GROK_API_KEY — Grok 分類器金鑰（必要）
"""

import argparse
import json
import sys
import os
from pathlib import Path
from datetime import datetime

# 路徑設定
sys.path.insert(0, str(Path(__file__).parent))

from scrapers.xinhua_scraper import XinhuaTWScraper
from classifiers.grok_classifier import GrokNewsClassifier


MILITARY_KEYWORDS = [
    "軍演", "演習", "軍機", "架次", "逾越", "中線", "殲", "轟6",
    "航母", "海空聯訓", "戰備警巡", "聯合打擊", "飛彈", "導彈",
    "東部戰區", "解放軍", "共軍", "警巡", "軍艦", "戰艦",
]


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


def _get_article_url(article: dict) -> str:
    """從 classified 結構或原始結構中取得 URL（支援巢狀 original_article）"""
    return (
        article.get("url")
        or (article.get("original_article") or {}).get("url", "")
        or ""
    )


def merge_articles(existing: list, new_articles: list) -> list:
    """合併文章，以 original_article.url 去重"""
    seen_urls = {_get_article_url(a) for a in existing if _get_article_url(a)}
    merged = list(existing)
    added = 0
    for article in new_articles:
        url = _get_article_url(article)
        if url and url not in seen_urls:
            merged.append(article)
            seen_urls.add(url)
            added += 1
    print(f"  Merged: {added} new, {len(existing)} existing → {len(merged)} total")
    return merged


def apply_source_overrides(classified: list) -> list:
    """
    對新華社文章強制套用分類規則：
    - 含軍演/軍事關鍵字 → Military_Exercise (country1=CN), is_relevant=True
    - 其他 → CCP_news_and_blog, is_relevant=True
    """
    for article in classified:
        orig = article.get("original_article", {})
        if orig.get("source", "") != "xinhua":
            continue
        text = orig.get("title", "") + " " + orig.get("content", "")
        if any(kw in text for kw in MILITARY_KEYWORDS):
            article["category"] = "Military_Exercise"
            article["country1"] = article.get("country1") or "CN"
        else:
            article["category"] = "CCP_news_and_blog"
        article["is_relevant"] = True
    return classified


def main():
    parser = argparse.ArgumentParser(description="Standalone Xinhua Scraper")
    parser.add_argument("--days", type=int, default=7, help="Days back to scrape")
    parser.add_argument("--no-push", action="store_true", help="Skip GitHub push")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Xinhua Scraper — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. 爬取新華社
    # ------------------------------------------------------------------
    print("\n[1/3] 爬取新華社新聞...")

    with XinhuaTWScraper() as scraper:
        articles = scraper.run(days_back=args.days)

    if not articles:
        print("⚠️  No articles scraped. Exiting.")
        sys.exit(0)

    print(f"✓ Scraped {len(articles)} articles")

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

    # 新華社來源強制套用分類規則
    classified = apply_source_overrides(classified)
    relevant = [r for r in classified if r.get("is_relevant", False)]

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
    # 4. Git push
    # ------------------------------------------------------------------
    if not args.no_push:
        from updaters.github_updater import GitHubUpdater
        try:
            updater = GitHubUpdater()
            updater.configure_git(name="PLA Data Bot", email="bot@example.com")
            success = updater.commit_and_push_data(
                data_files=[str(classified_file), str(relevant_file)],
                message=f"🤖 Xinhua update: {datetime.now():%Y-%m-%d %H:%M}",
            )
            print("✓ Pushed to GitHub" if success else "⚠️  No changes to push")
        except Exception as e:
            print(f"✗ GitHub push error: {e}")

    print("\n" + "=" * 60)
    print("✅ Xinhua scrape completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
