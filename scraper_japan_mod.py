import re
import sys
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
# 注意：海峽欄位（與那國/宮古/大禹/對馬）改由 _detect_straits 處理，
# 必須同時滿足「艦艇通過」語境，避免單純飛機飛經或順帶提及被誤判
BINARY_FIELD_KEYWORDS = {
    '空中': ['ヘリコプター', '艦載機', '航空機', '発着艦', '艦載ヘリ', '飛行活動'],
    '聯合演訓': ['共同訓練', '合同演習', '共同行動', '連合演習', '共同演習'],
    '艦通過': ['通過', '航行'],
    '航母活動': ['空母', '航空母艦', '遼寧', '山東', '福建',
                'リャオニン', 'シャンドン', 'フージェン'],
}

# 海峽偵測：日文海峽關鍵詞
STRAIT_JP_KEYWORDS = {
    '與那國': ['与那国'],
    '宮古': ['宮古'],
    '大禹': ['大隅'],
    '對馬': ['対馬'],
}

# 艦艇實體指標（具體船艦／船種，避免把「艦載機」這種飛機誤判成艦艇）
SHIP_INDICATORS = [
    '艦艇', '駆逐艦', '護衛艦', '巡洋艦', '補給艦', '揚陸艦',
    '航空母艦', '空母', '潜水艦', '掃海艦', '哨戒艦',
    '情報収集艦', '観測艦', '測量艦', 'ミサイル艇',
    'フリゲート', 'コルベット',
]

# 艦艇航行／通過動詞
PASSAGE_VERBS = [
    '通過', '航行',
    '北上', '南下', '東進', '西進',
    '北進', '南進',
    '北東進', '南東進', '北西進', '南西進',
    '進出', '進入',
]

# 海峽名稱對照（欄位名 → 繁中名稱）
STRAIT_NAMES_ZH = {
    '宮古': '宮古海峽',
    '對馬': '對馬海峽',
    '大禹': '大隅海峽',
    '與那國': '與那國島附近',
}


def _detect_country(text):
    """從日文文本偵測國家。

    不要在沒有任何國家標記時預設回傳「中國」—— 那會把俄羅斯艦艇
    和無法判定的報告全部算成解放軍。實測 2026 年有多筆俄羅斯艦艇
    （守護級、杜布納級、維什尼亞級）混在資料裡，若無國家欄位就無法
    在下游篩掉。判定不出來時回傳「未知」，由使用端決定怎麼處理。
    """
    has_china = '中国' in text
    has_russia = 'ロシア' in text or '露海軍' in text
    if has_china and has_russia:
        return '中國、俄羅斯'
    if has_russia:
        return '俄羅斯'
    if has_china:
        return '中國'
    return '未知'


def _split_sentences(text):
    """依日文句讀切句。

    只在「。」斷句，**不能**把換行當句子邊界。PDF 文字抽取會在句子中間
    插入換行，實測診斷輸出的「句子」長這樣：

        「その後、これらの艦艇が与那国島（沖縄県）と西表島との間の海域を南西進し、太」
        「（日）から６日（月）にかけて与那国島と西表島との間の海域を南西進した後、令和」

    第一段結尾是「太」（太平洋被截斷），第二段開頭是「（日）」—— 都是
    換行片段而非句子。照 \\n 斷句會把「なお」「ものと同一である」這類
    關鍵措辭跟海峽名切到不同片段，過往指涉的判定就永遠碰不到。

    日文不用空白分詞，所以直接把換行去掉再依「。」切即可。

    接合換行後句子會變長，可能一句裡同時含「本次航跡」與「なお」補述，
    例如：

        「その後、当該艦艇が大隅海峡を東進した、なお、当該艦艇は６月
          ２７日に対馬海峡を南西進したものと同一である」

    整句判為過往指涉的話，連本次航跡也會被丟掉。因此在「なお」處再切
    一刀，讓補述自成一段，前半的本次航跡得以保留。
    """
    joined = re.sub(r'[\r\n]+', '', text)
    segments = []
    for sentence in joined.split('。'):
        # 用 lookahead 保留「なお」在後段開頭，供 _is_prior_reference 判別
        for seg in re.split(r'(?=なお[、，])', sentence):
            seg = seg.strip()
            if seg:
                segments.append(seg)
    return segments


