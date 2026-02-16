#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLA 7-Day Sorties Prediction System
====================================
GitHub 部署版本 - 自動更新 prediction.csv

功能：
1. 載入最新資料
2. 訓練模型（CatBoost + TimeSeriesSplit + Quantile Regression）
3. 生成未來7天預測
4. 更新 data/prediction.csv

改進項目 (v2.3):
- CatBoost 引擎 (CV MAE: 7.83)
- 修正 target leaking
- 精簡至 32 個核心特徵
- TimeSeriesSplit 時序交叉驗證
- Quantile Regression 信賴區間
- 最佳參數: depth=6, lr=0.03, iterations=400

v2.3.1 修正:
- Fix 1: Quantile Regressor 改用 log1p 空間訓練，expm1 還原（信賴區間尺度統一）
- Fix 2: Carrier 改為 shift(1).rolling(7).sum()，預測時使用 self._recent_carrier
- Recency Boost: 近期資料重複以強化 regime 適應
- 假日資料改由 CSV 讀取 (data/cn_holidays.csv)

v2.4.0 Anti-Leaking 改善:
- Walk-Forward Multi-Step CV: 模擬真實 7 步迭代預測，按 Day-1~Day-7 分層報告 MAE
- Per-fold Scaler: CV 每個 fold 獨立 fit RobustScaler（修復 Leak #2）
- Per-fold time_weight: CV 內按 fold 訓練集最新日期計算權重（修復 Leak #3）
- Embargo gap=7: 訓練集和驗證集之間隔離 7 天（避免 lag_7 邊界洩漏）
- 細緻 lag 特徵: lag_2/3/5/21 + 加速度 + 交互特徵 + 動量
- 零活動 regime 偵測: zero_count_3d/7d, consecutive_zero
- Spike 偵測: spike_7d, spike_ratio
- 新聞先行指標: news_classified.json 事件計數 + 情緒分數
- 政治特徵擴充: US_Taiwan_interaction, Foreign_battleship (merged_data_M)

Usage:
    python pla_predictor.py

Author: PLA Data Dashboard
Version: 2.4.0
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sklearn.preprocessing import RobustScaler
from sklearn.ensemble import RandomForestClassifier, GradientBoostingRegressor
from sklearn.model_selection import TimeSeriesSplit
from imblearn.over_sampling import SMOTE
import warnings
import os
import json

# CatBoost - 若未安裝則 fallback 到 sklearn
try:
    import catboost as cb
    USE_CATBOOST = True
except ImportError:
    USE_CATBOOST = False
    print("[Warning] CatBoost not installed, using sklearn GradientBoosting as fallback")

warnings.filterwarnings('ignore')

# ============================================================
# 配置
# ============================================================

DATA_SOURCES = {
    'sorties': 'https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main/data/JapanandBattleship.csv',
    'political': 'https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main/data/merged_comprehensive_data_M.csv',
    'weather': 'https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main/data/airport_weather_forecast.csv',
    'news': 'https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main/data/news_classified.json',
}

NEWS_LOCAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'news_classified.json')

OUTPUT_PATH = 'data/prediction.csv'
HIGH_THRESHOLD = 60
PREDICTION_DAYS = 7

# CatBoost 最佳參數
CATBOOST_PARAMS = {
    'iterations': 400,
    'depth': 6,
    'learning_rate': 0.03,
    'l2_leaf_reg': 3,
    'random_state': 42,
    'verbose': 0
}

# Recency Boost: 強化近期資料以適應 regime 變化
# 近期資料會被重複 FACTOR 次，讓模型更偏向近期模式
RECENCY_BOOST_WEEKS = 4    # 最近幾週的資料要加強
RECENCY_BOOST_FACTOR = 3   # 重複倍數（1=不重複, 3=近期資料出現3次）

# ============================================================
# 中國假日資料 - 從 CSV 讀取
# ============================================================

HOLIDAYS_CSV_URL = 'https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main/data/cn_holidays.csv'
HOLIDAYS_CSV_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'cn_holidays.csv')

CN_HOLIDAY_DATES = set()

def _load_holidays():
    """從 CSV 載入假日資料"""
    global CN_HOLIDAY_DATES
    # 優先讀取本地檔案，若無則從 GitHub 讀取
    try:
        if os.path.exists(HOLIDAYS_CSV_LOCAL):
            df = pd.read_csv(HOLIDAYS_CSV_LOCAL, encoding='utf-8-sig')
        else:
            df = pd.read_csv(HOLIDAYS_CSV_URL, encoding='utf-8-sig')
        CN_HOLIDAY_DATES = set(df['date'].astype(str).str.strip().tolist())
        print(f"  假日資料: {len(CN_HOLIDAY_DATES)} 筆")
    except Exception as e:
        print(f"  [Warning] 無法載入假日 CSV: {e}，使用空集合")
        CN_HOLIDAY_DATES = set()

_load_holidays()


def get_holiday_features(date):
    """取得假日特徵"""
    date_str = date.strftime('%Y-%m-%d')
    is_holiday = date_str in CN_HOLIDAY_DATES
    return int(is_holiday)


