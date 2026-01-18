#!/usr/bin/env python3
"""
===============================================================================
PLA Data Dashboard 新聞爬蟲與自動更新主程式
News Scraper & Auto-Update Main Script
===============================================================================

每日自動執行流程：
1. 爬取新華社和中央社新聞
2. 使用 Grok API 分類和情緒分析
3. 更新 CSV 數據集
4. 推送到 GitHub

使用方法：
    python main.py                    # 完整執行
    python main.py --scrape-only      # 只爬取不分類
    python main.py --no-push          # 不推送到 GitHub
    python main.py --days 3           # 爬取過去 3 天

環境變數：
    GROK_API_KEY    - Grok API 密鑰
    GITHUB_TOKEN    - GitHub Token（GitHub Actions 自動提供）
"""

import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

# 添加 scripts 目錄到路徑
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from scrapers import XinhuaTWScraper, CNAScraper
from classifiers import GrokNewsClassifier
from updaters import CSVUpdater, GitHubUpdater


def parse_args():
    """解析命令行參數"""
    parser = argparse.ArgumentParser(
        description='PLA Data Dashboard News Scraper & Updater'
    )
    parser.add_argument(
        '--days', type=int, default=7,
        help='爬取過去幾天的新聞 (default: 7)'
    )
    parser.add_argument(
        '--scrape-only', action='store_true',
        help='只爬取新聞，不進行分類'
    )
    parser.add_argument(
        '--no-push', action='store_true',
        help='不推送到 GitHub'
    )
    parser.add_argument(
        '--output', type=str, default=None,
        help='輸出 CSV 路徑'
    )
    parser.add_argument(
        '--debug', action='store_true',
        help='啟用調試模式'
    )
    return parser.parse_args()


