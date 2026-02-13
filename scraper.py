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
    
    # [GitHub Actions 修正關鍵]
    # 在 GitHub Actions 必須使用 Headless 模式 (因為沒有螢幕)。
    # 但舊版 --headless 容易被擋，必須使用新版 "--headless=new" 才能騙過防火牆。
    chrome_options.add_argument('--headless=new')
    
    # CI/CD 環境標準設定
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--window-size=1920,1080') # 強制設定視窗大小，避免 RWD 隱藏元素
    
    # 反爬蟲偽裝設定
    chrome_options.add_argument('--disable-blink-features=AutomationControlled') 
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"]) 
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_argument('--lang=zh-TW') # 模擬繁體中文環境
    
    chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
    
    # 使用 webdriver_manager 自動管理驅動 (若報錯可改回直接呼叫 webdriver.Chrome())
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except:
        # 備用方案：直接使用系統路徑的 chromedriver
        driver = webdriver.Chrome(options=chrome_options)
        
    return driver

def extract_numbers_from_text(text):
    """從文本中提取共機共艦數量"""
    aircraft = 0
    vessel = 0

    aircraft_match = re.search(r'共機\s*(\d+)\s*架次', text)
    if aircraft_match:
        aircraft = int(aircraft_match.group(1))

    vessel_match = re.search(r'共艦\s*(\d+)\s*艘', text)
    if vessel_match:
        vessel = int(vessel_match.group(1))

    return aircraft, vessel

def get_latest_date_from_csv():
    """從 CSV 讀取最新日期"""
    try:
        if not os.path.exists(CSV_FILE):
            return None

        df = pd.read_csv(CSV_FILE, encoding='utf-8-sig')

        if df.empty or 'date' not in df.columns:
            return None

        # 確保日期格式正確讀取
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
        print("沒有新資料需要寫入")
        return

    os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)

    if os.path.exists(CSV_FILE):
        df_existing = pd.read_csv(CSV_FILE, encoding='utf-8-sig')
    else:
        df_existing = pd.DataFrame(columns=['date', 'pla_aircraft_sorties', 'plan_vessel_sorties'])

    df_new = pd.DataFrame(new_data, columns=['date', 'pla_aircraft_sorties', 'plan_vessel_sorties'])
    df_combined = pd.concat([df_existing, df_new], ignore_index=True)

    # 統一日期格式並去重
    df_combined['date'] = pd.to_datetime(df_combined['date'], format='%Y/%m/%d')
    df_combined = df_combined.sort_values('date')
    df_combined['date'] = df_combined['date'].dt.strftime('%Y/%m/%d')
    df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')

    df_combined.to_csv(CSV_FILE, index=False, encoding='utf-8-sig')
    print(f"成功寫入 {len(new_data)} 筆資料到 {CSV_FILE}")

