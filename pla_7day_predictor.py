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

Usage:
    python pla_predictor.py

Author: PLA Data Dashboard
Version: 2.3.0
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
}

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

# ============================================================
# 中國假日資料 (2020-2026)
# ============================================================

CN_HOLIDAY_DATES = set()

# 2020-2026 假日
for d in ['2020-01-01'] + [f'2020-01-{d:02d}' for d in range(24, 32)] + [f'2020-02-{d:02d}' for d in range(1, 3)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2020-04-{d:02d}' for d in range(4, 7)] + [f'2020-05-{d:02d}' for d in range(1, 6)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2020-06-{d:02d}' for d in range(25, 28)] + [f'2020-10-{d:02d}' for d in range(1, 9)]:
    CN_HOLIDAY_DATES.add(d)

for d in [f'2021-01-{d:02d}' for d in range(1, 4)] + [f'2021-02-{d:02d}' for d in range(11, 18)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2021-04-{d:02d}' for d in range(3, 6)] + [f'2021-05-{d:02d}' for d in range(1, 6)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2021-06-{d:02d}' for d in range(12, 15)] + [f'2021-09-{d:02d}' for d in range(19, 22)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2021-10-{d:02d}' for d in range(1, 8)]:
    CN_HOLIDAY_DATES.add(d)

for d in [f'2022-01-{d:02d}' for d in range(1, 4)] + ['2022-01-31'] + [f'2022-02-{d:02d}' for d in range(1, 7)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2022-04-{d:02d}' for d in range(3, 6)] + ['2022-04-30'] + [f'2022-05-{d:02d}' for d in range(1, 5)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2022-06-{d:02d}' for d in range(3, 6)] + [f'2022-09-{d:02d}' for d in range(10, 13)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2022-10-{d:02d}' for d in range(1, 8)]:
    CN_HOLIDAY_DATES.add(d)

for d in ['2022-12-31'] + [f'2023-01-{d:02d}' for d in range(1, 3)] + [f'2023-01-{d:02d}' for d in range(21, 28)]:
    CN_HOLIDAY_DATES.add(d)
for d in ['2023-04-05'] + [f'2023-04-{d:02d}' for d in [29, 30]] + [f'2023-05-{d:02d}' for d in range(1, 4)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2023-06-{d:02d}' for d in range(22, 25)] + [f'2023-09-{d:02d}' for d in [29, 30]]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2023-10-{d:02d}' for d in range(1, 7)]:
    CN_HOLIDAY_DATES.add(d)

for d in ['2023-12-30', '2023-12-31', '2024-01-01'] + [f'2024-02-{d:02d}' for d in range(10, 18)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2024-04-{d:02d}' for d in range(4, 7)] + [f'2024-05-{d:02d}' for d in range(1, 6)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2024-06-{d:02d}' for d in range(8, 11)] + [f'2024-09-{d:02d}' for d in range(15, 18)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2024-10-{d:02d}' for d in range(1, 8)]:
    CN_HOLIDAY_DATES.add(d)

for d in ['2025-01-01'] + [f'2025-01-{d:02d}' for d in range(28, 32)] + [f'2025-02-{d:02d}' for d in range(1, 5)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2025-04-{d:02d}' for d in range(4, 7)] + [f'2025-05-{d:02d}' for d in range(1, 6)] + ['2025-05-31']:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2025-06-{d:02d}' for d in range(1, 3)] + [f'2025-10-{d:02d}' for d in range(1, 9)]:
    CN_HOLIDAY_DATES.add(d)

for d in [f'2026-01-{d:02d}' for d in range(1, 4)] + [f'2026-02-{d:02d}' for d in range(15, 24)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2026-04-{d:02d}' for d in range(4, 7)] + [f'2026-05-{d:02d}' for d in range(1, 6)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2026-06-{d:02d}' for d in range(19, 22)] + [f'2026-09-{d:02d}' for d in range(25, 28)]:
    CN_HOLIDAY_DATES.add(d)
for d in [f'2026-10-{d:02d}' for d in range(1, 8)]:
    CN_HOLIDAY_DATES.add(d)


def get_holiday_features(date):
    """取得假日特徵"""
    date_str = date.strftime('%Y-%m-%d')
    is_holiday = date_str in CN_HOLIDAY_DATES
    return int(is_holiday)


class PLAPredictor:
    """PLA 架次預測系統 v2.3 - CatBoost"""

    def __init__(self, high_threshold=None):
        self.reg_model = None           # 主回歸模型
        self.reg_lower = None           # 下界分位數回歸
        self.reg_upper = None           # 上界分位數回歸
        self.clf_model = None           # 分類模型
        self.scaler_continuous = RobustScaler()
        self.scaler_counts = RobustScaler()
        self.political_events = None
        self.weather_data = None
        self.latest_data = None
        self.latest_date = None
        self.high_threshold = high_threshold or HIGH_THRESHOLD
        self.cv_scores = []

        # v2.3: 精簡至 32 個核心特徵
        self.cyclic_cols = ['month_sin', 'month_cos', 'dow_sin', 'dow_cos']
        self.binary_cols = []  # 移除低效二元特徵
        self.continuous_cols = [
            # 滯後特徵
            'lag_1', 'lag_7', 'lag_14', 'lag_30',
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
            'ema_trend'
        ]
        self.count_cols = ['carrier', 'cn_stmt_7d']
        self.holiday_cols = ['is_holiday']

    def load_data(self, sorties_path=None, political_path=None):
        """載入資料"""
        print("=" * 60)
        print("PLA 7-Day Prediction System v2.3")
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

        print(f"  架次資料: {len(df_sorties)} 筆")
        print(f"  政治事件: {len(df_political)} 筆")
        print(f"  最新日期: {self.latest_date.strftime('%Y-%m-%d')}")

        return df_sorties, df_political

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

        # === 滯後特徵 ===
        for lag in [1, 7, 14, 30]:
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

        # === 外部特徵 ===
        df['carrier'] = df['航母活動'].fillna(0) if '航母活動' in df.columns else 0

        pol_feat_7d = self._create_political_features(df, df_political, 7)
        df['cn_stmt_7d'] = pol_feat_7d['cn_stmt_7d'].values

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

    def train(self, df):
        """訓練模型 (v2.3 - CatBoost + TimeSeriesSplit + Quantile Regression)"""
        print("[3] Training model (CatBoost)..." if USE_CATBOOST else "[3] Training model (sklearn)...")

        target = 'pla_aircraft_sorties'
        X_base = self._scale_features(df, fit=True)
        X_full = np.hstack([X_base, df[self.holiday_cols].values])

        y_reg = df[target].values
        y_clf = df['is_high'].values
        weights = df['time_weight'].values

        # === TimeSeriesSplit 交叉驗證 ===
        print("    [3.1] TimeSeriesSplit cross-validation...")
        tscv = TimeSeriesSplit(n_splits=5)
        cv_mae_scores = []
        cv_rmse_scores = []

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X_full)):
            X_train, X_val = X_full[train_idx], X_full[val_idx]
            y_train, y_val = y_reg[train_idx], y_reg[val_idx]
            w_train = weights[train_idx]

            fold_model = self._create_regressor()
            fold_model.fit(X_train, np.log1p(y_train), sample_weight=w_train)

            y_pred = np.expm1(fold_model.predict(X_val))
            y_pred = np.maximum(0, y_pred)

            mae = np.mean(np.abs(y_val - y_pred))
            rmse = np.sqrt(np.mean((y_val - y_pred) ** 2))
            cv_mae_scores.append(mae)
            cv_rmse_scores.append(rmse)

        self.cv_scores = cv_mae_scores
        print(f"         CV MAE:  {np.mean(cv_mae_scores):.2f} +/- {np.std(cv_mae_scores):.2f}")
        print(f"         CV RMSE: {np.mean(cv_rmse_scores):.2f} +/- {np.std(cv_rmse_scores):.2f}")

        # === 分類模型 ===
        print("    [3.2] Training classifier...")
        k = min(5, int(y_clf.sum()) - 1)
        if k >= 1:
            smote = SMOTE(random_state=42, k_neighbors=k)
            X_res, y_res = smote.fit_resample(X_base, y_clf)
            w_res = np.ones(len(X_res))
        else:
            X_res, y_res, w_res = X_base, y_clf, weights

        self.clf_model = RandomForestClassifier(
            n_estimators=200, max_depth=6, class_weight='balanced', random_state=42
        )
        self.clf_model.fit(X_res, y_res, sample_weight=w_res)

        # === 主回歸模型 ===
        print("    [3.3] Training main regressor...")
        self.reg_model = self._create_regressor()
        self.reg_model.fit(X_full, np.log1p(y_reg), sample_weight=weights)

        # === Quantile Regression ===
        print("    [3.4] Training quantile regressors...")
        self.reg_lower = self._create_regressor(quantile=0.05)
        self.reg_upper = self._create_regressor(quantile=0.95)
        self.reg_lower.fit(X_full, y_reg, sample_weight=weights)
        self.reg_upper.fit(X_full, y_reg, sample_weight=weights)

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

    def predict_7_days(self):
        """預測未來7天 (v2.3 - Quantile Regression 信賴區間)"""
        print("[4] Generating 7-day predictions...")

        df = self.latest_data
        target = 'pla_aircraft_sorties'
        current_window = df.tail(60)[target].tolist()

        predictions = []

        for i in range(PREDICTION_DAYS):
            target_date = self.latest_date + timedelta(days=i+1)
            is_holiday = get_holiday_features(target_date)
            pol_7d = self._get_future_political_features(target_date, 7)

            # 計算 EMA
            ema_7 = self._compute_ema(current_window[-7:], 7)
            ema_14 = self._compute_ema(current_window[-14:], 14)

            # 計算變化率和差分 (使用 shift 避免 leaking)
            pct_change_1d = (current_window[-2] - current_window[-3]) / (current_window[-3] + 1) if len(current_window) >= 3 else 0
            pct_change_7d = (current_window[-2] - current_window[-9]) / (current_window[-9] + 1) if len(current_window) >= 9 else 0
            pct_change_1d = np.clip(pct_change_1d, -2, 2)
            pct_change_7d = np.clip(pct_change_7d, -2, 2)

            diff_1 = current_window[-2] - current_window[-3] if len(current_window) >= 3 else 0
            diff_7 = current_window[-2] - current_window[-9] if len(current_window) >= 9 else 0

            features = {
                'month_sin': np.sin(2 * np.pi * target_date.month / 12),
                'month_cos': np.cos(2 * np.pi * target_date.month / 12),
                'dow_sin': np.sin(2 * np.pi * target_date.dayofweek / 7),
                'dow_cos': np.cos(2 * np.pi * target_date.dayofweek / 7),
                'lag_1': current_window[-1],
                'lag_7': current_window[-7], 'lag_14': current_window[-14], 'lag_30': current_window[-30],
                'ma_3': np.mean(current_window[-3:]), 'ma_7': np.mean(current_window[-7:]),
                'ma_14': np.mean(current_window[-14:]), 'ma_30': np.mean(current_window[-30:]),
                'ema_7': ema_7, 'ema_14': ema_14,
                'min_7': np.min(current_window[-7:]),
                'max_7': np.max(current_window[-7:]),
                'std_3': np.std(current_window[-3:]), 'std_7': np.std(current_window[-7:]),
                'std_14': np.std(current_window[-14:]), 'std_30': np.std(current_window[-30:]),
                'pct_change_1d': pct_change_1d, 'pct_change_7d': pct_change_7d,
                'diff_1': diff_1, 'diff_7': diff_7,
                'compression': min(3, np.mean(current_window[-3:]) / (np.mean(current_window[-14:]) + 1)),
                'trend_3d': np.mean(current_window[-3:]) - np.mean(current_window[-7:]),
                'trend_7d': np.mean(current_window[-7:]) - np.mean(current_window[-14:]),
                'volatility_ratio': np.std(current_window[-7:]) / (np.mean(current_window[-7:]) + 1),
                'ema_trend': ema_7 - ema_14,
                'carrier': 0,
                'cn_stmt_7d': pol_7d['cn_stmt_7d']
            }

            feat_df = pd.DataFrame([features])
            X_base = self._scale_features(feat_df, fit=False)
            X_full = np.hstack([X_base, [[is_holiday]]])

            # 預測
            prob_high = self.clf_model.predict_proba(X_base)[0, 1]
            pred = max(0, np.expm1(self.reg_model.predict(X_full)[0]))

            # Quantile Regression 信賴區間
            lower_raw = max(0, self.reg_lower.predict(X_full)[0])
            upper_raw = max(0, self.reg_upper.predict(X_full)[0])

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
                'cn_stmt_7d': pol_7d['cn_stmt_7d'],
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
        predictions['model_version'] = '2.3.0'
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
    parser.add_argument('--output', type=str, default='prediction.csv', help='Output file path')

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
