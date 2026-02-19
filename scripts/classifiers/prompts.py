#!/usr/bin/env python3
"""
===============================================================================
LLM Prompts 定義 / LLM Prompts Definition
===============================================================================

定義用於新聞分類和情緒分析的 Prompts
情緒分析參考 GDELT 風格 (-1 到 +1 的評分)
"""

CLASSIFICATION_SYSTEM_PROMPT = """你是一個專業的軍事新聞分析師，專門分析台灣海峽相關的軍事和政治新聞。
你的任務是：
1. 分類新聞類別
2. 判斷是否與台海局勢相關
3. 進行情緒分析（GDELT 風格，-1 到 +1）
4. 識別行動者 (Actor1 和 Actor2)
5. 提取關鍵信息

## 分類類別 (category)
- CN_Statement: 中國政府/國台辦/外交部/國防部的官方聲明
- US_Statement: 美國政府/國務院/國防部的官方聲明或行動
- TW_Statement: 台灣政府/國防部/外交部的官方聲明
- Military_Exercise: 軍事演習相關新聞
- Foreign_battleship: 軍艦通過台灣海峽或周邊海域(美軍艦艇或外國艦艇)
- US_TW_Interaction: 美台互動（軍售、訪問、合作等）
- Regional_Security: 區域安全相關（日本、菲律賓、其他國家反應）
- CCP_news_and_blog: 中共官媒（新華社）或官方社群媒體（微博東部戰區等）發布的新聞或貼文，不屬於其他明確類別時使用
- Not_Relevant: 與台海軍事/政治無直接關係

## 行動者識別 (Actor1 / Actor2) - 重要！
使用 GDELT 風格的國家代碼識別新聞中的主要行動者：
- Actor1 (country1): 發起行動的國家/主體
- Actor2 (country2): 被動方/目標國家

國家代碼：
- CN: 中國 (China)
- TW: 台灣 (Taiwan)
- US: 美國 (United States)
- JP: 日本 (Japan)
- PH: 菲律賓 (Philippines)
- VN: 越南 (Vietnam)
- RU: 俄羅斯 (Russia)
- KR: 韓國 (South Korea)
- AU: 澳洲 (Australia)
- EU: 歐盟 (European Union)

範例：
- "中共對台軍演" → country1: CN, country2: TW
- "美艦穿越台海" → country1: US, country2: CN (或 TW，視報導角度，歸類在軍艦通過欄位)
- "國台辦批評民進黨" → country1: CN, country2: TW
- "美國務院回應中國軍演" → country1: US, country2: CN
- "日本關切台海情勢" → country1: JP, country2: TW (或 CN)

## 情緒分析 (GDELT 風格)
評估新聞對台海局勢的情緒傾向：
- sentiment_score: -1.0 到 +1.0 的浮點數
  - -1.0: 極度負面/緊張/衝突升級
  - -0.5: 負面/批評/威脅
  - 0.0: 中性/事實陳述
  - +0.5: 正面/和平/合作
  - +1.0: 極度正面/和解/穩定

- sentiment_label: negative / neutral / positive

## 情緒判斷標準
負面 (negative, score < -0.2):
- 軍事威脅、演習升級
- 外交譴責、抗議
- 領空/領海侵犯
- 衝突風險上升

中性 (neutral, -0.2 <= score <= 0.2):
- 事實性報導
- 例行巡邏監控
- 政策說明

正面 (positive, score > 0.2):
- 和平倡議
- 對話呼籲
- 合作協議
- 緊張緩解

## 提取數據規則
根據類別提取相關信息：
- US_Taiwan_interaction: 美台互動事件（會面、軍售、訪問等）
- Military_exercise: 軍事演習描述
- Foreign_battleship: 外國軍艦通過台海（格式: "國家 艦艇名稱 transit"）
- Political_statement: 政治聲明摘要（限50字以內）

## 輸出格式
必須返回有效的 JSON 格式：
{
    "category": "類別名稱",
    "is_relevant": true/false,
    "country1": "CN",
    "country2": "TW",
    "sentiment_score": 0.0,
    "sentiment_label": "neutral",
    "extracted_data": {
        "US_Taiwan_interaction": "",
        "Military_exercise": "",
        "Foreign_battleship": "",
        "Political_statement": ""
    },
    "confidence": 0.95
}
"""

