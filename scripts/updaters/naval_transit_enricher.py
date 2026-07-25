#!/usr/bin/env python3
"""外國軍艦通過台海紀錄的正規化與欄位抽取。

存在的理由：naval_transit_updater 把**新聞標題**整條當成艦艇描述
（`ships = original.get("title", "")`），所以資料庫裡會出現
「7艘共艦台海周邊活動國軍嚴密監控| 政治」這種東西 —— 版面標籤、
共軍動態、缺國家、沒有艦型，全都要人工回頭清。

把清洗與抽取移到寫入端，新資料進來就是乾淨的，不必再人工確認。

抽取結果一律附信心度：規則明確命中（艦名前綴、舷號格式）才算高信心，
靠關鍵字猜的標為低信心並保留原文，讓下游能決定要不要採信。
"""

import re

# 新聞版面標籤，由 CNA 等來源的標題原封帶入
NEWS_SECTION_TAGS = [
    "| 政治", "｜政治", "| 兩岸", "｜兩岸", "| 軍事", "｜軍事",
    "| 國際", "｜國際", "| 焦點", "｜焦點",
]

# 艦名前綴 → 國家。這是最可靠的判定依據，比新聞裡的國名提及穩定得多。
HULL_PREFIX_COUNTRY = {
    "USS": "US", "USNS": "US",
    "JS": "JP", "JDS": "JP",
    "HMS": "UK",
    "HMCS": "Canada",
    "HMAS": "Australia",
    "HMNZS": "New Zealand",
    "FGS": "Germany",
    "FNS": "France",
    "HNLMS": "Netherlands",
    "ITS": "Italy",
    "ROKS": "South Korea",
}

# 中文國名 → 代碼。艦名前綴抓不到時的備援。
COUNTRY_KEYWORDS = [
    ("美國", "US"), ("美軍", "US"), ("美海軍", "US"),
    ("日本", "JP"), ("海上自衛隊", "JP"), ("日艦", "JP"),
    ("英國", "UK"), ("英軍", "UK"), ("皇家海軍", "UK"),
    ("加拿大", "Canada"), ("加海軍", "Canada"),
    ("澳洲", "Australia"), ("澳大利亞", "Australia"),
    ("紐西蘭", "New Zealand"),
    ("法國", "France"), ("德國", "Germany"),
    ("荷蘭", "Netherlands"), ("義大利", "Italy"),
    ("越南", "Vietnam"), ("土耳其", "Turkey"),
    ("韓國", "South Korea"),
]

# 舷號：DDG-113 / T-AGS 65 / DD-107 / FFH 156 / CG-67
HULL_NUMBER_RE = re.compile(
    r"\b((?:T-)?[A-Z]{2,4})\s*[-–]?\s*(\d{2,4})\b")

# 艦型關鍵字。順序有意義 —— 先比對長詞，避免「驅逐艦」蓋掉「飛彈驅逐艦」。
SHIP_TYPE_KEYWORDS = [
    "神盾驅逐艦", "飛彈驅逐艦", "導彈驅逐艦", "驅逐艦",
    "海洋調查船", "海洋測量船", "測量艦", "調查船",
    "綜合補給艦", "補給艦",
    "巡防衛艦", "巡防艦", "護衛艦", "巡洋艦", "巡邏艦",
    "航空母艦", "航艦", "兩棲攻擊艦", "船塢登陸艦",
    "情報收集艦", "偵察艦", "潛艦", "潛艇",
    "海警船", "公務船",
]

# 共軍動態的標記。這些列是被預測的對象本身，不是外國艦艇通過 ——
# 混進 Foreign_battleship 會讓欄位語意崩壞，當特徵還會造成標籤洩漏。
PLA_KEYWORDS = ["共艦", "中共軍艦", "共軍", "中國海警", "解放軍", "中共航艦"]


def clean_text(text):
    """移除新聞版面標籤與多餘空白。"""
    out = str(text or "").strip()
    for tag in NEWS_SECTION_TAGS:
        out = out.replace(tag, "")
    return re.sub(r"\s+", " ", out).strip()


