python#!/usr/bin/env python3
import re
import httpx
import time
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import quote
from .base_scraper import BaseScraper

class CNAScraper(BaseScraper):
    """
    中央社軍事新聞爬蟲
    繼承自 BaseScraper，整合搜尋結果與分類列表爬取邏輯
    """
    
    BASE_URL = "https://www.cna.com.tw"
    SEARCH_URL = "https://www.cna.com.tw/search/hysearchws.aspx"
    
    # 擴展關鍵字以增加覆蓋率
    KEYWORDS = ["軍演", "軍艦", "台海", "國台辦", "解放軍", "共軍", "東部戰區"]
    
    # 常態性列表頁面
    LIST_URLS = [
        "https://www.cna.com.tw/list/acn.aspx",  # 兩岸
        "https://www.cna.com.tw/list/aipl.aspx", # 政治
    ]
    
    # 使用證實有效的標頭，避開壓縮編碼導致的亂碼問題
    CNA_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Referer": "https://www.cna.com.tw/",
    }

    def __init__(self, timeout: int = 30, delay: float = 1.5):
        # 初始化父類別，name 設為 "cna"
        super().__init__(name="cna", timeout=timeout, delay=delay)
        # 覆寫 client 標頭以符合 CNA 要求
        self.client.headers.update(self.CNA_HEADERS)

    def _extract_date_from_url(self, url: str) -> str:
        """從 URL 提取日期字串 (YYYY-MM-DD)"""
        match = re.search(r'/(\d{8})\d+\.aspx', url)
        if match:
            d = match.group(1)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return ""

    def parse_page_items(self, content: str) -> List[Dict]:
        """
        解析搜尋結果或列表頁面
        整合 listInfo 與 <a><h2> 多重結構
        """
        articles = []
        seen_urls = set()

        # 模式 1: 搜尋結果專用結構
        pattern_search = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>.*?<div[^>]+class=["\']listInfo["\'][^>]*>.*?<h2[^>]*>([^<]+)</h2>'
        
        # 模式 2: 列表頁通用結構
        pattern_list = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>\s*<h2[^>]*>([^<]+)</h2>\s*</a>'

        # 合併解析
        for pattern in [pattern_search, pattern_list]:
            matches = re.findall(pattern, content, re.DOTALL)
            for url_part, title in matches:
                full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
                if full_url not in seen_urls and len(title.strip()) >= 5:
                    seen_urls.add(full_url)
                    articles.append({
                        'title': title.strip(),
                        'url': full_url,
                        'date': self._extract_date_from_url(full_url)
                    })
        return articles

    def scrape_full_content(self, url: str) -> str:
        """爬取並解析單篇文章正文"""
        html = self.fetch_page(url)
        if not html:
            return ""
        
        # 鎖定內文容器
        paragraph_match = re.search(r'class="paragraph"[^>]*>(.*?)</div>', html, re.DOTALL)
        content = paragraph_match.group(1) if paragraph_match else ""
        
        if not content:
            article_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
            content = article_match.group(1) if article_match else ""

        # 清理標籤與多餘空格
        content = re.sub(r'<[^>]+>', ' ', content)
        content = re.sub(r'\s+', ' ', content)
        return content.strip()

    def run(self, days_back: int = 7) -> List[Dict]:
        """
        執行爬取主流程
        
        Returns:
            標準格式的新聞列表，每個元素包含:
            - date: str (YYYY-MM-DD)
            - title: str
            - content: str
            - url: str
            - source: str ('cna')
            - category: str (空字串，待分類)
            - sentiment: str (空字串，待分析)
        """
        print(f"[{self.name}] 正在開始任務，追蹤過去 {days_back} 天內容...")
        raw_articles = []
        collected_urls = set()

        # 1. 處理搜尋關鍵字
        for kw in self.KEYWORDS:
            search_url = f"{self.SEARCH_URL}?q={quote(kw)}"
            html = self.fetch_page(search_url)
            if html:
                items = self.parse_page_items(html)
                for item in items:
                    date_obj = self.parse_date(item['date'])
                    if date_obj and self.is_within_days(date_obj, days_back):
                        if item['url'] not in collected_urls:
                            collected_urls.add(item['url'])
                            raw_articles.append(item)

        # 2. 處理分類列表頁
        for list_url in self.LIST_URLS:
            html = self.fetch_page(list_url)
            if html:
                items = self.parse_page_items(html)
                for item in items:
                    date_obj = self.parse_date(item['date'])
                    if date_obj and self.is_within_days(date_obj, days_back):
                        if item['url'] not in collected_urls:
                            collected_urls.add(item['url'])
                            raw_articles.append(item)

        # 3. 爬取內文
        print(f"[{self.name}] 總計找到 {len(raw_articles)} 篇符合日期的潛在新聞，開始抓取內文...")
        for article in raw_articles:
            article['content'] = self.scrape_full_content(article['url'])

        # 4. 調用父類別方法轉換為標準格式
        standardized = self.to_standard_format(raw_articles)
        
        print(f"[{self.name}] 完成！成功爬取 {len(standardized)} 篇新聞")
        return standardized


if __name__ == "__main__":
    with CNAScraper(delay=1.0) as scraper:
        results = scraper.run(days_back=7)
        
        print(f"\n{'='*70}")
        print(f"總計爬取: {len(results)} 篇新聞")
        print(f"{'='*70}\n")
        
        for i, news in enumerate(results[:5], 1):  # 只顯示前 5 篇
            print(f"{i}. [{news['date']}] {news['title']}")
            print(f"   來源: {news['source']}")
            print(f"   URL: {news['url']}")
            print(f"   內文長度: {len(news['content'])} 字元")
            print(f"   Category: {news['category']}")
            print(f"   Sentiment: {news['sentiment']}")
            print()
