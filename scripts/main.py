#!/usr/bin/env python3
"""
===============================================================================
主更新腳本 / Main Update Script
===============================================================================

整合 CNA / Xinhua 爬蟲、Grok 分類器與 GitHub 更新器
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
import os

# ---------------------------------------------------------------------------
# Path 設定
# ---------------------------------------------------------------------------

# 添加父目錄到 Python 路徑
sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.cna_scraper import CNAScraper
# from scrapers.xinhua_scraper import XinhuaScraper  # 若尚未實作可先註解
from classifiers.grok_classifier import GrokNewsClassifier
from updaters.github_updater import GitHubUpdater


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Daily News Update Script")
    parser.add_argument("--days", type=int, default=7, help="Days back to scrape")
    parser.add_argument("--no-push", action="store_true", help="Skip GitHub push")
    args = parser.parse_args()

    print("=" * 70)
    print(f"🚀 Starting Daily News Update - {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"📅 Scraping news from past {args.days} days")
    print("=" * 70)

    all_articles = []
    stats = {
        "timestamp": datetime.now().isoformat(),
        "days_back": args.days,
        "sources": {}
    }

    # -----------------------------------------------------------------------
    # 1. CNA
    # -----------------------------------------------------------------------

    print("\n[1/4] 爬取中央社新聞...")
    try:
        with CNAScraper(delay=1.0) as cna:
            cna_articles = cna.run(days_back=args.days)
            all_articles.extend(cna_articles)

            stats["sources"]["cna"] = {
                "scraped": len(cna_articles),
                "status": "success"
            }

            print(f"✓ CNA: {len(cna_articles)} 篇新聞")
    except Exception as e:
        print(f"✗ CNA Error: {e}")
        stats["sources"]["cna"] = {
            "status": "failed",
            "error": str(e)
        }

    # -----------------------------------------------------------------------
    # 2. Xinhua（可選）
    # -----------------------------------------------------------------------

    print("\n[2/4] 爬取新華社新聞...")
    try:
        # 若 XinhuaScraper 尚未實作，請維持註解
        # with XinhuaScraper(delay=1.0) as xinhua:
        #     xinhua_articles = xinhua.run(days_back=args.days)
        #     all_articles.extend(xinhua_articles)
        #
        #     stats["sources"]["xinhua"] = {
        #         "scraped": len(xinhua_articles),
        #         "status": "success"
        #     }
        #
        #     print(f"✓ Xinhua: {len(xinhua_articles)} 篇新聞")

        print("⚠️  Xinhua scraper not enabled.")
        stats["sources"]["xinhua"] = {
            "status": "skipped"
        }

    except Exception as e:
        print(f"✗ Xinhua Error: {e}")
        stats["sources"]["xinhua"] = {
            "status": "failed",
            "error": str(e)
        }

    if not all_articles:
        print("\n❌ No articles scraped. Exiting.")
        sys.exit(1)

    print(f"\n📊 Total articles scraped: {len(all_articles)}")

    # -----------------------------------------------------------------------
    # 3. Grok 分類
    # -----------------------------------------------------------------------

    print("\n[3/4] 使用 Grok 進行新聞分類...")
    api_key = os.environ.get("GROK_API_KEY")

    if not api_key:
        print("❌ GROK_API_KEY not found in environment")
        sys.exit(1)

    try:
        with GrokNewsClassifier(api_key) as classifier:
            classified = classifier.classify_batch(all_articles, delay=1.0)
            relevant = classifier.filter_relevant(classified)

            stats["classification"] = {
                "total": len(classified),
                "relevant": len(relevant),
                "status": "success"
            }

            print(f"✓ Classified: {len(classified)} 篇")
            print(f"✓ Relevant: {len(relevant)} 篇")

    except Exception as e:
        print(f"✗ Classification Error: {e}")
        stats["classification"] = {
            "status": "failed",
            "error": str(e)
        }
        sys.exit(1)

    # -----------------------------------------------------------------------
    # 4. 儲存結果
    # -----------------------------------------------------------------------

    print("\n[4/4] 保存數據...")
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    classified_file = data_dir / "news_classified.json"
    relevant_file = data_dir / "news_relevant.json"

    with classified_file.open("w", encoding="utf-8") as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)

    with relevant_file.open("w", encoding="utf-8") as f:
        json.dump(relevant, f, ensure_ascii=False, indent=2)

    print(f"✓ Saved: {classified_file}")
    print(f"✓ Saved: {relevant_file}")

    # -----------------------------------------------------------------------
    # 5. GitHub 推送
    # -----------------------------------------------------------------------

    if args.no_push:
        print("\n[5/5] Skipping GitHub push (--no-push)")
        return

    print("\n[5/5] 推送到 GitHub...")
    try:
        updater = GitHubUpdater()
        updater.configure_git(
            name="PLA Data Bot",
            email="bot@example.com"
        )

        updater.create_summary_log(stats, "data/last_update.json")

        success = updater.commit_and_push_data(
            data_files=[
                "data/news_classified.json",
                "data/news_relevant.json",
                "data/last_update.json"
            ],
            message=f"🤖 Auto-update: {datetime.now():%Y-%m-%d %H:%M}"
        )

        print("✓ Pushed to GitHub" if success else "⚠️  No changes to push")

    except Exception as e:
        print(f"✗ GitHub Error: {e}")
        stats["github_push"] = {
            "status": "failed",
            "error": str(e)
        }

    print("\n" + "=" * 70)
    print("✅ Update completed successfully!")
    print("=" * 70)


if __name__ == "__main__":
    main()