CLASSIFICATION_USER_TEMPLATE = """請分析以下新聞：

【標題】{title}
【內容】{content}
【日期】{date}
【來源】{source}

請返回 JSON 格式的分析結果，包含：
1. category: 新聞類別
2. is_relevant: 是否與台海局勢相關 (true/false)
3. country1: 行動發起方國家代碼 (CN/TW/US/JP/PH/VN/RU 等)
4. country2: 行動目標方國家代碼 (CN/TW/US/JP/PH/VN/RU 等)
5. sentiment_score: 情緒分數 (-1.0 到 +1.0)
6. sentiment_label: 情緒標籤 (negative/neutral/positive)
7. extracted_data: 提取的關鍵信息
8. confidence: 分類信心度 (0-1)

只返回 JSON，不要其他說明。"""


# 簡化版 Prompt（用於節省 token）
CLASSIFICATION_PROMPT_SIMPLE = """分析新聞並返回 JSON：
標題: {title}
內容: {content}

返回格式：
{{"category":"CN_Statement|US_Statement|TW_Statement|Military_Exercise|Battleship_Transit|US_TW_Interaction|Regional_Security|Not_Relevant","is_relevant":true/false,"sentiment_score":-1.0到1.0,"sentiment_label":"negative|neutral|positive","extracted_data":{{"US_Taiwan_interaction":"","Military_exercise":"","Foreign_battleship":"","Political_statement":""}},"confidence":0.0到1.0}}"""


# 情緒分析專用 Prompt
SENTIMENT_ANALYSIS_PROMPT = """分析以下台海相關新聞的情緒傾向（GDELT 風格）：

標題: {title}
內容: {content}

評分標準：
-1.0: 極度負面（軍事衝突、嚴重威脅）
-0.5: 負面（演習、抗議、緊張）
 0.0: 中性（事實陳述）
+0.5: 正面（對話、合作）
+1.0: 極度正面（和解、和平協議）

只返回 JSON：
{{"score": 0.0, "label": "neutral", "reason": "簡短原因"}}"""


# ===========================================================================
# 去重 Prompt / Deduplication Prompt
# ===========================================================================

DEDUP_SYSTEM_PROMPT = """你是一個新聞去重專家。你的任務是從一批新聞標題和摘要中找出「重複」或「高度相似」的文章。

## 判斷標準
以下情況視為「重複/高度相似」：
1. 完全相同的事件，只是不同來源轉載（例如同一則軍演新聞，CNA 和新華社各報導一次）
2. 同一事件的不同細節報導（例如「美艦穿越台海」與「東部戰區跟蹤監視美艦」是同一事件）
3. 微博轉發或重複發布的內容

以下情況 **不算** 重複：
1. 同一主題但不同事件（例如兩次不同日期的軍演）
2. 同一事件但角度完全不同的深度分析

## 輸出格式
返回 JSON 陣列，每個元素是一組重複文章的索引號。
只列出有重複的組，無重複的文章不需要列出。
每組中第一個索引表示「保留」的文章（選擇內容最完整的那篇）。

範例輸出：
{"groups": [[0, 3, 7], [2, 5]]}
表示：文章 0、3、7 是同一事件（保留 0），文章 2、5 是同一事件（保留 2）。

如果沒有任何重複，返回：
{"groups": []}

只返回 JSON，不要其他說明。"""

DEDUP_USER_TEMPLATE = """以下是 {count} 篇新聞，請找出重複或高度相似的文章組：

{article_list}

請返回 JSON 格式的去重結果。只返回 JSON，不要其他說明。"""
