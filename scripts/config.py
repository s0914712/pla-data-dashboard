#!/usr/bin/env python3
"""
===============================================================================
配置文件 / Configuration
===============================================================================
GitHub Repo: https://github.com/s0914712/pla-data-dashboard
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
    # scripts/config.py -> 專案根目錄是 parent.parent
    # pla-data-dashboard/
    # ├── scripts/
    # │   └── config.py  <- 這裡
    # └── data/
    #     └── merged_comprehensive_data_M.csv
    BASE_DIR = Path(__file__).parent.parent  # 從 scripts/ 回到專案根目錄
    DATA_DIR = BASE_DIR / "data"
    CSV_FILENAME = "merged_comprehensive_data_M.csv"
    CSV_PATH = DATA_DIR / CSV_FILENAME
    LOG_DIR = DATA_DIR / "logs"
    
    # JapanandBattleship.csv 資料來源 (pla_aircraft_sorties)
    JAPAN_BATTLESHIP_LOCAL = DATA_DIR / "JapanandBattleship.csv"
    JAPAN_BATTLESHIP_GITHUB_URL = (
        "https://raw.githubusercontent.com/s0914712/pla-data-dashboard/"
        "main/data/JapanandBattleship.csv"
    )
    
    # GitHub 設定
    REPO_NAME = os.environ.get('GITHUB_REPOSITORY', 's0914712/pla-data-dashboard')
    ENABLE_GITHUB_PUSH = True
    
    # 新聞來源
    XINHUA_URL = "https://www.news.cn/tw/index.html"
    CNA_SEARCH_URL = "https://www.cna.com.tw/search/hysearchws.aspx"
    CNA_LIST_URL = "https://www.cna.com.tw/list/acn.aspx"  # 兩岸新聞列表
    CNA_KEYWORDS = ["軍演", "軍艦通過"]  # 搜索關鍵字
    
    @classmethod
    def validate(cls):
        """驗證必要配置"""
        errors = []
        if not cls.GROK_API_KEY:
            errors.append("GROK_API_KEY not set")
        return errors
    
    @classmethod
    def print_paths(cls):
        """印出路徑設定（調試用）"""
        print(f"BASE_DIR: {cls.BASE_DIR}")
        print(f"DATA_DIR: {cls.DATA_DIR}")
        print(f"CSV_PATH: {cls.CSV_PATH}")
        print(f"CSV exists: {cls.CSV_PATH.exists()}")
