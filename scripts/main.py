#!/usr/bin/env python3
"""
===============================================================================
主更新腳本 / Main Update Script
===============================================================================

整合 CNA/Xinhua 爬蟲、Grok 分類器和 GitHub 更新器
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

# 添加父目錄到路徑
sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.cna_scraper import CNAScraper
# from scrapers.xinhua_scraper import XinhuaScraper  # 如果有的話
from classifiers.grok_classifier import GrokNewsClassifier
from updaters.github_updater import GitHubUpdater


def main():
    parser = argparse.ArgumentParser(description='Daily News Update Script')
    parser.add_argument('--days', type=int, default=7, help='Days back to scrape')
    parser.add_argument('--no-push', action='store_true', help='Skip GitHub push')
    args = parser.parse_args()
    
    print("=" * 70)
    print(f"🚀 Starting Daily News Update - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📅 Scraping news from past {args.days} days")
    print("=" * 70)
    
    all_articles = []
    stats = {
        'timestamp': datetime.now().isoformat(),
        'days_back': args.days,
        'sources': {}
    }
    
    # 1. 爬取 CNA 新聞
    print("\n[1/4] 爬取中央社新聞...")
    try:
        with CNAScraper(delay=1.0) as cna:
            cna_articles = cna.run(days_back=args.days)
            all_articles.extend(cna_articles)
            stats['sources']['cna'] = {
                'scraped': len(cna_articles),
                'status': 'success'
            }
            print(f"✓ CNA: {len(cna_articles)} 篇新聞")
    except Exception as e:
        print(f"✗ CNA Error: {e}")
        stats['sources']['cna'] = {'status': 'failed', 'error': str(e)}
    
    # 2. 爬取新華社新聞（如果有）
    # print("\n[2/4] 爬取新華社新聞...")
    # try:
    #     with XinhuaScraper(delay=1.0) as xinhua:
    #         xinhua_articles = xinhua.run(days_back=args.days)
    #         all_articles.extend(xinhua_articles)
    #         stats['sources']['xinhua'] = {
    #             'scraped': len(xinhua_articles),
    #             'status': 'success'
    #         }
    #         print(f"✓ Xinhua: {len(xinhua_articles)} 篇新聞")
    # except Exception as e:
    #     print(f"✗ Xinhua Error: {e}")
    #     stats['sources']['xinhua'] = {'status': 'failed', 'error': str(e)}
    
    if not all_articles:
        print("\n❌ No articles scraped. Exiting.")
        sys.exit(1)
    
    print(f"\n📊 Total articles scraped: {len(all_articles)}")
    
    # 3. 使用 Grok 分類
    print("\n[2/4] 使用 Grok 進行新聞分類...")
    import os
    api_key = os.environ.get('GROK_API_KEY')
    
    if not api_key:
        print("❌ GROK_API_KEY not found in environment")
        sys.exit(1)
    
    try:
        with GrokNewsClassifier(api_key) as classifier:
            classified = classifier.classify_batch(all_articles, delay=1.0)
            relevant = classifier.filter_relevant(classified)
            
            stats['classification'] = {
                'total': len(classified),
                'relevant': len(relevant),
                'status': 'success'
            }
            print(f"✓ Classified: {len(classified)} 篇")
            print(f"✓ Relevant: {len(relevant)} 篇")
    except Exception as e:
        print(f"✗ Classification Error: {e}")
        stats['classification'] = {'status': 'failed', 'error': str(e)}
        sys.exit(1)
    
    # 4. 保存結果
    print("\n[3/4] 保存數據...")
    data_dir = Path('data')
    data_dir.mkdir(exist_ok=True)
    
    # 保存分類結果
    output_file = data_dir / 'news_classified.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)
    print(f"✓ Saved: {output_file}")
    
    # 保存相關新聞
    relevant_file = data_dir / 'news_relevant.json'
    with open(relevant_file, 'w', encoding='utf-8') as f:
        json.dump(relevant, f, ensure_ascii=False, indent=2)
    print(f"✓ Saved: {relevant_file}")
    
    # 5. 推送到 GitHub
    if not args.no_push:
        print("\n[4/4] 推送到 GitHub...")
        try:
            updater = GitHubUpdater()
            updater.configure_git(name="PLA Data Bot", email="bot@example.com")
            
            # 創建摘要日誌
            updater.create_summary_log(stats, 'data/last_update.json')
            
            # 推送數據文件
            data_files = [
                'data/news_classified.json',
                'data/news_relevant.json',
                'data/last_update.json'
            ]
            
            success = updater.commit_and_push_data(
                data_files=data_files,
                message=f"🤖 Auto-update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
            
            if success:
                print("✓ Pushed to GitHub")
            else:
                print("⚠️  Push failed or no changes")
        except Exception as e:
            print(f"✗ GitHub Error: {e}")
            stats['github_push'] = {'status': 'failed', 'error': str(e)}
    else:
        print("\n[4/4] Skipping GitHub push (--no-push flag)")
    
    print("\n" + "=" * 70)
    print("✅ Update completed successfully!")
    print("=" * 70)


if __name__ == '__main__':
    main()
