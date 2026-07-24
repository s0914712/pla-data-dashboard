#!/usr/bin/env python3
"""
===============================================================================
CNA 新聞爬蟲 (SerpAPI 版本)
===============================================================================
使用 Google News API (via SerpAPI) 來避開 CNA 網站的反爬蟲機制
"""

import httpx
import time
import os
import re
from datetime import datetime
from typing import List, Dict, Optional
from .base_scraper import BaseScraper


class CNAScraper(BaseScraper):
    """
    中央社軍事新聞爬蟲 (SerpAPI 版本)
    完全使用 SerpAPI Google News，避免 403/429 錯誤
    """
    
    BASE_URL = "https://www.cna.com.tw"
    SERPAPI_URL = "https://serpapi.com/search.json"
    
    # 搜尋關鍵字 - 精準聚焦台海軍事活動
    KEYWORDS = [
        "共機 site:cna.com.tw",           # 共軍飛機活動
        "美艦台海 site:cna.com.tw",        # 美艦通過台海
        "軍演 台海 site:cna.com.tw",       # 軍事演習
        "軍售台灣 site:cna.com.tw",        # 對台軍售
        "訪台 site:cna.com.tw",           # 官員訪台
        "東部戰區 site:cna.com.tw",       # 東部戰區公告
        "實彈射擊 site:cna.com.tw",        # 實彈射擊公告（海事局/航行警告）
        "航行警告 site:cna.com.tw",        # 海事局航行警告 / 禁航公告
        "禁航 海域 site:cna.com.tw",       # 劃設禁航區
    ]

    def __init__(self, timeout: int = 30, delay: float = 1.5):
        super().__init__(name="cna", timeout=timeout, delay=delay)
        self.serpapi_key = os.environ.get('SERPAPI_KEY', '')
        
        if not self.serpapi_key:
            raise ValueError("❌ 必須設定 SERPAPI_KEY 環境變數")
        
        print(f"[{self.name}] ✓ SerpAPI 已啟用")
        self.article_client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Accept-Language": "zh-TW,zh;q=0.9",
            },
            follow_redirects=True
        )

    def _extract_date_from_url(self, url: str) -> str:
        """從 URL 提取日期字串 (YYYY-MM-DD)"""
        match = re.search(r'/(\d{8})\d+\.aspx', url)
        if match:
            d = match.group(1)
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return ""

    def _is_relevant_title(self, title: str) -> bool:
        """
        檢查標題是否相關
        必須包含至少一個核心關鍵字
        """
        # 核心關鍵字 - 至少要有其中一個
        core_keywords = [
            '共機', '共艦', '共軍', '解放軍',
            '美艦', '美軍',
            '軍演', '演習', '戰備',
            '實彈射擊', '實彈', '射擊',      # 實彈射擊公告
            '航行警告', '禁航', '海事局',    # 海事局航行警告 / 禁航公告
            '軍售', '軍購',
            '訪台', '過境',
            '東部戰區', '南部戰區',
            '台海', '海峽',
            '國防部', '國防部長',
            '飛彈', '導彈',
            '戰機', '軍機',
            '航母', '艦隊',
        ]
        
        # 排除關鍵字 - 包含這些的直接排除
        exclude_keywords = [
            '股市', '股價', '匯率',
            '天氣', '氣象',
            '藝人', '明星', '電影',
            '演唱會', '音樂',
            '選舉', '投票',  # 除非有其他軍事關鍵字
            '疫情', 'COVID',
        ]
        
        title_lower = title.lower()
        
        # 先檢查排除關鍵字
        for exclude in exclude_keywords:
            if exclude in title:
                return False
        
        # 檢查是否包含核心關鍵字
        for keyword in core_keywords:
            if keyword in title:
                return True
        
        return False

    def _search_with_serpapi(self, query: str, days_back: int = 7) -> List[Dict]:
        """
        使用 SerpAPI Google News 搜尋
        
        Args:
            query: 搜尋關鍵字 (已包含 site:cna.com.tw)
            days_back: 天數範圍
            
        Returns:
            新聞列表
        """
        print(f"[{self.name}] 🔍 SerpAPI 搜尋: {query}")
        
        try:
            params = {
                'engine': 'google_news',
                'q': query,
                'api_key': self.serpapi_key,
                'gl': 'tw',  # 台灣
                'hl': 'zh-TW',  # 繁體中文
                'num': 30  # 每個關鍵字返回 30 筆
            }
            
            response = httpx.get(self.SERPAPI_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            articles = []
            news_results = data.get('news_results', [])
            
            total_results = len(news_results)
            filtered_count = 0
            
            print(f"[{self.name}] 📥 返回 {total_results} 筆結果")
            
            for item in news_results:
                # 提取基本資訊
                title = item.get('title', '')
                link = item.get('link', '')
                
                # 只處理 CNA 的連結
                if 'cna.com.tw' not in link:
                    continue
                
                # 🔍 標題相關性過濾
                if not self._is_relevant_title(title):
                    filtered_count += 1
                    continue
                
                # 提取日期
                date_str = self._extract_date_from_url(link)
                
                # 如果 URL 沒有日期，嘗試從 API 回傳的時間解析
                if not date_str:
                    # 嘗試多種日期格式
                    date_value = item.get('date', '')
                    
                    # 格式1: 字典格式 {'iso_date': '...'}
                    if isinstance(date_value, dict):
                        iso_date = date_value.get('iso_date', '')
                        if iso_date:
                            try:
                                date_obj = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
                                date_str = date_obj.strftime('%Y-%m-%d')
                            except:
                                pass
                    # 格式2: 直接是 ISO 字符串
                    elif isinstance(date_value, str) and date_value:
                        try:
                            date_obj = datetime.fromisoformat(date_value.replace('Z', '+00:00'))
                            date_str = date_obj.strftime('%Y-%m-%d')
                        except:
                            pass
                    
                    # 如果還是沒有，跳過
                    if not date_str:
                        continue
                
                # 檢查日期範圍
                date_obj = self.parse_date(date_str)
                if not date_obj or not self.is_within_days(date_obj, days_back):
                    continue
                
                articles.append({
                    'title': title.strip(),
                    'url': link,
                    'date': date_str
                })
            
            print(f"[{self.name}] ✓ 找到 {len(articles)} 篇相關新聞 (過濾掉 {filtered_count} 篇不相關)")
            time.sleep(self.delay)  # 避免 API 限流
            return articles
            
        except Exception as e:
            print(f"[{self.name}] ✗ SerpAPI 錯誤: {e}")
            return []

    def scrape_full_content(self, url: str) -> str:
        """
        爬取並解析單篇文章正文
        使用更寬鬆的錯誤處理
        """
        try:
            time.sleep(self.delay)
            response = self.article_client.get(url)
            
            # 即使 403 也嘗試解析（有些內容可能在錯誤頁面）
            if response.status_code == 403:
                print(f"[{self.name}] ⚠️  {url} 返回 403，嘗試提取標題")
                return f"[無法獲取完整內文，可能需要瀏覽器訪問]"
            
            response.raise_for_status()
            html = response.text

            # 鎖定內文區塊：從 class="paragraph" 起，至常見的頁尾/分享/相關新聞前止。
            # （舊版只抓到第一個 </div>，遇到段落間的相關新聞插入 <div> 會被截斷，
            #  導致像海事局航行警告的經緯度座標所在的後段落遺失。）
            start = html.find('class="paragraph"')
            region = html[start:] if start != -1 else html
            for marker in ('class="paragraphInfo"', 'class="shareBar"',
                           'class="social', 'id="stories"', 'class="relatedNews"',
                           'class="moreArticle"', '<footer'):
                mi = region.find(marker)
                if mi != -1:
                    region = region[:mi]
                    break

            # 擷取區塊內所有 <p> 段落文字並串接，保留完整內文（含座標段落）
            paras = re.findall(r'<p[^>]*>(.*?)</p>', region, re.DOTALL)
            if paras:
                content = " ".join(paras)
            else:
                # 退回：整段 paragraph 容器，或 <article>
                pm = re.search(r'class="paragraph"[^>]*>(.*?)</article>', html, re.DOTALL)
                if not pm:
                    pm = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
                content = pm.group(1) if pm else region

            # 清理標籤與多餘空格
            content = re.sub(r'<[^>]+>', ' ', content)
            content = re.sub(r'\s+', ' ', content)

            return content.strip() if content else "[內文提取失敗]"
            
        except Exception as e:
            print(f"[{self.name}] ✗ 內文抓取錯誤 ({url}): {e}")
            return "[內文提取失敗]"

    def run(self, days_back: int = 7) -> List[Dict]:
        """
        執行爬取主流程（完全使用 SerpAPI）
        
        Args:
            days_back: 追蹤天數
            
        Returns:
            標準格式新聞列表
        """
        print(f"[{self.name}] 🚀 開始任務 (SerpAPI 模式)")
        print(f"[{self.name}] 📅 追蹤過去 {days_back} 天內容")
        print(f"[{self.name}] 🔑 使用 {len(self.KEYWORDS)} 個搜尋關鍵字")
        
        raw_articles = []
        collected_urls = set()

        # 使用 SerpAPI 搜尋所有關鍵字
        for i, query in enumerate(self.KEYWORDS, 1):
            print(f"\n[{self.name}] [{i}/{len(self.KEYWORDS)}] 處理關鍵字...")
            articles = self._search_with_serpapi(query, days_back)
            
            for article in articles:
                if article['url'] not in collected_urls:
                    collected_urls.add(article['url'])
                    raw_articles.append(article)
            
            print(f"[{self.name}] ✓ 累計收集: {len(raw_articles)} 篇（去重後）")

        if not raw_articles:
            print(f"\n[{self.name}] ❌ 未找到任何新聞")
            return []

        # 爬取內文
        print(f"\n[{self.name}] 📥 開始爬取 {len(raw_articles)} 篇文章內文...")
        success_count = 0
        
        for i, article in enumerate(raw_articles, 1):
            print(f"[{self.name}] [{i}/{len(raw_articles)}] {article['title'][:50]}...")
            article['content'] = self.scrape_full_content(article['url'])
            
            if article['content'] and "[內文提取失敗]" not in article['content']:
                success_count += 1

        # 轉換為標準格式
        standardized = self.to_standard_format(raw_articles)
        
        print(f"\n[{self.name}] ✅ 完成！")
        print(f"[{self.name}] 📊 總計: {len(standardized)} 篇新聞")
        print(f"[{self.name}] 📄 內文成功: {success_count}/{len(raw_articles)} 篇")
        
        return standardized

    def close(self):
        """關閉連接"""
        super().close()
        self.article_client.close()


if __name__ == "__main__":
    import sys
    
    # 檢查環境變數
    if not os.environ.get('SERPAPI_KEY'):
        print("❌ 錯誤: 請設定 SERPAPI_KEY 環境變數")
        print("   export SERPAPI_KEY='your_api_key'")
        sys.exit(1)
    
    print("=" * 70)
    print("CNA Scraper 測試模式 (SerpAPI)")
    print("=" * 70)
    
    try:
        with CNAScraper(delay=1.5) as scraper:
            results = scraper.run(days_back=3)
            
            print(f"\n{'='*70}")
            print(f"總計爬取: {len(results)} 篇新聞")
            print(f"{'='*70}\n")
            
            for i, news in enumerate(results[:5], 1):
                print(f"{i}. [{news['date']}] {news['title']}")
                print(f"   來源: {news['source']}")
                print(f"   URL: {news['url']}")
                print(f"   內文長度: {len(news.get('content', ''))} 字元")
                print()
    
    except ValueError as e:
        print(f"\n{e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
