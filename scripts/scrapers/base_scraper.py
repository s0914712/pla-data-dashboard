#!/usr/bin/env python3
"""
===============================================================================
基礎爬蟲類 / Base Scraper Class
===============================================================================
提供統一的請求處理、日期解析、輸出格式等功能
"""

import httpx
import time
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from abc import ABC, abstractmethod


class BaseScraper(ABC):
    """基礎爬蟲類"""
    
    # 常用 User-Agent
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]
    
    def __init__(self, name: str, timeout: int = 30, delay: float = 1.0):
        self.name = name
        self.timeout = timeout
        self.delay = delay
        self.client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": self.USER_AGENTS[0]},
            follow_redirects=True
        )
    
    def fetch_page(self, url: str, retries: int = 3) -> Optional[str]:
        """
        獲取網頁內容，帶重試機制
        
        Args:
            url: 目標 URL
            retries: 重試次數
            
        Returns:
            網頁內容或 None
        """
        for attempt in range(retries):
            try:
                time.sleep(self.delay)
                response = self.client.get(url)
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as e:
                print(f"[{self.name}] Attempt {attempt + 1} failed for {url}: {e}")
                if attempt < retries - 1:
                    time.sleep(self.delay * (attempt + 1))
        return None
    
    def parse_date(self, date_str: str) -> Optional[datetime]:
        """
        解析各種日期格式
        
        支持格式:
        - 2026-01-17
        - 2026/01/17
        - 2026年01月17日
        - 01/17/2026
        """
        if not date_str:
            return None
            
        date_str = date_str.strip()
        
        patterns = [
            (r'(\d{4})-(\d{1,2})-(\d{1,2})', '%Y-%m-%d'),
            (r'(\d{4})/(\d{1,2})/(\d{1,2})', '%Y/%m/%d'),
            (r'(\d{4})年(\d{1,2})月(\d{1,2})日', None),
            (r'(\d{1,2})/(\d{1,2})/(\d{4})', '%m/%d/%Y'),
        ]
        
        for pattern, fmt in patterns:
            match = re.search(pattern, date_str)
            if match:
                if fmt:
                    try:
                        # 重建日期字串
                        date_part = '-'.join(match.groups())
                        return datetime.strptime(date_part.replace('/', '-'), '%Y-%m-%d')
                    except ValueError:
                        continue
                else:
                    # 中文格式
                    try:
                        year, month, day = map(int, match.groups())
                        return datetime(year, month, day)
                    except ValueError:
                        continue
        
        return None
    
    def is_within_days(self, date: datetime, days_back: int) -> bool:
        """檢查日期是否在指定天數內"""
        if not date:
            return False
        cutoff = datetime.now() - timedelta(days=days_back)
        return date >= cutoff
    
    def to_standard_format(self, articles: List[Dict]) -> List[Dict]:
        """
        轉換為標準輸出格式
        
        標準格式:
        {
            'date': str (YYYY-MM-DD),
            'title': str,
            'content': str,
            'url': str,
            'source': str (xinhua/cna),
            'category': str (初始為空，待分類)
        }
        """
        standardized = []
        for article in articles:
            date_obj = self.parse_date(article.get('date', ''))
            std_article = {
                'date': date_obj.strftime('%Y-%m-%d') if date_obj else '',
                'title': article.get('title', '').strip(),
                'content': article.get('content', '').strip(),
                'url': article.get('url', ''),
                'source': self.name,
                'category': '',
                'sentiment': ''
            }
            if std_article['date'] and std_article['title']:
                standardized.append(std_article)
        return standardized
    
    @abstractmethod
    def run(self, days_back: int = 7) -> List[Dict]:
        """執行爬取（子類實現）"""
        pass
    
    def close(self):
        """關閉連接"""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
