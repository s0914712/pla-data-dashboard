import re
import requests
from datetime import datetime, timedelta
import PyPDF2
import io
import httpx
import json
import os
import pandas as pd



# CSV 文件路徑
CSV_FILE = 'data/JapanandBattleship.csv'

# 歷史記錄文件（避免重複爬取）
HISTORY_FILE = 'data/japan_scrape_history.json'

APERTIS_API_KEY = os.getenv('APERTIS_API_KEY') or os.getenv('STIMA_API_KEY')
APERTIS_MODEL = 'gemini-2.5-flash-lite-preview-06-17'
APERTIS_BASE_URL = 'https://api.apertis.ai/v1'

# PDF 基礎 URL
PDF_BASE_URL = 'https://www.mod.go.jp/js/pdf'

# 要爬取的天數（從今天往回推）
DAYS_TO_CHECK = 30

# 每天最多檢查幾個 PDF 編號
MAX_PDF_NUM_PER_DAY = 10

# 目標年份
TARGET_YEAR = '2026'

# HTTP 請求設定
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# 0/1 分析欄位
BINARY_FIELDS = ['空中', '聯合演訓', '艦通過', '航母活動', '與那國', '宮古', '大禹', '對馬', '進', '出']

# 是否使用 LLM（預設為規則分析，設定 USE_LLM=1 啟用 AI 分析）
USE_LLM = os.getenv('USE_LLM', '0') == '1'

# =================================================
# 規則分析相關常數與輔助函數
# =================================================

# 日文艦艇級別 → 繁體中文翻譯字典
SHIP_CLASS_DICT = {
    # 中國海軍
    'ルーヤンⅢ級': '旅洋III級驅逐艦',
    'ルーヤンIII級': '旅洋III級驅逐艦',
    'ルーヤンⅡ級': '旅洋II級驅逐艦',
    'ルーヤンII級': '旅洋II級驅逐艦',
    'ルーヤンＩ級': '旅洋I級驅逐艦',
    'ルーヤンI級': '旅洋I級驅逐艦',
    'ルーヤン級': '旅洋級驅逐艦',
    'ジャンカイⅡ級': '江凱II級護衛艦',
    'ジャンカイII級': '江凱II級護衛艦',
    'ジャンカイＩ級': '江凱I級護衛艦',
    'ジャンカイI級': '江凱I級護衛艦',
    'ジャンカイ級': '江凱級護衛艦',
    'ジャンウェイⅡ級': '江衛II級護衛艦',
    'ジャンウェイII級': '江衛II級護衛艦',
    'ジャンウェイＩ級': '江衛I級護衛艦',
    'ジャンウェイI級': '江衛I級護衛艦',
    'ジャンウェイ級': '江衛級護衛艦',
    'ジャンダオ級': '江島級護衛艦',
    'レンハイ級': '南昌級驅逐艦',
    'ソブレメンヌイ級': '現代級驅逐艦',
    'フチ級': '福池級綜合補給艦',
    'ルージョウ級': '旅洲級巡洋艦',
    'ドンディアオ級': '東調級情報收集艦',
    'ユージャオ級': '玉昭級船塢登陸艦',
    'ユーティン級': '玉亭級戰車登陸艦',
    'シャンⅡ級': '商II級核動力潛艇',
    'シャンII級': '商II級核動力潛艇',
    'シャンＩ級': '商I級核動力潛艇',
    'シャンI級': '商I級核動力潛艇',
    'ユアン級': '元級潛艇',
    'ソン級': '宋級潛艇',
    # 俄羅斯海軍
    'ウダロイⅠ級': '無畏I級驅逐艦',
    'ウダロイI級': '無畏I級驅逐艦',
    'ウダロイ級': '無畏級驅逐艦',
    'スラヴァ級': '光榮級巡洋艦',
    'スラバ級': '光榮級巡洋艦',
    'ステレグシチーⅢ級': '守護III級護衛艦',
    'ステレグシチーIII級': '守護III級護衛艦',
    'ステレグシチー級': '守護級護衛艦',
    'グリシャⅤ級': '格里莎V級護衛艦',
    'グリシャV級': '格里莎V級護衛艦',
    'グリシャ級': '格里莎級護衛艦',
    'ドゥブナ級': '杜布納級補給艦',
    'バルク級': '巴爾克級遠洋拖船',
    'ヴィシュニャ級': '維什尼亞級情報收集艦',
    'キロ級': '基洛級潛艇',
    'マルシャル・ネデリン級': '涅傑林元帥級觀測艦',
}

