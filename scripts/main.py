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
from scrapers.weibo_scraper import WeiboScraper
from classifiers.grok_classifier import GrokNewsClassifier
from updaters.github_updater import GitHubUpdater
from updaters.naval_transit_updater import NavalTransitUpdater
# ---------------------------------------------------------------------------
# Helper: JSON 合併
# ---------------------------------------------------------------------------
def _load_existing_json(path):
    """載入既有 JSON 檔案，若不存在或格式錯誤則回傳空列表"""
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except (json.JSONDecodeError, Exception):
        pass
    return []


def _merge_articles(existing, new_articles):
    """合併文章列表，以 original_article.url 去重"""
    def _get_url(article):
        url = article.get("original_article", {}).get("url", "")
        if not url:
            url = article.get("url", "")
        return url

    seen_urls = set()
    merged = []
    # 新文章優先（分類可能更新），再補上舊文章
    for article in new_articles:
        url = _get_url(article)
        if url and url not in seen_urls:
            seen_urls.add(url)
            merged.append(article)
        elif not url:
            merged.append(article)
    for article in existing:
        url = _get_url(article)
        if url and url not in seen_urls:
            seen_urls.add(url)
            merged.append(article)
    return merged


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
    print("\n[1/6] 爬取中央社新聞...")
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
    print("\n[2/6] 爬取新華社新聞...")
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
    # -----------------------------------------------------------------------
    # 3. Weibo（東部戰區）
    # -----------------------------------------------------------------------
    print("\n[3/6] 爬取微博貼文...")
    try:
        with WeiboScraper() as weibo:
            weibo_articles = weibo.run(days_back=args.days)
            all_articles.extend(weibo_articles)

            stats["sources"]["weibo"] = {
                "scraped": len(weibo_articles),
                "status": "success"
            }

            print(f"✓ Weibo: {len(weibo_articles)} 篇貼文")
            logger.info(f"Weibo: scraped {len(weibo_articles)} posts")
    except Exception as e:
        print(f"✗ Weibo Error: {e}")
        logger.error(f"Weibo Error: {e}")
        stats["sources"]["weibo"] = {
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
    # 4. Grok 去重 + 分類
    # -----------------------------------------------------------------------
    print("\n[4/6] 使用 Grok 進行去重與新聞分類...")
    api_key = os.environ.get("GROK_API_KEY")
    if not api_key:
        print("❌ GROK_API_KEY not found in environment")
        sys.exit(1)
    try:
        with GrokNewsClassifier(api_key) as classifier:
            # 去重：先用 LLM 識別重複/高度相似文章
            deduped = classifier.deduplicate_batch(all_articles)
            stats["deduplication"] = {
                "before": len(all_articles),
                "after": len(deduped),
                "removed": len(all_articles) - len(deduped),
            }
            logger.info(
                f"Dedup: {len(all_articles)} → {len(deduped)} "
                f"(removed {len(all_articles) - len(deduped)})"
            )
            # 分類
            classified = classifier.classify_batch(deduped, delay=1.0)
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
    # 5. 儲存結果（合併既有資料，避免覆蓋其他爬蟲的成果）
    # -----------------------------------------------------------------------
    print("\n[5/6] 保存數據...")
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    classified_file = data_dir / "news_classified.json"
    relevant_file = data_dir / "news_relevant.json"
    # 讀取既有資料並合併（以 url 去重）
    existing_classified = _load_existing_json(classified_file)
    existing_relevant = _load_existing_json(relevant_file)
    merged_classified = _merge_articles(existing_classified, classified)
    merged_relevant = _merge_articles(existing_relevant, relevant)
    with classified_file.open("w", encoding="utf-8") as f:
        json.dump(merged_classified, f, ensure_ascii=False, indent=2)
    with relevant_file.open("w", encoding="utf-8") as f:
        json.dump(merged_relevant, f, ensure_ascii=False, indent=2)
    print(f"✓ Saved: {classified_file} ({len(merged_classified)} articles)")
    print(f"✓ Saved: {relevant_file} ({len(merged_relevant)} articles)")
    logger.info(f"Saved: {classified_file}, {relevant_file}")
    # -----------------------------------------------------------------------
    # 5b. 更新 naval_transits.csv（Foreign_battleship → 軍艦通過記錄）
    # -----------------------------------------------------------------------
    print("\n[5b/6] 更新軍艦通過記錄...")
    try:
        naval_csv = data_dir / "naval_transits.csv"
        sortie_csv = data_dir / "merged_comprehensive_data_M.csv"
        transit_updater = NavalTransitUpdater(
            csv_path=str(naval_csv),
            sortie_csv_path=str(sortie_csv) if sortie_csv.exists() else None,
        )
        transit_count = transit_updater.update_from_classified(merged_classified)
        stats["naval_transits"] = {
            "new_entries": transit_count,
            "status": "success",
        }
        print(f"✓ Naval transits: {transit_count} new entries")
        logger.info(f"Naval transits updated: {transit_count} new entries")
        # -----------------------------------------------------------------
        # 5c. 將 naval_transits.csv 全部記錄轉為 JSON，合併到分類結果中
        # -----------------------------------------------------------------
        print("\n[5c/6] 同步軍艦通過記錄到 JSON...")
        transit_articles = transit_updater.csv_to_json_articles()
        if transit_articles:
            merged_classified = _merge_articles(merged_classified, transit_articles)
            merged_relevant = _merge_articles(merged_relevant, transit_articles)
            with classified_file.open("w", encoding="utf-8") as f:
                json.dump(merged_classified, f, ensure_ascii=False, indent=2)
            with relevant_file.open("w", encoding="utf-8") as f:
                json.dump(merged_relevant, f, ensure_ascii=False, indent=2)
            print(f"✓ Synced {len(transit_articles)} transit records to JSON")
            logger.info(f"Synced {len(transit_articles)} transit records to JSON")
    except Exception as e:
        print(f"✗ Naval transit update error: {e}")
        logger.error(f"Naval transit update error: {e}")
        stats["naval_transits"] = {"status": "failed", "error": str(e)}
    # 儲存執行日誌到 data/logs/
    log_file = _save_execution_log(stats, start_time, log_capture, success=True)
    # -----------------------------------------------------------------------
    # 6. GitHub 推送
    # -----------------------------------------------------------------------
    if args.no_push:
        print("\n[6/6] Skipping GitHub push (--no-push)")
        return
    print("\n[6/6] 推送到 GitHub...")
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
            "data/naval_transits.csv",
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
