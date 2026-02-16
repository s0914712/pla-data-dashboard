"""
新聞爬蟲模組 / News Scrapers Module
"""

from .base_scraper import BaseScraper


def __getattr__(name):
    """Lazy import to avoid requiring all dependencies in every workflow"""
    if name == 'XinhuaTWScraper':
        from .xinhua_scraper import XinhuaTWScraper
        return XinhuaTWScraper
    elif name == 'CNAScraper':
        from .cna_scraper import CNAScraper
        return CNAScraper
    elif name == 'WeiboScraper':
        from .weibo_scraper import WeiboScraper
        return WeiboScraper
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ['BaseScraper', 'XinhuaTWScraper', 'CNAScraper', 'WeiboScraper']
