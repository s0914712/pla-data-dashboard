#!/usr/bin/env python3
"""
===============================================================================
新華社台灣頻道爬蟲 / Xinhua Taiwan News Scraper
===============================================================================

目標: https://www.news.cn/tw/index.html
用途: 爬取中國政府聲明 (CN Statement)
"""

import re
import json
from datetime import datetime
from typing import List, Dict, Optional
from .base_scraper import BaseScraper


class XinhuaTWScraper(BaseScraper):
    """新華社台灣頻道爬蟲"""
    
    BASE_URL = "https://www.news.cn"
    INDEX_URL = "https://www.news.cn/tw/index.html"
    
    def __init__(self):
        super().__init__(name="xinhua", timeout=30, delay=1.5)
    
    def parse_index_page(self, content: str) -> List[Dict]:
        """
        解析首頁內容，提取文章列表
        
        支援 HTML 和 Markdown 格式
        """
        articles = []
        seen_urls = set()
        
        # 嘗試 Markdown 格式 (Claude web_fetch 輸出)
        md_pattern = r'\[([^\]]+)\]\((https?://[^\)]+/tw/\d{8}/[a-f0-9]+/c\.html)\)'
        md_matches = re.findall(md_pattern, content)
        
        if md_matches:
            for title, url in md_matches:
                if url not in seen_urls and len(title) >= 5:
                    seen_urls.add(url)
                    # 從 URL 提取日期
                    date_match = re.search(r'/tw/(\d{8})/', url)
                    date_str = ''
                    if date_match:
                        date_raw = date_match.group(1)
                        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                    
                    articles.append({
                        'title': title.strip(),
                        'url': url,
                        'date': date_str,
                        'content': ''
                    })
        
        # 嘗試 HTML 格式
        html_pattern = r'<a[^>]+href=["\']([^"\']*?/tw/\d{8}/[a-f0-9]+/c\.html)["\'][^>]*>([^<]+)</a>'
        html_matches = re.findall(html_pattern, content)
        
        for url, title in html_matches:
            full_url = url if url.startswith('http') else f"{self.BASE_URL}{url}"
            if full_url not in seen_urls and len(title) >= 5:
                seen_urls.add(full_url)
                date_match = re.search(r'/tw/(\d{8})/', url)
                date_str = ''
                if date_match:
                    date_raw = date_match.group(1)
                    date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                
                articles.append({
                    'title': title.strip(),
                    'url': full_url,
                    'date': date_str,
                    'content': ''
                })
        
        return articles
    
    def parse_article_page(self, content: str) -> str:
        """
        解析文章頁面，提取正文
        """
        # 移除 HTML 標籤（簡化處理）
        text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        
        # 提取主要內容（新華社特徵）
        # 尋找新聞正文區域
        patterns = [
            r'新华社[^。]+。(.{100,2000}?)(?:【责任编辑|【纠错】|分享到)',
            r'據[^。]+報導[，,](.{100,2000}?)(?:【责任编辑|【纠错】|分享到)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()
        
        # 如果無法匹配，返回清理後的前500字
        clean_text = text.strip()[:500]
        return clean_text if len(clean_text) > 50 else ''
    
    def scrape_article(self, url: str) -> Optional[str]:
        """爬取單篇文章內容"""
        html = self.fetch_page(url)
        if html:
            return self.parse_article_page(html)
        return None
    
    def run(self, days_back: int = 7) -> List[Dict]:
        """
        執行完整爬取流程
        
        Args:
            days_back: 爬取過去幾天的新聞
            
        Returns:
            標準格式的文章列表
        """
        print(f"[{self.name}] Starting scrape...")
        
        # 1. 獲取首頁
        index_html = self.fetch_page(self.INDEX_URL)
        if not index_html:
            print(f"[{self.name}] Failed to fetch index page")
            return []
        
        # 2. 解析文章列表
        articles = self.parse_index_page(index_html)
        print(f"[{self.name}] Found {len(articles)} articles on index page")
        
        # 3. 過濾日期範圍內的文章
        filtered = []
        for article in articles:
            date_obj = self.parse_date(article['date'])
            if date_obj and self.is_within_days(date_obj, days_back):
                filtered.append(article)
        
        print(f"[{self.name}] {len(filtered)} articles within {days_back} days")
        
        # 4. 爬取文章內容（可選，取決於需求）
        for article in filtered:
            content = self.scrape_article(article['url'])
            if content:
                article['content'] = content
        
        # 5. 轉換為標準格式
        return self.to_standard_format(filtered)


def test_parser():
    """測試解析器"""
    sample_content = """
[外交部：坚决反对建交国与中国台湾地区商签任何具有主权意涵和官方性质的协定](https://www.news.cn/tw/20260116/fea9b01ed84c450995add48209ef33bf/c.html)

针对美国和中国台湾地区日前达成贸易协议削减台对美半导体出口关税，外交部发言人郭嘉昆1月16日在例行记者会上说，中方一贯坚决反对建交国与中国台湾地区商签任何具有主权意涵和官方性质的协定。

2026-01-16

[国台办：民进党当局改变不了"台独"必然败亡的下场](https://www.news.cn/tw/20260114/5f0dde6951e041cbae45bb4726d78a9b/c.html)

国务院台办发言人朱凤莲14日在例行新闻发布会上表示，世界上只有一个中国，台湾是中国的一部分。

2026-01-14
    """
    
    scraper = XinhuaTWScraper()
    articles = scraper.parse_index_page(sample_content)
    
    print(f"Parsed {len(articles)} articles:")
    for article in articles:
        print(f"  - {article['date']}: {article['title'][:30]}...")
    
    return articles


if __name__ == '__main__':
    test_parser()
