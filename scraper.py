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
from datetime import datetime, timedelta
import argparse
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

def parse_date_from_text(text):
    """
    統一的日期解析函數，支援多種格式
    返回格式：YYYY/MM/DD 或 None

    ⚠️ 只適用於「列表頁標題」這種單一日期的短字串。
    不要拿來解析詳細頁內文 —— 內文的日期是一個區間
    （「115年7月30日 0600時 至 115年7月31日 0600時止」），
    本函數會回傳第一個匹配，也就是區間起點，比資料日期早一天。
    內文請用 parse_report_date()。
    """
    date = None
    
    # 格式1：115.02.14 (列表頁常見格式)
    date_match = re.search(r'(\d{3})\.(\d{2})\.(\d{2})', text)
    if date_match:
        roc_year = int(date_match.group(1))
        month = date_match.group(2)
        day = date_match.group(3)
        west_year = roc_year + 1911
        return f"{west_year}/{month}/{day}"
    
    # 格式2：中華民國 114 年 2 月 14 日 (詳細頁格式)
    date_match = re.search(r'中華民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if date_match:
        roc_year = int(date_match.group(1))
        month = date_match.group(2).zfill(2)
        day = date_match.group(3).zfill(2)
        west_year = roc_year + 1911
        return f"{west_year}/{month}/{day}"
    
    # 格式3：114年2月14日 (備用格式)
    date_match = re.search(r'(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if date_match:
        roc_year = int(date_match.group(1))
        month = date_match.group(2).zfill(2)
        day = date_match.group(3).zfill(2)
        west_year = roc_year + 1911
        return f"{west_year}/{month}/{day}"
    
    return None

def parse_report_date(text):
    """從詳細頁內文取「資料日期」＝報告區間的結束日（0600時止的那一天）。

    國防部每份發布涵蓋「前一日 0600 至當日 0600」，內文寫成：

        中華民國115年7月30日（星期四）0600時至115年7月31日（星期五）0600時止

    列表頁標題用的是發布日 (115.07.31)，也就是區間結束日。兩條路徑必須回傳
    同一天，否則同一份報告會因為走了哪條路徑而被標到不同日期 —— 而且因為
    區間起點那天通常已存在於 CSV，去重時會被直接丟掉，症狀是「當天資料靜默消失」，
    不會報錯，非常難發現。

    回傳格式 YYYY/MM/DD，解析不出來回 None。
    """
    if not text:
        return None

    # 1) 點分格式（115.07.31）。內文偶爾也會出現，語意就是發布日，直接採用。
    m = re.search(r'(\d{3})\.(\d{2})\.(\d{2})', text)
    if m:
        return f"{int(m.group(1)) + 1911}/{m.group(2)}/{m.group(3)}"

    # 2) 明確比對「X 至 Y」的日期區間，取結束日 Y。
    #    刻意不用「全文最後一個日期」——body_text 是整頁純文字，含導覽列與頁尾，
    #    最後一個日期很可能根本不屬於這份報告。這裡限定兩個日期之間必須以「至」
    #    相連且不跨句號，才視為同一個區間。
    DATE = r'(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日'
    m = re.search(DATE + r'[^。]*?至[^。]*?' + DATE, text)
    if m:
        roc_year, month, day = m.group(4), m.group(5), m.group(6)
        return f"{int(roc_year) + 1911}/{month.zfill(2)}/{day.zfill(2)}"

    # 3) 沒有區間就退回第一個日期（單一日期的頁面，第一個就是它）
    m = re.search(DATE, text)
    if m:
        return f"{int(m.group(1)) + 1911}/{m.group(2).zfill(2)}/{m.group(3).zfill(2)}"

    return None


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

    # 日期正規化後當索引，才能逐欄對齊
    for d in (df_existing, df_new):
        d['date'] = pd.to_datetime(d['date'], format='%Y/%m/%d', errors='coerce')
    df_existing = df_existing.dropna(subset=['date']).set_index('date')
    df_new = df_new.dropna(subset=['date']).set_index('date')

    # 逐「欄」更新，不是整列取代。
    #
    # 這支爬蟲只產生 date / pla_aircraft_sorties / plan_vessel_sorties 三欄，
    # 但 CSV 有 17 欄 —— 其餘 13 欄（空中、艦型、備考、國家…）是
    # scraper_japan_mod.py 寫的。如果用 concat + drop_duplicates(keep='last')
    # 整列取代，重爬某一天會把那 13 欄全部清成 NaN，靜默毀掉日本防衛省的資料。
    # DataFrame.update 只用 other 的非 NA 值更新對得上的 index+欄位，正是要的語意。
    overlap = df_existing.index.intersection(df_new.index)
    if len(overlap):
        df_existing.update(df_new.loc[overlap])

    # 全新的日期才追加
    fresh = df_new[~df_new.index.isin(df_existing.index)]
    df_combined = pd.concat([df_existing, fresh]) if len(fresh) else df_existing

    df_combined = df_combined.sort_index()
    df_combined.index = df_combined.index.strftime('%Y/%m/%d')
    df_combined.index.name = 'date'

    df_combined.to_csv(CSV_FILE, encoding='utf-8-sig')
    print(f"成功寫入 {len(new_data)} 筆資料到 {CSV_FILE}")

def main(refresh_from=None):
    print(f"\n{'='*60}")
    print("開始爬取國防部資料...")
    print(f"{'='*60}\n")

    latest_date = get_latest_date_from_csv()
    if latest_date:
        print(f"📅 CSV 最新日期: {latest_date.strftime('%Y/%m/%d')}")
    else:
        print(f"無現有資料，將爬取所有資料")
        latest_date = datetime.min

    # --refresh-from：把「已存在就跳過」的界線往前推，讓指定日期之後的資料
    # 重新爬一次並覆蓋。平常不會用到，是用來修正已經寫錯的日子 ——
    # 沒有這個開關的話，save_to_csv 的 keep='last' 永遠不會被觸發，
    # 因為既有日期在這裡就被 skip 掉了，根本走不到合併那一步。
    if refresh_from:
        latest_date = min(latest_date, refresh_from - timedelta(days=1))
        print(f"♻️  重爬模式：{refresh_from.strftime('%Y/%m/%d')} 起的資料將被重新抓取並覆蓋")

    all_data = []
    processed_urls = set()
    processed_dates = set()

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
                            if href and "plaact" in href and ("中共" in text or "動態" in text or re.search(r'\d{3}\.\d{2}\.\d{2}', text)):
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
                            link_text = link_info.get('text', '')
                        else:
                            detail_url = f"https://www.mnd.gov.tw{link_info.get('href')}"
                            link_text = link_info.get_text(strip=True)

                        # ============ 關鍵改進：從列表頁提取日期 ============
                        date_from_list = parse_date_from_text(link_text)
                        
                        if not detail_url.startswith('http'):
                            detail_url = f"https://www.mnd.gov.tw{detail_url}"

                        if detail_url in processed_urls:
                            continue
                        processed_urls.add(detail_url)

                        # 如果列表頁就有日期，先檢查是否需要爬取
                        if date_from_list:
                            current_date = datetime.strptime(date_from_list, '%Y/%m/%d')
                            if current_date <= latest_date:
                                print(f"  [{idx:2d}] {date_from_list} 已存在，跳過")
                                continue

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

                        # 優先使用列表頁日期，若無則從詳細頁解析。
                        # 內文一定要用 parse_report_date（取區間結束日＝發布日），
                        # 用 parse_date_from_text 會拿到區間起點，比列表頁早一天。
                        date = date_from_list if date_from_list else parse_report_date(body_text)

                        # 跳過已處理過的日期（不同連結可能指向同一天）
                        if date and date in processed_dates:
                            print(f"  [{idx:2d}] {date} 日期已處理過，跳過重複連結")
                            driver.back()
                            time.sleep(1)
                            continue

                        if not date:
                            print(f"  [{idx:2d}] ⚠️ 找不到日期，跳過")
                            
                            # ==================== DEBUG 區域 ====================
                            print(f"    🔍 [DEBUG] 網頁標題: {driver.title}")
                            print(f"    🔍 [DEBUG] 當前網址: {driver.current_url}")
                            print(f"    🔍 [DEBUG] 列表頁文字: {link_text[:100]}...")
                            # 預覽抓到的文字，確認是否被擋
                            preview_text = body_text[:200].replace('\n', ' ') if body_text else "無內容"
                            print(f"    🔍 [DEBUG] 內文預覽: {preview_text}...")
                            
                            if "Access Denied" in body_text or "403 Forbidden" in body_text:
                                print(f"    🛑 [CRITICAL] 偵測到存取被拒！IP 可能被封鎖或 Headless 特徵被抓。")
                            
                            # 儲存 debug 檔案
                            debug_file = f"debug_{detail_url.split('/')[-1]}.txt"
                            try:
                                with open(debug_file, 'w', encoding='utf-8') as f:
                                    f.write(f"URL: {detail_url}\n")
                                    f.write(f"Title: {driver.title}\n")
                                    f.write(f"List Text: {link_text}\n")
                                    f.write(f"{'='*60}\n")
                                    f.write(body_text)
                                print(f"    💾 已儲存 debug 檔案: {debug_file}")
                            except Exception as e:
                                print(f"    ⚠️ 無法儲存 debug 檔案: {e}")
                            # ====================================================

                            driver.back()
                            time.sleep(1)
                            continue

                        # 再次檢查日期（雙重保險）
                        current_date = datetime.strptime(date, '%Y/%m/%d')
                        if current_date <= latest_date:
                            print(f"  [{idx:2d}] {date} 已存在，跳過")
                            driver.back()
                            time.sleep(1)
                            continue

                        # 提取共機共艦數量
                        aircraft, vessel = extract_numbers_from_text(body_text)

                        all_data.append([date, aircraft, vessel])
                        processed_dates.add(date)
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
    parser = argparse.ArgumentParser(description='國防部共機共艦動態爬蟲')
    parser.add_argument('--refresh-from', metavar='YYYY/MM/DD', default=None,
                        help='重新爬取此日期（含）之後的資料並覆蓋既有值，'
                             '用於修正寫錯的日子。平常排程不需要此參數')
    args = parser.parse_args()

    refresh_from = None
    if args.refresh_from:
        try:
            refresh_from = datetime.strptime(args.refresh_from, '%Y/%m/%d')
        except ValueError:
            parser.error(f"--refresh-from 格式應為 YYYY/MM/DD，收到 {args.refresh_from!r}")

    main(refresh_from=refresh_from)
