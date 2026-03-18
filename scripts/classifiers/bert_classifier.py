#!/usr/bin/env python3
"""
===============================================================================
BERT 新聞分類器 / BERT News Classifier
===============================================================================

使用微調後的 bert-base-chinese 進行新聞分類和情緒分析。
搭配 rule_extractor 提取 country codes 和 extracted_data。

介面與 GrokNewsClassifier 完全相容，可透過環境變數切換：
  CLASSIFIER_BACKEND=bert   使用 BERT（預設）
  CLASSIFIER_BACKEND=grok   使用 Grok LLM
"""

import json
import re
import hashlib
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict

import torch
from transformers import BertTokenizer

from .train_bert_classifier import BertMultiTaskClassifier
from .rule_extractor import extract_actors, extract_data


class BertNewsClassifier:
    """使用微調 BERT 進行新聞分類和情緒分析"""

    def __init__(self, model_dir: str = "models/bert_news_classifier"):
        self.model_dir = Path(model_dir)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # 載入 label config
        config_path = self.model_dir / "label_config.json"
        if not config_path.exists():
            raise FileNotFoundError(
                f"模型設定檔不存在: {config_path}\n"
                f"請先執行訓練: python -m scripts.classifiers.train_bert_classifier"
            )
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.category_labels = self.config["category_labels"]
        self.sentiment_labels = self.config["sentiment_labels"]
        self.max_len = self.config.get("max_len", 256)

        # 載入 tokenizer
        self.tokenizer = BertTokenizer.from_pretrained(str(self.model_dir))

        # 載入模型
        self.model = BertMultiTaskClassifier(
            pretrained_name=self.config.get("pretrained_name", "bert-base-chinese"),
            num_categories=len(self.category_labels),
            num_sentiments=len(self.sentiment_labels),
        )
        state_dict = torch.load(
            self.model_dir / "model.pt",
            map_location=self.device,
            weights_only=True,
        )
        self.model.load_state_dict(state_dict)
        self.model.to(self.device)
        self.model.eval()

        print(f"[BertClassifier] ========== 診斷資訊 ==========")
        print(f"[BertClassifier] Model dir : {self.model_dir}")
        print(f"[BertClassifier] Device    : {self.device}")
        print(f"[BertClassifier] Categories: {len(self.category_labels)}")
        print(f"[BertClassifier] ================================")

    # ------------------------------------------------------------------
    # 核心分類
    # ------------------------------------------------------------------
    def classify_single(self, article: Dict) -> Dict:
        """
        分類單篇新聞（介面與 GrokNewsClassifier 相容）。

        Args:
            article: {date, title, content, url, source}

        Returns:
            {category, is_relevant, country1, country2,
             sentiment_score, sentiment_label, extracted_data,
             confidence, original_article}
        """
        title = article.get("title", "")
        content = article.get("content", "")[:400]
        text = f"{title} [SEP] {content}" if content else title

        if not text.strip():
            return self._default_result(article)

        # BERT 推論
        enc = self.tokenizer(
            text,
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        input_ids = enc["input_ids"].to(self.device)
        attention_mask = enc["attention_mask"].to(self.device)

        with torch.no_grad():
            cat_logits, sent_logits = self.model(input_ids, attention_mask)

        # 類別
        cat_probs = torch.softmax(cat_logits, dim=1).squeeze(0)
        cat_id = cat_probs.argmax().item()
        category = self.category_labels[cat_id]
        confidence = cat_probs[cat_id].item()

        # 情緒
        sent_probs = torch.softmax(sent_logits, dim=1).squeeze(0)
        sent_id = sent_probs.argmax().item()
        sentiment_label = self.sentiment_labels[sent_id]
        # 將 3-class softmax 轉為 -1~+1 分數
        sentiment_score = self._probs_to_score(sent_probs)

        is_relevant = category != "Not_Relevant"

        # 規則式提取
        full_text = f"{title} {content}"
        country1, country2 = extract_actors(full_text, category)
        extracted = extract_data(full_text, category)

        return {
            "category": category,
            "is_relevant": is_relevant,
            "country1": country1,
            "country2": country2,
            "sentiment_score": round(sentiment_score, 3),
            "sentiment_label": sentiment_label,
            "extracted_data": extracted,
            "confidence": round(confidence, 3),
            "original_article": article,
        }

    def _probs_to_score(self, sent_probs: torch.Tensor) -> float:
        """將 [negative, neutral, positive] 的機率轉為 -1~+1 分數"""
        # 加權：negative=-1, neutral=0, positive=+1
        weights = torch.tensor([-1.0, 0.0, 1.0], device=sent_probs.device)
        return (sent_probs * weights).sum().item()

    # ------------------------------------------------------------------
    # 去重（TF-IDF 餘弦相似度，不依賴 LLM）
    # ------------------------------------------------------------------
    def deduplicate_batch(
        self, articles: List[Dict], similarity_threshold: float = 0.85
    ) -> List[Dict]:
        """
        使用標題+內容的 n-gram 指紋進行去重。

        1. URL 完全比對去重
        2. 字元 n-gram 指紋 + Jaccard 相似度
        """
        if len(articles) <= 1:
            return articles

        print(f"[BertClassifier] 開始去重，共 {len(articles)} 篇文章...")

        # Phase 1: URL 去重
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
            print(f"[BertClassifier]   URL 去重: 移除 {removed_by_url} 篇")

        # Phase 2: n-gram 指紋去重
        def _fingerprint(article):
            title = article.get("title", "")
            content = article.get("content", "")[:200]
            text = title + content
            # 字元 3-gram
            ngrams = set()
            for i in range(len(text) - 2):
                ngrams.add(text[i : i + 3])
            return ngrams

        fingerprints = [_fingerprint(a) for a in url_deduped]
        keep = [True] * len(url_deduped)

        for i in range(len(url_deduped)):
            if not keep[i]:
                continue
            for j in range(i + 1, len(url_deduped)):
                if not keep[j]:
                    continue
                fp_i, fp_j = fingerprints[i], fingerprints[j]
                if not fp_i or not fp_j:
                    continue
                # Jaccard similarity
                intersection = len(fp_i & fp_j)
                union = len(fp_i | fp_j)
                if union > 0 and intersection / union >= similarity_threshold:
                    keep[j] = False

        deduped = [a for a, k in zip(url_deduped, keep) if k]
        removed_by_sim = len(url_deduped) - len(deduped)
        if removed_by_sim:
            print(f"[BertClassifier]   相似度去重: 移除 {removed_by_sim} 篇")

        total_removed = len(articles) - len(deduped)
        print(
            f"[BertClassifier] 去重完成: {len(articles)} → {len(deduped)} "
            f"(移除 {total_removed} 篇)"
        )
        return deduped

    # ------------------------------------------------------------------
    # 批量分類
    # ------------------------------------------------------------------
    def classify_batch(self, articles: List[Dict], delay: float = 0.0) -> List[Dict]:
        """
        批量分類（BERT 不需要 delay，但保留參數相容性）。
        """
        results = []
        total = len(articles)
        for i, article in enumerate(articles, 1):
            if i % 20 == 0 or i == total:
                print(f"[BertClassifier] Processing {i}/{total}...")
            result = self.classify_single(article)
            results.append(result)
        return results

    def filter_relevant(self, classified: List[Dict]) -> List[Dict]:
        """過濾出相關新聞"""
        return [r for r in classified if r.get("is_relevant", False)]

    def _default_result(self, article: Dict) -> Dict:
        return {
            "category": "Not_Relevant",
            "is_relevant": False,
            "country1": "",
            "country2": "",
            "sentiment_score": 0.0,
            "sentiment_label": "neutral",
            "extracted_data": {},
            "confidence": 0.0,
            "original_article": article,
        }

    def close(self):
        """釋放資源（保持介面相容）"""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
