#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日本防衛省中國海軍艦艇動向爬蟲 - GitHub Actions 版本
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import time
import re
import requests
from datetime import datetime
import PyPDF2
import io
from openai import OpenAI
import json
import os
import pandas as pd

# ==================== 設定區 ====================

# CSV 文件路徑
CSV_FILE = 'data/JapanandBattleship.csv'

# Stima API 設定（從環境變量讀取）
STIMA_API_KEY = os.getenv('STIMA_API_KEY')
STIMA_MODEL = 'grok-4.1-fast:free'

# 日本防衛省網站
BASE_URL = 'https://www.mod.go.jp/js/press/index.html'

# 要爬取的頁數
MAX_PDFS = 10

# 0/1 分析欄位
BINARY_FIELDS = ['空中', '聯合演訓', '艦通過', '航母活動', '與那國', '宮古', '大禹', '對馬', '進', '出']

# =================================================


def init_driver():
    """初始化 Selenium WebDriver"""
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


def extract_text_from_pdf(pdf_url):
    """從 PDF URL 提取文本"""
    try:
        response = requests.get(pdf_url, timeout=30)
        response.raise_for_status()

        pdf_file = io.BytesIO(response.content)
        pdf_reader = PyPDF2.PdfReader(pdf_file)

        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"

        return text.strip()
    except Exception as e:
        print(f"    ❌ PDF 讀取失敗: {e}")
        return None