# 句中的明示日期，例如「3月8日」「３月５日」（全形亦可，\d 在 Python3
# 會匹配全形數字）。用來區分「本次航跡」與「回顧先前航行」。
EXPLICIT_DATE_RE = re.compile(r'(\d{1,2})月(\d{1,2})日')

# 指向「過往事例」而非本次航跡的措辭。防衛省的報告常在末段補述
# 「なお、当該艦艇は先般…を通過している」，講的是先前的航行；
# 把這種句子算進來，就會讓一次對馬海峽的通報同時標記到與那國。
PRIOR_REFERENCE_MARKERS = [
    '先般', '前回', '過去', '昨年', '前年', '以前',
    'これまで', '既に', 'すでに',
]

# 「…したものと同一である」= 這批艦艇與先前某次航行的是同一批。
# 純粹是身分識別，講的不是本次航跡。實測三筆殘留衝突有兩筆靠這個判掉：
#   p20260318_02「令和８年３月１５日に与那国島…北東進したものと同一で」
#   p20260701_01「なお、当該艦艇は、６月２７日に対馬海峡を南西進したものと同一である」
IDENTITY_REFERENCE_MARKERS = ['ものと同一', 'と同一である', '同一のもの']

# 「なお、」開頭的補述句在防衛省報告裡幾乎都是回顧先前航行。單看「なお」
# 太寬鬆，因此要求同時出現明示日期或身分識別措辭才判為過往指涉。
SUPPLEMENTARY_MARKER = 'なお'


def _is_prior_reference(sentence):
    if any(mark in sentence for mark in PRIOR_REFERENCE_MARKERS):
        return True
    if any(mark in sentence for mark in IDENTITY_REFERENCE_MARKERS):
        return True
    if SUPPLEMENTARY_MARKER in sentence and EXPLICIT_DATE_RE.search(sentence):
        # 例：p20260310_01「なお、これらの艦艇は３月５日（木）から６日（金）
        # にかけて、対馬海峡を南西進」—— 報告日是 3/10，講的是 3/5 的事。
        return True
    return False


def _strait_is_ship_passage(text, jp_keyword):
    """判斷文本中該海峽關鍵詞的出現是否為「艦艇通過」語境。

    以「句子」為範圍判斷，需同時滿足：同一句內有海峽名 + 艦艇指標 +
    航行/通過動詞。

    原本用 ±120 字元的滑動窗口，實際上會跨過好幾個句子。防衛省的
    PDF 常在同一段落裡先描述本次航跡、再附帶提及其他海域或過往事例，
    240 字元的窗口足以把不相干的海峽一起吃進來 —— 結果是 2026-01 起
    出現 13 筆地理上不可能的組合，例如 2026-02-16 同時標記對馬海峽
    （北緯 34 度）與與那國（北緯 24 度），相距約 1,400 公里，同一批
    艦艇一天內不可能都經過。那些列的艦型多為俄羅斯艦艇（守護級、
    杜布納級、巴爾克級），實際只通過對馬海峽。
    """
    return _strait_evidence_count(text, jp_keyword) > 0


# 各海峽的概略緯度，用於地理一致性檢查。
STRAIT_LATITUDE = {
    '對馬': 34.4,    # 對馬海峽，日本海入口
    '大禹': 31.0,    # 大隅海峽，九州南方
    '宮古': 24.8,    # 宮古海峽
    '與那國': 24.4,  # 與那國島附近，最接近台灣
}

# 同日可同時成立的最大緯度跨距（度）。宮古與與那國相鄰（差 0.4 度）
# 可同時出現；對馬與宮古相差近 10 度則否。
MAX_STRAIT_LAT_SPAN = 7.0