# 二進位欄位對應的日文關鍵詞
BINARY_FIELD_KEYWORDS = {
    '空中': ['ヘリコプター', '艦載機', '航空機', '発着艦', '艦載ヘリ', '飛行活動'],
    '聯合演訓': ['共同訓練', '合同演習', '共同行動', '連合演習', '共同演習'],
    '艦通過': ['通過', '航行'],
    '航母活動': ['空母', '航空母艦', '遼寧', '山東', '福建',
                'リャオニン', 'シャンドン', 'フージェン'],
    '與那國': ['与那国'],
    '宮古': ['宮古'],
    '大禹': ['大隅'],
    '對馬': ['対馬'],
}

# 海峽名稱對照（欄位名 → 繁中名稱）
STRAIT_NAMES_ZH = {
    '宮古': '宮古海峽',
    '對馬': '對馬海峽',
    '大禹': '大隅海峽',
    '與那國': '與那國島附近',
}


def _detect_country(text):
    """從日文文本偵測國家"""
    has_china = '中国' in text
    has_russia = 'ロシア' in text or '露海軍' in text
    if has_china and has_russia:
        return '中國、俄羅斯'
    elif has_russia:
        return '俄羅斯'
    else:
        return '中國'


def _detect_direction(text):
    """偵測航行方向：進（進入東海）/ 出（駛向太平洋）"""
    entering = 0  # 進
    exiting = 0   # 出

    # 太平洋 → 東シナ海 = 進
    if re.search(r'太平洋.{0,15}(から|より).{0,25}東シナ海', text):
        entering = 1
    if re.search(r'東シナ海.{0,10}(へ|に).{0,15}(向け|航行)', text):
        entering = 1
    # 宮古海峽北西進 = 進入東海
    if '宮古' in text and re.search(r'(北西進|西進)', text):
        entering = 1

    # 東シナ海 → 太平洋 = 出
    if re.search(r'東シナ海.{0,15}(から|より).{0,25}太平洋', text):
        exiting = 1
    if re.search(r'太平洋.{0,10}(へ|に).{0,15}(向け|航行)', text):
        exiting = 1
    if re.search(r'太平洋へ(向け)?航行', text):
        exiting = 1
    # 宮古海峽南東進 = 駛向太平洋
    if '宮古' in text and re.search(r'(南東進|東進)', text):
        exiting = 1

    # 日本海 → 對馬海峽南下 / 對馬海峽北上
    if '対馬' in text:
        if re.search(r'(南下|南西進)', text):
            exiting = 1
        if re.search(r'(北上|北東進)', text):
            entering = 1

    return entering, exiting


def _extract_ship_classes(text):
    """從日文文本提取艦艇級別並翻譯為繁中"""
    found = []
    for jp_name, zh_name in SHIP_CLASS_DICT.items():
        if jp_name in text and zh_name not in found:
            found.append(zh_name)
    if not found:
        return '未提及'
    return '、'.join(found)


def _extract_ship_count(text):
    """提取艦艇數量"""
    # 嘗試找 "計N隻" 或 "N隻" 的模式
    matches = re.findall(r'(?:計|合計)?\s*(\d+)\s*隻', text)
    if matches:
        return max(int(m) for m in matches)
    # 退回：計算字典中出現的不同艦級數
    count = sum(1 for jp_name in SHIP_CLASS_DICT if jp_name in text)
    return max(count, 1)


def _generate_remark(country, ship_count, ship_classes, active_straits, entering, exiting):
    """生成繁中備註（70字以內）"""
    country_part = country + '海軍'

    # 艦艇描述
    if ship_classes != '未提及':
        class_list = ship_classes.split('、')
        if len(class_list) <= 2:
            ships_part = f'{ship_count}艘{ship_classes}'
        else:
            ships_part = f'{ship_count}艘艦艇'
    else:
        ships_part = f'{ship_count}艘艦艇'

    # 海峽
    strait_parts = [STRAIT_NAMES_ZH[s] for s in active_straits if s in STRAIT_NAMES_ZH]
    strait_str = '經' + '、'.join(strait_parts) if strait_parts else ''

    # 方向
    if entering and exiting:
        dir_str = '往返東海與太平洋航行'
    elif entering:
        dir_str = '向東海航行'
    elif exiting:
        dir_str = '向太平洋航行'
    else:
        dir_str = '航行'

    remark = f'{country_part}{ships_part}{strait_str}{dir_str}。'
    if len(remark) > 70:
        remark = remark[:69] + '。'
    return remark


