#!/usr/bin/env python3
"""
===============================================================================
新華社台灣頻道爬蟲 / Xinhua Taiwan News Scraper
===============================================================================

目標: https://www.news.cn/tw/
用途: 爬取中國政府聲明 (CN Statement)

使用 crawl4ai + BeautifulSoup 進行網頁爬取與解析
"""

import asyncio
import re
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler
from .base_scraper import BaseScraper


MILITARY_KEYWORDS = [
    "軍演", "演習", "軍機", "架次", "逾越", "中線", "殲", "轟6",
    "航母", "海空聯訓", "戰備警巡", "聯合打擊", "飛彈", "導彈",
    "東部戰區", "解放軍", "共軍", "警巡", "軍艦", "戰艦",
]


class XinhuaTWScraper(BaseScraper):
    """新華社台灣頻道爬蟲（使用 crawl4ai + BeautifulSoup）"""

    BASE_URL = "https://www.news.cn"
    INDEX_URL = "https://www.news.cn/tw/"

    def __init__(self):
        super().__init__(name="xinhua", timeout=30, delay=1.5)

    async def _crawl_page(self, url: str) -> Optional[dict]:
        """
        使用 crawl4ai 抓取頁面，返回 html 和 markdown

        Returns:
            dict with 'html' and 'markdown' keys, or None on failure
        """
        try:
            async with AsyncWebCrawler(verbose=True) as crawler:
                result = await crawler.arun(url=url)
                return {"html": result.html, "markdown": result.markdown}
        except Exception as e:
            print(f"[{self.name}] crawl4ai failed for {url}: {e}")
            return None

    def parse_index_page_html(self, html: str) -> List[Dict]:
        """
        方法1: 從 HTML 用 BeautifulSoup 解析標題（更準確）
        """
        soup = BeautifulSoup(html, "html.parser")
        articles = []
        seen_urls = set()

        for link in soup.find_all("a", href=True):
            href = link["href"]
            title = link.get_text(strip=True)

            # 過濾掉空白、導航欄、太短的文字
            if not title or len(title) < 10:
                continue

            # 匹配新華社台灣頻道文章 URL 格式
            url_match = re.search(r"/tw/(\d{8})/([a-f0-9]+)/c\.html", href)
            if not url_match:
                continue

            full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            # 從 URL 提取日期
            date_raw = url_match.group(1)
            date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"

            articles.append({
                "title": title.strip(),
                "url": full_url,
                "date": date_str,
                "content": "",
            })

        return articles

    def parse_index_page_markdown(self, markdown: str) -> List[Dict]:
        """
        方法2: 從 Markdown 提取標題（備用）
        提取 [標題](URL) 格式的連結
        """
        articles = []
        seen_urls = set()

        md_pattern = r"\[([^\]]+)\]\((https?://[^\)]+/tw/\d{8}/[a-f0-9]+/c\.html)\)"
        matches = re.findall(md_pattern, markdown)

        for title, url in matches:
            if url in seen_urls or len(title) < 10:
                continue
            # 排除導航類文字
            if any(x in title for x in ["新华", "ENGLISH", "http", "www"]):
                continue

            seen_urls.add(url)

            date_match = re.search(r"/tw/(\d{8})/", url)
            date_str = ""
            if date_match:
                date_raw = date_match.group(1)
                date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"

            articles.append({
                "title": title.strip(),
                "url": url,
                "date": date_str,
                "content": "",
            })

        return articles

    def parse_index_page(self, content: str) -> List[Dict]:
        """
        解析首頁內容，提取文章列表

        保留向後相容：支援 HTML 和 Markdown 格式
        """
        articles = []
        seen_urls = set()

        # 嘗試 Markdown 格式
        md_pattern = r"\[([^\]]+)\]\((https?://[^\)]+/tw/\d{8}/[a-f0-9]+/c\.html)\)"
        md_matches = re.findall(md_pattern, content)

        if md_matches:
            for title, url in md_matches:
                if url not in seen_urls and len(title) >= 5:
                    seen_urls.add(url)
                    date_match = re.search(r"/tw/(\d{8})/", url)
                    date_str = ""
                    if date_match:
                        date_raw = date_match.group(1)
                        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                    articles.append({
                        "title": title.strip(),
                        "url": url,
                        "date": date_str,
                        "content": "",
                    })

        # 嘗試 HTML 格式
        html_pattern = r'<a[^>]+href=["\']([^"\']*?/tw/\d{8}/[a-f0-9]+/c\.html)["\'][^>]*>([^<]+)</a>'
        html_matches = re.findall(html_pattern, content)

        for url, title in html_matches:
            full_url = url if url.startswith("http") else f"{self.BASE_URL}{url}"
            if full_url not in seen_urls and len(title) >= 5:
                seen_urls.add(full_url)
                date_match = re.search(r"/tw/(\d{8})/", url)
                date_str = ""
                if date_match:
                    date_raw = date_match.group(1)
                    date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                articles.append({
                    "title": title.strip(),
                    "url": full_url,
                    "date": date_str,
                    "content": "",
                })

        return articles

    def parse_article_page(self, html: str) -> str:
        """
        解析文章頁面，使用 BeautifulSoup 提取正文
        """
        soup = BeautifulSoup(html, "html.parser")

        # 移除 script / style 標籤
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()

        # 嘗試找到新華社文章正文區域
        content_div = soup.find("div", id="detailContent") or soup.find(
            "div", class_="detail"
        )
        if content_div:
            return content_div.get_text(strip=True)

        # 回退：用全文提取
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text)

        # 新華社特徵匹配
        patterns = [
            r"新华社[^。]+。(.{100,2000}?)(?:【责任编辑|【纠错】|分享到)",
            r"據[^。]+報導[，,](.{100,2000}?)(?:【责任编辑|【纠错】|分享到)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()

        clean_text = text.strip()[:500]
        return clean_text if len(clean_text) > 50 else ""

    async def _async_run(self, days_back: int = 7) -> List[Dict]:
        """
        非同步執行完整爬取流程
        """
        print(f"[{self.name}] Starting scrape with crawl4ai...")

        # 1. 使用 crawl4ai 抓取首頁
        crawl_result = await self._crawl_page(self.INDEX_URL)
        if not crawl_result:
            print(f"[{self.name}] Failed to fetch index page")
            return []

        # 2. 用 BeautifulSoup 解析 HTML（主要方法）
        articles = self.parse_index_page_html(crawl_result["html"])
        print(f"[{self.name}] [HTML] Found {len(articles)} articles")

        # 3. 如果 HTML 解析結果不足，用 Markdown 補充
        if len(articles) < 5 and crawl_result["markdown"]:
            md_articles = self.parse_index_page_markdown(crawl_result["markdown"])
            seen_urls = {a["url"] for a in articles}
            for article in md_articles:
                if article["url"] not in seen_urls:
                    articles.append(article)
                    seen_urls.add(article["url"])
            print(
                f"[{self.name}] [HTML+Markdown] Total {len(articles)} articles after merge"
            )

        # 4. 過濾日期範圍內的文章
        filtered = []
        for article in articles:
            date_obj = self.parse_date(article["date"])
            if date_obj and self.is_within_days(date_obj, days_back):
                filtered.append(article)

        print(f"[{self.name}] {len(filtered)} articles within {days_back} days")

        # 5. 爬取文章內容
        for article in filtered:
            crawl_detail = await self._crawl_page(article["url"])
            if crawl_detail:
                content = self.parse_article_page(crawl_detail["html"])
                if content:
                    article["content"] = content

        # 6. 轉換為標準格式
        return self.to_standard_format(filtered)

    @staticmethod
    def apply_category_overrides(classified: list) -> list:
        """
        對新華社文章強制套用分類規則（在 AI 分類後執行）：
        - 含軍演/軍事關鍵字 → Military_Exercise (country1=CN), is_relevant=True
        - 其他 → CCP_news_and_blog, is_relevant=True
        """
        for article in classified:
            orig = article.get("original_article", {})
            if orig.get("source", "") != "xinhua":
                continue
            text = orig.get("title", "") + " " + orig.get("content", "")
            if any(kw in text for kw in MILITARY_KEYWORDS):
                article["category"] = "Military_Exercise"
                article["country1"] = article.get("country1") or "CN"
            else:
                article["category"] = "CCP_news_and_blog"
            article["is_relevant"] = True
        return classified

    def run(self, days_back: int = 7) -> List[Dict]:
        """
        執行完整爬取流程（同步入口）

        Args:
            days_back: 爬取過去幾天的新聞

        Returns:
            標準格式的文章列表
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 已在 async 環境中（如 Jupyter / Colab）
            import nest_asyncio

            nest_asyncio.apply()
            return loop.run_until_complete(self._async_run(days_back))
        else:
            return asyncio.run(self._async_run(days_back))


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


if __name__ == "__main__":
    test_parser()