def _enforce_strait_geography(straits, evidence=None):
    """剔除地理上不可能同時成立的海峽組合。

    同一批艦艇一天內不可能橫跨相距上千公里的海峽。衝突時依「文本證據
    強度」決定保留哪一群，而不是照緯度猜。

    這點很重要：2026 年那批誤判多半是俄羅斯艦艇（守護級、杜布納級、
    巴爾克級），實際航跡是對馬海峽（海參崴往東海），與那國才是誤判。
    若照「保留南側」之類的幾何規則去猜，會正好砍掉真的那個。證據強度
    （該海峽在多少個含通過語境的句子裡出現）才是可靠依據。

    evidence 為 None 時（例如對既有資料做事後檢查）不做取捨，只回報
    衝突，交由人工判斷 —— 沒有文本就沒有依據，猜了只會製造假資料。
    """
    active = [f for f, v in straits.items() if v == 1 and f in STRAIT_LATITUDE]
    if len(active) < 2:
        return straits, []

    lats = sorted((STRAIT_LATITUDE[f], f) for f in active)
    if lats[-1][0] - lats[0][0] <= MAX_STRAIT_LAT_SPAN:
        return straits, []

    # 依最大緯度斷層切成南北兩群
    gaps = [(lats[i + 1][0] - lats[i][0], i) for i in range(len(lats) - 1)]
    _, split_at = max(gaps)
    south = [f for _, f in lats[:split_at + 1]]
    north = [f for _, f in lats[split_at + 1:]]

    if evidence is None:
        return straits, []      # 無文本可依據，不做取捨

    score = lambda group: sum(evidence.get(f, 0) for f in group)
    if score(south) == score(north):
        return straits, []      # 證據相當，同樣不猜

    keep = south if score(south) > score(north) else north
    dropped = [f for f in active if f not in keep]
    cleaned = dict(straits)
    for f in dropped:
        cleaned[f] = 0
    return cleaned, dropped


def _sentences_with_ship_context(text):
    """逐句掃描，並讓「艦艇主語」延續到後續句子。

    防衛省的報告只在第一句點名艦艇，之後用「その後、…」接續描述航跡：

        ロシア海軍のステレグシチー級フリゲート1隻が対馬海峡を南下した。
        その後、3月10日、与那国島と台湾との間の海域を南東進した。

    第二句沒有任何艦艇名詞。若要求每一句都自帶艦艇指標，這種跨句的
    多段航跡會被整段漏掉 —— 這正是先前把句子級規則寫太嚴的後果。
    因此一旦出現艦艇指標就視為「艦艇語境成立」，延續到後續句子；
    只有遇到指向過往事例的句子才中斷（那一段講的是別次航行）。
    """
    ship_context = False
    for sentence in _split_sentences(text):
        if _is_prior_reference(sentence):
            ship_context = False       # 過往事例，語境重置且該句不採計
            yield sentence, False
            continue
        if any(ind in sentence for ind in SHIP_INDICATORS):
            ship_context = True
        yield sentence, ship_context


def _strait_evidence_count(text, jp_keyword):
    """該海峽出現在多少個「艦艇通過」語境的句子裡。"""
    n = 0
    for sentence, has_ship in _sentences_with_ship_context(text):
        if jp_keyword not in sentence or not has_ship:
            continue
        if any(verb in sentence for verb in PASSAGE_VERBS):
            n += 1
    return n


def detect_strait_conflict(straits):
    """對既有資料做事後檢查，回傳地理上互斥的海峽組合（不修改資料）。"""
    active = [f for f, v in straits.items() if v == 1 and f in STRAIT_LATITUDE]
    if len(active) < 2:
        return []
    lats = sorted(STRAIT_LATITUDE[f] for f in active)
    return active if (lats[-1] - lats[0]) > MAX_STRAIT_LAT_SPAN else []


