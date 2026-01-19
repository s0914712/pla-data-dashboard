#!/usr/bin/env python3
"""
===============================================================================
中央社軍事新聞爬蟲 / CNA Military News Scraper
===============================================================================

爬取來源:
- https://www.cna.com.tw/search/hysearchws.aspx?q=軍演
- https://www.cna.com.tw/search/hysearchws.aspx?q=軍艦
- https://www.cna.com.tw/search/hysearchws.aspx?q=台海
- https://www.cna.com.tw/search/hysearchws.aspx?q=國台辦

用途: 提取軍演、軍艦、美台互動、政治聲明等新聞
"""

import re
import json
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import quote
from .base_scraper import BaseScraper


class CNAScraper(BaseScraper):
    """中央社軍事新聞爬蟲"""
    
    BASE_URL = "https://www.cna.com.tw"
    SEARCH_URL = "https://www.cna.com.tw/search/hysearchws.aspx"
    
    # 搜索關鍵字
    KEYWORDS = ["軍演", "軍艦通過"]
    
    # 列表頁面 URL（兩岸新聞）
    LIST_URLS = [
        "https://www.cna.com.tw/list/acn.aspx",  # 兩岸新聞列表
    ]
    
    def __init__(self):
        super().__init__(name="cna", timeout=30, delay=1.5)
    
    def build_search_url(self, keyword: str, page: int = 1) -> str:
        """構建搜索 URL"""
        encoded_keyword = quote(keyword)
        # 加入更多參數確保獲取完整結果
        return f"{self.SEARCH_URL}?q={encoded_keyword}"
    
    def parse_search_results(self, content: str) -> List[Dict]:
        """
        解析搜索結果頁面
        
        支援 HTML 和 Markdown 格式
        """
        articles = []
        seen_urls = set()
        
        # 模式 1: Markdown 格式 (Claude web_fetch 輸出)
        # [標題](https://www.cna.com.tw/news/acn/202601170192.aspx)
        md_pattern = r'\[([^\]]+)\]\((https?://www\.cna\.com\.tw/news/[a-z]+/\d+\.aspx)\)'
        md_matches = re.findall(md_pattern, content)
        
        for title, url in md_matches:
            title = title.strip()
            # 過濾掉導航文字和太短的標題
            if url not in seen_urls and len(title) >= 8:
                # 排除非新聞連結
                if not any(x in title.lower() for x in ['下載', '訂閱', '更多', '首頁', 'app']):
                    seen_urls.add(url)
                    
                    # 從 URL 提取日期 (格式: /news/acn/202601170192.aspx)
                    date_match = re.search(r'/(\d{8})\d+\.aspx', url)
                    date_str = ''
                    if date_match:
                        date_raw = date_match.group(1)
                        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                    
                    articles.append({
                        'title': title,
                        'url': url,
                        'date': date_str,
                        'content': ''
                    })
        
        # 模式 2: HTML 格式 - 搜索結果列表
        # <a href="/news/aipl/202601170192.aspx">標題</a>
        html_pattern = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>([^<]+)</a>'
        html_matches = re.findall(html_pattern, content)
        
        for url, title in html_matches:
            title = title.strip()
            full_url = url if url.startswith('http') else f"{self.BASE_URL}{url}"
            
            if full_url not in seen_urls and len(title) >= 8:
                if not any(x in title.lower() for x in ['下載', '訂閱', '更多', '首頁', 'app']):
                    seen_urls.add(full_url)
                    
                    date_match = re.search(r'/(\d{8})\d+\.aspx', url)
                    date_str = ''
                    if date_match:
                        date_raw = date_match.group(1)
                        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                    
                    articles.append({
                        'title': title,
                        'url': full_url,
                        'date': date_str,
                        'content': ''
                    })
        
        # 模式 3: 列表頁面格式 (## 標題 + URL 組合)
        # ## 共軍通報美國2艘軍艦穿越台海　川習會後首次
        # 2026/01/17 19:56](/news/acn/202601170192.aspx)
        list_pattern = r'##\s*([^\n]+)\n[^\n]*\]\(/news/([a-z]+/\d+\.aspx)\)'
        list_matches = re.findall(list_pattern, content)
        
        for title, url_part in list_matches:
            title = title.strip()
            full_url = f"{self.BASE_URL}/news/{url_part}"
            
            if full_url not in seen_urls and len(title) >= 8:
                seen_urls.add(full_url)
                
                date_match = re.search(r'/(\d{8})\d+\.aspx', url_part)
                date_str = ''
                if date_match:
                    date_raw = date_match.group(1)
                    date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                
                articles.append({
                    'title': title,
                    'url': full_url,
                    'date': date_str,
                    'content': ''
                })
        
        print(f"[{self.name}] Parsed {len(articles)} articles from page")
        return articles
    
    def parse_article_page(self, content: str) -> str:
        """解析文章頁面，提取正文"""
        # 清理 HTML
        text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        
        # 嘗試找到 CNA 文章正文區域
        # CNA 文章通常在 <article> 或 class="paragraph" 中
        article_match = re.search(r'<article[^>]*>(.*?)</article>', text, re.DOTALL)
        if article_match:
            text = article_match.group(1)
        
        paragraph_match = re.search(r'class="paragraph"[^>]*>(.*?)</div>', text, re.DOTALL)
        if paragraph_match:
            text = paragraph_match.group(1)
        
        # 清理標籤
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        
        return text.strip()[:500] if text else ''
    
    def scrape_article(self, url: str) -> Optional[str]:
        """爬取單篇文章內容"""
        html = self.fetch_page(url)
        if html:
            return self.parse_article_page(html)
        return None
    
    def search_keyword(self, keyword: str, days_back: int = 7, max_pages: int = 3) -> List[Dict]:
        """
        搜索特定關鍵字的新聞
        
        Args:
            keyword: 搜索關鍵字
            days_back: 過濾天數
            max_pages: 最大搜索頁數
            
        Returns:
            文章列表
        """
        all_articles = []
        seen_urls = set()
        
        for page in range(1, max_pages + 1):
            url = self.build_search_url(keyword, page)
            html = self.fetch_page(url)
            
            if not html:
                break
            
            articles = self.parse_search_results(html)
            
            if not articles:
                break
            
            for article in articles:
                if article['url'] not in seen_urls:
                    date_obj = self.parse_date(article['date'])
                    if date_obj and self.is_within_days(date_obj, days_back):
                        seen_urls.add(article['url'])
                        all_articles.append(article)
        
        return all_articles
    
    def scrape_list_page(self, url: str, days_back: int = 7) -> List[Dict]:
        """
        爬取列表頁面（如 /list/acn.aspx）
        
        Args:
            url: 列表頁面 URL
            days_back: 過濾天數
            
        Returns:
            文章列表
        """
        articles = []
        html = self.fetch_page(url)
        
        if not html:
            return articles
        
        # 解析列表頁面的新聞連結
        parsed = self.parse_search_results(html)
        
        for article in parsed:
            date_obj = self.parse_date(article['date'])
            if date_obj and self.is_within_days(date_obj, days_back):
                articles.append(article)
        
        return articles
    
    def run(self, days_back: int = 7) -> List[Dict]:
        """
        執行完整爬取流程
        
        Args:
            days_back: 爬取過去幾天的新聞
            
        Returns:
            標準格式的文章列表
        """
        print(f"[{self.name}] Starting scrape...")
        
        all_articles = []
        seen_urls = set()
        
        # 1. 搜索關鍵字
        for keyword in self.KEYWORDS:
            print(f"[{self.name}] Searching: {keyword}")
            articles = self.search_keyword(keyword, days_back)
            
            for article in articles:
                if article['url'] not in seen_urls:
                    seen_urls.add(article['url'])
                    all_articles.append(article)
            
            print(f"[{self.name}] Found {len(articles)} articles for '{keyword}'")
        
        # 2. 爬取列表頁面（兩岸新聞）
        for list_url in self.LIST_URLS:
            print(f"[{self.name}] Scraping list page: {list_url}")
            articles = self.scrape_list_page(list_url, days_back)
            
            for article in articles:
                if article['url'] not in seen_urls:
                    seen_urls.add(article['url'])
                    all_articles.append(article)
            
            print(f"[{self.name}] Found {len(articles)} articles from list page")
        
        print(f"[{self.name}] Total unique articles: {len(all_articles)}")
        
        # 爬取文章內容
        for article in all_articles:
            content = self.scrape_article(article['url'])
            if content:
                article['content'] = content
        
        # 轉換為標準格式
        return self.to_standard_format(all_articles)


def test_with_sample_data():
    """使用範例數據測試"""
    # 使用硬編碼的範例數據（來自你提供的 cna_military_scraper.py）
    sample_news = [
        {
            "date": "2026-01-17",
            "title": "共軍通報美國2艘軍艦穿越台海　川習會後首次",
            "url": "https://www.cna.com.tw/news/acn/202601170192.aspx",
            "content": "共軍東部戰區今天通報，美國海軍普雷布爾號驅逐艦及狄爾號補給艦17日過航台灣海峽..."
        },
        {
            "date": "2026-01-17",
            "title": "共軍無人機侵入東沙島領空　宣稱正常飛行訓練",
            "url": "https://www.cna.com.tw/news/acn/202601170178.aspx",
            "content": "國防部今天表示，中共無人機侵入東沙島領空，屬高度挑釁行為..."
        },
    ]
    
    scraper = CNAScraper()
    standardized = scraper.to_standard_format(sample_news)
    
    print(f"Standardized {len(standardized)} articles:")
    for article in standardized:
        print(f"  - {article['date']}: {article['title'][:40]}...")
    
    return standardized


if __name__ == '__main__':
    test_with_sample_data()
