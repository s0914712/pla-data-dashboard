#!/usr/bin/env python3
import re
import httpx
import time
import random
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from urllib.parse import quote
from .base_scraper import BaseScraper

class CNAScraper(BaseScraper):
    """
    中央社軍事新聞爬蟲（含 SerpAPI 備援）
    當 CNA 網站限流時，自動切換到 Google News API
    """
    
    BASE_URL = "https://www.cna.com.tw"
    SEARCH_URL = "https://www.cna.com.tw/search/hysearchws.aspx"
    SERPAPI_URL = "https://serpapi.com/search.json"
    
    # 減少關鍵字數量
    KEYWORDS = ["共軍", "東部戰區", "台海"]
    
    # 常態性列表頁面
    LIST_URLS = [
        "https://www.cna.com.tw/list/acn.aspx",  # 兩岸
        "https://www.cna.com.tw/list/aipl.aspx", # 政治
    ]
    
    CNA_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Referer": "https://www.cna.com.tw/",
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }

    def __init__(self, timeout: int = 30, delay: float = 3.0):
        super().__init__(name="cna", timeout=timeout, delay=delay)
        self.client.headers.update(self.CNA_HEADERS)
        self.request_count = 0
        self.debug = True
        self.serpapi_key = os.environ.get('SERPAPI_KEY', '')
        self.use_serpapi_fallback = bool(self.serpapi_key)
        self.serpapi_used = False
        
        if self.use_serpapi_fallback:
            print(f"[{self.name}] ✓ SerpAPI 備援已啟用")
        else:
            print(f"[{self.name}] ⚠️  未設定 SERPAPI_KEY，無法使用備援")

    def _search_with_serpapi(self, query: str, days_back: int = 7) -> List[Dict]:
        """
        使用 SerpAPI Google News 搜尋
        
        Args:
            query: 搜尋關鍵字
            days_back: 天數範圍
            
        Returns:
            新聞列表
        """
        if not self.serpapi_key:
            print(f"[{self.name}] ✗ SerpAPI key 未設定")
            return []
        
        print(f"[{self.name}] 🔄 使用 SerpAPI 搜尋: {query}")
        
        try:
            # 構建搜尋參數
            params = {
                'engine': 'google_news',
                'q': f'{query} site:cna.com.tw',  # 限定中央社網站
                'api_key': self.serpapi_key,
                'gl': 'tw',  # 台灣
                'hl': 'zh-TW',  # 繁體中文
                'num': 20  # 返回 20 筆結果
            }
            
            response = httpx.get(self.SERPAPI_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            articles = []
            news_results = data.get('news_results', [])
            
            print(f"[{self.name}] 📥 SerpAPI 返回 {len(news_results)} 筆結果")
            
            for item in news_results:
                # 提取基本資訊
                title = item.get('title', '')
                link = item.get('link', '')
                
                # 只處理 CNA 的連結
                if 'cna.com.tw' not in link:
                    continue
                
                # 提取日期
                date_str = self._extract_date_from_url(link)
                if not date_str:
                    # 嘗試從 iso_date 解析
                    iso_date = item.get('date', {}).get('iso_date', '') if isinstance(item.get('date'), dict) else item.get('iso_date', '')
                    if iso_date:
                        try:
                            date_obj = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
                            date_str = date_obj.strftime('%Y-%m-%d')
                        except:
                            continue
                
                # 檢查日期範圍
                date_obj = self.parse_date(date_str)
                if not date_obj or not self.is_within_days(date_obj, days_back):
                    continue
                
                articles.append({
                    'title': title.strip(),
                    'url': link,
                    'date': date_str,
                    'source_method': 'serpapi'
                })
            
            print(f"[{self.name}] ✓ SerpAPI 找到 {len(articles)} 篇符合日期的新聞")
            self.serpapi_used = True
            return articles
            
        except Exception as e:
            print(f"[{self.name}] ✗ SerpAPI 錯誤: {e}")
            return []

    def fetch_page(self, url: str, retries: int = 3) -> Optional[str]:
        """
        獲取網頁內容（增強版，含 429 偵測）
        """
        for attempt in range(retries):
            try:
                self.request_count += 1
                
                # 每 5 個請求後加長延遲
                if self.request_count % 5 == 0:
                    extra_delay = random.uniform(2, 5)
                    if self.debug:
                        print(f"[{self.name}] 📊 已發送 {self.request_count} 個請求，額外延遲 {extra_delay:.1f}s")
                    time.sleep(extra_delay)
                
                # 基本延遲 + 隨機抖動
                delay = self.delay + random.uniform(0, 1)
                time.sleep(delay)
                
                if self.debug:
                    print(f"[{self.name}] 🌐 請求 #{self.request_count}: {url[:80]}...")
                
                response = self.client.get(url)
                
                # 檢查狀態碼
                if self.debug:
                    print(f"[{self.name}] 📥 狀態碼: {response.status_code}, 內容長度: {len(response.text)} 字元")
                
                # 429 限流偵測
                if response.status_code == 429:
                    print(f"[{self.name}] ⚠️  被限流 (429)！")
                    if attempt < retries - 1:
                        wait_time = (2 ** attempt) * 10
                        print(f"[{self.name}] ⏰ 等待 {wait_time}s 後重試...")
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"[{self.name}] ❌ 達到最大重試次數，將啟用 SerpAPI 備援")
                        return None  # 返回 None 觸發備援
                
                response.raise_for_status()
                
                # 檢查內容是否有效
                if len(response.text) < 100:
                    print(f"[{self.name}] ⚠️  回應內容過短 ({len(response.text)} 字元)")
                    if self.debug:
                        print(f"[{self.name}] 📄 內容預覽: {response.text[:200]}")
                
                return response.text
                
            except httpx.HTTPStatusError as e:
                print(f"[{self.name}] ❌ Attempt {attempt + 1}/{retries} - HTTP {e.response.status_code}")
                if self.debug and hasattr(e.response, 'text'):
                    print(f"[{self.name}] 📄 錯誤回應: {e.response.text[:300]}")
                
                if attempt < retries - 1:
                    wait_time = (2 ** attempt) * 5
                    time.sleep(wait_time)
            
            except Exception as e:
                print(f"[{self.name}] ❌ Attempt {attempt + 1}/{retries} - Error: {type(e).__name__}: {e}")
                if attempt < retries - 1:
                    time.sleep(self.delay * (attempt + 1))
        
        return None

    def _extract_date_from_url(self, url: str) -> str:
        """從 URL 提取日期字串 (YYYY-MM-DD)"""
        match = re.search(r'/(\d{8})\d+\.aspx', url)
        if match:
            d = match.group(1)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return ""

    def parse_page_items(self, content: str, source_type: str = "unknown") -> List[Dict]:
        """解析搜尋結果或列表頁面"""
        if self.debug:
            print(f"[{self.name}] 🔍 解析 {source_type} 頁面 ({len(content)} 字元)...")
        
        articles = []
        seen_urls = set()

        # 模式 1: 搜尋結果專用結構
        pattern_search = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>.*?<div[^>]+class=["\']listInfo["\'][^>]*>.*?<h2[^>]*>([^<]+)</h2>'
        
        # 模式 2: 列表頁通用結構
        pattern_list = r'<a[^>]+href=["\']([/]?news/[a-z]+/\d+\.aspx)["\'][^>]*>\s*<h2[^>]*>([^<]+)</h2>\s*</a>'

        for pattern_name, pattern in [("搜尋結構", pattern_search), ("列表結構", pattern_list)]:
            matches = re.findall(pattern, content, re.DOTALL)
            
            if self.debug and matches:
                print(f"[{self.name}] ✓ {pattern_name} 找到 {len(matches)} 個匹配")
            
            for url_part, title in matches:
                full_url = url_part if url_part.startswith('http') else f"{self.BASE_URL}{url_part}"
                if full_url not in seen_urls and len(title.strip()) >= 5:
                    seen_urls.add(full_url)
                    articles.append({
                        'title': title.strip(),
                        'url': full_url,
                        'date': self._extract_date_from_url(full_url),
                        'source_method': 'direct'
                    })
        
        if self.debug:
            print(f"[{self.name}] 📋 解析結果: {len(articles)} 篇文章")
        
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
        
        if self.debug and content:
            print(f"[{self.name}] 📄 內文長度: {len(content)} 字元")
        
        return content.strip()

    def run(self, days_back: int = 7, use_search: bool = False) -> List[Dict]:
        """
        執行爬取主流程（含 SerpAPI 備援）
        
        Args:
            days_back: 追蹤天數
            use_search: 是否使用關鍵字搜尋
        """
        print(f"[{self.name}] 🚀 開始任務，追蹤過去 {days_back} 天內容")
        print(f"[{self.name}] ⚙️  使用關鍵字搜尋: {'是' if use_search else '否'}")
        print(f"[{self.name}] ⏱️  請求延遲: {self.delay}s")
        
        raw_articles = []
        collected_urls = set()
        direct_fetch_failed = False

        # 1. 優先處理分類列表頁
        print(f"\n[{self.name}] 📰 處理分類列表頁...")
        for i, list_url in enumerate(self.LIST_URLS, 1):
            print(f"[{self.name}] [{i}/{len(self.LIST_URLS)}] {list_url}")
            html = self.fetch_page(list_url)
            
            if html:
                items = self.parse_page_items(html, source_type=f"列表頁 {i}")
                for item in items:
                    date_obj = self.parse_date(item['date'])
                    if date_obj and self.is_within_days(date_obj, days_back):
                        if item['url'] not in collected_urls:
                            collected_urls.add(item['url'])
                            raw_articles.append(item)
                print(f"[{self.name}] ✓ 累計收集: {len(raw_articles)} 篇")
            else:
                print(f"[{self.name}] ✗ 無法獲取列表頁")
                direct_fetch_failed = True

        # 2. 可選：處理搜尋關鍵字
        if use_search and not direct_fetch_failed:
            print(f"\n[{self.name}] 🔍 處理關鍵字搜尋...")
            for i, kw in enumerate(self.KEYWORDS, 1):
                search_url = f"{self.SEARCH_URL}?q={quote(kw)}"
                print(f"[{self.name}] [{i}/{len(self.KEYWORDS)}] 搜尋: {kw}")
                
                html = self.fetch_page(search_url)
                if html:
                    items = self.parse_page_items(html, source_type=f"搜尋:{kw}")
                    for item in items:
                        date_obj = self.parse_date(item['date'])
                        if date_obj and self.is_within_days(date_obj, days_back):
                            if item['url'] not in collected_urls:
                                collected_urls.add(item['url'])
                                raw_articles.append(item)
                    print(f"[{self.name}] ✓ 累計收集: {len(raw_articles)} 篇")
                else:
                    print(f"[{self.name}] ✗ 搜尋失敗: {kw}")
                    direct_fetch_failed = True
                    break

        # 3. 啟用 SerpAPI 備援（如果直接抓取失敗或結果太少）
        if (direct_fetch_failed or len(raw_articles) < 5) and self.use_serpapi_fallback:
            print(f"\n[{self.name}] 🔄 啟用 SerpAPI 備援...")
            
            for kw in self.KEYWORDS:
                serpapi_articles = self._search_with_serpapi(kw, days_back)
                for article in serpapi_articles:
                    if article['url'] not in collected_urls:
                        collected_urls.add(article['url'])
                        raw_articles.append(article)
            
            print(f"[{self.name}] ✓ SerpAPI 備援完成，總計: {len(raw_articles)} 篇")

        if not raw_articles:
            print(f"\n[{self.name}] ❌ 未找到任何新聞")
            return []

        # 4. 爬取內文
        print(f"\n[{self.name}] 📥 開始爬取 {len(raw_articles)} 篇文章內文...")
        for i, article in enumerate(raw_articles, 1):
            print(f"[{self.name}] [{i}/{len(raw_articles)}] {article['title'][:50]}...")
            article['content'] = self.scrape_full_content(article['url'])
            # 移除內部標記
            article.pop('source_method', None)

        # 5. 轉換為標準格式
        standardized = self.to_standard_format(raw_articles)
        
        print(f"\n[{self.name}] ✅ 完成！成功爬取 {len(standardized)} 篇新聞")
        print(f"[{self.name}] 📊 總請求數: {self.request_count}")
        if self.serpapi_used:
            print(f"[{self.name}] 🔄 已使用 SerpAPI 備援")
        
        return standardized


if __name__ == "__main__":
    print("=" * 70)
    print("CNA Scraper 測試模式（含 SerpAPI 備援）")
    print("=" * 70)
    
    with CNAScraper(delay=3.0) as scraper:
        # 只使用列表頁，不用搜尋（避免 429）
        results = scraper.run(days_back=7, use_search=False)
        
        print(f"\n{'='*70}")
        print(f"總計爬取: {len(results)} 篇新聞")
        print(f"{'='*70}\n")
        
        for i, news in enumerate(results[:5], 1):
            print(f"{i}. [{news['date']}] {news['title']}")
            print(f"   來源: {news['source']}")
            print(f"   內文: {len(news['content'])} 字元")
            print()