def analyze_with_rules(pdf_text, date):
    """規則分析 PDF 文本（無需 LLM）"""
    # 有效性已由 is_target_navy_pdf 在 main() 中預先檢查
    country = _detect_country(pdf_text)

    result = {'valid_report': 1, '國家': country}

    # 二進位欄位
    for field, keywords in BINARY_FIELD_KEYWORDS.items():
        result[field] = 1 if any(kw in pdf_text for kw in keywords) else 0

    # 進/出方向
    entering, exiting = _detect_direction(pdf_text)
    result['進'] = entering
    result['出'] = exiting

    # 艦型
    ship_classes = _extract_ship_classes(pdf_text)
    result['艦型'] = ship_classes

    # 艦艇數量
    ship_count = _extract_ship_count(pdf_text)

    # 活躍海峽
    active_straits = [f for f in ['與那國', '宮古', '大禹', '對馬'] if result.get(f) == 1]

    # 備註
    result['remark'] = _generate_remark(country, ship_count, ship_classes, active_straits, entering, exiting)

    return result


# =================================================

def generate_pdf_urls(start_date, end_date):
    """生成日期範圍內所有可能的 PDF URL"""
    urls = []
    current_date = start_date

    while current_date <= end_date:
        year = current_date.strftime('%Y')
        date_str = current_date.strftime('%Y%m%d')

        # 每天可能有多個 PDF (01, 02, 03...)
        for num in range(1, MAX_PDF_NUM_PER_DAY + 1):
            pdf_filename = f"p{date_str}_{num:02d}.pdf"
            pdf_url = f"{PDF_BASE_URL}/{year}/{pdf_filename}"
            csv_date = current_date.strftime('%Y/%m/%d')
            urls.append({
                'url': pdf_url,
                'date': csv_date,
                'filename': pdf_filename
            })

        current_date += timedelta(days=1)

    return urls


def load_history():
    """載入已處理的 PDF 歷史記錄"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 讀取歷史記錄失敗: {e}")
    return {"processed_pdfs": []}


def save_history(history):
    """儲存已處理的 PDF 歷史記錄"""
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 儲存歷史記錄失敗: {e}")


def download_pdf(url):
    """下載 PDF 並返回內容，404 返回 None"""
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        print(f"    ❌ 下載失敗: {e}")
        return None


def is_target_navy_pdf(pdf_text):
    """判斷 PDF 是否為中國或俄羅斯海軍艦艇動向相關，排除統計/非動向報告"""
    # 排除：海賊対処哨戒機活動報告
    if '海賊対処' in pdf_text and ('哨戒機' in pdf_text or 'Ｐ－３Ｃ' in pdf_text or 'P-3C' in pdf_text):
        return False
    # 排除：緊急発進（スクランブル）架次統計報告
    if '緊急発進' in pdf_text and any(kw in pdf_text for kw in ['回数', '実施状況', '状況について', '統計']):
        return False
    china_keywords = ['中国', '艦艇', '海軍', '護衛艦', '駆逐艦', '空母', '補給艦']
    russia_keywords = ['ロシア', 'ロシア海軍', 'ロシア連邦', '露海軍', 'ウダロイ', 'スラヴァ', 'ステレグシチー']
    return any(kw in pdf_text for kw in china_keywords + russia_keywords)


def extract_text_from_pdf(pdf_url):
    """從 PDF URL 提取文本"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(pdf_url, timeout=30, headers=headers)
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