def main():
    """主執行流程"""
    args = parse_args()
    
    # 初始化日誌
    log = {
        'timestamp': datetime.now().isoformat(),
        'args': vars(args),
        'steps': [],
        'status': 'running'
    }
    
    print("=" * 70)
    print("PLA Data Dashboard News Scraper & Updater")
    print(f"Started at: {log['timestamp']}")
    print("=" * 70)
    
    # 驗證配置
    errors = Config.validate()
    if errors and not args.scrape_only:
        print(f"\n⚠️  Configuration errors: {errors}")
        print("Set GROK_API_KEY environment variable or use --scrape-only")
        if not args.debug:
            sys.exit(1)
    
    all_news = []
    
    # ================================================================
    # Step 1: 爬取新聞
    # ================================================================
    print("\n" + "=" * 70)
    print("Step 1: Scraping news...")
    print("=" * 70)
    
    # 新華社爬蟲
    try:
        print(f"\n[1.1] Scraping Xinhua Taiwan ({Config.XINHUA_URL})...")
        with XinhuaTWScraper() as scraper:
            xinhua_news = scraper.run(days_back=args.days)
        print(f"      Found {len(xinhua_news)} articles from Xinhua")
        all_news.extend(xinhua_news)
        log['steps'].append({'scraper': 'xinhua', 'count': len(xinhua_news), 'status': 'success'})
    except Exception as e:
        print(f"      ❌ Xinhua scraper error: {e}")
        log['steps'].append({'scraper': 'xinhua', 'error': str(e), 'status': 'failed'})
    
    # 中央社爬蟲
    try:
        print(f"\n[1.2] Scraping CNA Military ({Config.CNA_SEARCH_URL})...")
        with CNAScraper() as scraper:
            cna_news = scraper.run(days_back=args.days)
        print(f"      Found {len(cna_news)} articles from CNA")
        all_news.extend(cna_news)
        log['steps'].append({'scraper': 'cna', 'count': len(cna_news), 'status': 'success'})
    except Exception as e:
        print(f"      ❌ CNA scraper error: {e}")
        log['steps'].append({'scraper': 'cna', 'error': str(e), 'status': 'failed'})
    
    print(f"\n📊 Total news scraped: {len(all_news)}")
    
    if not all_news:
        print("\n⚠️  No news found. Exiting.")
        log['status'] = 'no_news'
        return log
    
    # 如果只爬取，保存原始數據並退出
    if args.scrape_only:
        raw_output = Path('data') / 'raw_news.json'
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        with open(raw_output, 'w', encoding='utf-8') as f:
            json.dump({
                'scraped_at': datetime.now().isoformat(),
                'total': len(all_news),
                'articles': all_news
            }, f, ensure_ascii=False, indent=2)
        print(f"\n✅ Raw news saved to {raw_output}")
        log['status'] = 'scrape_only_complete'
        return log
    
    # ================================================================
    # Step 2: LLM 分類和情緒分析
    # ================================================================
    print("\n" + "=" * 70)
    print("Step 2: Classifying with Grok API...")
    print("=" * 70)
    
    classified_news = []
    try:
        with GrokNewsClassifier(api_key=Config.GROK_API_KEY) as classifier:
            classified_news = classifier.classify_batch(all_news, delay=1.5)
            relevant_news = classifier.filter_relevant(classified_news)
        
        print(f"\n📊 Classification results:")
        print(f"   - Total processed: {len(classified_news)}")
        print(f"   - Relevant news: {len(relevant_news)}")
        
        # 統計類別分佈
        categories = {}
        for item in classified_news:
            cat = item.get('category', 'Unknown')
            categories[cat] = categories.get(cat, 0) + 1
        print(f"   - Categories: {categories}")
        
        # 統計情緒分佈
        sentiments = {'positive': 0, 'neutral': 0, 'negative': 0}
        for item in classified_news:
            label = item.get('sentiment_label', 'neutral')
            sentiments[label] = sentiments.get(label, 0) + 1
        print(f"   - Sentiments: {sentiments}")
        
        log['steps'].append({
            'classifier': 'grok',
            'total': len(classified_news),
            'relevant': len(relevant_news),
            'categories': categories,
            'sentiments': sentiments,
            'status': 'success'
        })
        
    except Exception as e:
        print(f"\n❌ Classification error: {e}")
        log['steps'].append({'classifier': 'grok', 'error': str(e), 'status': 'failed'})
        relevant_news = []
    
    if not relevant_news:
        print("\n⚠️  No relevant news found after classification.")
        log['status'] = 'no_relevant_news'
    
    # ================================================================
    # Step 3: 更新 CSV
    # ================================================================
    print("\n" + "=" * 70)
    print("Step 3: Updating CSV...")
    print("=" * 70)
    
    csv_path = args.output or str(Config.CSV_PATH)
    
    try:
        updater = CSVUpdater(csv_path)
        updated_count = updater.update_from_classified(classified_news)
        output_path = updater.save()
        
        stats = updater.get_stats()
        print(f"\n📊 CSV Update results:")
        print(f"   - Records updated: {updated_count}")
        print(f"   - Total rows: {stats['total_rows']}")
        print(f"   - Date range: {stats['date_range']}")
        
        log['steps'].append({
            'updater': 'csv',
            'updated': updated_count,
            'output': output_path,
            'stats': stats,
            'status': 'success'
        })
        
    except Exception as e:
        print(f"\n❌ CSV update error: {e}")
        log['steps'].append({'updater': 'csv', 'error': str(e), 'status': 'failed'})
        output_path = None
    
    # ================================================================
    # Step 4: 推送到 GitHub
    # ================================================================
    if not args.no_push and output_path and Config.ENABLE_GITHUB_PUSH:
        print("\n" + "=" * 70)
        print("Step 4: Pushing to GitHub...")
        print("=" * 70)
        
        try:
            github = GitHubUpdater('.')
            github.configure_git()
            
            # 保存日誌
            log_file = github.create_run_log(log)
            
            # 提交文件
            files_to_commit = [
                output_path,
                log_file
            ]
            
            success = github.commit_and_push(
                files_to_commit,
                message=f"Auto-update: {datetime.now().strftime('%Y-%m-%d')} ({updated_count} records)"
            )
            
            if success:
                print("\n✅ Successfully pushed to GitHub")
                log['steps'].append({'github': 'push', 'status': 'success'})
            else:
                print("\n⚠️  GitHub push failed")
                log['steps'].append({'github': 'push', 'status': 'failed'})
                
        except Exception as e:
            print(f"\n❌ GitHub error: {e}")
            log['steps'].append({'github': 'push', 'error': str(e), 'status': 'failed'})
    else:
        print("\n⏭️  Skipping GitHub push")
    
    # ================================================================
    # 完成
    # ================================================================
    log['status'] = 'completed'
    log['completed_at'] = datetime.now().isoformat()
    
    print("\n" + "=" * 70)
    print("✅ Update completed!")
    print(f"   Finished at: {log['completed_at']}")
    print("=" * 70)
    
    return log


if __name__ == "__main__":
    main()
