#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多機場天氣爬蟲 - GitHub Actions 版本
使用 Windy API 取得福州、上海、廣州機場天氣預報
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
import os
import math
import logging

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================
# 機場資料庫
# ============================================
AIRPORTS = {
    'ZSFZ': {
        'icao': 'ZSFZ',
        'iata': 'FOC',
        'name': '福州長樂國際機場',
        'city': '福州',
        'lat': 25.9311,
        'lon': 119.6631
    },
    'ZSPD': {
        'icao': 'ZSPD',
        'iata': 'PVG',
        'name': '上海浦東國際機場',
        'city': '上海',
        'lat': 31.1434,
        'lon': 121.8052
    },
    'ZGGG': {
        'icao': 'ZGGG',
        'iata': 'CAN',
        'name': '廣州白雲國際機場',
        'city': '廣州',
        'lat': 23.3924,
        'lon': 113.2988
    }
}

API_URL = "https://api.windy.com/api/point-forecast/v2"


def fetch_weather(api_key: str, lat: float, lon: float) -> dict:
    """取得天氣資料"""
    payload = {
        "lat": lat,
        "lon": lon,
        "model": "gfs",
        "parameters": ["temp", "dewpoint", "wind", "windGust", "rh", "precip", "lclouds", "mclouds", "hclouds", "ptype"],
        "levels": ["surface"],
        "key": api_key
    }
    
    response = requests.post(API_URL, json=payload, headers={'Content-Type': 'application/json'}, timeout=30)
    response.raise_for_status()
    return response.json()


def parse_weather(api_data: dict, idx: int) -> dict:
    """解析單一時間點天氣"""
    weather = {}
    
    # 溫度 (K -> C)
    if 'temp-surface' in api_data and api_data['temp-surface'][idx] is not None:
        weather['temp'] = round(api_data['temp-surface'][idx] - 273.15, 1)
    else:
        weather['temp'] = None
    
    # 露點 (K -> C)
    if 'dewpoint-surface' in api_data and api_data['dewpoint-surface'][idx] is not None:
        weather['dewpoint'] = round(api_data['dewpoint-surface'][idx] - 273.15, 1)
    else:
        weather['dewpoint'] = None
    
    # 風速
    if 'wind_u-surface' in api_data and 'wind_v-surface' in api_data:
        u = api_data['wind_u-surface'][idx]
        v = api_data['wind_v-surface'][idx]
        if u is not None and v is not None:
            wind_ms = math.sqrt(u**2 + v**2)
            weather['wind_knots'] = round(wind_ms * 1.94384, 1)
            weather['wind_dir'] = round((270 - math.atan2(v, u) * 180 / math.pi) % 360, 0)
        else:
            weather['wind_knots'] = None
            weather['wind_dir'] = None
    
    # 陣風
    if 'gust-surface' in api_data and api_data['gust-surface'][idx] is not None:
        weather['gust_knots'] = round(api_data['gust-surface'][idx] * 1.94384, 1)
    else:
        weather['gust_knots'] = None
    
    # 降水
    if 'past3hprecip-surface' in api_data and api_data['past3hprecip-surface'][idx] is not None:
        weather['precip'] = round(api_data['past3hprecip-surface'][idx], 2)
    else:
        weather['precip'] = 0
    
    # 雲層
    clouds = []
    for key in ['lclouds-surface', 'mclouds-surface', 'hclouds-surface']:
        if key in api_data and api_data[key][idx] is not None:
            clouds.append(api_data[key][idx])
    weather['cloud'] = round(max(clouds), 1) if clouds else None
    
    # 降水類型
    ptype_map = {0: '無', 1: '雨', 3: '凍雨', 5: '雪', 7: '雨夾雪', 8: '冰珠'}
    if 'ptype-surface' in api_data and api_data['ptype-surface'][idx] is not None:
        weather['ptype'] = ptype_map.get(int(api_data['ptype-surface'][idx]), '未知')
    else:
        weather['ptype'] = '無'
    
    return weather


