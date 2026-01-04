#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLA Aircraft Sortie Prediction Model
自動化訓練與預測腳本 - 用於 GitHub Actions
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.preprocessing import StandardScaler, MinMaxScaler, RobustScaler
from catboost import CatBoostRegressor
import joblib
import json
import os
import warnings
warnings.filterwarnings('ignore')

# ========== 配置 ==========
TARGET_COL = 'pla_aircraft_sorties'
EVENT_COLUMNS = ['聯合演訓', '艦通過', '航母活動', '與那國', '宮古', '大禹', '對馬', '進', '出']
PREDICTION_DAYS = 7
MIN_PREDICTION = 2.0

# ========== 多尺度正規化器 ==========
class MultiScaleNormalizer:
    """不同類型特徵使用不同的正規化策略"""
    def __init__(self):
        self.scalers = {}
        self.feature_groups = {}

    def fit(self, X, feature_groups):
        self.feature_groups = feature_groups
        
        if 'binary' in feature_groups:
            self.scalers['binary'] = None
        
        if 'weighted' in feature_groups:
            self.scalers['weighted'] = RobustScaler()
            weighted_cols = feature_groups['weighted']
            if len(weighted_cols) > 0:
                self.scalers['weighted'].fit(X[weighted_cols])
        
        if 'numerical' in feature_groups:
            self.scalers['numerical'] = StandardScaler()
            numerical_cols = feature_groups['numerical']
            if len(numerical_cols) > 0:
                self.scalers['numerical'].fit(X[numerical_cols])
        
        if 'cyclical' in feature_groups:
            self.scalers['cyclical'] = None
        
        if 'other' in feature_groups:
            self.scalers['other'] = MinMaxScaler()
            other_cols = feature_groups['other']
            if len(other_cols) > 0:
                self.scalers['other'].fit(X[other_cols])
        
        return self

    def transform(self, X):
        X_scaled = X.copy()
        for group_name, cols in self.feature_groups.items():
            if len(cols) == 0:
                continue
            if group_name == 'binary' or group_name == 'cyclical':
                pass
            elif group_name in self.scalers and self.scalers[group_name] is not None:
                X_scaled[cols] = self.scalers[group_name].transform(X[cols])
        return X_scaled

    def fit_transform(self, X, feature_groups):
        self.fit(X, feature_groups)
        return self.transform(X)


def load_and_prepare_data(csv_path):
    """載入並準備數據"""
    print(f"[1] 載入數據: {csv_path}")
    df = pd.read_csv(csv_path)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    
    # 基本時間特徵
    df['year'] = df['date'].dt.year
    df['month'] = df['date'].dt.month
    df['quarter'] = df['date'].dt.quarter
    df['day_of_week'] = df['date'].dt.dayofweek
    df['day_of_month'] = df['date'].dt.day
    
    # 篩選年份
    df = df[(df['year'] >= 2022) & (df['year'] <= 2026)]
    
    # 填充事件欄位
    for col in EVENT_COLUMNS:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype(int)
    
    print(f"   數據範圍: {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")
    print(f"   總筆數: {len(df)}")
    
    return df


def calculate_event_weights(train_data, target_col, event_cols):
    """計算每個事件發生時的平均架次"""
    event_weights = {}
    baseline_sorties = train_data[target_col].mean()
    
    print("\n[2] 事件影響力分析:")
    print("-" * 60)
    
    for event in event_cols:
        if event in train_data.columns:
            event_data = train_data[train_data[event] == 1]
            if len(event_data) > 0:
                avg_sorties = event_data[target_col].mean()
                event_count = len(event_data)
                relative_impact = avg_sorties / baseline_sorties
            else:
                avg_sorties = baseline_sorties
                event_count = 0
                relative_impact = 1.0
            
            event_weights[event] = {
                'avg_sorties': avg_sorties,
                'count': event_count,
                'relative_impact': relative_impact
            }
            print(f"   {event}: 發生{event_count}次, 平均{avg_sorties:.1f}架次, 影響係數{relative_impact:.2f}")
    
    return event_weights


