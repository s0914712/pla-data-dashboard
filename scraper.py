from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
from datetime import datetime
import os

# ==================== 設定區 ====================
CSV_FILE = 'data/JapanandBattleship.csv'
base_url = "https://www.mnd.gov.tw/news/plaactlist"
total_pages = 4
start_page = 1
# =================================================

def init_driver():
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def extract_numbers_from_text(text):
    """從文本中提取共機共艦數量"""
    aircraft_patterns = [
        r'共機\s*(\d+)\s*架次',
        r'共機：?\s*(\d+)',
        r'(\d+)\s*架次',
    ]

    vessel_patterns = [
        r'共艦\s*(\d+)\s*艘',
        r'共艦：?\s*(\d+)',
        r'(\d+)\s*艘',
    ]

    aircraft = 0
    vessel = 0

    for pattern in aircraft_patterns:
        match = re.search(pattern, text)
        if match:
            try:
                aircraft = int(match.group(1))
                break
            except:
                continue

    for pattern in vessel_patterns:
        match = re.search(pattern, text)
        if match:
            try:
                vessel = int(match.group(1))
                break
            except:
                continue

    return aircraft, vessel

def get_latest_date_from_csv():
    """從 CSV 讀取最新日期"""
    try:
        if not os.path.exists(CSV_FILE):
            print(f"⚠️ CSV 檔案不存在: {CSV_FILE}")
            return None
            
        df = pd.read_csv(CSV_FILE, encoding='utf-8-sig')
        
        if df.empty or 'date' not in df.columns:
            return None
        
        # 轉換日期並找出最新的
        dates = pd.to_datetime(df['date'], format='%Y/%m/%d', errors='coerce')
        latest_date = dates.max()
        
        if pd.isna(latest_date):
            return None
            
        return latest_date
        
    except Exception as e:
        print(f"讀取 CSV 時發生錯誤: {e}")
        return None

def save_to_csv(new_data):
    """將新資料附加到 CSV"""
    if not new_data:
        print("ℹ️ 沒有新資料需要寫入")
        return
    
    # 確保目錄存在
    os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
    
    # 讀取現有資料
    if os.path.exists(CSV_FILE):
        df_existing = pd.read_csv(CSV_FILE, encoding='utf-8-sig')
    else:
        # 如果檔案不存在，創建新的 DataFrame
        df_existing = pd.DataFrame(columns=['date', 'pla_aircraft_sorties', 'plan_vessel_sorties'])
    
    # 創建新資料的 DataFrame
    df_new = pd.DataFrame(new_data, columns=['date', 'pla_aircraft_sorties', 'plan_vessel_sorties'])
    
    # 合併資料
    df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    
    # 按日期排序
    df_combined['date'] = pd.to_datetime(df_combined['date'], format='%Y/%m/%d')
    df_combined = df_combined.sort_values('date')
    df_combined['date'] = df_combined['date'].dt.strftime('%Y/%m/%d')
    
    # 移除重複的日期（保留最新的）
    df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')
    
    # 儲存
    df_combined.to_csv(CSV_FILE, index=False, encoding='utf-8-sig')
    print(f"✅ 成功寫入 {len(new_data)} 筆資料到 {CSV_FILE}")

