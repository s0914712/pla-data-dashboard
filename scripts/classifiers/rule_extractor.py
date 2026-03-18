#!/usr/bin/env python3
"""
===============================================================================
規則式資訊提取器 / Rule-based Information Extractor
===============================================================================

用關鍵字比對和正則表達式從新聞文本中提取：
  1. country1 / country2（GDELT 風格 actor code）
  2. extracted_data（Political_statement, Military_exercise,
     Foreign_battleship, US_Taiwan_interaction）

搭配 BERT 分類器使用，替代 LLM 的自由文本提取。
"""

import re
from typing import Dict, Tuple

# ---------------------------------------------------------------------------
# 國家關鍵字 → GDELT code
# ---------------------------------------------------------------------------
COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "CN": [
        "中國", "中共", "共軍", "解放軍", "國台辦", "國防部", "外交部",
        "東部戰區", "南部戰區", "北部戰區", "中央軍委", "習近平",
        "人民解放軍", "中華人民共和國", "北京", "PLA", "China", "Beijing",
        "新華社", "央視", "環球時報",
    ],
    "TW": [
        "台灣", "臺灣", "國軍", "國防部", "蔡英文", "賴清德",
        "民進黨", "國民黨", "外交部", "台北", "Taiwan", "Taipei",
        "中華民國", "總統府",
    ],
    "US": [
        "美國", "美軍", "美艦", "五角大廈", "國務院", "白宮",
        "拜登", "川普", "國防部長", "美國海軍", "United States",
        "US Navy", "USS", "Pentagon", "State Department",
        "美國國務", "印太司令",
    ],
    "JP": [
        "日本", "自衛隊", "海上自衛隊", "Japan", "岸田", "石破",
        "日本防衛", "護衛艦",
    ],
    "PH": ["菲律賓", "Philippines", "Manila", "馬尼拉"],
    "VN": ["越南", "Vietnam", "河內"],
    "RU": ["俄羅斯", "Russia", "俄軍", "俄艦"],
    "KR": ["韓國", "South Korea", "首爾"],
    "AU": ["澳洲", "Australia", "澳大利亞"],
    "EU": ["歐盟", "European Union", "EU"],
}

# 優先級（發起方通常是新聞主角）
_ACTOR_PRIORITY = ["CN", "US", "TW", "JP", "RU", "PH", "VN", "KR", "AU", "EU"]

# ---------------------------------------------------------------------------
# 軍艦名稱模式
# ---------------------------------------------------------------------------
_SHIP_PATTERNS = [
    # 美軍
    r"(?:USS\s+\w+)",
    r"(?:美[艦軍].*?(?:號|艦|級))",
    r"(?:驅逐艦|巡洋艦|補給艦|航母|航空母艦).*?(?:號|級)",
    # 日本
    r"(?:護衛艦.*?號)",
    # 通用
    r"(?:軍艦|戰艦|艦艇)\S{1,10}",
]

_TRANSIT_KEYWORDS = [
    "穿越台海", "通過台灣海峽", "過航台灣海峽", "穿越台灣海峽",
    "台海巡航", "通過台海", "transit", "Taiwan Strait",
    "巴士海峽", "宮古海峽", "穿越海峽",
]

_EXERCISE_KEYWORDS = [
    "軍演", "演習", "實彈", "聯合演訓", "軍事演練", "演練",
    "火力演習", "戰備巡航", "圍台", "封鎖演練",
    "exercise", "drill", "live-fire",
]

_US_TW_KEYWORDS = [
    "軍售", "對台軍售", "美台", "台美",
    "訪台", "訪問台灣", "過境",
    "台灣關係法", "TAIPEI Act",
    "美國在台協會", "AIT",
    "台灣旅行法",
]


def extract_actors(text: str, category: str = "") -> Tuple[str, str]:
    """
    從文本中識別 country1（行動方）和 country2（目標方）。

    利用出現順序和類別提示推斷主從關係。
    """
    found: dict[str, int] = {}  # code → first position
    for code in _ACTOR_PRIORITY:
        for kw in COUNTRY_KEYWORDS[code]:
            pos = text.find(kw)
            if pos != -1:
                if code not in found or pos < found[code]:
                    found[code] = pos
                break  # 一個 code 只記第一次出現

    if not found:
        return "", ""

    # 按出現位置排序
    sorted_codes = sorted(found, key=lambda c: found[c])

    # 類別提示可覆蓋
    if category == "CN_Statement" and "CN" in found:
        sorted_codes = ["CN"] + [c for c in sorted_codes if c != "CN"]
    elif category == "US_Statement" and "US" in found:
        sorted_codes = ["US"] + [c for c in sorted_codes if c != "US"]
    elif category == "TW_Statement" and "TW" in found:
        sorted_codes = ["TW"] + [c for c in sorted_codes if c != "TW"]

    country1 = sorted_codes[0]
    country2 = sorted_codes[1] if len(sorted_codes) > 1 else ""
    return country1, country2


def extract_data(text: str, category: str = "") -> Dict[str, str]:
    """
    根據類別提取 extracted_data 的四個欄位。
    """
    result = {
        "US_Taiwan_interaction": "",
        "Military_exercise": "",
        "Foreign_battleship": "",
        "Political_statement": "",
    }

    # Foreign_battleship
    if category == "Foreign_battleship" or any(kw in text for kw in _TRANSIT_KEYWORDS):
        ships = []
        for pat in _SHIP_PATTERNS:
            ships.extend(re.findall(pat, text))
        transit_desc = _extract_sentence_with_keywords(text, _TRANSIT_KEYWORDS)
        if ships:
            result["Foreign_battleship"] = ", ".join(dict.fromkeys(ships)) + " transit"
        elif transit_desc:
            result["Foreign_battleship"] = transit_desc[:100]

    # Military_exercise
    if category == "Military_Exercise" or any(kw in text for kw in _EXERCISE_KEYWORDS):
        result["Military_exercise"] = _extract_sentence_with_keywords(
            text, _EXERCISE_KEYWORDS, max_len=100
        )

    # US_Taiwan_interaction
    if category == "US_TW_Interaction" or any(kw in text for kw in _US_TW_KEYWORDS):
        result["US_Taiwan_interaction"] = _extract_sentence_with_keywords(
            text, _US_TW_KEYWORDS, max_len=100
        )

    # Political_statement（聲明類）
    if category in ("CN_Statement", "US_Statement", "TW_Statement"):
        result["Political_statement"] = _extract_statement_summary(text)

    return result


def _extract_sentence_with_keywords(
    text: str, keywords: list[str], max_len: int = 80
) -> str:
    """找到包含關鍵字的句子，回傳最相關的片段"""
    # 以句號、分號、換行切分
    sentences = re.split(r"[。；\n]", text)
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if any(kw in sent for kw in keywords):
            return sent[:max_len]
    return ""


def _extract_statement_summary(text: str, max_len: int = 50) -> str:
    """提取政治聲明摘要（取第一句有意義的句子）"""
    sentences = re.split(r"[。；\n]", text)
    for sent in sentences:
        sent = sent.strip()
        if len(sent) >= 10:
            return sent[:max_len]
    return text[:max_len] if text else ""
