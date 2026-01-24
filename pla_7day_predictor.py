#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLA 7-Day Sorties Prediction System
====================================
整合模型：政治事件高架次預警 + 正常日回歸預測

功能：
1. 載入最新資料
2. 訓練整合模型
3. 生成未來7天預測
4. 輸出 prediction.csv

Author: PLA Data Dashboard
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import RandomForestClassifier
from imblearn.over_sampling import SMOTE
import warnings
import os

warnings.filterwarnings('ignore')

# ============================================================
# 配置
# ============================================================

# 資料來源 (可設為本地路徑或 GitHub raw URL)
DATA_SOURCES = {
    'sorties': 'https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main/data/JapanandBattleship.csv',
    'political': 'https://raw.githubusercontent.com/s0914712/pla-data-dashboard/main/data/merged_comprehensive_data_M.csv'
}

# 模型參數
HIGH_THRESHOLD = 60  # 高架次門檻
PREDICTION_DAYS = 7  # 預測天數


class PLAPredictor:
    """
    PLA 架次預測系統
    
    整合：
    1. 分類模型：預測高架次機率 (>=60)
    2. 正常日回歸：預測正常狀態基線
    3. 高架次回歸：預測高強度狀態
    """
    
    def __init__(self, high_threshold=60):
        self.high_threshold = high_threshold
        self.clf_model = None
        self.reg_normal = None
        self.reg_high = None
        self.scaler = StandardScaler()
        self.feature_cols = []
        self.political_events = None
        self.latest_data = None
        self.latest_date = None
        
    def load_data(self, sorties_path=None, political_path=None):
        """載入資料"""
        print("=" * 60)
        print("PLA 7-Day Prediction System")
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

        print(f"  架次資料: {len(df_sorties)} 筆")
        print(f"  政治事件: {len(df_political)} 筆")
        print(f"  最新日期: {self.latest_date.strftime('%Y-%m-%d')}")

        return df_sorties, df_political
    
    def _create_political_features(self, df, df_events, window_days):
        """建立政治事件特徵"""
        features = []
        
        for _, row in df.iterrows():
            current_date = row['date']
            
            mask = (df_events['date'] >= current_date - timedelta(days=window_days)) & \
                   (df_events['date'] < current_date)
            past_events = df_events[mask]
            
            # 美台互動
            us_tw = 0
            if 'US_Taiwan_interaction' in past_events.columns:
                us_tw = (past_events['US_Taiwan_interaction'].notna() & 
                         (past_events['US_Taiwan_interaction'].astype(str).str.len() > 2)).sum()
            
            # 中共政治聲明
            cn_stmt = 0
            if 'Political_statement' in past_events.columns:
                cn_stmt = past_events['Political_statement'].astype(str).str.contains(
                    '中共|中國|中方|國台辦', na=False).sum()
            
            # 外艦通過
            foreign_ship = 0
            if 'Foreign_battleship' in past_events.columns:
                foreign_ship = (past_events['Foreign_battleship'].notna() & 
                                (past_events['Foreign_battleship'].astype(str).str.len() > 2)).sum()
            
            feat = {
                f'us_tw_{window_days}d': us_tw,
                f'cn_stmt_{window_days}d': cn_stmt,
                f'foreign_ship_{window_days}d': foreign_ship,
            }
            features.append(feat)
        
        return pd.DataFrame(features)
    
    def prepare_features(self, df_sorties, df_political):
        """準備訓練特徵"""
        print(f"\n[2] 特徵工程...")
        
        df = df_sorties.copy()
        target = 'pla_aircraft_sorties'
        
        # 時間特徵
        df['month'] = df['date'].dt.month
        df['day_of_week'] = df['date'].dt.dayofweek
        df['high_risk_month'] = df['date'].dt.month.isin([4, 8, 9, 10]).astype(int)
        df['is_weekend'] = (df['day_of_week'] >= 5).astype(int)
        
        # Lag 特徵
        for lag in [1, 2, 3, 7, 14, 30]:
            df[f'lag_{lag}'] = df[target].shift(lag)
        
        # 移動統計
        for window in [3, 7, 14, 30]:
            df[f'ma_{window}'] = df[target].shift(1).rolling(window, min_periods=1).mean()
            df[f'min_{window}'] = df[target].shift(1).rolling(window, min_periods=1).min()
            df[f'max_{window}'] = df[target].shift(1).rolling(window, min_periods=1).max()
            df[f'std_{window}'] = df[target].shift(1).rolling(window, min_periods=1).std()
        
        df['has_zero_7d'] = (df['min_7'] == 0).astype(int)
        df['compression'] = df['ma_3'] / (df['ma_14'] + 1)
        
        # 海軍活動
        if '艦通過' in df.columns:
            df['naval_pass'] = df['艦通過'].fillna(0)
        else:
            df['naval_pass'] = 0
            
        if '航母活動' in df.columns:
            df['carrier'] = df['航母活動'].fillna(0)
        else:
            df['carrier'] = 0
        
        # 政治事件特徵
        for window in [3, 7]:
            pol_feat = self._create_political_features(df, df_political, window)
            for col in pol_feat.columns:
                df[col] = pol_feat[col].values
        
        # 目標變數
        df['is_high'] = (df[target] >= self.high_threshold).astype(int)
        
        # 移除 NaN
        df = df.dropna(subset=['lag_30', 'ma_30']).copy()
        
        # 定義特徵
        self.feature_cols = [
            'month', 'day_of_week', 'high_risk_month', 'is_weekend',
            'lag_1', 'lag_2', 'lag_3', 'lag_7', 'lag_14', 'lag_30',
            'ma_3', 'ma_7', 'ma_14', 'ma_30',
            'min_3', 'min_7', 'min_14', 'min_30',
            'max_3', 'max_7', 'max_14', 'max_30',
            'std_3', 'std_7', 'std_14', 'std_30',
            'has_zero_7d', 'compression',
            'naval_pass', 'carrier',
            'us_tw_3d', 'cn_stmt_3d', 'foreign_ship_3d',
            'us_tw_7d', 'cn_stmt_7d', 'foreign_ship_7d'
        ]
        
        self.latest_data = df
        
        print(f"  特徵數: {len(self.feature_cols)}")
        print(f"  訓練樣本: {len(df)}")
        print(f"  高架次樣本: {df['is_high'].sum()} ({df['is_high'].mean()*100:.2f}%)")
        
        return df
    
    def train(self, df):
        """訓練模型"""
        print(f"\n[3] 訓練模型...")
        
        target = 'pla_aircraft_sorties'
        X = df[self.feature_cols].values
        y_reg = df[target].values
        y_clf = df['is_high'].values
        
        # 標準化
        X_scaled = self.scaler.fit_transform(X)
        
        # ========== 1. 分類模型 ==========
        print("  訓練分類模型 (高架次預警)...")
        k = min(5, int(y_clf.sum()) - 1)
        if k >= 1:
            smote = SMOTE(random_state=42, k_neighbors=k)
            X_res, y_res = smote.fit_resample(X_scaled, y_clf)
        else:
            X_res, y_res = X_scaled, y_clf
        
        self.clf_model = RandomForestClassifier(
            n_estimators=200, max_depth=6, 
            class_weight='balanced', random_state=42
        )
        self.clf_model.fit(X_res, y_res)
        
        # ========== 2. 正常日回歸模型 ==========
        print("  訓練正常日回歸模型...")
        normal_mask = y_reg < self.high_threshold
        X_normal = X[normal_mask]
        y_normal = y_reg[normal_mask]
        
        from sklearn.ensemble import GradientBoostingRegressor
        self.reg_normal = GradientBoostingRegressor(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            random_state=42
        )
        self.reg_normal.fit(X_normal, y_normal)
        
        # ========== 3. 高架次回歸模型 ==========
        print("  訓練高架次回歸模型...")
        high_mask = y_reg >= 30  # 用較低門檻增加樣本
        X_high = X[high_mask]
        y_high = y_reg[high_mask]
        
        self.reg_high = GradientBoostingRegressor(
            n_estimators=150, max_depth=4, learning_rate=0.05,
            random_state=42
        )
        self.reg_high.fit(X_high, y_high)
        
        print("  模型訓練完成！")
        
        return self
    
    def _get_future_political_features(self, target_date, window_days):
        """取得未來日期的政治事件特徵"""
        if self.political_events is None:
            return {f'us_tw_{window_days}d': 0, f'cn_stmt_{window_days}d': 0, f'foreign_ship_{window_days}d': 0}
        
        mask = (self.political_events['date'] >= target_date - timedelta(days=window_days)) & \
               (self.political_events['date'] < target_date)
        past = self.political_events[mask]
        
        us_tw = 0
        if 'US_Taiwan_interaction' in past.columns:
            us_tw = (past['US_Taiwan_interaction'].notna() & 
                     (past['US_Taiwan_interaction'].astype(str).str.len() > 2)).sum()
        
        cn_stmt = 0
        if 'Political_statement' in past.columns:
            cn_stmt = past['Political_statement'].astype(str).str.contains(
                '中共|中國|中方|國台辦', na=False).sum()
        
        foreign_ship = 0
        if 'Foreign_battleship' in past.columns:
            foreign_ship = (past['Foreign_battleship'].notna() & 
                            (past['Foreign_battleship'].astype(str).str.len() > 2)).sum()
        
        return {
            f'us_tw_{window_days}d': us_tw,
            f'cn_stmt_{window_days}d': cn_stmt,
            f'foreign_ship_{window_days}d': foreign_ship
        }
    
    def predict_7_days(self):
        """預測未來7天"""
        print(f"\n[4] 生成 7 天預測...")
        
        df = self.latest_data
        target = 'pla_aircraft_sorties'
        
        # 準備滾動窗口
        recent = df.tail(60)
        current_window = recent[target].tolist()
        
        # 計算歷史基線
        normal_baseline = np.mean([x for x in current_window if x < self.high_threshold])
        
        # 預測結果
        predictions = []
        
        for i in range(PREDICTION_DAYS):
            target_date = self.latest_date + timedelta(days=i+1)
            
            # 建立特徵
            pol_3d = self._get_future_political_features(target_date, 3)
            pol_7d = self._get_future_political_features(target_date, 7)
            
            features = {
                'month': target_date.month,
                'day_of_week': target_date.dayofweek,
                'high_risk_month': 1 if target_date.month in [4, 8, 9, 10] else 0,
                'is_weekend': 1 if target_date.dayofweek >= 5 else 0,
                'lag_1': current_window[-1],
                'lag_2': current_window[-2],
                'lag_3': current_window[-3],
                'lag_7': current_window[-7],
                'lag_14': current_window[-14],
                'lag_30': current_window[-30],
                'ma_3': np.mean(current_window[-3:]),
                'ma_7': np.mean(current_window[-7:]),
                'ma_14': np.mean(current_window[-14:]),
                'ma_30': np.mean(current_window[-30:]),
                'min_3': np.min(current_window[-3:]),
                'min_7': np.min(current_window[-7:]),
                'min_14': np.min(current_window[-14:]),
                'min_30': np.min(current_window[-30:]),
                'max_3': np.max(current_window[-3:]),
                'max_7': np.max(current_window[-7:]),
                'max_14': np.max(current_window[-14:]),
                'max_30': np.max(current_window[-30:]),
                'std_3': np.std(current_window[-3:]),
                'std_7': np.std(current_window[-7:]),
                'std_14': np.std(current_window[-14:]),
                'std_30': np.std(current_window[-30:]),
                'has_zero_7d': 1 if np.min(current_window[-7:]) == 0 else 0,
                'compression': np.mean(current_window[-3:]) / (np.mean(current_window[-14:]) + 1),
                'naval_pass': 0,
                'carrier': 0,
                **pol_3d,
                **pol_7d
            }
            
            X_step = pd.DataFrame([features])[self.feature_cols].values
            X_step_scaled = self.scaler.transform(X_step)
            
            # 分類預測
            prob_high = self.clf_model.predict_proba(X_step_scaled)[0, 1]
            
            # 回歸預測
            pred_normal = max(0, self.reg_normal.predict(X_step)[0])
            pred_high = max(0, self.reg_high.predict(X_step)[0])
            
            # 整合預測
            if prob_high > 0.5:
                pred_ensemble = pred_high
            elif prob_high > 0.3:
                pred_ensemble = 0.5 * pred_normal + 0.5 * pred_high
            else:
                pred_ensemble = pred_normal
            
            # 信心區間
            uncertainty = 8 * (1 + i * 0.15)
            lower = max(0, pred_ensemble - 1.96 * uncertainty)
            upper = pred_ensemble + 1.96 * uncertainty
            
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
                'predicted_sorties': round(pred_ensemble, 1),
                'lower_bound': round(lower, 1),
                'upper_bound': round(upper, 1),
                'high_event_probability': round(prob_high * 100, 1),
                'risk_level': risk_level,
                'normal_model_pred': round(pred_normal, 1),
                'high_model_pred': round(pred_high, 1),
                'political_signal_3d': pol_3d['cn_stmt_3d'] + pol_3d['us_tw_3d'],
                'political_signal_7d': pol_7d['cn_stmt_7d'] + pol_7d['us_tw_7d']
            })
            
            # 更新窗口
            current_window.append(pred_ensemble)
        
        return pd.DataFrame(predictions)
    
    def run(self, sorties_path=None, political_path=None, output_path='prediction.csv'):
        """執行完整預測流程"""

        # Ensure output directory exists (only if path has a directory component)
        output_dir = os.path.dirname(output_path)
        if output_dir:  # Only create if not empty string
            os.makedirs(output_dir, exist_ok=True)

        # 載入資料
        df_sorties, df_political = self.load_data(sorties_path, political_path)

        # 準備特徵
        df = self.prepare_features(df_sorties, df_political)

        # 訓練模型
        self.train(df)

        # 預測
        predictions = self.predict_7_days()

        # 輸出
        print(f"\n[5] 輸出預測結果...")

        # 加入 metadata
        predictions['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        predictions['model_version'] = '2.0-ensemble'
        predictions['data_latest_date'] = self.latest_date.strftime('%Y-%m-%d')

        # 初始化新欄位
        predictions['actual_sorties'] = np.nan
        predictions['prediction_error'] = np.nan

        # ========== 歷史記錄管理 (Append + Merge) ==========
        print(f"\n[6] 比較實際值並更新歷史記錄...")

        # 載入實際值資料
        actual_data = df_sorties[['date', 'pla_aircraft_sorties']].copy()
        actual_data['date'] = actual_data['date'].dt.strftime('%Y-%m-%d')
        actual_dict = dict(zip(actual_data['date'], actual_data['pla_aircraft_sorties']))

        # 讀取現有歷史記錄
        existing_history = pd.DataFrame()
        if os.path.exists(output_path):
            try:
                existing_history = pd.read_csv(output_path, encoding='utf-8-sig')
                print(f"  讀取現有記錄: {len(existing_history)} 筆")
            except:
                existing_history = pd.DataFrame()

        # 更新現有記錄的實際值
        if not existing_history.empty:
            for idx, row in existing_history.iterrows():
                pred_date = row['date']
                if pred_date in actual_dict:
                    actual_val = actual_dict[pred_date]
                    existing_history.loc[idx, 'actual_sorties'] = actual_val
                    if pd.notna(row['predicted_sorties']) and pd.notna(actual_val):
                        existing_history.loc[idx, 'prediction_error'] = actual_val - row['predicted_sorties']

        # 合併新預測與歷史記錄
        if not existing_history.empty:
            # 移除與新預測重複的日期
            existing_dates = set(existing_history['date'])
            new_dates = set(predictions['date'])
            overlap_dates = existing_dates & new_dates

            if overlap_dates:
                print(f"  覆蓋日期: {', '.join(sorted(overlap_dates))}")
                existing_history = existing_history[~existing_history['date'].isin(overlap_dates)]

            # 合併
            combined = pd.concat([existing_history, predictions], ignore_index=True)
        else:
            combined = predictions.copy()

        # 再次更新所有記錄的實際值
        for idx, row in combined.iterrows():
            pred_date = row['date']
            if pred_date in actual_dict:
                actual_val = actual_dict[pred_date]
                combined.loc[idx, 'actual_sorties'] = actual_val
                if pd.notna(row['predicted_sorties']) and pd.notna(actual_val):
                    combined.loc[idx, 'prediction_error'] = actual_val - row['predicted_sorties']

        # 排序並儲存
        combined = combined.sort_values('date').reset_index(drop=True)
        combined.to_csv(output_path, index=False, encoding='utf-8-sig')
        print(f"  已儲存: {output_path} ({len(combined)} 筆記錄)")

        # ========== 計算預測準確度統計 ==========
        print(f"\n[7] 預測準確度分析...")

        has_actual = combined[combined['actual_sorties'].notna()]
        if len(has_actual) > 0:
            errors = has_actual['prediction_error'].dropna()
            if len(errors) > 0:
                mae = np.abs(errors).mean()
                rmse = np.sqrt((errors ** 2).mean())
                mape = (np.abs(errors) / (has_actual['actual_sorties'].dropna() + 1)).mean() * 100

                # 方向準確率 (預測高低方向是否正確)
                correct_direction = 0
                total_comparisons = 0
                for idx, row in has_actual.iterrows():
                    if pd.notna(row['predicted_sorties']) and pd.notna(row['actual_sorties']):
                        # 以 30 架次作為高低分界
                        pred_high = row['predicted_sorties'] >= 30
                        actual_high = row['actual_sorties'] >= 30
                        if pred_high == actual_high:
                            correct_direction += 1
                        total_comparisons += 1

                direction_accuracy = (correct_direction / total_comparisons * 100) if total_comparisons > 0 else 0

                print(f"  歷史預測數: {len(has_actual)}")
                print(f"  MAE (平均絕對誤差): {mae:.2f} 架次")
                print(f"  RMSE (均方根誤差): {rmse:.2f} 架次")
                print(f"  MAPE (平均百分比誤差): {mape:.1f}%")
                print(f"  方向準確率: {direction_accuracy:.1f}%")
        else:
            print("  尚無可比較的歷史預測")

        # 顯示預測
        print("\n" + "=" * 80)
        print("【7 天預測結果】")
        print("=" * 80)
        print(f"\n{'日期':<12} {'星期':<10} {'預測':>8} {'95% CI':>15} {'高架次機率':>10} {'風險':<8} {'實際':>6} {'誤差':>8}")
        print("-" * 80)

        for _, row in predictions.iterrows():
            ci = f"[{row['lower_bound']:.0f}-{row['upper_bound']:.0f}]"
            risk_emoji = {'HIGH': '🔴', 'MEDIUM-HIGH': '🟠', 'MEDIUM': '🟡', 'LOW': '🟢'}
            emoji = risk_emoji.get(row['risk_level'], '')

            actual_str = f"{row['actual_sorties']:.0f}" if pd.notna(row['actual_sorties']) else '-'
            error_str = f"{row['prediction_error']:+.1f}" if pd.notna(row['prediction_error']) else '-'

            print(f"{row['date']:<12} {row['day_of_week']:<10} {row['predicted_sorties']:>8.1f} {ci:>15} "
                  f"{row['high_event_probability']:>9.1f}% {emoji} {row['risk_level']:<8} {actual_str:>6} {error_str:>8}")

        print("-" * 80)
        print(f"平均預測: {predictions['predicted_sorties'].mean():.1f} 架次")
        print(f"最高風險日: {predictions.loc[predictions['high_event_probability'].idxmax(), 'date']}")

        return predictions


# ============================================================
# 主程式
# ============================================================

if __name__ == "__main__":
    import argparse
    import traceback
    import sys

    parser = argparse.ArgumentParser(description='PLA 7-Day Sorties Prediction')
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

        # Create a minimal error output file so the workflow doesn't fail on cp
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