def assess_flight(weather: dict) -> tuple:
    """評估飛行適航性，返回 (suitable, risk_level, reasons)"""
    suitable = True
    risk = 'LOW'
    reasons = []
    
    # 能見度估算（根據露點差和降水）
    visibility = 10000
    if weather['temp'] is not None and weather['dewpoint'] is not None:
        diff = weather['temp'] - weather['dewpoint']
        if diff < 1:
            visibility = 500
        elif diff < 2:
            visibility = 2000
    
    if weather['precip'] and weather['precip'] > 10:
        visibility = min(visibility, 2000)
    elif weather['precip'] and weather['precip'] > 2:
        visibility = min(visibility, 5000)
    
    # 評估
    if visibility < 1000:
        suitable, risk = False, 'HIGH'
        reasons.append('低能見度')
    elif visibility < 5000:
        risk = 'MEDIUM'
        reasons.append('能見度偏低')
    
    if weather['wind_knots'] and weather['wind_knots'] > 35:
        suitable, risk = False, 'HIGH'
        reasons.append('強風')
    elif weather['wind_knots'] and weather['wind_knots'] > 25:
        risk = 'MEDIUM' if risk == 'LOW' else risk
        reasons.append('風速偏強')
    
    if weather['gust_knots'] and weather['gust_knots'] > 40:
        suitable, risk = False, 'HIGH'
        reasons.append('強陣風')
    
    if weather['precip'] and weather['precip'] > 10:
        suitable, risk = False, 'HIGH'
        reasons.append('大降水')
    elif weather['precip'] and weather['precip'] > 2:
        risk = 'MEDIUM' if risk == 'LOW' else risk
        reasons.append('有降水')
    
    if weather['ptype'] in ['凍雨', '冰珠']:
        suitable, risk = False, 'HIGH'
        reasons.append(weather['ptype'])
    elif weather['ptype'] == '雪':
        risk = 'MEDIUM' if risk == 'LOW' else risk
        reasons.append('降雪')
    
    if weather['temp'] is not None and weather['dewpoint'] is not None:
        if weather['temp'] - weather['dewpoint'] < 2:
            risk = 'MEDIUM' if risk == 'LOW' else risk
            reasons.append('有霧')
    
    if not reasons:
        reasons.append('良好')
    
    return suitable, risk, reasons