def create_weighted_features(df, event_weights, is_train=True):
    """創建加權的事件特徵"""
    
    for event, weights in event_weights.items():
        if event in df.columns:
            df[f'{event}_binary'] = df[event]
            df[f'{event}_weighted'] = df[event] * weights['avg_sorties']
            df[f'{event}_impact'] = df[event] * weights['relative_impact']
    
    # 組合威脅指數
    df['threat_weighted'] = 0
    for event in ['航母活動', '聯合演訓', '與那國', '宮古', '艦通過', '大禹', '對馬']:
        if event in event_weights:
            weight = event_weights[event]['avg_sorties']
            df['threat_weighted'] += df[event] * weight
    
    # 交互特徵
    if '航母活動' in event_weights and '與那國' in event_weights:
        carrier_weight = event_weights['航母活動']['avg_sorties']
        strait_weight = (event_weights['與那國']['avg_sorties'] +
                        event_weights.get('宮古', {}).get('avg_sorties', 0)) / 2
        df['carrier_strait_weighted'] = (
            df['航母活動'] * carrier_weight *
            (df['與那國'] + df['宮古']) * strait_weight
        )
    
    # 多重事件效應
    df['multi_event_weighted'] = 0
    for event in EVENT_COLUMNS:
        if event in event_weights:
            df['multi_event_weighted'] += df[event] * event_weights[event]['avg_sorties']
    
    df['event_complexity'] = df[EVENT_COLUMNS].sum(axis=1) * df['multi_event_weighted']
    
    return df


