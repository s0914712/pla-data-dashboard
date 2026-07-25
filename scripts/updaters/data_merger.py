#!/usr/bin/env python3
"""
===============================================================================
資料合併器 / Data Merger
===============================================================================

從 GitHub 下載 JapanandBattleship.csv 並合併 pla_aircraft_sorties 等欄位
"""

import httpx
import pandas as pd
import numpy as np
from io import StringIO
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List


class DataMerger:
    """從外部資料源合併數據"""
    
    # GitHub Raw URL（主要）
    JAPAN_BATTLESHIP_URL = (
        "https://raw.githubusercontent.com/s0914712/pla-data-dashboard/"
        "main/data/JapanandBattleship.csv"
    )
    
    # GitHub API URL（備用）
    GITHUB_API_URL = (
        "https://api.github.com/repos/s0914712/pla-data-dashboard/"
        "contents/data/JapanandBattleship.csv"
    )
    
    # 從 JapanandBattleship.csv 複製到 comprehensive 的欄位。
    #
    # 原本只有前兩個，導致防衛省辛苦解析出來的海峽通過與國家判定完全
    # 進不了 comprehensive —— 稽核時才發現那邊的 Foreign_battleship
    # 有 57 筆從未寫入，根因就是沒人負責搬。
    MERGE_COLUMNS = [
        'pla_aircraft_sorties',
        'china_carrier_present',
        # 日本水域通過（由 scraper_japan_mod 的句子級判讀產出）
        '與那國', '宮古', '大禹', '對馬',
        # 國家判定，用於在下游區分共軍與俄艦
        '國家',
    ]
    
    def __init__(self, timeout: int = 30, local_path: Optional[str] = None):
        """
        初始化
        
        Args:
            timeout: HTTP 請求超時
            local_path: 本地 JapanandBattleship.csv 路徑（優先使用）
        """
        self.timeout = timeout
        self.local_path = local_path
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/vnd.github.v3.raw+json"
            }
        )
        self._japan_data: Optional[pd.DataFrame] = None
    
    def fetch_japan_battleship_data(self) -> Optional[pd.DataFrame]:
        """
        獲取 JapanandBattleship.csv 資料
        
        優先順序：
        1. 本地檔案
        2. GitHub Raw URL
        3. GitHub API
        
        Returns:
            DataFrame 或 None（失敗時）
        """
        # 1. 嘗試本地檔案
        if self.local_path:
            local_file = Path(self.local_path)
            if local_file.exists():
                try:
                    print(f"[DataMerger] Loading from local: {self.local_path}")
                    df = pd.read_csv(local_file, encoding='utf-8-sig')
                    return self._process_dataframe(df)
                except Exception as e:
                    print(f"[DataMerger] Local file error: {e}")
        
        # 2. 嘗試 GitHub Raw URL
        try:
            print(f"[DataMerger] Fetching from GitHub Raw...")
            response = self.client.get(self.JAPAN_BATTLESHIP_URL)
            if response.status_code == 200:
                df = pd.read_csv(StringIO(response.text), encoding='utf-8')
                return self._process_dataframe(df)
            else:
                print(f"[DataMerger] GitHub Raw returned: {response.status_code}")
        except Exception as e:
            print(f"[DataMerger] GitHub Raw error: {e}")
        
        # 3. 嘗試 GitHub API
        try:
            print(f"[DataMerger] Fetching from GitHub API...")
            response = self.client.get(
                self.GITHUB_API_URL,
                headers={"Accept": "application/vnd.github.v3.raw"}
            )
            if response.status_code == 200:
                df = pd.read_csv(StringIO(response.text), encoding='utf-8')
                return self._process_dataframe(df)
            else:
                print(f"[DataMerger] GitHub API returned: {response.status_code}")
        except Exception as e:
            print(f"[DataMerger] GitHub API error: {e}")
        
        print("[DataMerger] All fetch methods failed")
        return None
    
    def _process_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """處理並標準化 DataFrame"""
        # 標準化日期欄位
        if 'date' in df.columns:
            df['date_normalized'] = pd.to_datetime(
                df['date'], errors='coerce'
            ).dt.strftime('%Y/%m/%d')
        
        print(f"[DataMerger] Loaded {len(df)} rows")
        self._japan_data = df
        return df
    
    def merge_pla_sorties(self, target_df: pd.DataFrame) -> pd.DataFrame:
        """
        將 pla_aircraft_sorties 等欄位合併到目標 DataFrame
        
        Args:
            target_df: 目標 DataFrame（主數據集）
            
        Returns:
            合併後的 DataFrame
        """
        # 確保已下載資料
        if self._japan_data is None:
            self.fetch_japan_battleship_data()
        
        if self._japan_data is None:
            print("[DataMerger] No source data available, skipping merge")
            return target_df
        
        source_df = self._japan_data.copy()
        result_df = target_df.copy()
        
        # 標準化目標 DataFrame 的日期
        result_df['date_normalized'] = pd.to_datetime(
            result_df['date'], errors='coerce'
        ).dt.strftime('%Y/%m/%d')
        
        # 建立來源資料的日期索引
        source_dict = {}
        for _, row in source_df.iterrows():
            date_key = row.get('date_normalized', '')
            if date_key and pd.notna(date_key):
                source_dict[date_key] = row
        
        # 合併資料
        updated_count = 0
        for idx, row in result_df.iterrows():
            date_key = row.get('date_normalized', '')
            
            if date_key in source_dict:
                source_row = source_dict[date_key]
                
                # 複製指定欄位
                for col in self.MERGE_COLUMNS:
                    if col in source_row.index:
                        source_val = source_row[col]
                        
                        # 只在目標為空時更新
                        if col in result_df.columns:
                            current_val = result_df.loc[idx, col]
                            if pd.isna(current_val) or current_val == '':
                                if pd.notna(source_val):
                                    result_df.loc[idx, col] = source_val
                                    updated_count += 1
                        else:
                            # 欄位不存在，新增
                            result_df.loc[idx, col] = source_val
                            updated_count += 1
        
        # 移除臨時欄位
        if 'date_normalized' in result_df.columns:
            result_df = result_df.drop(columns=['date_normalized'])
        
        print(f"[DataMerger] Merged {updated_count} values for pla_aircraft_sorties")
        return result_df
    
    def get_stats(self) -> Dict:
        """獲取來源資料統計"""
        if self._japan_data is None:
            return {'status': 'not_loaded'}
        
        df = self._japan_data
        stats = {
            'status': 'loaded',
            'total_rows': len(df),
            'columns': list(df.columns),
        }
        
        # 統計 pla_aircraft_sorties
        if 'pla_aircraft_sorties' in df.columns:
            stats['pla_aircraft_sorties'] = {
                'non_null': int(df['pla_aircraft_sorties'].notna().sum()),
                'min': float(df['pla_aircraft_sorties'].min()) if df['pla_aircraft_sorties'].notna().any() else None,
                'max': float(df['pla_aircraft_sorties'].max()) if df['pla_aircraft_sorties'].notna().any() else None,
            }
        
        return stats
    
    def close(self):
        """關閉連接"""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def test_merger():
    """測試合併器"""
    with DataMerger() as merger:
        # 下載資料
        df = merger.fetch_japan_battleship_data()
        
        if df is not None:
            print("\n📊 Source Data Stats:")
            print(merger.get_stats())
            
            # 顯示前幾行
            print("\n📋 Sample data:")
            cols = ['date', 'pla_aircraft_sorties', 'china_carrier_present']
            available_cols = [c for c in cols if c in df.columns]
            print(df[available_cols].head(10).to_string())


if __name__ == '__main__':
    test_merger()
