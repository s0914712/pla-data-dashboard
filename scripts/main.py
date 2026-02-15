#!/usr/bin/env python3
"""
===============================================================================
主更新腳本 / Main Update Script
===============================================================================
整合 CNA / Xinhua 爬蟲、Grok 分類器與 GitHub 更新器
"""
import argparse
import json
import logging
import sys
import io
import traceback
from pathlib import Path
from datetime import datetime
import os
# ---------------------------------------------------------------------------
# Path 設定
# ---------------------------------------------------------------------------
# 添加父目錄到 Python 路徑
sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.cna_scraper import CNAScraper
from scrapers.xinhua_scraper import XinhuaTWScraper
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
    # -------------------------------------------------------------------
    # 設定日誌捕獲 (同時輸出到 console 和記憶體)
    # -------------------------------------------------------------------
    start_time = datetime.now()
    log_capture = io.StringIO()
    log_handler = logging.StreamHandler(log_capture)
    log_handler.setLevel(logging.INFO)
    log_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger = logging.getLogger('daily_update')
    logger.setLevel(logging.INFO)
    logger.addHandler(log_handler)
    print("=" * 70)
    print(f"🚀 Starting Daily News Update - {start_time:%Y-%m-%d %H:%M:%S}")
    print(f"📅 Scraping news from past {args.days} days")
    print("=" * 70)
    logger.info(f"Starting Daily News Update - {start_time:%Y-%m-%d %H:%M:%S}")
    logger.info(f"Days back: {args.days}")
    all_articles = []
    stats = {
        "timestamp": start_time.isoformat(),
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
            logger.info(f"CNA: scraped {len(cna_articles)} articles")
    except Exception as e:
        print(f"✗ CNA Error: {e}")
        logger.error(f"CNA Error: {e}")
        stats["sources"]["cna"] = {
            "status": "failed",
            "error": str(e)
        }
    # -----------------------------------------------------------------------
    # 2. Xinhua（可選）
    # -----------------------------------------------------------------------
    print("\n[2/4] 爬取新華社新聞...")
    try:
        with XinhuaTWScraper() as xinhua:
            xinhua_articles = xinhua.run(days_back=args.days)
            all_articles.extend(xinhua_articles)

            stats["sources"]["xinhua"] = {
                "scraped": len(xinhua_articles),
                "status": "success"
            }

            print(f"✓ Xinhua: {len(xinhua_articles)} 篇新聞")
            logger.info(f"Xinhua: scraped {len(xinhua_articles)} articles")
    except Exception as e:
        print(f"✗ Xinhua Error: {e}")
        logger.error(f"Xinhua Error: {e}")
        stats["sources"]["xinhua"] = {
            "status": "failed",
            "error": str(e)
        }
    if not all_articles:
        print("\n❌ No articles scraped. Exiting.")
        logger.error("No articles scraped. Exiting.")
        # 即使失敗也寫入 log
        _save_execution_log(stats, start_time, log_capture, success=False)
        sys.exit(1)
    print(f"\n📊 Total articles scraped: {len(all_articles)}")
    logger.info(f"Total articles scraped: {len(all_articles)}")
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
            logger.info(f"Classified: {len(classified)}, Relevant: {len(relevant)}")
    except Exception as e:
        print(f"✗ Classification Error: {e}")
        logger.error(f"Classification Error: {e}")
        stats["classification"] = {
            "status": "failed",
            "error": str(e)
        }
        _save_execution_log(stats, start_time, log_capture, success=False)
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
    logger.info(f"Saved: {classified_file}, {relevant_file}")
    # 儲存執行日誌到 data/logs/
    log_file = _save_execution_log(stats, start_time, log_capture, success=True)
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
        # 收集要推送的檔案（包含 log）
        push_files = [
            "data/news_classified.json",
            "data/news_relevant.json",
            "data/last_update.json",
        ]
        if log_file and Path(log_file).exists():
            push_files.append(str(log_file))
        success = updater.commit_and_push_data(
            data_files=push_files,
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
# ---------------------------------------------------------------------------
# Helper: 儲存執行日誌
# ---------------------------------------------------------------------------
def _save_execution_log(stats, start_time, log_capture, success=True):
    """將執行統計與日誌輸出寫入 data/logs/<timestamp>.json"""
    try:
        end_time = datetime.now()
        logs_dir = Path("data/logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_data = {
            "run_timestamp": start_time.isoformat(),
            "end_timestamp": end_time.isoformat(),
            "duration_seconds": round((end_time - start_time).total_seconds(), 1),
            "success": success,
            "stats": stats,
            "console_output": log_capture.getvalue()
        }
        log_filename = logs_dir / f"{start_time:%Y%m%d_%H%M%S}.json"
        with log_filename.open("w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        print(f"📝 Log saved: {log_filename}")
        return str(log_filename)
    except Exception as e:
        print(f"⚠️  Failed to save log: {e}")
        return None
if __name__ == "__main__":
    main()