def main():
    print(f"\n{'='*60}")
    print("開始爬取國防部資料...")
    print(f"{'='*60}\n")

    latest_date = get_latest_date_from_csv()
    if latest_date:
        print(f"📅 CSV 最新日期: {latest_date.strftime('%Y/%m/%d')}")
    else:
        print(f"無現有資料，將爬取所有資料")
        latest_date = datetime.min

    all_data = []
    processed_urls = set()

    driver = init_driver()
    print("✓ 瀏覽器啟動成功\n")

    try:
        for page in range(start_page, total_pages + 1):
            try:
                page_url = base_url if page == 1 else f"{base_url}&Page={page}"

                print(f"📄 第 {page} 頁: {page_url}")
                driver.get(page_url)
                
                # 等待列表讀取
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                time.sleep(3) # 給予額外緩衝時間

                soup = BeautifulSoup(driver.page_source, "html.parser")

                # 方法1: BS4 找 plaact 連結
                links = soup.find_all("a", href=re.compile(r'/news/plaact/\d+'))

                # 方法2: Selenium 補強 (針對動態加載)
                if not links:
                    selenium_links = driver.find_elements(By.TAG_NAME, "a")
                    links = []
                    for link in selenium_links:
                        try:
                            href = link.get_attribute("href")
                            text = link.text
                            if href and "plaact" in href and ("中共" in text or "動態" in text):
                                links.append({'href': href, 'text': text})
                        except:
                            continue
                else:
                    links = [{'href': f"https://www.mnd.gov.tw{link.get('href')}", 
                              'text': link.get_text(strip=True)} for link in links]

                print(f"  找到 {len(links)} 個 plaact 連結")

                for idx, link_info in enumerate(links, 1):
                    try:
                        if isinstance(link_info, dict):
                            detail_url = link_info['href']
                        else:
                            detail_url = f"https://www.mnd.gov.tw{link_info.get('href')}"

                        if not detail_url.startswith('http'):
                            detail_url = f"https://www.mnd.gov.tw{detail_url}"

                        if detail_url in processed_urls:
                            continue
                        processed_urls.add(detail_url)

                        # print(f"  [{idx:2d}] 讀取中...", end=" ") 

                        # 訪問詳細頁面
                        driver.get(detail_url)
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.TAG_NAME, "body"))
                        )
                        time.sleep(2) # 關鍵：等待內文渲染

                        # 獲取頁面內容
                        detail_soup = BeautifulSoup(driver.page_source, "html.parser")
                        
                        # 增加防呆：如果 body 為 None
                        if not detail_soup.body:
                            print(f"  [{idx:2d}] ⚠️ 抓取到的頁面沒有 body，可能載入失敗")
                            continue
                            
                        body_text = detail_soup.body.get_text(separator="\n", strip=True)

                        # [修正點 3] 優化日期提取正則表達式，容許空格
                        date = None
                        # 格式：中華民國 114 年 2 月 13 日 (允許中間有空白)
                        date_match = re.search(r'中華民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', body_text)
                        
                        if date_match:
                            roc_year = int(date_match.group(1))
                            month = date_match.group(2).zfill(2)
                            day = date_match.group(3).zfill(2)
                            west_year = roc_year + 1911
                            date = f"{west_year}/{month}/{day}"

                        # 備用日期格式 (114年2月13日)
                        if not date:
                            alt_match = re.search(r'(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', body_text)
                            if alt_match:
                                roc_year = int(alt_match.group(1))
                                month = alt_match.group(2).zfill(2)
                                day = alt_match.group(3).zfill(2)
                                west_year = roc_year + 1911
                                date = f"{west_year}/{month}/{day}"

                        if not date:
                            print(f"  [{idx:2d}] ⚠️ 找不到日期，跳過")
                            
                            # ==================== DEBUG 區域 ====================
                            print(f"    🔍 [DEBUG] 網頁標題: {driver.title}")
                            print(f"    🔍 [DEBUG] 當前網址: {driver.current_url}")
                            # 預覽抓到的文字，確認是否被擋
                            preview_text = body_text[:200].replace('\n', ' ') if body_text else "無內容"
                            print(f"    🔍 [DEBUG] 內文預覽: {preview_text}...")
                            
                            if "Access Denied" in body_text or "403 Forbidden" in body_text:
                                print(f"    🛑 [CRITICAL] 偵測到存取被拒！IP 可能被封鎖或 Headless 特徵被抓。")
                            # ====================================================

                            driver.back()
                            time.sleep(1)
                            continue

                        # 檢查日期是否比最新日期新
                        current_date = datetime.strptime(date, '%Y/%m/%d')
                        if current_date <= latest_date:
                            print(f"  [{idx:2d}] {date} 已存在，跳過")
                            driver.back()
                            time.sleep(1)
                            continue

                        # 提取共機共艦數量
                        aircraft, vessel = extract_numbers_from_text(body_text)

                        all_data.append([date, aircraft, vessel])
                        # 成功輸出
                        print(f"  [{idx:2d}] {date} | 共機 {aircraft:2d} | 共艦 {vessel:2d}")

                        # 返回列表頁
                        driver.back()
                        time.sleep(1)

                    except Exception as e:
                        print(f"\n  [{idx:2d}] 處理發生錯誤: {e}")
                        try:
                            driver.get(page_url) # 嘗試回到列表頁
                            time.sleep(2)
                        except:
                            pass
                        continue

            except Exception as e:
                print(f"處理第 {page} 頁失敗: {e}")
                continue

    finally:
        driver.quit()
        print("\n瀏覽器已關閉")

    # 儲存資料
    print(f"\n{'='*60}")
    if all_data:
        all_data.sort(key=lambda x: datetime.strptime(x[0], '%Y/%m/%d'))

        save_to_csv(all_data)

        print(f"\n完成！共爬取 {len(all_data)} 筆新資料")
        print(f"\n最新 5 筆資料:")
        print(f"{'日期':<12} | {'共機':<4} | {'共艦':<4}")
        print("-" * 30)
        for row in all_data[-5:]:
            print(f"{row[0]:<12} | {row[1]:>4} | {row[2]:>4}")
    else:
        print("沒有新資料需要寫入")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