def get_daily_summary(api_data: dict, airport: dict) -> list:
    """取得每日摘要"""
    if 'ts' not in api_data:
        return []
    
    # 按日期分組
    daily = {}
    for idx, ts in enumerate(api_data['ts']):
        dt = datetime.fromtimestamp(ts / 1000)
        date_str = dt.strftime('%Y-%m-%d')
        
        if date_str not in daily:
            daily[date_str] = {'temps': [], 'winds': [], 'gusts': [], 'precips': [], 'clouds': [], 'suitable': [], 'risks': []}
        
        weather = parse_weather(api_data, idx)
        suitable, risk, _ = assess_flight(weather)
        
        if weather['temp'] is not None:
            daily[date_str]['temps'].append(weather['temp'])
        if weather['wind_knots'] is not None:
            daily[date_str]['winds'].append(weather['wind_knots'])
        if weather['gust_knots'] is not None:
            daily[date_str]['gusts'].append(weather['gust_knots'])
        if weather['precip'] is not None:
            daily[date_str]['precips'].append(weather['precip'])
        if weather['cloud'] is not None:
            daily[date_str]['clouds'].append(weather['cloud'])
        
        daily[date_str]['suitable'].append(suitable)
        daily[date_str]['risks'].append(risk)
    
    # 生成摘要
    summaries = []
    today = datetime.now().date()
    
    for date_str, data in sorted(daily.items()):
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        days_ahead = (date_obj - today).days
        
        if days_ahead < 0 or days_ahead > 7:  # 只取未來 7 天
            continue
        
        n_total = len(data['suitable'])
        n_suitable = sum(data['suitable'])
        n_high_risk = sum(1 for r in data['risks'] if r == 'HIGH')
        
        # 整日風險評估
        if n_high_risk > 0:
            daily_risk = 'HIGH'
        elif n_suitable < n_total / 2:
            daily_risk = 'MEDIUM'
        else:
            daily_risk = 'LOW'
        
        summary = {
            'date': date_str,
            'days_ahead': days_ahead,
            'airport_icao': airport['icao'],
            'airport_iata': airport['iata'],
            'airport_name': airport['name'],
            'city': airport['city'],
            'temp_min': round(min(data['temps']), 1) if data['temps'] else None,
            'temp_max': round(max(data['temps']), 1) if data['temps'] else None,
            'wind_avg': round(sum(data['winds']) / len(data['winds']), 1) if data['winds'] else None,
            'wind_max': round(max(data['winds']), 1) if data['winds'] else None,
            'gust_max': round(max(data['gusts']), 1) if data['gusts'] else None,
            'precip_total': round(sum(data['precips']), 2) if data['precips'] else 0,
            'cloud_avg': round(sum(data['clouds']) / len(data['clouds']), 1) if data['clouds'] else None,
            'flight_ok_count': n_suitable,
            'flight_total_count': n_total,
            'flight_ok_ratio': round(n_suitable / n_total * 100, 1) if n_total > 0 else 0,
            'daily_risk': daily_risk,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        summaries.append(summary)
    
    return summaries


def main():
    """主程式"""
    # 從環境變數取得 API Key
    api_key = os.environ.get('WINDY_API_KEY')
    if not api_key:
        logger.error("❌ 請設定 WINDY_API_KEY 環境變數")
        return
    
    logger.info("🛫 開始抓取多機場天氣資料...")
    
    all_summaries = []
    
    for icao, airport in AIRPORTS.items():
        try:
            logger.info(f"📍 抓取 {airport['name']} ({icao})...")
            api_data = fetch_weather(api_key, airport['lat'], airport['lon'])
            summaries = get_daily_summary(api_data, airport)
            all_summaries.extend(summaries)
            logger.info(f"✅ {airport['city']}: 取得 {len(summaries)} 天預報")
        except Exception as e:
            logger.error(f"❌ {icao} 失敗: {e}")
    
    if not all_summaries:
        logger.error("❌ 無法取得任何資料")
        return
    
    # 建立 DataFrame
    df = pd.DataFrame(all_summaries)
    
    # 排序：先按日期，再按機場
    df = df.sort_values(['date', 'airport_icao']).reset_index(drop=True)
    
    # 輸出 CSV
    output_dir = 'data'
    os.makedirs(output_dir, exist_ok=True)
    
    # 完整預報檔案
    output_file = os.path.join(output_dir, 'airport_weather_forecast.csv')
    df.to_csv(output_file, index=False, encoding='utf-8-sig')
    logger.info(f"💾 已儲存: {output_file}")
    
    # 今日比較檔案
    today_str = datetime.now().strftime('%Y-%m-%d')
    df_today = df[df['date'] == today_str]
    if not df_today.empty:
        today_file = os.path.join(output_dir, 'today_comparison.csv')
        df_today.to_csv(today_file, index=False, encoding='utf-8-sig')
        logger.info(f"💾 已儲存: {today_file}")
    
    # 輸出摘要到 console
    print("\n" + "=" * 80)
    print(f"📊 多機場天氣預報摘要 (更新時間: {datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 80)
    
    for date in df['date'].unique()[:4]:  # 只顯示前 4 天
        date_df = df[df['date'] == date]
        days_ahead = date_df['days_ahead'].iloc[0]
        day_label = {0: '今日', 1: '明日', 2: '後日'}.get(days_ahead, f'+{days_ahead}日')
        
        print(f"\n【{day_label}】{date}")
        print("-" * 80)
        print(f"{'城市':<6} | {'溫度':<12} | {'風速':<14} | {'降水':<8} | {'風險':<8} | {'適飛率':<8}")
        print("-" * 80)
        
        for _, row in date_df.iterrows():
            temp_str = f"{row['temp_min']}~{row['temp_max']}°C"
            wind_str = f"avg {row['wind_avg']}kt, max {row['wind_max']}kt"
            precip_str = f"{row['precip_total']}mm"
            risk_symbol = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🔴'}.get(row['daily_risk'], '⚪')
            
            print(f"{row['city']:<6} | {temp_str:<12} | {wind_str:<14} | {precip_str:<8} | {risk_symbol} {row['daily_risk']:<5} | {row['flight_ok_ratio']}%")
    
    print("\n" + "=" * 80)
    print(f"✅ 完成！共 {len(df)} 筆資料")


if __name__ == "__main__":
    main()
