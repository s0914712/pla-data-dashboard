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
- https://www.cna.com.tw/list/acn.aspx (兩岸新聞列表)
- https://www.cna.com.tw/list/aipl.aspx (政治新聞列表)

用途: 提取軍演、軍艦、美台互動、政治聲明等新聞
"""

import re
import json
import httpx
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import quote
from .base_scraper import BaseScraper


class CNAScraper(BaseScraper):
    """中央社軍事新聞爬蟲"""
    
    BASE_URL = "https://www.cna.com.tw"
    SEARCH_URL = "https://www.cna.com.tw/search/hysearchws.aspx"
    
    # 擴展搜索關鍵字
    KEYWORDS = [
        "軍演", "軍艦", "台海", "國台辦", "解放軍", 
        "共軍", "軍事", "東部戰區"
    ]
    
    # 列表頁面 URL
    LIST_URLS = [
        "https://www.cna.com.tw/list/acn.aspx",   # 兩岸新聞列表
        "https://www.cna.com.tw/list/aipl.aspx",  # 政治新聞列表
    ]
    
    # CNA 專用請求頭
    CNA_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "Referer": "https://www.cna.com.tw/",
    }
    
    def __init__(self):
        super().__init__(name="cna", timeout=30, delay=2.0)
        # 重新建立 client 使用 CNA 專用頭
        self.client.close()
        self.client = httpx.Client(
            timeout=self.timeout,
            headers=self.CNA_HEADERS,
            follow_redirects=True
        )
    
    def build_search_url(self, keyword: str, page: int = 1) -> str:
        """構建搜索 URL"""
        encoded_keyword = quote(keyword)
        return f"{self.SEARCH_URL}?q={encoded_keyword}"
    
    def fetch_page(self, url: str, retries: int = 3) -> Optional[str]:
        """
        獲取網頁內容（覆寫父類方法，增加調試）
        """
        import time
        for attempt in range(retries):
            try:
                time.sleep(self.delay)
                response = self.client.get(url)
                response.raise_for_status()
                content = response.text
                
                # 調試: 顯示內容長度
                print(f"[{self.name}] Fetched {url[:60]}... ({len(content)} chars)")
                
                # 檢查是否被阻擋
                if len(content) < 1000:
                    print(f"[{self.name}] Warning: Response too short, might be blocked")
                    print(f"[{self.name}] Content preview: {content[:500]}")
                
                return content
                
            except httpx.HTTPError as e:
                print(f"[{self.name}] Attempt {attempt + 1} failed for {url}: {e}")
                if attempt < retries - 1:
                    time.sleep(self.delay * (attempt + 1))
        return None
    
    def parse_search_results(self, content: str) -> List[Dict]:
        """
        解析搜索結果頁面 - 修正版
        
        CNA 搜索結果 HTML 結構:
        <li>
          <a href="/news/acn/202601170192.aspx">
            <div class="listInfo">
              <h2>共軍通報美國2艘軍艦穿越台海 川習會後首次</h2>
              <div class="date">2026/01/17 19:56</div>
            </div>
          </a>
        </li>
        """
        articles = []
        seen_urls = set()
        
        # ============================================================
        # 模式 1: CNA 搜索結果 - listInfo 結構 (主要模式!)
        # ============================================================
        listinfo_pattern = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>.*?<div[^>]+class=["\']listInfo["\'][^>]*>.*?<h2[^>]*>([^<]+)</h2>.*?<div[^>]+class=["\']date["\'][^>]*>([^<]+)</div>'
        listinfo_matches = re.findall(listinfo_pattern, content, re.DOTALL)
        
        for url_part, title, date_text in listinfo_matches:
            title = title.strip()
            full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
            
            if full_url not in seen_urls and len(title) >= 5:
                seen_urls.add(full_url)
                date_str = self._extract_date_from_url(full_url)
                articles.append({
                    'title': title,
                    'url': full_url,
                    'date': date_str,
                    'date_text': date_text.strip(),
                    'content': ''
                })
        
        # ============================================================
        # 模式 2: <a> 內的 <h2> 標籤 (更寬鬆匹配)
        # ============================================================
        h2_pattern = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>[\s\S]*?<h2[^>]*>([^<]+)</h2>'
        h2_matches = re.findall(h2_pattern, content)
        
        for url_part, title in h2_matches:
            title = title.strip()
            full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
            
            if full_url not in seen_urls and len(title) >= 5:
                seen_urls.add(full_url)
                date_str = self._extract_date_from_url(full_url)
                articles.append({
                    'title': title,
                    'url': full_url,
                    'date': date_str,
                    'content': ''
                })
        
        # ============================================================
        # 模式 3: mainList 內的 <li> 項目
        # ============================================================
        main_list_match = re.search(r'<ul[^>]+class=["\']mainList["\'][^>]*>([\s\S]*?)</ul>', content)
        if main_list_match:
            main_list_content = main_list_match.group(1)
            li_pattern = r'<li[^>]*>([\s\S]*?)</li>'
            li_matches = re.findall(li_pattern, main_list_content)
            
            for li_content in li_matches:
                url_match = re.search(r'href=["\']([/]?news/[a-z]+/\d+\.aspx)["\']', li_content)
                h2_match = re.search(r'<h2[^>]*>([^<]+)</h2>', li_content)
                
                if url_match and h2_match:
                    url_part = url_match.group(1)
                    title = h2_match.group(1).strip()
                    full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
                    
                    if full_url not in seen_urls and len(title) >= 5:
                        seen_urls.add(full_url)
                        date_str = self._extract_date_from_url(full_url)
                        articles.append({
                            'title': title,
                            'url': full_url,
                            'date': date_str,
                            'content': ''
                        })
        
        # ============================================================
        # 模式 4: 備用 - Markdown 格式 (Claude web_fetch)
        # ============================================================
        md_pattern = r'\[([^\]]+)\]\((https?://www\.cna\.com\.tw/news/[a-z]+/\d+\.aspx)\)'
        md_matches = re.findall(md_pattern, content)
        
        for title, url in md_matches:
            title = title.strip()
            if url not in seen_urls and len(title) >= 8:
                if not any(x in title.lower() for x in ['下載', '訂閱', '更多', '首頁', 'app']):
                    seen_urls.add(url)
                    date_str = self._extract_date_from_url(url)
                    articles.append({
                        'title': title,
                        'url': url,
                        'date': date_str,
                        'content': ''
                    })
        
        # ============================================================
        # 模式 5: 備用 - 直接 <a> 文字 (最後手段)
        # ============================================================
        if len(articles) == 0:
            html_pattern = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>([^<]+)</a>'
            html_matches = re.findall(html_pattern, content)
            
            for url_part, title in html_matches:
                title = title.strip()
                full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
                
                if full_url not in seen_urls and len(title) >= 8:
                    if not any(x in title.lower() for x in ['下載', '訂閱', '更多', '首頁', 'app']):
                        seen_urls.add(full_url)
                        date_str = self._extract_date_from_url(full_url)
                        articles.append({
                            'title': title,
                            'url': full_url,
                            'date': date_str,
                            'content': ''
                        })
        
        # ============================================================
        # 模式 6: JSON 資料 (有些頁面可能嵌入 JSON)
        # ============================================================
        if len(articles) == 0:
            # 嘗試找 JSON 格式的新聞資料
            json_pattern = r'"HeadLine"\s*:\s*"([^"]+)"[^}]*"PageUrl"\s*:\s*"([^"]+)"'
            json_matches = re.findall(json_pattern, content)
            
            for title, url_part in json_matches:
                title = title.strip()
                if '/news/' in url_part:
                    full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
                    if full_url not in seen_urls and len(title) >= 5:
                        seen_urls.add(full_url)
                        date_str = self._extract_date_from_url(full_url)
                        articles.append({
                            'title': title,
                            'url': full_url,
                            'date': date_str,
                            'content': ''
                        })
        
        # ============================================================
        # 模式 7: 任何包含 /news/ 的連結 (最寬鬆)
        # ============================================================
        if len(articles) == 0:
            # 找所有新聞連結
            all_links = re.findall(r'href=["\']([^"\']*?/news/[a-z]+/\d+\.aspx)["\']', content)
            print(f"[{self.name}] Found {len(all_links)} news links in fallback mode")
            
            for url_part in all_links:
                full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    date_str = self._extract_date_from_url(full_url)
                    # 用 URL 中的 ID 作為臨時標題
                    articles.append({
                        'title': f"新聞 {url_part.split('/')[-1]}",
                        'url': full_url,
                        'date': date_str,
                        'content': '',
                        'needs_title': True  # 標記需要獲取真實標題
                    })
        
        print(f"[{self.name}] Parsed {len(articles)} articles from page")
        return articles
    
    def _extract_date_from_url(self, url: str) -> str:
        """從 URL 提取日期"""
        match = re.search(r'/(\d{8})\d+\.aspx', url)
        if match:
            date_raw = match.group(1)
            return f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        return ''
    
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
    
    def scrape_article(self, url: str) -> tuple:
        """
        爬取單篇文章內容和標題
        
        Returns:
            (content, title) 元組
        """
        html = self.fetch_page(url)
        if html:
            content = self.parse_article_page(html)
            
            # 提取標題
            title = ''
            title_match = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
            if title_match:
                title = title_match.group(1).strip()
            else:
                # 嘗試 og:title
                og_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
                if og_match:
                    title = og_match.group(1).strip()
            
            return content, title
        return '', ''
    
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
        
        # 爬取文章內容（並獲取缺失的標題）
        for article in all_articles:
            content, title = self.scrape_article(article['url'])
            if content:
                article['content'] = content
            # 如果需要獲取真實標題
            if article.get('needs_title') and title:
                article['title'] = title
                del article['needs_title']
        
        # 過濾掉沒有有效標題的文章
        valid_articles = [a for a in all_articles if not a.get('needs_title') and len(a.get('title', '')) >= 5]
        
        print(f"[{self.name}] Valid articles with titles: {len(valid_articles)}")
        
        # 轉換為標準格式
        return self.to_standard_format(valid_articles)


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
