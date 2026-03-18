"""
分類器介面定義 / Classifier Interface Definition

定義 NewsClassifier Protocol，確保所有分類器後端實作一致的介面。
"""

from typing import Dict, List, Protocol, runtime_checkable


@runtime_checkable
class NewsClassifier(Protocol):
    """新聞分類器的統一介面"""

    def classify_single(self, article: Dict) -> Dict:
        """分類單篇新聞"""
        ...

    def classify_batch(self, articles: List[Dict], delay: float = 0.0) -> List[Dict]:
        """批量分類新聞"""
        ...

    def deduplicate_batch(self, articles: List[Dict], **kwargs) -> List[Dict]:
        """去重"""
        ...

    def filter_relevant(self, classified: List[Dict]) -> List[Dict]:
        """過濾相關新聞"""
        ...

    def close(self) -> None:
        """釋放資源"""
        ...

    def __enter__(self) -> "NewsClassifier":
        ...

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        ...