class PLAPredictor:
    """PLA 架次預測系統 v2.4 - CatBoost + Anti-Leaking"""

    def __init__(self, high_threshold=None):
        self.reg_model = None           # 主回歸模型
        self.reg_lower = None           # 下界分位數回歸
        self.reg_upper = None           # 上界分位數回歸
        self.clf_model = None           # 分類模型
        self.scaler_continuous = RobustScaler()
        self.scaler_counts = RobustScaler()
        self.political_events = None
        self.news_data = None            # v2.4: news_classified.json
        self.weather_data = None
        self.latest_data = None
        self.latest_date = None
        self.high_threshold = high_threshold or HIGH_THRESHOLD
        self.cv_scores = []

        # v2.3: 精簡至 32 個核心特徵
        self.cyclic_cols = ['month_sin', 'month_cos', 'dow_sin', 'dow_cos']
        self.binary_cols = []  # 移除低效二元特徵
        self.continuous_cols = [
            # 滯後特徵 (v2.4: 增加 lag_2/3/5/21 填補資訊斷層)
            'lag_1', 'lag_2', 'lag_3', 'lag_5', 'lag_7', 'lag_14', 'lag_21', 'lag_30',
            # 移動平均
            'ma_3', 'ma_7', 'ma_14', 'ma_30',
            # EMA
            'ema_7', 'ema_14',
            # 統計特徵
            'min_7', 'max_7',
            'std_3', 'std_7', 'std_14', 'std_30',
            # 變化率 (修正 target leaking)
            'pct_change_1d', 'pct_change_7d',
            # 差分
            'diff_1', 'diff_7',
            # 衍生特徵
            'compression', 'trend_3d', 'trend_7d', 'volatility_ratio',
            'ema_trend',
            # v2.4: 自回歸加速度與交互特徵
            'accel_1d', 'lag_ratio_1_7', 'lag_diff_1_2', 'lag_diff_2_3', 'momentum_3d',
            # v2.4: 零活動 regime 偵測
            'zero_count_3d', 'zero_count_7d', 'consecutive_zero',
            # v2.4: 近期 spike 偵測
            'spike_7d', 'spike_ratio',
            # v2.4: 新聞情緒
            'news_avg_sentiment',
        ]
        self.count_cols = ['carrier', 'cn_stmt_7d',
                           'us_tw_interaction_7d', 'foreign_battleship_7d',
                           'news_military_count', 'news_us_tw_count',
                           'news_relevant_count']
        self.holiday_cols = ['is_holiday']

    def load_data(self, sorties_path=None, political_path=None):
        """載入資料"""
        print("=" * 60)
        print("PLA 7-Day Prediction System v2.4")
        print(f"Engine: {'CatBoost' if USE_CATBOOST else 'sklearn GradientBoosting'}")
        print("=" * 60)
        print(f"\n[1] 載入資料...")

        sorties_path = sorties_path or DATA_SOURCES['sorties']
        political_path = political_path or DATA_SOURCES['political']

        print(f"  架次資料來源: {sorties_path}")
        print(f"  政治事件來源: {political_path}")

        # 載入架次資料
        df_sorties = None
        for encoding in ['utf-8-sig', 'utf-8', 'latin-1']:
            try:
                df_sorties = pd.read_csv(sorties_path, encoding=encoding)
                print(f"  成功載入架次資料 (encoding: {encoding})")
                break
            except Exception as e:
                print(f"  嘗試 {encoding} 編碼失敗: {e}")

        if df_sorties is None:
            raise ValueError(f"無法載入架次資料: {sorties_path}")

        # 檢查必要欄位
        required_cols = ['date', 'pla_aircraft_sorties']
        missing_cols = [c for c in required_cols if c not in df_sorties.columns]
        if missing_cols:
            print(f"  警告: 架次資料缺少欄位: {missing_cols}")
            print(f"  可用欄位: {list(df_sorties.columns)}")
            raise ValueError(f"架次資料缺少必要欄位: {missing_cols}")

        df_sorties['date'] = pd.to_datetime(df_sorties['date'], errors='coerce')
        df_sorties = df_sorties[df_sorties['date'].notna()].copy()
        df_sorties = df_sorties[df_sorties['pla_aircraft_sorties'].notna()].copy()
        df_sorties = df_sorties.sort_values('date').reset_index(drop=True)

        if len(df_sorties) < 60:
            raise ValueError(f"架次資料筆數不足: {len(df_sorties)} (需要至少 60 筆)")

        # 載入政治事件資料
        df_political = None
        for encoding in ['utf-8-sig', 'utf-8', 'latin-1']:
            try:
                df_political = pd.read_csv(political_path, encoding=encoding)
                print(f"  成功載入政治事件資料 (encoding: {encoding})")
                break
            except Exception as e:
                print(f"  嘗試 {encoding} 編碼失敗: {e}")

        if df_political is None:
            print(f"  警告: 無法載入政治事件資料，將使用空資料集")
            df_political = pd.DataFrame({'date': []})

        if 'date' in df_political.columns:
            df_political['date'] = pd.to_datetime(df_political['date'], errors='coerce')
            df_political = df_political[df_political['date'].notna()].copy()

        self.political_events = df_political
        self.latest_date = df_sorties['date'].max()

        # 載入天氣資料
        try:
            self.weather_data = pd.read_csv(DATA_SOURCES['weather'], encoding='utf-8-sig')
            self.weather_data['date'] = pd.to_datetime(self.weather_data['date'])
        except:
            self.weather_data = None

        # v2.4: 載入新聞分類資料
        self.news_data = self._load_news_data()

        print(f"  架次資料: {len(df_sorties)} 筆")
        print(f"  政治事件: {len(df_political)} 筆")
        print(f"  新聞資料: {len(self.news_data)} 篇")
        print(f"  最新日期: {self.latest_date.strftime('%Y-%m-%d')}")

        return df_sorties, df_political

    def _load_news_data(self):
        """v2.4: 載入 news_classified.json"""
        try:
            if os.path.exists(NEWS_LOCAL_PATH):
                with open(NEWS_LOCAL_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
            else:
                import urllib.request
                with urllib.request.urlopen(DATA_SOURCES['news']) as resp:
                    return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print(f"  [Warning] 無法載入新聞資料: {e}")
            return []

    def _extract_news_features(self, target_date, lookback_days=7):
        """v2.4: 從 news_classified.json 提取先行指標特徵（嚴格 < target_date）"""
        if not self.news_data:
            return {
                'news_military_count': 0, 'news_us_tw_count': 0,
                'news_relevant_count': 0, 'news_avg_sentiment': 0,
            }

        window_start = target_date - timedelta(days=lookback_days)
        recent = []
        for n in self.news_data:
            try:
                nd = pd.to_datetime(n['original_article']['date'])
                if window_start <= nd < target_date:
                    recent.append(n)
            except (KeyError, ValueError):
                continue

        return {
            'news_military_count': sum(1 for n in recent if n.get('category') == 'Military_Exercise'),
            'news_us_tw_count': sum(1 for n in recent if n.get('category') == 'US_TW_Interaction'),
            'news_relevant_count': sum(1 for n in recent if n.get('is_relevant')),
            'news_avg_sentiment': float(np.mean([n.get('sentiment_score', 0) for n in recent])) if recent else 0,
        }

    def _create_political_features(self, df, df_events, window_days):
        features = []
        for _, row in df.iterrows():
            current_date = row['date']
            mask = (df_events['date'] >= current_date - timedelta(days=window_days)) & (df_events['date'] < current_date)
            past = df_events[mask]

            cn_stmt = past['Political_statement'].astype(str).str.contains('中共|中國|中方|國台辦', na=False).sum() if 'Political_statement' in past.columns else 0
            features.append({f'cn_stmt_{window_days}d': cn_stmt})
        return pd.DataFrame(features)

    def prepare_features(self, df_sorties, df_political):
        """準備特徵 (v2.3 - 修正 target leaking + 精簡特徵)"""
        print("[2] Feature engineering (v2.3)...")

        df = df_sorties.copy()
        target = 'pla_aircraft_sorties'

        # === 週期性特徵 ===
        df['month_sin'] = np.sin(2 * np.pi * df['date'].dt.month / 12)
        df['month_cos'] = np.cos(2 * np.pi * df['date'].dt.month / 12)
        df['dow_sin'] = np.sin(2 * np.pi * df['date'].dt.dayofweek / 7)
        df['dow_cos'] = np.cos(2 * np.pi * df['date'].dt.dayofweek / 7)

        # === 滯後特徵 (v2.4: 增加 lag_2/3/5/21 填補資訊斷層) ===
        for lag in [1, 2, 3, 5, 7, 14, 21, 30]:
            df[f'lag_{lag}'] = df[target].shift(lag)

        # === 滾動統計特徵 ===
        for window in [3, 7, 14, 30]:
            df[f'ma_{window}'] = df[target].shift(1).rolling(window, min_periods=1).mean()
            df[f'std_{window}'] = df[target].shift(1).rolling(window, min_periods=1).std().fillna(0)

        df['min_7'] = df[target].shift(1).rolling(7, min_periods=1).min()
        df['max_7'] = df[target].shift(1).rolling(7, min_periods=1).max()

        # === EMA ===
        df['ema_7'] = df[target].shift(1).ewm(span=7, adjust=False).mean()
        df['ema_14'] = df[target].shift(1).ewm(span=14, adjust=False).mean()
        df['ema_trend'] = df['ema_7'] - df['ema_14']

        # === 變化率 (修正 target leaking) ===
        shifted = df[target].shift(1)
        df['pct_change_1d'] = ((shifted - shifted.shift(1)) / (shifted.shift(1) + 1)).fillna(0).clip(-2, 2)
        df['pct_change_7d'] = ((shifted - shifted.shift(7)) / (shifted.shift(7) + 1)).fillna(0).clip(-2, 2)

        # === 差分 (修正 target leaking) ===
        df['diff_1'] = (shifted - shifted.shift(1)).fillna(0)
        df['diff_7'] = (shifted - shifted.shift(7)).fillna(0)

        # === 衍生特徵 ===
        df['compression'] = (df['ma_3'] / (df['ma_14'] + 1)).clip(0, 3)
        df['trend_3d'] = df['ma_3'] - df['ma_7']
        df['trend_7d'] = df['ma_7'] - df['ma_14']
        df['volatility_ratio'] = df['std_7'] / (df['ma_7'] + 1)

        # === v2.4: 自回歸加速度與交互特徵 ===
        df['accel_1d'] = df['lag_1'] - 2 * df['lag_2'] + df['lag_3']  # 趨勢加速度
        df['lag_ratio_1_7'] = df['lag_1'] / (df['lag_7'] + 1)         # 短期 vs 週期比
        df['lag_diff_1_2'] = df['lag_1'] - df['lag_2']                # 最近 1 天變化
        df['lag_diff_2_3'] = df['lag_2'] - df['lag_3']                # 前 1 天變化
        df['momentum_3d'] = df['lag_1'] - df['lag_3']                 # 3 天動量

        # === v2.4: 零活動 regime 偵測 ===
        df['zero_count_3d'] = df[target].shift(1).rolling(3, min_periods=1).apply(
            lambda x: (x == 0).sum(), raw=True).fillna(0)
        df['zero_count_7d'] = df[target].shift(1).rolling(7, min_periods=1).apply(
            lambda x: (x == 0).sum(), raw=True).fillna(0)
        df['consecutive_zero'] = df[target].shift(1).rolling(7, min_periods=1).apply(
            lambda x: self._max_consecutive_zero(x), raw=True).fillna(0)

        # === v2.4: 近期 spike 偵測 ===
        df['spike_7d'] = df[target].shift(1).rolling(7, min_periods=1).max().fillna(0)
        df['spike_ratio'] = df['lag_1'] / (df['spike_7d'] + 1)

        # === 外部特徵 ===
        # carrier: 過去7天航母活動累計（shift(1) 避免同步洩漏，預測時可用歷史值）
        if '航母活動' in df.columns:
            carrier_raw = df['航母活動'].fillna(0).astype(str).str.strip()
            carrier_binary = ((carrier_raw != '') & (carrier_raw != '0') &
                              (carrier_raw != '0.0') & (carrier_raw != 'nan')).astype(int)
            df['carrier'] = carrier_binary.shift(1).rolling(7, min_periods=1).sum().fillna(0)
        else:
            df['carrier'] = 0
        # 保存最近 7 天航母活動用於預測階段
        self._recent_carrier = df['carrier'].iloc[-1] if len(df) > 0 else 0

        pol_feat_7d = self._create_political_features(df, df_political, 7)
        df['cn_stmt_7d'] = pol_feat_7d['cn_stmt_7d'].values

        # === v2.4: US-Taiwan 互動與外國軍艦特徵（來自 merged_comprehensive_data_M） ===
        us_tw_feat = []
        fb_feat = []
        for _, row in df.iterrows():
            current_date = row['date']
            mask = (df_political['date'] >= current_date - timedelta(days=7)) & (df_political['date'] < current_date)
            past = df_political[mask]
            us_tw_feat.append(past['US_Taiwan_interaction'].notna().sum() if 'US_Taiwan_interaction' in past.columns else 0)
            fb_feat.append(past['Foreign_battleship'].notna().sum() if 'Foreign_battleship' in past.columns else 0)
        df['us_tw_interaction_7d'] = us_tw_feat
        df['foreign_battleship_7d'] = fb_feat

        # === v2.4: 新聞分類先行指標特徵 ===
        news_features_list = []
        for _, row in df.iterrows():
            news_features_list.append(self._extract_news_features(row['date']))
        news_df = pd.DataFrame(news_features_list)
        for col in news_df.columns:
            df[col] = news_df[col].values

        # === 假日特徵 ===
        df['is_holiday'] = df['date'].apply(lambda d: get_holiday_features(d))

        # === 時間衰減權重 ===
        max_date = df['date'].max()
        days_ago = (max_date - df['date']).dt.days
        df['time_weight'] = np.exp(-0.002 * days_ago)
        df['time_weight'] = df['time_weight'] / df['time_weight'].sum() * len(df)

        df['is_high'] = (df[target] >= HIGH_THRESHOLD).astype(int)
        df = df.dropna(subset=['lag_30', 'ma_30', 'ema_14']).copy()

        self.latest_data = df
        n_features = len(self.cyclic_cols) + len(self.binary_cols) + len(self.continuous_cols) + len(self.count_cols) + len(self.holiday_cols)
        print(f"    Features: {n_features} | Samples: {len(df)}")
        return df

    def _scale_features(self, df, fit=True):
        X_cyclic = df[self.cyclic_cols].values
        X_continuous = df[self.continuous_cols].values
        X_counts = df[self.count_cols].values

        if fit:
            X_continuous_scaled = self.scaler_continuous.fit_transform(X_continuous)
            X_counts_scaled = self.scaler_counts.fit_transform(X_counts)
        else:
            X_continuous_scaled = self.scaler_continuous.transform(X_continuous)
            X_counts_scaled = self.scaler_counts.transform(X_counts)

        return np.hstack([X_cyclic, X_continuous_scaled, X_counts_scaled])

    def _create_regressor(self, quantile=None):
        """創建回歸模型"""
        if USE_CATBOOST:
            if quantile is not None:
                return cb.CatBoostRegressor(
                    iterations=CATBOOST_PARAMS['iterations'],
                    depth=CATBOOST_PARAMS['depth'],
                    learning_rate=CATBOOST_PARAMS['learning_rate'],
                    loss_function=f'Quantile:alpha={quantile}',
                    l2_leaf_reg=CATBOOST_PARAMS['l2_leaf_reg'],
                    random_state=CATBOOST_PARAMS['random_state'],
                    verbose=0
                )
            return cb.CatBoostRegressor(**CATBOOST_PARAMS)
        else:
            if quantile is not None:
                return GradientBoostingRegressor(
                    n_estimators=200, max_depth=5, learning_rate=0.05,
                    loss='quantile', alpha=quantile, random_state=42
                )
            return GradientBoostingRegressor(
                n_estimators=300, max_depth=5, learning_rate=0.05,
                loss='huber', random_state=42
            )

    def _build_single_day_features(self, window, base_date, day_offset, df_political=None):
        """v2.4: 從 window 建構單天特徵（供 walk-forward CV 和 predict_7_days 共用）"""
        target_date = base_date + timedelta(days=day_offset + 1)
        is_holiday = get_holiday_features(target_date)

        ema_7 = self._compute_ema(window[-7:], 7)
        ema_14 = self._compute_ema(window[-14:], 14)

        pct_change_1d = (window[-2] - window[-3]) / (window[-3] + 1) if len(window) >= 3 else 0
        pct_change_7d = (window[-2] - window[-9]) / (window[-9] + 1) if len(window) >= 9 else 0
        pct_change_1d = np.clip(pct_change_1d, -2, 2)
        pct_change_7d = np.clip(pct_change_7d, -2, 2)
        diff_1 = window[-2] - window[-3] if len(window) >= 3 else 0
        diff_7 = window[-2] - window[-9] if len(window) >= 9 else 0

        ma_3 = np.mean(window[-3:])
        ma_7 = np.mean(window[-7:])
        ma_14 = np.mean(window[-14:])
        ma_30 = np.mean(window[-30:])

        # 新 lag 交互特徵
        lag_1, lag_2, lag_3 = window[-1], window[-2], window[-3]
        lag_5, lag_7, lag_14 = window[-5], window[-7], window[-14]
        lag_21 = window[-21] if len(window) >= 21 else window[0]
        lag_30 = window[-30] if len(window) >= 30 else window[0]

        # 零活動偵測
        recent_3 = window[-3:]
        recent_7 = window[-7:]
        zero_count_3d = sum(1 for v in recent_3 if v == 0)
        zero_count_7d = sum(1 for v in recent_7 if v == 0)
        consecutive_zero = self._max_consecutive_zero(recent_7)

        # spike 偵測
        spike_7d = max(recent_7)
        spike_ratio = lag_1 / (spike_7d + 1)

        # 政治特徵
        pol_7d = self._get_future_political_features(target_date, 7) if df_political is None else self._get_future_political_features(target_date, 7)
        news_feat = self._extract_news_features(target_date)

        features = {
            'month_sin': np.sin(2 * np.pi * target_date.month / 12),
            'month_cos': np.cos(2 * np.pi * target_date.month / 12),
            'dow_sin': np.sin(2 * np.pi * target_date.dayofweek / 7),
            'dow_cos': np.cos(2 * np.pi * target_date.dayofweek / 7),
            'lag_1': lag_1, 'lag_2': lag_2, 'lag_3': lag_3,
            'lag_5': lag_5, 'lag_7': lag_7, 'lag_14': lag_14,
            'lag_21': lag_21, 'lag_30': lag_30,
            'ma_3': ma_3, 'ma_7': ma_7, 'ma_14': ma_14, 'ma_30': ma_30,
            'ema_7': ema_7, 'ema_14': ema_14,
            'min_7': np.min(recent_7), 'max_7': np.max(recent_7),
            'std_3': np.std(recent_3), 'std_7': np.std(recent_7),
            'std_14': np.std(window[-14:]), 'std_30': np.std(window[-30:]),
            'pct_change_1d': pct_change_1d, 'pct_change_7d': pct_change_7d,
            'diff_1': diff_1, 'diff_7': diff_7,
            'compression': min(3, ma_3 / (ma_14 + 1)),
            'trend_3d': ma_3 - ma_7, 'trend_7d': ma_7 - ma_14,
            'volatility_ratio': np.std(recent_7) / (ma_7 + 1),
            'ema_trend': ema_7 - ema_14,
            'accel_1d': lag_1 - 2 * lag_2 + lag_3,
            'lag_ratio_1_7': lag_1 / (lag_7 + 1),
            'lag_diff_1_2': lag_1 - lag_2, 'lag_diff_2_3': lag_2 - lag_3,
            'momentum_3d': lag_1 - lag_3,
            'zero_count_3d': zero_count_3d, 'zero_count_7d': zero_count_7d,
            'consecutive_zero': consecutive_zero,
            'spike_7d': spike_7d, 'spike_ratio': spike_ratio,
            'news_avg_sentiment': news_feat['news_avg_sentiment'],
            'carrier': self._recent_carrier,
            'cn_stmt_7d': pol_7d['cn_stmt_7d'],
            'us_tw_interaction_7d': 0,  # 靜態（CV 中無法即時查詢）
            'foreign_battleship_7d': 0,
            'news_military_count': news_feat['news_military_count'],
            'news_us_tw_count': news_feat['news_us_tw_count'],
            'news_relevant_count': news_feat['news_relevant_count'],
            'is_holiday': is_holiday,
        }
        return features

    def train(self, df):
        """訓練模型 (v2.4 - CatBoost + Walk-Forward Multi-Step CV + Anti-Leaking)"""
        print("[3] Training model (CatBoost)..." if USE_CATBOOST else "[3] Training model (sklearn)...")

        target = 'pla_aircraft_sorties'
        X_base = self._scale_features(df, fit=True)
        X_full = np.hstack([X_base, df[self.holiday_cols].values])

        y_reg = df[target].values
        y_clf = df['is_high'].values
        weights = df['time_weight'].values

        # === Recency Boost: 重複近期資料以強化近期 regime ===
        recent_cutoff = df['date'].max() - timedelta(weeks=RECENCY_BOOST_WEEKS)
        recent_mask = (df['date'] >= recent_cutoff).values
        n_recent = int(recent_mask.sum())

        if RECENCY_BOOST_FACTOR > 1 and n_recent > 0:
            repeat = RECENCY_BOOST_FACTOR - 1
            X_full_boosted = np.vstack([X_full] + [X_full[recent_mask]] * repeat)
            X_base_boosted = np.vstack([X_base] + [X_base[recent_mask]] * repeat)
            y_reg_boosted = np.concatenate([y_reg] + [y_reg[recent_mask]] * repeat)
            y_clf_boosted = np.concatenate([y_clf] + [y_clf[recent_mask]] * repeat)
            w_boosted = np.concatenate([weights] + [weights[recent_mask]] * repeat)
            print(f"    Recency boost: last {RECENCY_BOOST_WEEKS}w ({n_recent} rows) "
                  f"x{RECENCY_BOOST_FACTOR} -> {len(X_full_boosted)} training samples")
        else:
            X_full_boosted, X_base_boosted = X_full, X_base
            y_reg_boosted, y_clf_boosted, w_boosted = y_reg, y_clf, weights

        # === v2.4: Walk-Forward Multi-Step CV (修復 Leak #1, #2, #3) ===
        print("    [3.1] Walk-Forward Multi-Step CV (7-day horizon)...")
        EMBARGO_DAYS = 7
        n_splits = 5
        horizon = 7
        n = len(df)
        fold_size = n // (n_splits + 1)

        all_errors = {d: [] for d in range(1, horizon + 1)}
        cv_mae_scores = []

        for fold in range(n_splits):
            train_end = fold_size * (fold + 2)
            test_start = train_end + EMBARGO_DAYS  # embargo gap
            test_end = min(test_start + horizon, n)

            if test_end <= test_start or train_end < 60:
                continue

            df_train = df.iloc[:train_end].copy()
            df_test = df.iloc[test_start:test_end].copy()

            if len(df_test) == 0:
                continue

            # Per-fold scaler (修復 Leak #2)
            scaler_cont_fold = RobustScaler()
            scaler_count_fold = RobustScaler()
            X_cont_train = scaler_cont_fold.fit_transform(df_train[self.continuous_cols].values)
            X_count_train = scaler_count_fold.fit_transform(df_train[self.count_cols].values)
            X_cyc_train = df_train[self.cyclic_cols].values
            X_train_base = np.hstack([X_cyc_train, X_cont_train, X_count_train])
            X_train_full = np.hstack([X_train_base, df_train[self.holiday_cols].values])

            # Per-fold time_weight (修復 Leak #3)
            train_max_date = df_train['date'].max()
            days_ago_fold = (train_max_date - df_train['date']).dt.days
            w_fold = np.exp(-0.002 * days_ago_fold.values)
            w_fold = w_fold / w_fold.sum() * len(w_fold)

            y_train = df_train[target].values
            fold_model = self._create_regressor()
            fold_model.fit(X_train_full, np.log1p(y_train), sample_weight=w_fold)

            # 模擬 7 步迭代預測（修復 Leak #1）
            window = df_train.tail(60)[target].tolist()
            base_date = train_max_date + timedelta(days=EMBARGO_DAYS)
            fold_errors = []

            for day in range(len(df_test)):
                features = self._build_single_day_features(window, base_date, day)
                feat_df = pd.DataFrame([features])

                # 用 fold scaler transform
                X_cont_pred = scaler_cont_fold.transform(feat_df[self.continuous_cols].values)
                X_count_pred = scaler_count_fold.transform(feat_df[self.count_cols].values)
                X_cyc_pred = feat_df[self.cyclic_cols].values
                X_pred_base = np.hstack([X_cyc_pred, X_cont_pred, X_count_pred])
                X_pred_full = np.hstack([X_pred_base, feat_df[self.holiday_cols].values])

                pred = max(0, np.expm1(fold_model.predict(X_pred_full)[0]))
                actual = df_test.iloc[day][target]

                error = abs(actual - pred)
                all_errors[day + 1].append(error)
                fold_errors.append(error)
                window.append(pred)  # 用預測值更新（模擬真實情境）

            if fold_errors:
                cv_mae_scores.append(np.mean(fold_errors))

        # 報告分層 MAE
        for day in range(1, horizon + 1):
            if all_errors[day]:
                mae = np.mean(all_errors[day])
                print(f"         Day-{day} MAE: {mae:.2f} (n={len(all_errors[day])})")

        if cv_mae_scores:
            self.cv_scores = cv_mae_scores
            overall_errors = [e for errs in all_errors.values() for e in errs]
            overall_mae = np.mean(overall_errors) if overall_errors else 0
            print(f"         Overall 7-day Walk-Forward MAE: {overall_mae:.2f}")
        else:
            self.cv_scores = [0]
            print("         [Warning] No valid CV folds")

        # === 分類模型（使用 boosted 資料）===
        print("    [3.2] Training classifier...")
        k = min(5, int(y_clf_boosted.sum()) - 1)
        if k >= 1:
            smote = SMOTE(random_state=42, k_neighbors=k)
            X_res, y_res = smote.fit_resample(X_base_boosted, y_clf_boosted)
            w_res = np.ones(len(X_res))
        else:
            X_res, y_res, w_res = X_base_boosted, y_clf_boosted, w_boosted

        self.clf_model = RandomForestClassifier(
            n_estimators=200, max_depth=6, class_weight='balanced', random_state=42
        )
        self.clf_model.fit(X_res, y_res, sample_weight=w_res)

        # === 主回歸模型（使用 boosted 資料）===
        print("    [3.3] Training main regressor...")
        self.reg_model = self._create_regressor()
        self.reg_model.fit(X_full_boosted, np.log1p(y_reg_boosted), sample_weight=w_boosted)

        # === Quantile Regression（使用 boosted 資料，log1p 空間）===
        print("    [3.4] Training quantile regressors (log1p space)...")
        self.reg_lower = self._create_regressor(quantile=0.05)
        self.reg_upper = self._create_regressor(quantile=0.95)
        self.reg_lower.fit(X_full_boosted, np.log1p(y_reg_boosted), sample_weight=w_boosted)
        self.reg_upper.fit(X_full_boosted, np.log1p(y_reg_boosted), sample_weight=w_boosted)

        print(f"    Model trained! Using: {'CatBoost' if USE_CATBOOST else 'sklearn'}")
        return self

    def _get_weather_adjustment(self, target_date):
        if self.weather_data is None:
            return 1.0, "N/A"

        weather = self.weather_data[
            (self.weather_data['date'] == target_date) &
            (self.weather_data['city'].str.contains('福州|廈門|Fuzhou|Xiamen', na=False, case=False))
        ]
        if weather.empty:
            weather = self.weather_data[self.weather_data['date'] == target_date]
        if weather.empty:
            return 1.0, "N/A"

        row = weather.iloc[0]
        risk = str(row.get('risk_level', 'LOW')).upper()

        if risk == 'HIGH':
            return 0.75, "High weather risk"
        elif risk == 'MEDIUM':
            return 0.9, "Medium weather risk"
        return 1.0, "Good weather"

    def _get_future_political_features(self, target_date, window_days):
        if self.political_events is None or self.political_events.empty:
            return {f'cn_stmt_{window_days}d': 0}

        mask = (self.political_events['date'] >= target_date - timedelta(days=window_days)) & (self.political_events['date'] < target_date)
        past = self.political_events[mask]

        cn_stmt = past['Political_statement'].astype(str).str.contains('中共|中國|中方|國台辦', na=False).sum() if 'Political_statement' in past.columns else 0
        return {f'cn_stmt_{window_days}d': cn_stmt}

    def _compute_ema(self, values, span):
        """計算指數移動平均"""
        alpha = 2 / (span + 1)
        ema = values[0]
        for v in values[1:]:
            ema = alpha * v + (1 - alpha) * ema
        return ema

    @staticmethod
    def _max_consecutive_zero(x):
        """計算序列中最大連續零的長度"""
        max_count = 0
        count = 0
        for v in x:
            if v == 0:
                count += 1
                max_count = max(max_count, count)
            else:
                count = 0
        return max_count

    def predict_7_days(self):
        """預測未來7天 (v2.4 - 使用共用 _build_single_day_features)"""
        print("[4] Generating 7-day predictions...")

        df = self.latest_data
        target = 'pla_aircraft_sorties'
        current_window = df.tail(60)[target].tolist()

        predictions = []

        for i in range(PREDICTION_DAYS):
            target_date = self.latest_date + timedelta(days=i+1)

            features = self._build_single_day_features(
                current_window, self.latest_date, i)

            is_holiday = features.pop('is_holiday')
            feat_df = pd.DataFrame([features])
            X_base = self._scale_features(feat_df, fit=False)
            X_full = np.hstack([X_base, [[is_holiday]]])

            # 預測
            prob_high = self.clf_model.predict_proba(X_base)[0, 1]
            pred = max(0, np.expm1(self.reg_model.predict(X_full)[0]))

            # Quantile Regression 信賴區間 (log1p → expm1 還原)
            lower_raw = max(0, np.expm1(self.reg_lower.predict(X_full)[0]))
            upper_raw = max(0, np.expm1(self.reg_upper.predict(X_full)[0]))

            # 天氣調整
            weather_adj, weather_reason = self._get_weather_adjustment(target_date)
            pred_final = pred * weather_adj

            # 隨預測天數增加不確定性
            uncertainty_growth = 1 + i * 0.1
            lower = max(0, lower_raw * weather_adj / uncertainty_growth)
            upper = upper_raw * weather_adj * uncertainty_growth

            if lower > pred_final:
                lower = max(0, pred_final * 0.7)
            if upper < pred_final:
                upper = pred_final * 1.3

            # 風險等級
            if prob_high > 0.5:
                risk_level = 'HIGH'
            elif prob_high > 0.3:
                risk_level = 'MEDIUM-HIGH'
            elif prob_high > 0.15:
                risk_level = 'MEDIUM'
            else:
                risk_level = 'LOW'

            ema_7 = features['ema_7']
            ema_14 = features['ema_14']

            predictions.append({
                'date': target_date.strftime('%Y-%m-%d'),
                'day_of_week': target_date.strftime('%A'),
                'predicted_sorties': round(pred_final, 1),
                'lower_bound': round(lower, 1),
                'upper_bound': round(upper, 1),
                'high_event_probability': round(prob_high * 100, 1),
                'risk_level': risk_level,
                'is_cn_holiday': is_holiday,
                'weather_adjustment': round(weather_adj, 2),
                'cn_stmt_7d': features['cn_stmt_7d'],
                'ema_7': round(ema_7, 1),
                'ema_14': round(ema_14, 1)
            })

            current_window.append(pred_final)

        return pd.DataFrame(predictions)

    def run(self, sorties_path=None, political_path=None, output_path=OUTPUT_PATH):
        """執行完整流程"""
        df_sorties, df_political = self.load_data(sorties_path, political_path)
        df = self.prepare_features(df_sorties, df_political)
        self.train(df)
        predictions = self.predict_7_days()

        # 加入 metadata
        predictions['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        predictions['model_version'] = '2.4.0'
        predictions['data_latest_date'] = self.latest_date.strftime('%Y-%m-%d')
        predictions['cv_mae'] = round(np.mean(self.cv_scores), 2) if self.cv_scores else None

        # 初始化實際值欄位
        predictions['actual_sorties'] = np.nan
        predictions['prediction_error'] = np.nan

        # 讀取現有記錄並合併
        print("[5] Updating prediction history...")

        actual_data = df_sorties[['date', 'pla_aircraft_sorties']].copy()
        actual_data['date'] = actual_data['date'].dt.strftime('%Y-%m-%d')
        actual_dict = dict(zip(actual_data['date'], actual_data['pla_aircraft_sorties']))

        existing = pd.DataFrame()
        if os.path.exists(output_path):
            try:
                existing = pd.read_csv(output_path, encoding='utf-8-sig')
                print(f"    Existing records: {len(existing)}")
            except:
                pass

        if not existing.empty:
            for idx, row in existing.iterrows():
                if row['date'] in actual_dict:
                    existing.loc[idx, 'actual_sorties'] = actual_dict[row['date']]
                    if pd.notna(row['predicted_sorties']):
                        existing.loc[idx, 'prediction_error'] = actual_dict[row['date']] - row['predicted_sorties']

            overlap = set(existing['date']) & set(predictions['date'])
            if overlap:
                existing = existing[~existing['date'].isin(overlap)]

            combined = pd.concat([existing, predictions], ignore_index=True)
        else:
            combined = predictions.copy()

        for idx, row in combined.iterrows():
            if row['date'] in actual_dict:
                combined.loc[idx, 'actual_sorties'] = actual_dict[row['date']]
                if pd.notna(row['predicted_sorties']):
                    combined.loc[idx, 'prediction_error'] = actual_dict[row['date']] - row['predicted_sorties']

        combined = combined.sort_values('date').reset_index(drop=True)

        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        combined.to_csv(output_path, index=False, encoding='utf-8-sig')
        print(f"    Saved: {output_path} ({len(combined)} records)")

        # 顯示預測
        print("\n" + "=" * 85)
        print("[7-Day Prediction] - CatBoost + Quantile Regression")
        print("=" * 85)
        print(f"\n{'Date':<12} {'Day':<10} {'Pred':>8} {'90% CI':>18} {'High%':>8} {'Risk':<12}")
        print("-" * 75)

        for _, row in predictions.iterrows():
            ci = f"[{row['lower_bound']:.0f} - {row['upper_bound']:.0f}]"
            print(f"{row['date']:<12} {row['day_of_week']:<10} {row['predicted_sorties']:>8.1f} {ci:>18} {row['high_event_probability']:>7.1f}% {row['risk_level']:<12}")

        print("-" * 75)
        avg_pred = predictions['predicted_sorties'].mean()
        avg_width = (predictions['upper_bound'] - predictions['lower_bound']).mean()
        print(f"Average: {avg_pred:.1f} sorties | Avg CI Width: {avg_width:.1f}")
        if self.cv_scores:
            print(f"Cross-Validation MAE: {np.mean(self.cv_scores):.2f} +/- {np.std(self.cv_scores):.2f}")
        print("=" * 85)

        return predictions


if __name__ == "__main__":
    import argparse
    import traceback
    import sys

    parser = argparse.ArgumentParser(description='PLA 7-Day Sorties Prediction v2.3')
    parser.add_argument('--sorties', type=str, default=None, help='Path to sorties data')
    parser.add_argument('--political', type=str, default=None, help='Path to political events data')
    parser.add_argument('--output', type=str, default=OUTPUT_PATH, help='Output file path')

    args = parser.parse_args()

    try:
        predictor = PLAPredictor(high_threshold=HIGH_THRESHOLD)
        predictions = predictor.run(
            sorties_path=args.sorties,
            political_path=args.political,
            output_path=args.output
        )
        print(f"\nPrediction completed successfully!")
        sys.exit(0)
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"ERROR: Prediction failed!")
        print(f"{'='*60}")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {str(e)}")
        print(f"\nFull traceback:")
        traceback.print_exc()

        # Create a minimal error output file
        try:
            error_df = pd.DataFrame([{
                'date': datetime.now().strftime('%Y-%m-%d'),
                'error': str(e),
                'status': 'FAILED',
                'predicted_sorties': None,
                'lower_bound': None,
                'upper_bound': None,
                'high_event_probability': None,
                'risk_level': 'ERROR'
            }])
            error_df.to_csv(args.output, index=False, encoding='utf-8-sig')
            print(f"\nCreated error output file: {args.output}")
        except Exception as e2:
            print(f"Failed to create error output: {e2}")

        sys.exit(1)