def analyze_with_stima(pdf_text, date):
    """使用 Stima API 分析 PDF 文本"""

    prompt = f"""你是一個專門分析中國海軍艦艇動向的專家。請仔細閱讀以下日本防衛省發布的中國海軍艦艇動向報告，並提取關鍵資訊。

報告日期：{date}

報告內容：
{pdf_text}

請根據報告內容，判斷以下各個欄位：

**0/1 欄位（是/否）：**
1. **空中**：是否有空中活動（飛機、直升機等） - 填 0 或 1
2. **聯合演訓**：是否提到聯合演習或訓練 - 填 0 或 1
3. **艦通過**：是否有艦艇通過特定海域 - 填 0 或 1
4. **航母活動**：是否有航空母艦相關活動 - 填 0 或 1
5. **與那國**：是否經過與那國島附近 - 填 0 或 1
6. **宮古**：是否經過宮古海峽 - 填 0 或 1
7. **大禹**：是否經過大隅海峽 - 填 0 或 1
8. **對馬**：是否經過對馬海峽 - 填 0 或 1
9. **進**：艦艇是否向東海方向航行（從太平洋進入東海） - 填 0 或 1
10. **出**：艦艇是否向太平洋方向航行（從東海出向太平洋） - 填 0 或 1

**文字欄位：**
11. **艦型**：提取具體的艦艇型號，使用中文名稱（例如：旅洋II級驅逐艦、江開級護衛艦、現代級驅逐艦、福池級綜合補給艦等）
    - 如果報告中提到多艘艦艇，請列出所有型號，用頓號「、」分隔
    - 如果沒有提到具體型號，填「未提及」
    - 優先使用中文通稱（旅洋、江開、現代級等）

12. **remark**：用繁體中文撰寫 70 字以內的簡要描述，概述此次活動的重點
    - 包含：艦艇數量、經過海域、航行方向、主要活動
    - 使用簡潔的書面語
    - 不超過 70 個中文字

請以 JSON 格式回覆，只回覆 JSON，不要有任何其他文字：
{{
  "空中": 0,
  "聯合演訓": 0,
  "艦通過": 0,
  "航母活動": 0,
  "與那國": 0,
  "宮古": 0,
  "大禹": 0,
  "對馬": 0,
  "進": 0,
  "出": 0,
  "艦型": "旅洋II級驅逐艦、江開級護衛艦",
  "remark": "中國海軍2艘艦艇由東海經對馬海峽向日本海航行，包括旅洋II級驅逐艦及江開級護衛艦。"
}}
"""

    try:
        client = OpenAI(
            api_key=STIMA_API_KEY,
            base_url="https://api.stima.tech/v1/"
        )

        chat_completion = client.chat.completions.create(
            model=STIMA_MODEL,
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        )

        response_text = chat_completion.choices[0].message.content

        # 提取 JSON
        response_text = response_text.strip()
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.startswith('```'):
            response_text = response_text[3:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        # 解析 JSON
        result = json.loads(response_text)

        return result

    except Exception as e:
        print(f"    ❌ Stima API 分析失敗: {e}")
        return None


def get_latest_date_from_csv():
    """從 CSV 讀取最新日期（含日本防衛省數據的日期）"""
    try:
        if not os.path.exists(CSV_FILE):
            print(f"⚠️ CSV 檔案不存在: {CSV_FILE}")
            return None
            
        df = pd.read_csv(CSV_FILE, encoding='utf-8-sig')
        
        if df.empty or 'date' not in df.columns:
            return None
        
        # 過濾出有艦型或 remark 數據的行（表示已處理過日本防衛省數據）
        df_filtered = df[(df['艦型'].notna() & (df['艦型'] != '')) | 
                         (df['remark'].notna() & (df['remark'] != ''))]
        
        if df_filtered.empty:
            return None
        
        # 轉換日期並找出最新的
        dates = pd.to_datetime(df_filtered['date'], format='%Y/%m/%d', errors='coerce')
        latest_date = dates.max()
        
        if pd.isna(latest_date):
            return None
            
        return latest_date
        
    except Exception as e:
        print(f"讀取 CSV 時發生錯誤: {e}")
        return None


def check_date_data_validity(date, df):
    """檢查指定日期的資料是否有效（至少有一個0/1欄位為1）"""
    try:
        # 找到對應日期的行
        mask = df['date'] == date
        
        if not mask.any():
            return False
        
        row = df[mask].iloc[0]
        
        # 檢查所有 0/1 欄位
        for field in BINARY_FIELDS:
            if field in row and pd.notna(row[field]):
                value = str(row[field]).strip()
                if value in ['1', '1.0']:
                    return True
        
        return False
        
    except Exception as e:
        print(f"      ⚠️  檢查資料完整性時發生錯誤: {e}")
        return False


def update_csv(date, data):
    """更新 CSV 文件中指定日期的資料"""
    try:
        if not os.path.exists(CSV_FILE):
            print(f"❌ CSV 檔案不存在: {CSV_FILE}")
            return False
            
        df = pd.read_csv(CSV_FILE, encoding='utf-8-sig')
        
        # 找到對應日期的行
        mask = df['date'] == date
        
        if not mask.any():
            print(f"      ⚠️  找不到日期 {date} 的行")
            return False
        
        # 更新數據
        for key, value in data.items():
            if key in df.columns:
                df.loc[mask, key] = value
        
        # 儲存（保持原有的編碼和格式）
        df.to_csv(CSV_FILE, index=False, encoding='utf-8-sig')
        print(f"      ✓ 已更新日期 {date} 的資料")
        return True
        
    except Exception as e:
        print(f"      ❌ 更新資料時發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主程式"""

    print("="*60)
    print("日本防衛省中國海軍艦艇動向爬蟲 V3 - GitHub Actions 版")
    print("="*60)
    
    # 檢查 API Key
    if not STIMA_API_KEY:
        print("❌ 錯誤：未設置 STIMA_API_KEY 環境變量")
        return

    # 讀取 CSV
    print("\n正在讀取 CSV...")
    try:
        df = pd.read_csv(CSV_FILE, encoding='utf-8-sig')
        print(f"✅ 成功讀取: {CSV_FILE}")
        print(f"📊 總行數: {len(df)}")
    except Exception as e:
        print(f"❌ 讀取失敗: {e}")
        return

    # 取得最新日期
    latest_date = get_latest_date_from_csv()
    if latest_date:
        print(f"📅 最新日本防衛省資料日期: {latest_date.strftime('%Y/%m/%d')}")
    else:
        print(f"📅 無現有日本防衛省資料")
        latest_date = datetime.min

    # 開始爬取
    print(f"\n{'='*60}")
    print("🚀 開始爬取日本防衛省資料...")
    print(f"{'='*60}\n")

    driver = init_driver()
    print("✓ 瀏覽器啟動成功\n")

    updated_pdfs = 0

    try:
        print(f"📄 訪問: {BASE_URL}")
        driver.get(BASE_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # 找所有 PDF 連結
        all_links = soup.find_all('a', href=re.compile(r'\.pdf$', re.I))

        china_navy_links = []
        for link in all_links:
            text = link.get_text(strip=True)
            if '中国' in text or '艦艇' in text or '動向' in text:
                china_navy_links.append(link)
            else:
                parent = link.find_parent(['p', 'li', 'div'])
                if parent:
                    parent_text = parent.get_text()
                    if '中国' in parent_text and '艦艇' in parent_text:
                        china_navy_links.append(link)

        print(f"  找到 {len(china_navy_links)} 個中國海軍相關 PDF\n")

        for idx, link in enumerate(china_navy_links[:MAX_PDFS], 1):
            try:
                href = link.get('href')

                # 構建完整 URL
                if href.startswith('http'):
                    pdf_url = href
                elif href.startswith('/'):
                    pdf_url = f"https://www.mod.go.jp{href}"
                else:
                    pdf_url = f"https://www.mod.go.jp/js/press/{href}"

                print(f"  [{idx:2d}/{min(len(china_navy_links), MAX_PDFS)}] 📥 {pdf_url}")

                # 提取日期
                link_text = link.get_text(strip=True)
                date_match = re.search(r'(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})', link_text + href)

                if date_match:
                    year = date_match.group(1)
                    month = date_match.group(2).zfill(2)
                    day = date_match.group(3).zfill(2)
                    date = f"{year}/{month}/{day}"
                else:
                    date = datetime.now().strftime('%Y/%m/%d')

                print(f"      📅 日期: {date}")

                # 檢查日期和資料完整性
                try:
                    current_date = datetime.strptime(date, '%Y/%m/%d')
                    if current_date <= latest_date:
                        is_valid = check_date_data_validity(date, df)
                        if is_valid:
                            print(f"      ⏭️  已存在且資料有效，跳過\n")
                            continue
                        else:
                            print(f"      ⚠️  已存在但資料全為0，重新處理")
                except:
                    pass

                # 提取 PDF 文本
                print(f"      📄 提取文本...", end=" ")
                pdf_text = extract_text_from_pdf(pdf_url)

                if not pdf_text:
                    print("失敗\n")
                    continue

                print(f"✓ ({len(pdf_text)} 字)")

                # 使用 Stima API 分析
                print(f"      🤖 AI 分析中...", end=" ")
                analysis = analyze_with_stima(pdf_text, date)

                if not analysis:
                    print("失敗\n")
                    continue

                print("✓")

                # 更新 CSV
                if update_csv(date, analysis):
                    updated_pdfs += 1

                # 顯示結果
                print(f"      ✅ {date}:")
                binary_str = " | ".join([f"{k}:{v}" for k, v in analysis.items() if k in BINARY_FIELDS and v == 1])
                if binary_str:
                    print(f"         {binary_str}")
                if '艦型' in analysis and analysis['艦型'] and analysis['艦型'] != '未提及':
                    print(f"         艦型: {analysis['艦型']}")
                if 'remark' in analysis and analysis['remark']:
                    remark_display = analysis['remark'][:50] + '...' if len(analysis['remark']) > 50 else analysis['remark']
                    print(f"         備註: {remark_display}")
                print()

                time.sleep(2)  # 避免請求過快

            except Exception as e:
                print(f"      ❌ 處理失敗: {e}\n")
                import traceback
                traceback.print_exc()
                continue

    except Exception as e:
        print(f"❌ 爬取失敗: {e}")
        import traceback
        traceback.print_exc()

    finally:
        driver.quit()
        print("✓ 瀏覽器已關閉")

    # 顯示總結
    print(f"\n{'='*60}")
    if updated_pdfs > 0:
        print(f"✅ 完成！")
        print(f"📊 總共更新 {updated_pdfs} 筆資料")
    else:
        print("ℹ️  沒有需要更新的資料")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