def _strait_trigger_sentences(text, jp_keyword):
    """回傳觸發該海峽判定的句子，供診斷用。"""
    hits = []
    for sentence, has_ship in _sentences_with_ship_context(text):
        if jp_keyword not in sentence or not has_ship:
            continue
        if any(verb in sentence for verb in PASSAGE_VERBS):
            hits.append(sentence)
    return hits


def diagnose_strait_conflict(text, straits):
    """偵測到地理衝突時，收集判斷依據供人工確認。

    目前仍有 4 筆單一 PDF 就產生不可能組合（2026-03-10、03-16、04-22、
    07-01，皆為俄羅斯艦艇）。可能是誤判，也可能是報告涵蓋數天的航跡 ——
    艦艇先經對馬海峽、數日後才到與那國，兩者都寫在同一份 PDF 裡。
    這兩種情況的修法完全不同，沒有原文無法判斷，所以先輸出證據而不猜。
    """
    conflicting = detect_strait_conflict(straits)
    if not conflicting:
        return None
    detail = {}
    for field in conflicting:
        sentences = []
        for kw in STRAIT_JP_KEYWORDS[field]:
            sentences.extend(_strait_trigger_sentences(text, kw))
        detail[field] = [
            {'sentence': s[:200],
             'explicit_dates': ['%s月%s日' % m for m in EXPLICIT_DATE_RE.findall(s)]}
            for s in sentences
        ]
    return detail


def _detect_straits(text, diagnostics=None, source=None):
    """偵測各海峽是否有艦艇通過。回傳 {欄位名: 0/1}。"""
    result, evidence = {}, {}
    for field, jp_keywords in STRAIT_JP_KEYWORDS.items():
        evidence[field] = sum(_strait_evidence_count(text, kw) for kw in jp_keywords)
        result[field] = 1 if evidence[field] > 0 else 0
    result, dropped = _enforce_strait_geography(result, evidence)
    if dropped:
        print(f"      ⚠️  海峽組合地理上不一致，依證據強度剔除: {'、'.join(dropped)}")

    if diagnostics is not None:
        detail = diagnose_strait_conflict(text, result)
        if detail:
            diagnostics.append({'source': source, 'straits': detail})
            print(f"      🔍 仍有地理衝突，已記錄觸發句供人工確認")
    return result


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

    # 二進位欄位（不含海峽）
    for field, keywords in BINARY_FIELD_KEYWORDS.items():
        result[field] = 1 if any(kw in pdf_text for kw in keywords) else 0

    # 海峽欄位（需艦艇通過語境）
    result.update(_detect_straits(pdf_text))

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

    # 備註寫入「備考」而非「remark」。
    # remark 是舊有的布林欄位（1439 筆 True / 133 筆 False），把生成的
    # 中文描述寫進去會蓋掉原本的語意，也讓該欄位同時混有布林值與文字。
    result['備考'] = _generate_remark(country, ship_count, ship_classes,
                                      active_straits, entering, exiting)

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
5. **與那國**：本次報告中艦艇（艦船）是否實際通過或航行於與那國島周邊海域 - 填 0 或 1
6. **宮古**：本次報告中艦艇（艦船）是否實際通過宮古海峽 - 填 0 或 1
7. **大禹**：本次報告中艦艇（艦船）是否實際通過大隅海峽 - 填 0 或 1
8. **對馬**：本次報告中艦艇（艦船）是否實際通過對馬海峽 - 填 0 或 1

