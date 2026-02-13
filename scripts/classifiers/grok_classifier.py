#!/usr/bin/env python3
"""
===============================================================================
Grok API 新聞分類器 / Grok News Classifier
===============================================================================

使用 Grok API (via apertis.ai) 進行新聞分類和情緒分析
支援 GDELT 風格的情緒分數 (-1 到 +1)
"""

import json
import httpx
import time
from typing import List, Dict, Optional
from .prompts import CLASSIFICATION_SYSTEM_PROMPT, CLASSIFICATION_USER_TEMPLATE


class GrokNewsClassifier:
    """使用 Grok API 進行新聞分類和情緒分析"""

    # 可重試的 HTTP 狀態碼
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    MAX_RETRIES = 3
    
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self.api_url = "https://api.apertis.ai/v1/chat/completions"
        self.model = "glm-4.5-air:free"
        # trust_env=False 防止 CI/CD 環境代理設定干擾
        self.client = httpx.Client(timeout=60, trust_env=False)
        self._debug_printed = False

        # 初始化時印出診斷資訊
        masked_key = self.api_key[:8] + "..." + self.api_key[-4:] if len(self.api_key) > 12 else "***"
        print(f"[GrokClassifier] ========== 診斷資訊 ==========")
        print(f"[GrokClassifier] API URL  : {self.api_url}")
        print(f"[GrokClassifier] Model    : {self.model}")
        print(f"[GrokClassifier] API Key  : {masked_key} (length={len(self.api_key)})")
        print(f"[GrokClassifier] Retries  : max {self.MAX_RETRIES} (backoff: 5s, 10s, 20s)")
        print(f"[GrokClassifier] ================================")
    
    def _call_api(self, messages: List[Dict]) -> Optional[str]:
        """
        調用 API，含自動重試 (503/429/500 等錯誤會重試)
        
        Args:
            messages: 消息列表
            
        Returns:
            API 回應內容，失敗時返回 None
        """
        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.1
                    }
                )

                # 成功
                if response.status_code == 200:
                    return response.json()["choices"][0]["message"]["content"]

                # 可重試的錯誤
                if response.status_code in self.RETRYABLE_STATUS_CODES:
                    wait_time = 5 * (2 ** (attempt - 1))  # 5s, 10s, 20s
                    print(f"[GrokClassifier] ⚠️  HTTP {response.status_code} (attempt {attempt}/{self.MAX_RETRIES})")
                    print(f"[GrokClassifier]    Body: {response.text[:200]}")
                    if attempt < self.MAX_RETRIES:
                        print(f"[GrokClassifier]    等待 {wait_time}s 後重試...")
                        time.sleep(wait_time)
                        continue
                    else:
                        print(f"[GrokClassifier] ❌ 已達最大重試次數，放棄此請求")
                        return None

                # 不可重試的錯誤 (401, 403, 400 等)
                print(f"[GrokClassifier] ❌ HTTP {response.status_code} (不可重試)")
                print(f"[GrokClassifier]    Body: {response.text[:300]}")
                if response.status_code in (401, 403):
                    print(f"[GrokClassifier]    🔑 請檢查 API Key 是否正確、是否過期")
                return None

            except httpx.ConnectError as e:
                wait_time = 5 * (2 ** (attempt - 1))
                print(f"[GrokClassifier] ⚠️  連線失敗 (attempt {attempt}/{self.MAX_RETRIES}): {e}")
                if attempt < self.MAX_RETRIES:
                    print(f"[GrokClassifier]    等待 {wait_time}s 後重試...")
                    time.sleep(wait_time)
                    continue
                print(f"[GrokClassifier] ❌ 連線失敗已達最大重試次數")
                return None

            except httpx.TimeoutException as e:
                wait_time = 5 * (2 ** (attempt - 1))
                print(f"[GrokClassifier] ⚠️  請求超時 (attempt {attempt}/{self.MAX_RETRIES}): {e}")
                if attempt < self.MAX_RETRIES:
                    print(f"[GrokClassifier]    等待 {wait_time}s 後重試...")
                    time.sleep(wait_time)
                    continue
                print(f"[GrokClassifier] ❌ 超時已達最大重試次數")
                return None

            except Exception as e:
                print(f"[GrokClassifier] ❌ 未預期錯誤: {type(e).__name__}: {e}")
                return None

        return None
    
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
        # 構建用戶消息
        user_message = CLASSIFICATION_USER_TEMPLATE.format(
            title=article.get('title', ''),
            content=article.get('content', '')[:500],  # 限制內容長度
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
        
        return self._parse_response(response, article)
    
    def _parse_response(self, response: str, article: Dict) -> Dict:
        """解析 API 回應"""
        try:
            # 嘗試提取 JSON
            json_match = response
            
            # 如果回應包含 ```json ... ``` 格式
            if '```json' in response:
                import re
                match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
                if match:
                    json_match = match.group(1)
            elif '```' in response:
                import re
                match = re.search(r'```\s*(.*?)\s*```', response, re.DOTALL)
                if match:
                    json_match = match.group(1)
            
            result = json.loads(json_match)
            
            # 確保必要欄位存在
            return {
                'category': result.get('category', 'Not_Relevant'),
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
    
    def classify_batch(self, articles: List[Dict], delay: float = 1.0) -> List[Dict]:
        """
        批量分類新聞
        
        Args:
            articles: 新聞列表
            delay: 請求間隔（秒）
            
        Returns:
            分類結果列表
        """
        results = []
        total = len(articles)
        
        for i, article in enumerate(articles, 1):
            print(f"[GrokClassifier] Processing {i}/{total}: {article.get('title', '')[:40]}...")
            
            result = self.classify_single(article)
            results.append(result)
            
            # 避免 API 限流
            if i < total:
                time.sleep(delay)
        
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
