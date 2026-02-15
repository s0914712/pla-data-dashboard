"""
新聞爬蟲模組 / News Scrapers Module
"""

from .base_scraper import BaseScraper
from .xinhua_scraper import XinhuaTWScraper
from .cna_scraper import CNAScraper
from .weibo_scraper import WeiboScraper

__all__ = ['BaseScraper', 'XinhuaTWScraper', 'CNAScraper', 'WeiboScraper']