**海峽欄位的關鍵判定規則（非常重要，避免誤判）：**
- 海峽欄位只記錄「艦艇（艦船）」實際的通過／航行行為。
- 飛機（航空機、Y-9、Y-8、Y-20、J-15、ヘリコプター 等）「飛經」海峽，不算；對應海峽欄位填 0。
- 報告若僅在地圖說明、過往背景或與本次無關處提到某海峽，但本次艦艇並未通過該海峽，該海峽欄位填 0。
- 一份報告中只勾選「該艦艇本次實際通過」的海峽。例如報告主軸是某艦從宮古海峽南東進往太平洋，未提到對馬，則對馬=0、與那國=0、大禹=0。
- 「沖繩-宮古間」「宮古島北的海域」等屬於宮古海峽範圍，宮古=1；單獨提到「與那國島南西」的海域則與那國=1。

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

        # 這些欄位是後來才加的，舊 CSV 沒有。原本的 `if key in df.columns`
        # 會把它們靜默丟掉 —— 國家判定就是這樣消失的（analyze_with_rules
        # 算出來了，但寫不進去），導致俄羅斯艦艇在下游無法辨識。
        for col in ('國家', '備考'):
            if col in data and col not in df.columns:
                df[col] = pd.NA

        dropped = [k for k in data if k not in df.columns]
        for key, value in data.items():
            if key in df.columns:
                df.loc[mask, key] = value
        if dropped:
            print(f"      ⚠️  以下欄位不存在於 CSV，未寫入: {', '.join(dropped)}")

        df.to_csv(CSV_FILE, index=False, encoding='utf-8-sig')
        print(f"      ✓ 已更新日期 {date} 的資料")
        return True

    except Exception as e:
        print(f"      ❌ 更新資料時發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        return False


def rebuild_strait_columns(target_dates=None):
    """重新分析歷史記錄中所有 PDF，依新規則回填 CSV 的四個海峽欄位。

    僅修改 與那國/宮古/大禹/對馬 四個欄位；其他欄位（艦型、remark、進、出 等）
    維持不變，以免覆蓋人工修正。
    """
    print("="*60)
    print("🔧 海峽欄位回填模式（重跑既有 PDF，僅更新四個海峽欄位）")
    print("="*60)

    if not os.path.exists(CSV_FILE):
        print(f"❌ CSV 不存在: {CSV_FILE}")
        return

    if target_dates:
        # 指定日期時直接依命名規則探測 PDF，不倚賴歷史記錄。
        # japan_scrape_history.json 只回溯到 2026-02-18，更早的問題日期
        # （例如 2025-07-24、2026-01-15）不在裡面，但 PDF 仍在防衛省網站上。
        pdf_files = []
        for d in target_dates:
            date_compact = d.replace('-', '').replace('/', '')
            for num in range(1, MAX_PDF_NUM_PER_DAY + 1):
                pdf_files.append(f"p{date_compact}_{num:02d}.pdf")
        print(f"🎯 指定日期模式: {len(target_dates)} 個日期，"
              f"探測 {len(pdf_files)} 個候選 PDF")
        print(f"   日期: {', '.join(target_dates)}")
    else:
        history = load_history()
        pdf_files = history.get("processed_pdfs", [])
        if not pdf_files:
            print("⚠️ 歷史記錄為空，沒有可回填的 PDF")
            return
        print(f"📂 歷史 PDF 數: {len(pdf_files)}")

    df = pd.read_csv(CSV_FILE, encoding='utf-8-sig')

    # 同一日期可能有多個 PDF，採 OR 聚合
    per_date = {}
    per_country = {}
    diagnostics = []
    strait_fields = list(STRAIT_JP_KEYWORDS.keys())

    for idx, filename in enumerate(pdf_files, 1):
        # filename 形如 p20260119_01.pdf
        m = re.match(r'p(\d{4})(\d{2})(\d{2})_\d+\.pdf', filename)
        if not m:
            print(f"  [{idx}] ⏭️ 跳過格式不符: {filename}")
            continue
        year, month, day = m.groups()
        date_str = f"{year}/{month}/{day}"
        url = f"{PDF_BASE_URL}/{year}/{filename}"

        print(f"  [{idx}/{len(pdf_files)}] 📥 {filename} ({date_str})...", end=" ")
        pdf_content = download_pdf(url)
        if not pdf_content:
            # 指定日期模式會把每天 01~10 號全部探一遍，多數不存在，屬正常
            print("不存在" if target_dates else "失敗（404 或下載失敗）")
            continue

        try:
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_content))
            text = ""
            for page in pdf_reader.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
            text = text.strip()
        except Exception as e:
            print(f"PDF 解析失敗: {e}")
            continue

        if not text:
            print("無文字")
            continue

        if not is_target_navy_pdf(text):
            # 非目標 PDF：四個海峽都是 0（但不主動覆蓋 CSV，等同沒貢獻）
            print("非艦艇動向，略過")
            continue

        straits = _detect_straits(text, diagnostics=diagnostics, source=filename)
        bucket = per_date.setdefault(date_str, {f: 0 for f in strait_fields})
        for f in strait_fields:
            if straits.get(f) == 1:
                bucket[f] = 1
        # 同時回填國家。反正 PDF 已經解析過了，順手記下來 —— 既有 1941 列
        # 都沒有國家欄位，導致俄羅斯艦艇（守護級、杜布納級、維什尼亞級）
        # 在下游無法與解放軍區分。
        countries = per_country.setdefault(date_str, set())
        detected = _detect_country(text)
        if detected != '未知':
            countries.update(p for p in detected.split('、') if p)

        flagged = [f for f, v in straits.items() if v == 1]
        print(f"海峽: {flagged if flagged else '無'} / 國家: {detected}")

    print(f"\n📝 共 {len(per_date)} 個日期需要回填")
    if '國家' not in df.columns:
        df['國家'] = pd.NA
    updated = 0
    for date_str, straits in sorted(per_date.items()):
        mask = df['date'] == date_str
        if not mask.any():
            print(f"  ⚠️ {date_str} 不在 CSV，略過")
            continue
        for f in strait_fields:
            df.loc[mask, f] = straits[f]
        countries = per_country.get(date_str) or set()
        if countries:
            df.loc[mask, '國家'] = '、'.join(sorted(countries))
        updated += 1
        flagged = [f for f, v in straits.items() if v == 1]
        country_str = '、'.join(sorted(countries)) if countries else '未知'
        print(f"  ✓ {date_str} → {flagged if flagged else '全部清為 0'} [{country_str}]")

    df.to_csv(CSV_FILE, index=False, encoding='utf-8-sig')
    print(f"\n✅ 完成，回填 {updated} 列")

    # 一律寫出診斷檔（即使沒有衝突），這樣檔案存在與否就能確認新版邏輯有跑到。
    os.makedirs('data/logs', exist_ok=True)
    out_path = 'data/logs/strait_conflicts.json'
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump({
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'pdfs_scanned': len(pdf_files),
            'dates_updated': updated,
            'conflict_count': len(diagnostics),
            'conflicts': diagnostics,
        }, fh, ensure_ascii=False, indent=2)

    # 同時把觸發句印到 stdout。GitHub Actions 的 runner 是 ephemeral，
    # 只要 commit 步驟沒 add 到這個檔案就會消失，但 log 一定留得下來。
    print("\n" + "=" * 60)
    if diagnostics:
        print(f"🔍 {len(diagnostics)} 份 PDF 仍有地理衝突（觸發句如下，另存 {out_path}）")
        for item in diagnostics:
            print(f"\n--- {item['source']}")
            for field, entries in item['straits'].items():
                for e in entries:
                    dates = '、'.join(e['explicit_dates']) or '無'
                    print(f"    [{field}] 明示日期={dates}")
                    print(f"        {e['sentence']}")
    else:
        print(f"✅ 沒有偵測到地理衝突（診斷檔仍已寫入 {out_path}）")
    print("=" * 60)


def main():
    """主程式"""
    import time

    if os.getenv('REBUILD_STRAITS') == '1' or '--rebuild-straits' in sys.argv:
        # 可指定要重跑的日期（逗號分隔），不指定則沿用歷史記錄裡的全部 PDF。
        raw_dates = os.getenv('REBUILD_DATES', '')
        for arg in sys.argv:
            if arg.startswith('--dates='):
                raw_dates = arg.split('=', 1)[1]
        dates = [d.strip() for d in raw_dates.split(',') if d.strip()]
        rebuild_strait_columns(target_dates=dates or None)
        return

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