def is_pla_activity(ships, country=""):
    """判斷是否為共軍動態而非外國艦艇通過。

    已標記為非 CN 國家時不算 —— 新聞常把共軍動態和外艦通過寫在同一則，
    例如「共軍派出32架戰機…同時英國海軍巡邏艦史佩號通過台海」講的是
    HMS Spey 的通過。
    """
    code = str(country or "").strip().upper()
    if code == "CN":
        return True
    if code and code not in ("NAN", ""):
        return False

    text = str(ships or "")
    # 沒有國家標記時，先看有沒有外國艦名前綴（USS/HMS/HMCS…）。
    # 那是外艦存在的明確證據，即使句子同時提到共軍也一樣 —— 新聞常把
    # 兩者寫在一起，例如「共軍派出32架戰機…同時英國海軍巡邏艦史佩號
    # 通過台海」，講的其實是 HMS Spey 的通過。
    if any(re.search(rf"\b{prefix}\b", text) for prefix in HULL_PREFIX_COUNTRY):
        return False
    return any(kw in text for kw in PLA_KEYWORDS)


def detect_country(text):
    """回傳 (國家代碼, 信心度)。前綴命中為 high，關鍵字為 medium。"""
    s = str(text or "")

    for prefix, country in HULL_PREFIX_COUNTRY.items():
        if re.search(rf"\b{prefix}\b", s):
            return country, "high"

    hits = {code for kw, code in COUNTRY_KEYWORDS if kw in s}
    if len(hits) == 1:
        return hits.pop(), "medium"
    if len(hits) > 1:
        # 多國聯合航行，維持既有 CSV 的 "Multi (...)" 寫法
        return f"Multi ({'+'.join(sorted(hits))})", "medium"
    return "", "none"


def extract_hull_numbers(text):
    """抽出舷號。過濾掉年份等明顯不是舷號的組合。"""
    found = []
    for prefix, number in HULL_NUMBER_RE.findall(str(text or "")):
        if prefix in ("DDG", "DD", "FFG", "FF", "FFH", "CG", "CV", "CVN",
                      "LHA", "LHD", "LPD", "SSN", "T-AGS", "T-AO", "T-AKE",
                      "AGS", "AOE", "AGI", "PP", "MCM"):
            found.append(f"{prefix}-{number}")
    # 去重但保留順序
    return ", ".join(dict.fromkeys(found))


def detect_ship_type(text):
    """抽出艦型。可能多種（雙艦通過），以頓號串接。"""
    s = str(text or "")
    found = []
    for keyword in SHIP_TYPE_KEYWORDS:
        if keyword in s and not any(keyword in f for f in found):
            found.append(keyword)
    return "、".join(found)


def enrich(record):
    """對單筆紀錄補齊結構化欄位，回傳 (enriched, notes)。

    只補空欄位，不覆蓋既有內容 —— 既有值可能是人工整理過的，比規則抽取
    可靠。notes 記錄這次做了什麼，方便在 log 裡追蹤。
    """
    out = dict(record)
    notes = []

    raw = " ".join(str(out.get(k) or "") for k in ("Ships", "Mission_Note"))
    cleaned = clean_text(out.get("Ships"))
    if cleaned != str(out.get("Ships") or ""):
        out["Ships"] = cleaned
        notes.append("清除版面標籤")

    def fill(field, value, label):
        if not value:
            return
        current = out.get(field)
        if current is None or str(current).strip() in ("", "nan", "None"):
            out[field] = value
            notes.append(f"{label}={value}")

    country, confidence = detect_country(raw)
    if confidence in ("high", "medium"):
        fill("Country", country, "國家")
        fill("Country_Confidence", confidence, "國家信心")

    fill("Hull_Number", extract_hull_numbers(raw), "舷號")
    fill("Ship_Type", detect_ship_type(raw), "艦型")
    fill("Date_Precision", "exact", None)

    return out, notes


def enrich_dataframe(df, verbose=True):
    """對整個 DataFrame 做正規化，回傳 (df, 統計)。"""
    stats = {"cleaned": 0, "country": 0, "hull": 0, "ship_type": 0, "pla": 0}
    for idx, row in df.iterrows():
        record = row.to_dict()
        if is_pla_activity(record.get("Ships"), record.get("Country")):
            stats["pla"] += 1
            continue
        enriched, notes = enrich(record)
        for key, value in enriched.items():
            if key in df.columns and value != record.get(key):
                df.at[idx, key] = value
        for note in notes:
            if note.startswith("清除"):
                stats["cleaned"] += 1
            elif note.startswith("國家="):
                stats["country"] += 1
            elif note.startswith("舷號="):
                stats["hull"] += 1
            elif note.startswith("艦型="):
                stats["ship_type"] += 1
        if verbose and notes:
            print(f"    {record.get('Date')}: {'; '.join(notes)}")
    return df, stats
