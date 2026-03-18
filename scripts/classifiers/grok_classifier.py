#!/usr/bin/env python3
"""
===============================================================================
Grok API 新聞分類器 / Grok News Classifier
===============================================================================

使用 Grok API (via apertis.ai) 進行新聞分類和情緒分析
支援 GDELT 風格的情緒分數 (-1 到 +1)
"""

import json
import hashlib
import re
import httpx
import time
from typing import List, Dict, Optional
from .prompts import (
    CLASSIFICATION_SYSTEM_PROMPT,
    CLASSIFICATION_USER_TEMPLATE,
    DEDUP_SYSTEM_PROMPT,
    DEDUP_USER_TEMPLATE,
)

# 合法的分類類別（用於驗證 LLM 輸出）
VALID_CATEGORIES = {
    "CN_Statement", "US_Statement", "TW_Statement",
    "Military_Exercise", "Foreign_battleship", "US_TW_Interaction",
    "Regional_Security", "CCP_news_and_blog", "Not_Relevant",
}


class GrokNewsClassifier:
    """使用 Grok API 進行新聞分類和情緒分析"""

    # 可重試的 HTTP 狀態碼
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    # Primary model：失敗 3 次後切換 Fallback
    PRIMARY_MAX_RETRIES = 3
    PRIMARY_RETRY_DELAYS = [10, 30, 60]       # 退避時間（秒）
    # Fallback model
    FALLBACK_MODEL = "gemini-2.5-flash"
    FALLBACK_MAX_RETRIES = 2
    FALLBACK_RETRY_DELAYS = [15, 30]
    
    def __init__(self, api_key: str, max_content_chars: int = 800,
                 enable_cache: bool = True):
        self.api_key = api_key.strip()
        self.api_url = "https://api.apertis.ai/v1/chat/completions"
        self.model = "gemini-2.0-flash-thinking-exp-0121:free"
        self.max_content_chars = max_content_chars
        # trust_env=False 防止 CI/CD 環境代理設定干擾
        self.client = httpx.Client(timeout=60, trust_env=False)
        self._debug_printed = False
        # 自適應延遲：記錄上次 rate-limit 建議的等待時間
        self._last_rate_limit_delay: float = 0.0
        # 分類結果快取（以文章內容 hash 為 key）
        self._cache: Dict[str, Dict] = {} if enable_cache else None

        # 初始化時印出診斷資訊
        masked_key = self.api_key[:8] + "..." + self.api_key[-4:] if len(self.api_key) > 12 else "***"
        print(f"[GrokClassifier] ========== 診斷資訊 ==========")
        print(f"[GrokClassifier] API URL      : {self.api_url}")
        print(f"[GrokClassifier] Primary Model: {self.model} (max {self.PRIMARY_MAX_RETRIES} retries)")
        print(f"[GrokClassifier] Fallback Model: {self.FALLBACK_MODEL} (after primary fails, max {self.FALLBACK_MAX_RETRIES} retries)")
        print(f"[GrokClassifier] API Key      : {masked_key} (length={len(self.api_key)})")
        print(f"[GrokClassifier] Content chars: {self.max_content_chars}")
        print(f"[GrokClassifier] Cache        : {'enabled' if self._cache is not None else 'disabled'}")
        print(f"[GrokClassifier] ================================")
    
    @staticmethod
    def _extract_json_text(raw: str) -> str:
        """從可能包含 markdown code block 的回應中提取 JSON 文字"""
        if '```json' in raw:
            m = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
            if m:
                return m.group(1)
        elif '```' in raw:
            m = re.search(r'```\s*(.*?)\s*```', raw, re.DOTALL)
            if m:
                return m.group(1)
        return raw

    def _try_model(self, messages: List[Dict], model: str,
                   max_retries: int, retry_delays: List[int]) -> Optional[str]:
        """
        以指定模型嘗試呼叫 API，含退避重試。

        Returns:
            成功時返回回應文字，全部重試失敗返回 None。
        """
        for attempt in range(1, max_retries + 1):
            try:
                response = self.client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.1
                    }
                )

                # 成功
                if response.status_code == 200:
                    return response.json()["choices"][0]["message"]["content"]

                # 可重試的錯誤
                if response.status_code in self.RETRYABLE_STATUS_CODES:
                    retry_after = None
                    if response.status_code == 429:
                        retry_after = response.headers.get('Retry-After')

                    if retry_after:
                        try:
                            wait_time = int(float(retry_after))
                        except (ValueError, TypeError):
                            wait_time = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                    else:
                        wait_time = retry_delays[min(attempt - 1, len(retry_delays) - 1)]

                    # 記錄 rate-limit 延遲供 classify_batch 自適應使用
                    if response.status_code == 429:
                        self._last_rate_limit_delay = max(self._last_rate_limit_delay, wait_time)

                    print(f"[GrokClassifier] ⚠️  [{model}] HTTP {response.status_code} "
                          f"(attempt {attempt}/{max_retries})")
                    print(f"[GrokClassifier]    Body: {response.text[:200]}")
                    if attempt < max_retries:
                        if retry_after:
                            print(f"[GrokClassifier]    Retry-After: {retry_after}s，等待 {wait_time}s 後重試...")
                        else:
                            print(f"[GrokClassifier]    等待 {wait_time}s 後重試...")
                        time.sleep(wait_time)
                        continue
                    return None

                # 不可重試的錯誤 (401, 403, 400 等)
                print(f"[GrokClassifier] ❌ [{model}] HTTP {response.status_code} (不可重試)")
                print(f"[GrokClassifier]    Body: {response.text[:300]}")
                if response.status_code in (401, 403):
                    print(f"[GrokClassifier]    🔑 請檢查 API Key 是否正確、是否過期")
                return None

            except httpx.ConnectError as e:
                wait_time = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                print(f"[GrokClassifier] ⚠️  [{model}] 連線失敗 (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    print(f"[GrokClassifier]    等待 {wait_time}s 後重試...")
                    time.sleep(wait_time)
                    continue
                return None

            except httpx.TimeoutException as e:
                wait_time = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                print(f"[GrokClassifier] ⚠️  [{model}] 請求超時 (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    print(f"[GrokClassifier]    等待 {wait_time}s 後重試...")
                    time.sleep(wait_time)
                    continue
                return None

            except Exception as e:
                print(f"[GrokClassifier] ❌ [{model}] 未預期錯誤: {type(e).__name__}: {e}")
                return None

        return None

    def _call_api(self, messages: List[Dict]) -> Optional[str]:
        """
        調用 API：Primary model 失敗 3 次後自動切換 Fallback model。

        Returns:
            API 回應內容，失敗時返回 None。
        """
        # Phase 1: primary model
        result = self._try_model(
            messages, self.model,
            self.PRIMARY_MAX_RETRIES, self.PRIMARY_RETRY_DELAYS
        )
        if result is not None:
            return result

        # Phase 2: fallback model
        print(f"[GrokClassifier] ⚠️  Primary model failed {self.PRIMARY_MAX_RETRIES} times, "
              f"switching to fallback: {self.FALLBACK_MODEL}")
        result = self._try_model(
            messages, self.FALLBACK_MODEL,
            self.FALLBACK_MAX_RETRIES, self.FALLBACK_RETRY_DELAYS
        )
        if result is None:
            print(f"[GrokClassifier] ❌ Fallback model also failed, giving up.")
        return result
    
    def classify_single(self, article: Dict) -> Dict:
        """
        分類單篇新聞並進行情緒分析

        Args:
            article: 新聞字典 {date, title, content, url, source}

        Returns:
            分類結果 {
                category, is_relevant, sentiment_score, sentiment_label,
                extracted_data, confidence
            }
        """
        title = article.get('title', '')
        content = article.get('content', '')[:self.max_content_chars]

        # 快取檢查
        cache_key = None
        if self._cache is not None:
            cache_key = hashlib.sha256(
                (title + content).encode('utf-8', errors='replace')
            ).hexdigest()
            if cache_key in self._cache:
                cached = self._cache[cache_key].copy()
                cached['original_article'] = article
                return cached

        # 構建用戶消息
        user_message = CLASSIFICATION_USER_TEMPLATE.format(
            title=title,
            content=content,
            date=article.get('date', ''),
            source=article.get('source', '')
        )

        messages = [
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]

        response = self._call_api(messages)

        if not response:
            return self._default_result(article)

        result = self._parse_response(response, article)

        # 寫入快取
        if cache_key is not None:
            self._cache[cache_key] = result

        return result
    
    def _parse_response(self, response: str, article: Dict) -> Dict:
        """解析 API 回應"""
        try:
            json_text = self._extract_json_text(response)
            result = json.loads(json_text)

            # 驗證 category 是否合法
            category = result.get('category', 'Not_Relevant')
            if category not in VALID_CATEGORIES:
                print(f"[GrokClassifier] ⚠️  Unexpected category '{category}', "
                      f"falling back to Not_Relevant")
                category = 'Not_Relevant'

            return {
                'category': category,
                'is_relevant': result.get('is_relevant', False),
                'country1': result.get('country1', ''),
                'country2': result.get('country2', ''),
                'sentiment_score': float(result.get('sentiment_score', 0)),
                'sentiment_label': result.get('sentiment_label', 'neutral'),
                'extracted_data': result.get('extracted_data', {}),
                'confidence': float(result.get('confidence', 0.5)),
                'original_article': article
            }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"[GrokClassifier] Parse error: {e}")
            return self._default_result(article)
    
    def _default_result(self, article: Dict) -> Dict:
        """返回默認結果（解析失敗時使用）"""
        return {
            'category': 'Not_Relevant',
            'is_relevant': False,
            'country1': '',
            'country2': '',
            'sentiment_score': 0.0,
            'sentiment_label': 'neutral',
            'extracted_data': {},
            'confidence': 0.0,
            'original_article': article
        }
    
    # ------------------------------------------------------------------
    # 去重 / Deduplication
    # ------------------------------------------------------------------
    def _build_dedup_list(self, articles: List[Dict]) -> str:
        """將文章列表格式化為去重 Prompt 用的文字"""
        lines = []
        for i, a in enumerate(articles):
            title = a.get("title", "")[:60]
            content = a.get("content", "")[:80]
            source = a.get("source", "")
            date = a.get("date", "")
            lines.append(f"[{i}] ({source} {date}) {title} — {content}")
        return "\n".join(lines)

    def deduplicate_batch(
        self, articles: List[Dict], batch_size: int = 30
    ) -> List[Dict]:
        """
        使用 LLM 識別並移除重複/高度相似的文章。

        將文章分批送給 LLM（每批 batch_size 篇），
        LLM 回傳重複組，每組保留第一篇（最完整的），其餘剔除。

        Args:
            articles: 原始文章列表
            batch_size: 每批送給 LLM 的文章數

        Returns:
            去重後的文章列表
        """
        if len(articles) <= 1:
            return articles

        print(f"[GrokClassifier] 開始去重，共 {len(articles)} 篇文章...")

        # 先做 URL 完全比對去重
        seen_urls = set()
        url_deduped = []
        for a in articles:
            url = a.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            url_deduped.append(a)

        removed_by_url = len(articles) - len(url_deduped)
        if removed_by_url:
            print(f"[GrokClassifier]   URL 完全比對去重: 移除 {removed_by_url} 篇")

        # LLM 語義去重（分批處理）
        all_keep_indices = set(range(len(url_deduped)))

        for start in range(0, len(url_deduped), batch_size):
            batch = url_deduped[start : start + batch_size]
            if len(batch) <= 1:
                continue

            article_list_text = self._build_dedup_list(batch)
            user_msg = DEDUP_USER_TEMPLATE.format(
                count=len(batch), article_list=article_list_text
            )

            messages = [
                {"role": "system", "content": DEDUP_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ]

            response = self._call_api(messages)
            if not response:
                print(f"[GrokClassifier]   批次 {start}-{start+len(batch)-1}: LLM 無回應，跳過")
                continue

            # 解析 LLM 回應
            try:
                text = self._extract_json_text(response)
                result = json.loads(text)
                groups = result.get("groups", [])

                batch_removed = 0
                for group in groups:
                    if not isinstance(group, list) or len(group) < 2:
                        continue
                    # group[0] 保留，其餘剔除
                    for idx in group[1:]:
                        global_idx = start + idx
                        if 0 <= idx < len(batch) and global_idx in all_keep_indices:
                            all_keep_indices.discard(global_idx)
                            batch_removed += 1

                if batch_removed:
                    print(
                        f"[GrokClassifier]   批次 {start}-{start+len(batch)-1}: "
                        f"發現 {len(groups)} 組重複，移除 {batch_removed} 篇"
                    )

            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"[GrokClassifier]   批次去重解析失敗: {e}")

        deduped = [url_deduped[i] for i in sorted(all_keep_indices)]
        total_removed = len(articles) - len(deduped)
        print(
            f"[GrokClassifier] 去重完成: {len(articles)} → {len(deduped)} "
            f"(移除 {total_removed} 篇重複)"
        )
        return deduped

    def classify_batch(self, articles: List[Dict], delay: float = 1.0) -> List[Dict]:
        """
        批量分類新聞

        Args:
            articles: 新聞列表
            delay: 基本請求間隔（秒），遇到 rate-limit 時自動延長

        Returns:
            分類結果列表
        """
        results = []
        total = len(articles)

        for i, article in enumerate(articles, 1):
            print(f"[GrokClassifier] Processing {i}/{total}: {article.get('title', '')[:40]}...")

            result = self.classify_single(article)
            results.append(result)

            # 自適應延遲：取基本延遲與 rate-limit 建議的較大值
            if i < total:
                adaptive_delay = max(delay, self._last_rate_limit_delay)
                if adaptive_delay > delay:
                    print(f"[GrokClassifier]   Rate-limit adaptive delay: {adaptive_delay}s")
                time.sleep(adaptive_delay)
                self._last_rate_limit_delay = 0.0  # 重置

        return results
    
    def filter_relevant(self, classified: List[Dict]) -> List[Dict]:
        """過濾出相關新聞"""
        return [r for r in classified if r.get('is_relevant', False)]
    
    def close(self):
        """關閉連接"""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def test_classifier():
    """測試分類器（需要 API Key）"""
    import os
    api_key = os.environ.get('GROK_API_KEY', '')
    
    if not api_key:
        print("GROK_API_KEY not set, skipping test")
        return
    
    sample_article = {
        'date': '2026-01-17',
        'title': '共軍通報美國2艘軍艦穿越台海　川習會後首次',
        'content': '共軍東部戰區今天通報，美國海軍普雷布爾號驅逐艦及狄爾號補給艦17日過航台灣海峽，東部戰區組織海空兵力全程跟蹤監視。',
        'url': 'https://www.cna.com.tw/news/acn/202601170192.aspx',
        'source': 'cna'
    }
    
    classifier = GrokNewsClassifier(api_key)
    result = classifier.classify_single(sample_article)
    
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    classifier.close()


if __name__ == '__main__':
    test_classifier()