def main():
    print(f"\n{'='*60}")
    print("🚀 開始爬取國防部資料...")
    print(f"{'='*60}\n")
    
    # 取得最新日期
    latest_date = get_latest_date_from_csv()
    if latest_date:
        print(f"📅 CSV 最新日期: {latest_date.strftime('%Y/%m/%d')}")
    else:
        print(f"📅 無現有資料，將爬取所有資料")
        latest_date = datetime.min
    
    all_data = []
    processed_urls = set()
    
    driver = init_driver()
    print("✓ 瀏覽器啟動成功\n")
    
    for page in range(start_page, total_pages + 1):
        try:
            if page == 1:
                page_url = base_url
            else:
                page_url = f"{base_url}/{page}"
            
            print(f"📄 第 {page} 頁: {page_url}")
            driver.get(page_url)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(3)
            
            soup = BeautifulSoup(driver.page_source, "html.parser")
            all_links = soup.find_all('a', href=re.compile(r'news/plaact/\d+'))
            
            print(f"  找到 {len(all_links)} 個 plaact 連結")
            
            for idx, link in enumerate(all_links, 1):
                try:
                    href = link.get('href')
                    
                    if href.startswith('/'):
                        detail_url = f"https://www.mnd.gov.tw{href}"
                    elif href.startswith('http'):
                        detail_url = href
                    else:
                        detail_url = f"https://www.mnd.gov.tw/{href}"
                    
                    if detail_url in processed_urls:
                        continue
                    processed_urls.add(detail_url)
                    
                    # 提取日期
                    date_elem = link.find('h5', class_='date')
                    if date_elem:
                        date_span = date_elem.find('span', class_='en')
                        date_text = date_span.get_text(strip=True) if date_span else date_elem.get_text(strip=True)
                    else:
                        date_text = None
                    
                    if date_text:
                        date_match = re.search(r'(\d{3,4})[./](\d{1,2})[./](\d{1,2})', date_text)
                        if date_match:
                            year = int(date_match.group(1))
                            if year < 1000:
                                year += 1911
                            month = date_match.group(2).zfill(2)
                            day = date_match.group(3).zfill(2)
                            date = f"{year}/{month}/{day}"
                        else:
                            date = None
                    else:
                        date = None
                    
                    if not date:
                        print(f"  [{idx:2d}] ⚠️ 找不到日期，跳過")
                        continue
                    
                    try:
                        current_date = datetime.strptime(date, '%Y/%m/%d')
                    except:
                        print(f"  [{idx:2d}] ⚠️ 日期格式錯誤: {date}")
                        continue
                    
                    if current_date <= latest_date:
                        print(f"  [{idx:2d}] {date} ⏭️  已存在")
                        continue
                    
                    # 檢查標題
                    title_elem = link.find('h4', class_='title')
                    if title_elem:
                        title_text = title_elem.get_text(strip=True)
                        if '中共解放軍' not in title_text and '臺海' not in title_text and '空域動態' not in title_text:
                            print(f"  [{idx:2d}] {date} ⏭️  非相關標題")
                            continue
                    
                    # 訪問詳細頁面
                    print(f"  [{idx:2d}] {date} ⏳ 讀取中...", end=" ")
                    driver.get(detail_url)
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                    time.sleep(2)
                    
                    detail_soup = BeautifulSoup(driver.page_source, "html.parser")
                    
                    content_areas = [
                        detail_soup.find('div', class_='content'),
                        detail_soup.find('div', class_='article'),
                        detail_soup.find('article'),
                        detail_soup.find('main'),
                        detail_soup.body
                    ]
                    
                    body_text = ""
                    for area in content_areas:
                        if area:
                            body_text = area.get_text(separator="\n", strip=True)
                            break
                    
                    # 提取數量
                    aircraft, vessel = extract_numbers_from_text(body_text)
                    
                    # 儲存資料 [date, aircraft, vessel]
                    all_data.append([date, aircraft, vessel])
                    print(f"✓ 共機 {aircraft:2d} | 共艦 {vessel:2d}")
                    
                    driver.back()
                    time.sleep(2)
                    
                except Exception as e:
                    print(f"\n  ❌ 處理項目 {idx} 時發生錯誤: {e}")
                    driver.get(page_url)
                    time.sleep(3)
                    continue
        
        except Exception as e:
            print(f"❌ 處理第 {page} 頁失敗: {e}")
            continue
    
    driver.quit()
    print("\n✓ 瀏覽器已關閉")
    
    # 儲存資料
    print(f"\n{'='*60}")
    if all_data:
        # 按日期排序
        all_data.sort(key=lambda x: datetime.strptime(x[0], '%Y/%m/%d'))
        
        save_to_csv(all_data)
        
        print(f"\n✅ 完成！")
        print(f"📊 總共爬取 {len(all_data)} 筆新資料")
        
        # 顯示資料摘要
        print(f"\n最新 5 筆資料:")
        print(f"{'日期':<12} | {'共機':<4} | {'共艦':<4}")
        print("─" * 30)
        for row in all_data[-5:]:
            print(f"{row[0]:<12} | {row[1]:>4} | {row[2]:>4}")
    else:
        print("ℹ️ 沒有新資料需要寫入")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
