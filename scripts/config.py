#!/usr/bin/env python3
"""
===============================================================================
配置文件 / Configuration
===============================================================================
"""

import os
from pathlib import Path


class Config:
    """專案配置"""
    
    # API Keys (從環境變數讀取)
    GROK_API_KEY = os.environ.get('GROK_API_KEY', '')
    GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
    
    # API 設定
    GROK_API_URL = "https://api.apertis.ai/v1/chat/completions"
    GROK_MODEL = "grok-4.1-fast:free"
    
    # 爬蟲設定
    DAYS_BACK = 7  # 爬取過去幾天的新聞
    REQUEST_TIMEOUT = 30
    REQUEST_DELAY = 1.0  # 請求間隔（秒）
    
    # 路徑設定
    BASE_DIR = Path(__file__).parent.parent
    DATA_DIR = BASE_DIR / "data"
    CSV_FILENAME = "merged_comprehensive_data_M.csv"
    CSV_PATH = DATA_DIR / CSV_FILENAME
    LOG_DIR = DATA_DIR / "logs"
    
    # GitHub 設定
    REPO_NAME = os.environ.get('GITHUB_REPOSITORY', '')
    ENABLE_GITHUB_PUSH = True
    
    # 新聞來源
    XINHUA_URL = "https://www.news.cn/tw/index.html"
    CNA_SEARCH_URL = "https://www.cna.com.tw/search/hysearchws.aspx"
    CNA_KEYWORDS = ["軍演", "軍艦", "台海", "國台辦"]
    
    @classmethod
    def validate(cls):
        """驗證必要配置"""
        errors = []
        if not cls.GROK_API_KEY:
            errors.append("GROK_API_KEY not set")
        return errors
