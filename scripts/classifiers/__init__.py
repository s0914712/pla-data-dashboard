"""
新聞分類模組 / News Classifiers Module

支援兩種後端：
  - GrokNewsClassifier: 使用 Grok LLM API
  - BertNewsClassifier: 使用微調 BERT（離線）

透過 CLASSIFIER_BACKEND 環境變數切換。
"""

from .grok_classifier import GrokNewsClassifier
from .bert_classifier import BertNewsClassifier
from .rule_extractor import extract_actors, extract_data
from .prompts import (
    CLASSIFICATION_SYSTEM_PROMPT,
    CLASSIFICATION_USER_TEMPLATE,
    CLASSIFICATION_PROMPT_SIMPLE,
    SENTIMENT_ANALYSIS_PROMPT
)

__all__ = [
    'GrokNewsClassifier',
    'BertNewsClassifier',
    'extract_actors',
    'extract_data',
    'CLASSIFICATION_SYSTEM_PROMPT',
    'CLASSIFICATION_USER_TEMPLATE',
    'CLASSIFICATION_PROMPT_SIMPLE',
    'SENTIMENT_ANALYSIS_PROMPT'
]