def analyze_with_apertis(pdf_text, date):
    """使用 Apertis API 分析 PDF 文本"""

    prompt = f"""你是一個專門分析中國及俄羅斯海軍艦艇動向的專家。請仔細閱讀以下日本防衛省發布的報告，判斷是否為「中國海軍或俄羅斯海軍艦艇動向」報告並提取關鍵資訊。

**重要前提：本功能處理「中國海軍或俄羅斯海軍艦艇通過/活動」的動向報告。**
以下類型的報告屬於「非艦艇動向報告」，請設定 valid_report=0，所有數值欄位填 0，remark 留空：
- 海賊対処（反海盜）任務的哨戒機活動報告（P-3C 等）
- 航空自衛隊緊急発進（スクランブル）架次統計報告
- 日本-美國或其他國家之間的聯合演習公告（非中國/俄羅斯參與）
- 其他與中國/俄羅斯海軍艦艇動向無關的報告

報告日期：{date}

報告內容：
{pdf_text}

若確認為「中國海軍或俄羅斯海軍艦艇動向」報告，請根據報告內容判斷以下欄位（針對**中國海軍及/或俄羅斯海軍**）：

**0/1 欄位（是/否）：**
1. **空中**：是否有空中活動（艦載機、直升機等） - 填 0 或 1
2. **聯合演訓**：是否與其他國家進行聯合演習或訓練（含中俄聯合） - 填 0 或 1
3. **艦通過**：艦艇是否通過特定海域 - 填 0 或 1
4. **航母活動**：航空母艦是否有相關活動 - 填 0 或 1
5. **與那國**：艦艇是否經過與那國島附近 - 填 0 或 1
6. **宮古**：艦艇是否經過宮古海峽 - 填 0 或 1
7. **大禹**：艦艇是否經過大隅海峽 - 填 0 或 1
8. **對馬**：艦艇是否經過對馬海峽 - 填 0 或 1
9. **進**：艦艇是否向東海方向航行（從太平洋進入東海） - 填 0 或 1
10. **出**：艦艇是否向太平洋方向航行（從東海出向太平洋） - 填 0 或 1

**文字欄位：**
11. **國家**：填寫「中國」、「俄羅斯」或「中國、俄羅斯」（若兩國艦艇同時出現）

12. **艦型**：提取艦艇的具體型號，使用中文名稱
    - 中國艦艇例如：旅洋II級驅逐艦、江開級護衛艦、現代級驅逐艦、福池級綜合補給艦等
    - 俄羅斯艦艇例如：烏達洛伊級驅逐艦、斯拉瓦級巡洋艦、光榮級巡洋艦、無畏級驅逐艦等
    - 如果報告中提到多艘艦艇，請列出所有型號，用頓號「、」分隔
    - 如果沒有提到具體型號，填「未提及」

13. **remark**：用繁體中文撰寫 70 字以內的簡要描述，概述此次艦艇活動的重點
    - 包含：國家、艦艇數量、經過海域、航行方向、主要活動
    - 使用簡潔的書面語
    - 不超過 70 個中文字

請以 JSON 格式回覆，只回覆 JSON，不要有任何其他文字：
{{
  "valid_report": 1,
  "國家": "中國",
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
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{APERTIS_BASE_URL}/chat/completions",
                headers={
                    "Authorization": APERTIS_API_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "model": APERTIS_MODEL,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 1024,
                    "temperature": 0.1
                }
            )

            response.raise_for_status()
            result_json = response.json()

            response_text = result_json["choices"][0]["message"]["content"]

        # 提取 JSON
        response_text = response_text.strip()
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.startswith('```'):
            response_text = response_text[3:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        result = json.loads(response_text)
        return result

    except httpx.HTTPStatusError as e:
        print(f"    ❌ Apertis API HTTP 錯誤: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.ConnectError as e:
        print(f"    ❌ Apertis API 連接錯誤: {e}")
        return None
    except Exception as e:
        print(f"    ❌ Apertis API 分析失敗: {e}")
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

        df_filtered = df[(df['艦型'].notna() & (df['艦型'] != '')) |
                         (df['remark'].notna() & (df['remark'] != ''))]

        if df_filtered.empty:
            return None

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
        mask = df['date'] == date

        if not mask.any():
            return False

        row = df[mask].iloc[0]

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

        mask = df['date'] == date

        if not mask.any():
            print(f"      ⚠️  找不到日期 {date} 的行")
            return False

        for key, value in data.items():
            if key in df.columns:
                df.loc[mask, key] = value

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
    import time

    print("="*60)
    print("日本防衛省中國/俄羅斯海軍艦艇動向爬蟲 V6 - 直接下載 PDF 版")
    print("="*60)

    if USE_LLM:
        if not APERTIS_API_KEY:
            print("❌ 錯誤：USE_LLM=1 但未設置 APERTIS_API_KEY 環境變量")
            return
        print("🤖 分析模式: LLM (Apertis API)")
    else:
        print("📋 分析模式: 規則分析（無需 API Key）")

    # 讀取 CSV
    print("\n正在讀取 CSV...")
    try:
        df = pd.read_csv(CSV_FILE, encoding='utf-8-sig')
        print(f"✅ 成功讀取: {CSV_FILE}")
        print(f"📊 總行數: {len(df)}")
    except Exception as e:
        print(f"❌ 讀取失敗: {e}")
        return

    latest_date = get_latest_date_from_csv()
    if latest_date:
        print(f"📅 最新日本防衛省資料日期: {latest_date.strftime('%Y/%m/%d')}")
    else:
        print(f"📅 無現有日本防衛省資料")
        latest_date = datetime.min

    # 載入歷史記錄
    history = load_history()
    processed_set = set(history.get("processed_pdfs", []))
    print(f"📂 已處理過的 PDF: {len(processed_set)} 個")

    print(f"\n{'='*60}")
    print("🚀 開始爬取日本防衛省資料（直接下載 PDF）...")
    print(f"{'='*60}\n")

    # 生成要檢查的 PDF URL 列表
    end_date = datetime.now()
    start_date = end_date - timedelta(days=DAYS_TO_CHECK)

    # 確保在目標年份範圍內
    if TARGET_YEAR:
        year_start = datetime(int(TARGET_YEAR), 1, 1)
        if start_date < year_start:
            start_date = year_start

    print(f"📅 檢查日期範圍: {start_date.strftime('%Y/%m/%d')} ~ {end_date.strftime('%Y/%m/%d')}")

    pdf_urls = generate_pdf_urls(start_date, end_date)
    print(f"📋 共生成 {len(pdf_urls)} 個可能的 PDF URL\n")

    updated_pdfs = 0
    found_pdfs = 0
    skipped_history = 0

    for idx, pdf_info in enumerate(pdf_urls, 1):
        pdf_url = pdf_info['url']
        date = pdf_info['date']
        filename = pdf_info['filename']

        # 檢查歷史記錄，跳過已處理的 PDF
        if filename in processed_set:
            skipped_history += 1
            continue

        # 檢查是否已存在有效資料
        try:
            current_date = datetime.strptime(date, '%Y/%m/%d')
            if current_date <= latest_date:
                is_valid = check_date_data_validity(date, df)
                if is_valid:
                    continue  # 靜默跳過已有資料的日期
        except:
            pass

        # 嘗試下載 PDF
        pdf_content = download_pdf(pdf_url)

        if not pdf_content:
            continue  # 404 或下載失敗，靜默跳過

        found_pdfs += 1
        # 記錄到歷史（不論後續分析結果如何，都不再重複下載）
        processed_set.add(filename)

        print(f"[{found_pdfs}] 📥 找到: {filename}")
        print(f"    📅 日期: {date}")

        # 解析 PDF
        try:
            pdf_file = io.BytesIO(pdf_content)
            pdf_reader = PyPDF2.PdfReader(pdf_file)

            pdf_text = ""
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    pdf_text += page_text + "\n"

            pdf_text = pdf_text.strip()
        except Exception as e:
            print(f"    ❌ PDF 解析失敗: {e}\n")
            continue

        if not pdf_text:
            print(f"    ⚠️ PDF 無文字內容\n")
            continue

        # 檢查是否為中國/俄羅斯海軍相關
        if not is_target_navy_pdf(pdf_text):
            print(f"    ⏭️ 非中國/俄羅斯海軍艦艇相關\n")
            continue

        print(f"    📄 提取文本: {len(pdf_text)} 字")

        # 分析（規則 or LLM）
        if USE_LLM and APERTIS_API_KEY:
            print(f"    🤖 AI 分析中...", end=" ")
            analysis = analyze_with_apertis(pdf_text, date)
        else:
            print(f"    📋 規則分析中...", end=" ")
            analysis = analyze_with_rules(pdf_text, date)

        if not analysis:
            print("失敗\n")
            continue

        print("✓")

        # 檢查是否判定為有效艦艇動向報告
        if analysis.get('valid_report', 1) == 0:
            print(f"    ⏭️ 判定為非艦艇動向報告，跳過\n")
            continue

        # 移除 valid_report 欄位，不寫入 CSV
        analysis.pop('valid_report', None)

        # 更新 CSV
        if update_csv(date, analysis):
            updated_pdfs += 1

        country = analysis.get('國家', '中國')
        print(f"    ✅ {date} ({country}):")
        binary_str = " | ".join([f"{k}:{v}" for k, v in analysis.items() if k in BINARY_FIELDS and v == 1])
        if binary_str:
            print(f"       {binary_str}")
        if '艦型' in analysis and analysis['艦型'] and analysis['艦型'] != '未提及':
            print(f"       艦型: {analysis['艦型']}")
        if 'remark' in analysis and analysis['remark']:
            remark_display = analysis['remark'][:50] + '...' if len(analysis['remark']) > 50 else analysis['remark']
            print(f"       備註: {remark_display}")
        print()

        if USE_LLM:
            time.sleep(1.5)  # LLM 模式避免請求過快

    # 儲存歷史記錄
    history["processed_pdfs"] = sorted(processed_set)
    save_history(history)

    print(f"\n{'='*60}")
    print(f"📊 掃描完成:")
    print(f"   歷史跳過: {skipped_history} 個（已處理過）")
    print(f"   新找到 PDF: {found_pdfs} 個")
    print(f"   更新資料: {updated_pdfs} 筆")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
