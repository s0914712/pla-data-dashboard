#!/usr/bin/env python3
"""
===============================================================================
CSV 數據更新器 / CSV Data Updater
===============================================================================

將分類後的新聞數據更新到主數據集
支援 GDELT 風格情緒欄位
"""

import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional


class CSVUpdater:
    """CSV 數據更新器"""
    
    # 目標 CSV 欄位
    COLUMNS = [
        'date', 'year', 'month', 'day', 'year_month', 'weekday', 'weekday_zh',
        'pla_aircraft_sorties', 'china_carrier_present',
        'country1', 'country2',  # Actor1, Actor2
        'US_Taiwan_interaction', 'Political_statement', 'Foreign_battleship',
        'sentiment_score', 'sentiment_label'  # 情緒欄位
    ]
    
    # 星期中文對照
    WEEKDAY_ZH = {
        0: '星期一', 1: '星期二', 2: '星期三', 3: '星期四',
        4: '星期五', 5: '星期六', 6: '星期日'
    }
    
    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)
        self.df = self._load_csv()
    
    def _load_csv(self) -> pd.DataFrame:
        """載入現有 CSV 或創建新的"""
        if self.csv_path.exists():
            df = pd.read_csv(self.csv_path, encoding='utf-8-sig')
            
            # 確保必要欄位存在
            for col in ['sentiment_score', 'sentiment_label']:
                if col not in df.columns:
                    df[col] = np.nan
            
            return df
        else:
            # 創建空的 DataFrame
            return pd.DataFrame(columns=self.COLUMNS)
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """解析日期字串"""
        if not date_str or pd.isna(date_str):
            return None
        
        date_str = str(date_str).strip()
        
        formats = [
            '%Y-%m-%d',
            '%Y/%m/%d',
            '%Y%m%d',
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        
        return None
    
    def _generate_date_fields(self, date_obj: datetime) -> Dict:
        """生成日期相關欄位"""
        return {
            'date': date_obj.strftime('%Y/%m/%d'),
            'year': date_obj.year,
            'month': date_obj.month,
            'day': date_obj.day,
            'year_month': date_obj.strftime('%y-%b'),
            'weekday': date_obj.strftime('%A'),
            'weekday_zh': self.WEEKDAY_ZH.get(date_obj.weekday(), '')
        }
    
    def _ensure_date_row(self, date_obj: datetime) -> int:
        """確保日期行存在，返回行索引"""
        date_str = date_obj.strftime('%Y/%m/%d')
        
        # 檢查是否已存在
        mask = self.df['date'] == date_str
        if mask.any():
            return self.df[mask].index[0]
        
        # 創建新行
        new_row = self._generate_date_fields(date_obj)
        
        # 添加其他欄位的預設值
        for col in self.COLUMNS:
            if col not in new_row:
                new_row[col] = np.nan
        
        # 添加到 DataFrame
        new_idx = len(self.df)
        self.df.loc[new_idx] = new_row
        
        return new_idx
    
    def update_from_classified(self, classified_news: List[Dict]) -> int:
        """
        從分類結果更新 CSV
        
        Args:
            classified_news: 分類後的新聞列表
            
        Returns:
            更新的記錄數
        """
        updated_count = 0
        
        for item in classified_news:
            if not item.get('is_relevant', False):
                continue
            
            # 獲取原始文章信息
            article = item.get('original_article', {})
            date_str = article.get('date', '')
            
            date_obj = self._parse_date(date_str)
            if not date_obj:
                continue
            
            # 確保日期行存在
            row_idx = self._ensure_date_row(date_obj)
            
            # 提取數據
            extracted = item.get('extracted_data', {})
            category = item.get('category', '')
            
            # 更新欄位（不覆蓋已有數據）
            updates = {}
            
            # Country1 (Actor1) - 行動發起方
            country1 = item.get('country1', '')
            if country1:
                current = self.df.loc[row_idx, 'country1']
                if pd.isna(current) or not current:
                    updates['country1'] = country1
            
            # Country2 (Actor2) - 行動目標方
            country2 = item.get('country2', '')
            if country2:
                current = self.df.loc[row_idx, 'country2']
                if pd.isna(current) or not current:
                    updates['country2'] = country2
            
            # 美台互動
            if extracted.get('US_Taiwan_interaction'):
                current = self.df.loc[row_idx, 'US_Taiwan_interaction']
                if pd.isna(current) or not current:
                    updates['US_Taiwan_interaction'] = extracted['US_Taiwan_interaction']
            
            # 政治聲明
            if extracted.get('Political_statement'):
                current = self.df.loc[row_idx, 'Political_statement']
                if pd.isna(current) or not current:
                    updates['Political_statement'] = extracted['Political_statement']
            
            # 外國軍艦
            if extracted.get('Foreign_battleship'):
                current = self.df.loc[row_idx, 'Foreign_battleship']
                if pd.isna(current) or not current:
                    updates['Foreign_battleship'] = extracted['Foreign_battleship']
            
            # 情緒分析（GDELT 風格）
            sentiment_score = item.get('sentiment_score')
            if sentiment_score is not None:
                current = self.df.loc[row_idx, 'sentiment_score']
                if pd.isna(current):
                    updates['sentiment_score'] = sentiment_score
            
            sentiment_label = item.get('sentiment_label')
            if sentiment_label:
                current = self.df.loc[row_idx, 'sentiment_label']
                if pd.isna(current) or not current:
                    updates['sentiment_label'] = sentiment_label
            
            # 應用更新
            if updates:
                for col, val in updates.items():
                    self.df.loc[row_idx, col] = val
                updated_count += 1
        
        return updated_count
    
    def save(self, output_path: Optional[str] = None) -> str:
        """
        保存 CSV
        
        Args:
            output_path: 輸出路徑（預設為原路徑）
            
        Returns:
            保存路徑
        """
        save_path = Path(output_path) if output_path else self.csv_path
        
        # 按日期排序
        self.df['_sort_date'] = pd.to_datetime(self.df['date'], errors='coerce')
        self.df = self.df.sort_values('_sort_date', ascending=True)
        self.df = self.df.drop(columns=['_sort_date'])
        
        # 移除完全空白的行
        self.df = self.df.dropna(how='all')
        
        # 重置索引
        self.df = self.df.reset_index(drop=True)
        
        # 保存
        self.df.to_csv(save_path, index=False, encoding='utf-8-sig')
        
        print(f"[CSVUpdater] Saved to {save_path}")
        return str(save_path)
    
    def get_stats(self) -> Dict:
        """獲取數據統計"""
        # 安全地獲取日期範圍
        date_min = None
        date_max = None
        if not self.df.empty and 'date' in self.df.columns:
            valid_dates = self.df['date'].dropna()
            if len(valid_dates) > 0:
                # 轉換為 datetime 進行比較
                dates_parsed = pd.to_datetime(valid_dates, errors='coerce').dropna()
                if len(dates_parsed) > 0:
                    date_min = dates_parsed.min().strftime('%Y-%m-%d')
                    date_max = dates_parsed.max().strftime('%Y-%m-%d')
        
        # 統計非空值
        non_null_cols = ['US_Taiwan_interaction', 'Political_statement', 
                        'Foreign_battleship', 'sentiment_score']
        non_null_counts = {}
        for col in non_null_cols:
            if col in self.df.columns:
                non_null_counts[col] = int(self.df[col].notna().sum())
            else:
                non_null_counts[col] = 0
        
        return {
            'total_rows': len(self.df),
            'date_range': {
                'min': date_min,
                'max': date_max
            },
            'non_null_counts': non_null_counts
        }


def test_updater():
    """測試更新器"""
    # 創建測試 CSV
    test_path = '/tmp/test_data.csv'
    
    # 範例分類結果
    classified = [
        {
            'category': 'Battleship_Transit',
            'is_relevant': True,
            'sentiment_score': -0.3,
            'sentiment_label': 'negative',
            'extracted_data': {
                'Foreign_battleship': 'US DDG Preble transit',
                'Political_statement': '東部戰區全程跟蹤監視'
            },
            'confidence': 0.9,
            'original_article': {
                'date': '2026-01-17',
                'title': '美國軍艦穿越台海',
                'source': 'cna'
            }
        },
        {
            'category': 'CN_Statement',
            'is_relevant': True,
            'sentiment_score': -0.6,
            'sentiment_label': 'negative',
            'extracted_data': {
                'Political_statement': '外交部反對建交國與台灣簽協定'
            },
            'confidence': 0.95,
            'original_article': {
                'date': '2026-01-16',
                'title': '外交部聲明',
                'source': 'xinhua'
            }
        }
    ]
    
    updater = CSVUpdater(test_path)
    count = updater.update_from_classified(classified)
    updater.save()
    
    print(f"Updated {count} records")
    print(f"Stats: {updater.get_stats()}")
    
    # 顯示結果
    print("\nResult DataFrame:")
    print(updater.df.to_string())


if __name__ == '__main__':
    test_updater()
