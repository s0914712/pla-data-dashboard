#!/usr/bin/env python3
"""
===============================================================================
微博爬蟲 / Weibo Scraper
===============================================================================

目標: https://m.weibo.cn/api/container/getIndex
用途: 爬取中國軍方微博帳號貼文（如東部戰區）

透過微博 Mobile API 抓取指定 UID 的貼文，需要有效的 Cookie。
Cookie 從環境變數 WEIBO_COOKIE 讀取。
"""

import os
import re
import time
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from .base_scraper import BaseScraper


# 預設追蹤的微博帳號 UID
DEFAULT_UIDS = {
    "eastern_theater": "7483054836",  # 東部戰區
}


class WeiboScraper(BaseScraper):
    """微博爬蟲（透過 Mobile API）"""

    API_BASE = "https://m.weibo.cn/api/container/getIndex"

    def __init__(self, uid: str = None, max_pages: int = 5):
        super().__init__(name="weibo", timeout=10, delay=2.0)
        self.uid = uid or os.environ.get(
            "WEIBO_TARGET_UID", DEFAULT_UIDS["eastern_theater"]
        )
        self.max_pages = max_pages
        self.cookie = os.environ.get("WEIBO_COOKIE", "")

        # 微博 Mobile API 專用 Headers
        self.api_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://m.weibo.cn/u/{self.uid}",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.cookie:
            self.api_headers["Cookie"] = self.cookie

    # ------------------------------------------------------------------
    # 微博日期解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_weibo_date(date_str: str) -> Optional[datetime]:
        """
        解析微博 API 回傳的各種日期格式

        格式範例:
        - "刚刚"
        - "X分钟前"
        - "X小时前"
        - "昨天 HH:MM"
        - "MM-DD"  (今年)
        - "Sat Feb 15 10:30:00 +0800 2026"  (完整格式)
        """
        if not date_str:
            return None

        now = datetime.now()

        # "刚刚"
        if "刚刚" in date_str:
            return now

        # "X分钟前"
        m = re.search(r"(\d+)\s*分[钟鐘]前", date_str)
        if m:
            return now - timedelta(minutes=int(m.group(1)))

        # "X小时前"
        m = re.search(r"(\d+)\s*小[时時]前", date_str)
        if m:
            return now - timedelta(hours=int(m.group(1)))

        # "昨天 HH:MM"
        m = re.search(r"昨天\s*(\d{1,2}):(\d{2})", date_str)
        if m:
            yesterday = now - timedelta(days=1)
            return yesterday.replace(
                hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
            )

        # "MM-DD" (今年)
        m = re.match(r"^(\d{1,2})-(\d{1,2})$", date_str.strip())
        if m:
            return datetime(now.year, int(m.group(1)), int(m.group(2)))

        # 完整英文格式: "Sat Feb 15 10:30:00 +0800 2026"
        try:
            return datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y").replace(
                tzinfo=None
            )
        except ValueError:
            pass

        # YYYY-MM-DD
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        return None

    # ------------------------------------------------------------------
    # HTML 清理
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_html(raw_text: str) -> str:
        """清除微博內容中的 HTML 標籤"""
        if not raw_text:
            return ""
        return re.sub(r"<[^>]+>", "", raw_text).strip()

    # ------------------------------------------------------------------
    # API 呼叫
    # ------------------------------------------------------------------
    def _get_container_id(self) -> str:
        """自動獲取用戶的微博貼文列表 Container ID"""
        params = {"type": "uid", "value": self.uid}
        try:
            resp = self.client.get(
                self.API_BASE, params=params, headers=self.api_headers
            )
            data = resp.json()
            if data.get("ok") == 1:
                tabs = data.get("data", {}).get("tabsInfo", {}).get("tabs", [])
                for tab in tabs:
                    if tab.get("tab_type") == "weibo" or tab.get(
                        "containerid", ""
                    ).startswith("107603"):
                        return tab["containerid"]
        except Exception as e:
            print(f"[{self.name}] Failed to get container ID: {e}")
        return f"107603{self.uid}"

    def _fetch_page_posts(self, container_id: str, page: int) -> List[Dict]:
        """抓取單頁微博貼文，回傳原始文章列表"""
        params = {
            "type": "uid",
            "value": self.uid,
            "containerid": container_id,
            "page": page,
        }
        try:
            resp = self.client.get(
                self.API_BASE,
                params=params,
                headers=self.api_headers,
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                print(f"[{self.name}] Page {page} HTTP {resp.status_code}")
                return []

            data = resp.json()
            if data.get("ok") != 1:
                msg = data.get("msg", "unknown")
                print(f"[{self.name}] Page {page} not ok: {msg}")
                return []

            cards = data.get("data", {}).get("cards", [])
            articles = []

            for card in cards:
                if card.get("card_type") not in (9, 89):
                    continue
                mblog = card.get("mblog")
                if not mblog:
                    continue

                raw_text = mblog.get("text", "")
                content = self._clean_html(raw_text)
                if not content:
                    continue

                created_at = mblog.get("created_at", "")
                date_obj = self._parse_weibo_date(created_at)
                date_str = date_obj.strftime("%Y-%m-%d") if date_obj else ""

                # 微博沒有標題，取前 50 字作為標題
                title = content[:50] + ("..." if len(content) > 50 else "")

                post_url = f"https://m.weibo.cn/detail/{mblog.get('id', '')}"

                # 提取影片連結
                video_url = ""
                page_info = mblog.get("page_info", {})
                if page_info and page_info.get("type") == "video":
                    media = page_info.get("media_info", {})
                    video_url = (
                        media.get("stream_url_hd")
                        or media.get("stream_url")
                        or ""
                    )

                articles.append({
                    "date": date_str,
                    "title": title,
                    "content": content,
                    "url": post_url,
                    "video_url": video_url,
                    "reposts_count": mblog.get("reposts_count", 0),
                    "comments_count": mblog.get("comments_count", 0),
                    "attitudes_count": mblog.get("attitudes_count", 0),
                })

            return articles

        except Exception as e:
            print(f"[{self.name}] Page {page} error: {e}")
            return []

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, days_back: int = 7) -> List[Dict]:
        """
        執行微博爬取流程

        Args:
            days_back: 爬取過去幾天的貼文

        Returns:
            標準格式的文章列表
        """
        if not self.cookie:
            print(f"[{self.name}] WEIBO_COOKIE not set, skipping")
            return []

        print(f"[{self.name}] Starting scrape for UID {self.uid}...")

        # 1. 取得 Container ID
        container_id = self._get_container_id()
        print(f"[{self.name}] Container ID: {container_id}")

        # 2. 逐頁抓取
        all_posts = []
        for page in range(1, self.max_pages + 1):
            print(f"[{self.name}] Fetching page {page}/{self.max_pages}...")
            posts = self._fetch_page_posts(container_id, page)
            if not posts:
                print(f"[{self.name}] No more posts at page {page}")
                break
            all_posts.extend(posts)

            # 隨機延遲避免被封
            sleep_time = random.uniform(self.delay, self.delay + 3)
            time.sleep(sleep_time)

        print(f"[{self.name}] Total posts fetched: {len(all_posts)}")

        # 3. 過濾日期範圍
        filtered = []
        for article in all_posts:
            date_obj = self.parse_date(article["date"])
            if date_obj and self.is_within_days(date_obj, days_back):
                filtered.append(article)

        print(f"[{self.name}] {len(filtered)} posts within {days_back} days")

        # 4. 轉換為標準格式
        return self.to_standard_format(filtered)


def test_scraper():
    """測試用（需設定 WEIBO_COOKIE 環境變數）"""
    scraper = WeiboScraper(max_pages=2)
    articles = scraper.run(days_back=7)
    print(f"\nFetched {len(articles)} articles:")
    for a in articles:
        print(f"  - {a['date']}: {a['title'][:40]}...")


if __name__ == "__main__":
    test_scraper()