def create_numerical_features(df, target_col):
    """創建數值型特徵"""
    
    # Lag 特徵
    for lag in [1, 3, 7, 14, 21, 30]:
        df[f'sortie_lag_{lag}'] = df[target_col].shift(lag)
    
    # 移動平均
    for window in [3, 7, 14, 21, 30]:
        df[f'sortie_ma_{window}'] = df[target_col].rolling(window, min_periods=1).mean()
        df[f'sortie_std_{window}'] = df[target_col].rolling(window, min_periods=max(2, window//3)).std()
    
    # 差分
    df['sortie_diff_1'] = df[target_col].diff(1)
    df['sortie_diff_3'] = df[target_col].diff(3)
    df['sortie_diff_7'] = df[target_col].diff(7)
    
    # 百分比變化
    df['sortie_pct_change_1'] = df[target_col].pct_change(1)
    df['sortie_pct_change_7'] = df[target_col].pct_change(7)
    
    # 週期性特徵
    df['month_sin'] = np.sin(2 * np.pi * df['month'] / 12)
    df['month_cos'] = np.cos(2 * np.pi * df['month'] / 12)
    df['dow_sin'] = np.sin(2 * np.pi * df['day_of_week'] / 7)
    df['dow_cos'] = np.cos(2 * np.pi * df['day_of_week'] / 7)
    
    # 二元時間特徵
    df['is_weekend'] = (df['day_of_week'] >= 5).astype(int)
    df['is_high_season'] = df['quarter'].isin([2, 3]).astype(int)
    
    return df


def get_feature_columns(df):
    """獲取特徵欄位分類"""
    binary_features = [col for col in df.columns if col.endswith('_binary')]
    weighted_features = [col for col in df.columns if col.endswith('_weighted') or col.endswith('_impact')]
    numerical_features = [col for col in df.columns if
                         'sortie_lag' in col or 'sortie_ma' in col or
                         'sortie_diff' in col or 'sortie_std' in col or
                         'sortie_pct' in col]
    cyclical_features = [col for col in df.columns if 'sin' in col or 'cos' in col]
    other_features = ['is_weekend', 'is_high_season', 'threat_weighted',
                     'multi_event_weighted', 'event_complexity']
    
    all_feature_cols = (binary_features + weighted_features +
                       numerical_features + cyclical_features + other_features)
    
    exclude_cols = ['date', TARGET_COL, 'year', 'month', 'quarter', 
                   'day_of_week', 'day_of_month'] + EVENT_COLUMNS
    feature_cols = [col for col in all_feature_cols if col in df.columns and col not in exclude_cols]
    
    # 去重
    seen = set()
    feature_cols = [x for x in feature_cols if not (x in seen or seen.add(x))]
    
    feature_groups = {
        'binary': [col for col in feature_cols if col in binary_features],
        'weighted': [col for col in feature_cols if col in weighted_features],
        'numerical': [col for col in feature_cols if col in numerical_features],
        'cyclical': [col for col in feature_cols if col in cyclical_features],
        'other': [col for col in feature_cols if col in other_features]
    }
    
    return feature_cols, feature_groups


def train_models(X_train, y_train, X_test, y_test):
    """訓練多個模型"""
    
    models_config = {
        'Conservative': {
            'iterations': 500,
            'learning_rate': 0.03,
            'depth': 5,
            'l2_leaf_reg': 5,
            'subsample': 0.7,
            'colsample_bylevel': 0.7,
            'min_data_in_leaf': 30,
            'random_seed': 42
        },
        'Balanced': {
            'iterations': 600,
            'learning_rate': 0.04,
            'depth': 6,
            'l2_leaf_reg': 4,
            'subsample': 0.75,
            'colsample_bylevel': 0.8,
            'min_data_in_leaf': 25,
            'random_seed': 42
        },
        'Aggressive': {
            'iterations': 700,
            'learning_rate': 0.05,
            'depth': 6,
            'l2_leaf_reg': 3,
            'subsample': 0.8,
            'colsample_bylevel': 0.85,
            'min_data_in_leaf': 20,
            'random_seed': 42
        }
    }
    
    models_dict = {}
    results = []
    
    print("\n[4] 訓練模型:")
    for model_name, params in models_config.items():
        print(f"   訓練 {model_name}...")
        model = CatBoostRegressor(**params, verbose=False)
        model.fit(
            X_train, y_train,
            eval_set=(X_test, y_test),
            early_stopping_rounds=50,
            verbose=False
        )
        
        models_dict[model_name] = model
        y_pred = model.predict(X_test)
        
        mae = mean_absolute_error(y_test, y_pred)
        rmse = np.sqrt(mean_squared_error(y_test, y_pred))
        r2 = r2_score(y_test, y_pred)
        
        results.append({
            'Model': model_name,
            'RMSE': rmse,
            'MAE': mae,
            'R2': r2
        })
        print(f"      RMSE: {rmse:.4f}, MAE: {mae:.4f}, R²: {r2:.4f}")
    
    return models_dict, results


def predict_future(n_days, models_dict, normalizer, event_weights, feature_cols, 
                   last_data, future_events=None):
    """預測未來 N 天"""
    
    last_date = last_data['date'].max()
    print(f"\n[5] 預測未來 {n_days} 天 (從 {last_date.strftime('%Y-%m-%d')} 開始)")
    
    if future_events is None:
        future_events = {event: [0] * n_days for event in EVENT_COLUMNS}
    
    future_dates = [last_date + timedelta(days=i+1) for i in range(n_days)]
    future_df = pd.DataFrame({'date': future_dates})
    
    future_df['year'] = future_df['date'].dt.year
    future_df['month'] = future_df['date'].dt.month
    future_df['quarter'] = future_df['date'].dt.quarter
    future_df['day_of_week'] = future_df['date'].dt.dayofweek
    future_df['day_of_month'] = future_df['date'].dt.day
    
    for event in EVENT_COLUMNS:
        future_df[event] = future_events.get(event, [0] * n_days)
    
    historical_mean = last_data[TARGET_COL].tail(30).mean()
    future_df[TARGET_COL] = historical_mean
    
    history_window = 60
    historical_data = last_data.tail(history_window).copy()
    
    predictions_list = []
    
    for day_idx in range(n_days):
        combined_df = pd.concat([historical_data, future_df.iloc[:day_idx+1]], ignore_index=True)
        combined_df = combined_df.sort_values('date').reset_index(drop=True)
        
        combined_df = create_weighted_features(combined_df, event_weights, is_train=False)
        combined_df = create_numerical_features(combined_df, TARGET_COL)
        
        current_row = combined_df.tail(1).copy()
        
        for col in feature_cols:
            if col in current_row.columns:
                current_row[col] = pd.to_numeric(current_row[col], errors='coerce').fillna(0)
                current_row[col] = current_row[col].replace([np.inf, -np.inf], 0)
            else:
                current_row[col] = 0
        
        X_future = current_row[feature_cols]
        X_future_scaled = normalizer.transform(X_future)
        
        day_predictions = []
        for model_name in ['Conservative', 'Balanced', 'Aggressive']:
            if model_name in models_dict:
                pred = models_dict[model_name].predict(X_future_scaled)[0]
                pred = max(MIN_PREDICTION, pred)
                day_predictions.append(pred)
        
        ensemble_pred = np.mean(day_predictions)
        if ensemble_pred < MIN_PREDICTION:
            ensemble_pred = max(MIN_PREDICTION, historical_mean * 0.5)
        
        predictions_list.append({
            'date': future_dates[day_idx].strftime('%Y-%m-%d'),
            'predicted_sorties': round(ensemble_pred, 2),
            'conservative': round(day_predictions[0], 2) if len(day_predictions) > 0 else MIN_PREDICTION,
            'balanced': round(day_predictions[1], 2) if len(day_predictions) > 1 else MIN_PREDICTION,
            'aggressive': round(day_predictions[2], 2) if len(day_predictions) > 2 else MIN_PREDICTION
        })
        
        future_df.loc[day_idx, TARGET_COL] = ensemble_pred
        print(f"   {future_dates[day_idx].strftime('%Y-%m-%d')}: {ensemble_pred:.1f} 架次")
    
    return pd.DataFrame(predictions_list)


def main():
    """主程式"""
    print("=" * 70)
    print("PLA AIRCRAFT SORTIE PREDICTION")
    print(f"執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # 確定數據路徑
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(script_dir, 'data', 'JapanandBattleship.csv')
    
    if not os.path.exists(data_path):
        # 嘗試其他可能的路徑
        data_path = os.path.join(script_dir, 'JapanandBattleship.csv')
    
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"找不到數據文件: {data_path}")
    
    # 載入數據
    df = load_and_prepare_data(data_path)
    
    # 分割數據
    split_idx = int(len(df) * 0.8)
    train_df = df[:split_idx].copy()
    test_df = df[split_idx:].copy()
    
    print(f"\n   訓練集: {len(train_df)} 筆")
    print(f"   測試集: {len(test_df)} 筆")
    
    # 填充缺失值
    train_median = train_df[TARGET_COL].median()
    train_df[TARGET_COL] = train_df[TARGET_COL].fillna(train_median)
    test_df[TARGET_COL] = test_df[TARGET_COL].fillna(train_median)
    
    # 計算事件權重
    event_weights = calculate_event_weights(train_df, TARGET_COL, EVENT_COLUMNS)
    
    # 創建特徵
    print("\n[3] 創建特徵...")
    train_df = create_weighted_features(train_df, event_weights, is_train=True)
    test_df = create_weighted_features(test_df, event_weights, is_train=False)
    train_df = create_numerical_features(train_df, TARGET_COL)
    test_df = create_numerical_features(test_df, TARGET_COL)
    
    # 獲取特徵欄位
    feature_cols, feature_groups = get_feature_columns(train_df)
    print(f"   總特徵數: {len(feature_cols)}")
    
    # 清理數據
    for col in feature_cols:
        if col in train_df.columns:
            train_df[col] = pd.to_numeric(train_df[col], errors='coerce').fillna(0)
            train_df[col] = train_df[col].replace([np.inf, -np.inf], 0)
        if col in test_df.columns:
            test_df[col] = pd.to_numeric(test_df[col], errors='coerce').fillna(0)
            test_df[col] = test_df[col].replace([np.inf, -np.inf], 0)
    
    # 準備訓練數據
    X_train = train_df[feature_cols]
    X_test = test_df[feature_cols]
    y_train = train_df[TARGET_COL]
    y_test = test_df[TARGET_COL]
    
    # 正規化
    normalizer = MultiScaleNormalizer()
    X_train_scaled = normalizer.fit_transform(X_train, feature_groups)
    X_test_scaled = normalizer.transform(X_test)
    
    # 訓練模型
    models_dict, results = train_models(X_train_scaled, y_train, X_test_scaled, y_test)
    
    # 預測未來
    predictions_df = predict_future(
        n_days=PREDICTION_DAYS,
        models_dict=models_dict,
        normalizer=normalizer,
        event_weights=event_weights,
        feature_cols=feature_cols,
        last_data=df,
        future_events=None
    )
    
    # 保存結果
    output_dir = os.path.join(script_dir, 'output')
    os.makedirs(output_dir, exist_ok=True)
    
    # 保存預測結果
    prediction_path = os.path.join(output_dir, 'prediction.csv')
    predictions_df.to_csv(prediction_path, index=False, encoding='utf-8-sig')
    print(f"\n✓ 預測結果已保存: {prediction_path}")
    
    # 保存模型性能
    results_df = pd.DataFrame(results)
    results_path = os.path.join(output_dir, 'model_performance.csv')
    results_df.to_csv(results_path, index=False)
    print(f"✓ 模型性能已保存: {results_path}")
    
    # 保存訓練元數據
    metadata = {
        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'data_range': {
            'start': df['date'].min().strftime('%Y-%m-%d'),
            'end': df['date'].max().strftime('%Y-%m-%d')
        },
        'train_samples': len(train_df),
        'test_samples': len(test_df),
        'best_rmse': min([r['RMSE'] for r in results]),
        'prediction_days': PREDICTION_DAYS,
        'event_weights': {k: {'avg_sorties': v['avg_sorties'], 
                              'relative_impact': v['relative_impact']} 
                         for k, v in event_weights.items()}
    }
    
    metadata_path = os.path.join(output_dir, 'metadata.json')
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(f"✓ 元數據已保存: {metadata_path}")
    
    # 保存模型
    model_path = os.path.join(output_dir, 'models.pkl')
    joblib.dump({
        'models': models_dict,
        'normalizer': normalizer,
        'event_weights': event_weights,
        'feature_cols': feature_cols
    }, model_path)
    print(f"✓ 模型已保存: {model_path}")
    
    print("\n" + "=" * 70)
    print("預測完成！")
    print("=" * 70)
    
    return predictions_df


if __name__ == '__main__':
    main()
