#!/usr/bin/env python3
"""
===============================================================================
Naval Transit Updater / 軍艦通過更新器
===============================================================================

從 news_classified.json 中篩選 Foreign_battleship 類別的新聞，
自動新增到 naval_transits.csv。
"""

import csv
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple


class NavalTransitUpdater:
    """從分類新聞中提取外國軍艦通過資料並更新 naval_transits.csv"""

    # Classifier country code → CSV Country 欄位
    COUNTRY_CODE_MAP = {
        "US": "US",
        "CN": "CN",
        "JP": "Japan",
        "UK": "UK",
        "AU": "Australia",
        "CA": "Canada",
        "FR": "France",
        "DE": "Germany",
        "NL": "Netherlands",
        "TR": "Turkey",
        "NZ": "New Zealand",
        "KR": "South Korea",
        "VN": "Vietnam",
        "PH": "Philippines",
    }

    # CSV Country → classifier country code（反向對應）
    COUNTRY_REVERSE_MAP = {
        "US": "US",
        "CN": "CN",
        "Japan": "JP",
        "UK": "UK",
        "Australia": "AU",
        "Canada": "CA",
        "France": "FR",
        "Germany": "DE",
        "Netherlands": "NL",
        "Turkey": "TR",
        "New Zealand": "NZ",
        "South Korea": "KR",
        "Vietnam": "VN",
        "Philippines": "PH",
    }

    # CSV 標頭（含尾端空欄位，用於存放 URL）
    FIELDNAMES = [
        "Date", "Year", "Ships", "Country",
        "Sorties_D0", "Sorties_Total_5d", "Sorties_Prev_5d", "Increase", "",
    ]

    def __init__(self, csv_path: str, sortie_csv_path: Optional[str] = None):
        self.csv_path = Path(csv_path)
        self.sortie_data: Dict[str, str] = {}  # date -> pla_aircraft_sorties
        if sortie_csv_path:
            self._load_sortie_data(sortie_csv_path)

    # ------------------------------------------------------------------
    # Sortie lookup
    # ------------------------------------------------------------------
    def _load_sortie_data(self, path: str) -> None:
        """從 merged_comprehensive_data_M.csv 載入 PLA 架次資料供查詢"""
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    date_str = row.get("date", "").strip()
                    sorties = row.get("pla_aircraft_sorties", "").strip()
                    if date_str and sorties:
                        norm = self._normalize_date(date_str)
                        if norm:
                            self.sortie_data[norm] = sorties
        except Exception as e:
            print(f"[NavalTransitUpdater] Warning: could not load sortie data: {e}")

    def _lookup_sorties(self, date_str: str) -> Tuple[str, str, str, str]:
        """
        查詢指定日期的架次資料。

        Returns:
            (Sorties_D0, Sorties_Total_5d, Sorties_Prev_5d, Increase)
            找不到時回傳四個空字串。
        """
        d0 = self.sortie_data.get(date_str, "")
        if not d0:
            return ("", "", "", "")

        # 計算 5 天總和
        try:
            dt = datetime.strptime(date_str, "%Y/%m/%d")
            total_5d = 0
            prev_5d = 0
            for i in range(5):
                key = (dt - timedelta(days=i)).strftime("%Y/%m/%d")
                val = self.sortie_data.get(key, "")
                if val:
                    total_5d += int(float(val))
            for i in range(5, 10):
                key = (dt - timedelta(days=i)).strftime("%Y/%m/%d")
                val = self.sortie_data.get(key, "")
                if val:
                    prev_5d += int(float(val))
            increase = total_5d - prev_5d
            return (d0, str(total_5d), str(prev_5d), str(increase))
        except Exception:
            return (d0, "", "", "")

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_date(date_str: str) -> Optional[str]:
        """
        將各種日期格式統一為 YYYY/M/D（與 naval_transits.csv 相同格式）。
        """
        if not date_str:
            return None
        date_str = date_str.strip()
        formats = ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return f"{dt.year}/{dt.month}/{dt.day}"
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------
    # Load existing CSV
    # ------------------------------------------------------------------
    def _load_existing(self) -> List[Dict[str, str]]:
        """載入現有的 naval_transits.csv"""
        rows: List[Dict[str, str]] = []
        if not self.csv_path.exists():
            return rows
        with open(self.csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return rows
            for line in reader:
                if not line or not any(cell.strip() for cell in line):
                    continue
                row = {}
                for i, col in enumerate(self.FIELDNAMES):
                    row[col] = line[i].strip() if i < len(line) else ""
                rows.append(row)
        return rows

    # ------------------------------------------------------------------
    # Dedup
    # ------------------------------------------------------------------
    @staticmethod
    def _is_duplicate(existing_rows: List[Dict], new_date: str, new_ships: str) -> bool:
        """
        檢查是否已經存在相同的軍艦通過記錄。
        以日期完全相同為主要判斷依據。
        """
        for row in existing_rows:
            if row.get("Date", "").strip() == new_date:
                return True
        return False

    # ------------------------------------------------------------------
    # Country extraction
    # ------------------------------------------------------------------
    def _extract_country(self, article: Dict) -> str:
        """從分類結果提取國家，嘗試對應到 CSV 常用格式"""
        country1 = article.get("country1", "")
        return self.COUNTRY_CODE_MAP.get(country1, country1)

    # ------------------------------------------------------------------
    # Main update logic
    # ------------------------------------------------------------------
    def update_from_classified(self, classified_news: List[Dict]) -> int:
        """
        從分類結果中篩選 Foreign_battleship 並新增到 CSV。

        Args:
            classified_news: 分類後的新聞列表（含 original_article）

        Returns:
            新增的記錄數
        """
        # 篩選 Foreign_battleship
        transits = [
            a for a in classified_news
            if a.get("category") == "Foreign_battleship" and a.get("is_relevant", False)
        ]

        if not transits:
            print("[NavalTransitUpdater] No Foreign_battleship articles found.")
            return 0

        existing = self._load_existing()
        added = 0

        for article in transits:
            original = article.get("original_article", {})
            raw_date = original.get("date", "")
            norm_date = self._normalize_date(raw_date)
            if not norm_date:
                print(f"[NavalTransitUpdater] Skipping: invalid date '{raw_date}'")
                continue

            # 取得艦艇描述：優先使用標題（通常包含完整艦艇名稱）
            ships = original.get("title", "").strip()
            if not ships:
                ships = article.get("extracted_data", {}).get("Foreign_battleship", "")

            if self._is_duplicate(existing, norm_date, ships):
                print(f"[NavalTransitUpdater] Skip duplicate: {norm_date}")
                continue

            # 年份
            try:
                year = str(datetime.strptime(norm_date, "%Y/%m/%d").year)
            except ValueError:
                year = ""

            country = self._extract_country(article)
            url = original.get("url", "")

            # 查詢架次資料
            s_d0, s_5d, s_prev5d, s_inc = self._lookup_sorties(norm_date)

            new_row = {
                "Date": norm_date,
                "Year": year,
                "Ships": ships,
                "Country": country,
                "Sorties_D0": s_d0,
                "Sorties_Total_5d": s_5d,
                "Sorties_Prev_5d": s_prev5d,
                "Increase": s_inc,
                "": url,
            }
            existing.append(new_row)
            added += 1
            print(f"[NavalTransitUpdater] Added: {norm_date} - {ships[:50]}...")

        if added > 0:
            self._save(existing)

        print(f"[NavalTransitUpdater] Done: {added} new transit(s) added.")
        return added

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    @staticmethod
    def _date_sort_key(row: Dict) -> str:
        """產生可排序的日期 key（YYYY/MM/DD 補零）"""
        date_str = row.get("Date", "")
        if not date_str:
            return "9999/99/99"
        try:
            dt = datetime.strptime(date_str, "%Y/%m/%d")
            return dt.strftime("%Y/%m/%d")
        except ValueError:
            return "9999/99/99"

    def _save(self, rows: List[Dict]) -> None:
        """將資料寫回 CSV（按日期排序）"""
        rows.sort(key=self._date_sort_key)
        with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            # 寫入標頭
            writer.writerow(self.FIELDNAMES)
            for row in rows:
                writer.writerow([row.get(col, "") for col in self.FIELDNAMES])

    # ------------------------------------------------------------------
    # CSV → JSON articles (for news_classified.json)
    # ------------------------------------------------------------------
    def _country_csv_to_code(self, country_csv: str) -> str:
        """
        將 CSV 的 Country 欄位轉換為 classifier 國家代碼。
        支援 "Multi (US+UK)" 格式，取第一個國家。
        """
        if not country_csv:
            return ""
        country_csv = country_csv.strip()

        # 直接對應
        if country_csv in self.COUNTRY_REVERSE_MAP:
            return self.COUNTRY_REVERSE_MAP[country_csv]

        # Multi (US+UK) 格式：提取第一個國家
        m = re.match(r"Multi\s*\(([^+)]+)", country_csv)
        if m:
            first = m.group(1).strip()
            return self.COUNTRY_REVERSE_MAP.get(first, first)

        return country_csv

    @staticmethod
    def _date_to_iso(date_str: str) -> str:
        """將 YYYY/M/D 轉為 YYYY-MM-DD（news_classified.json 使用的格式）"""
        try:
            dt = datetime.strptime(date_str, "%Y/%m/%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return date_str

    def csv_to_json_articles(self) -> List[Dict]:
        """
        將 naval_transits.csv 所有記錄轉換為 news_classified.json 相容的
        article 格式，以便前端直接顯示。

        Returns:
            article 格式的字典列表
        """
        rows = self._load_existing()
        articles: List[Dict] = []

        for row in rows:
            date_csv = row.get("Date", "").strip()
            if not date_csv:
                continue

            ships = row.get("Ships", "").strip()
            country_csv = row.get("Country", "").strip()
            url = row.get("", "").strip()  # unnamed last column
            country_code = self._country_csv_to_code(country_csv)
            date_iso = self._date_to_iso(date_csv)

            # 若沒有 URL，用 date 產生一個唯一識別字串
            if not url:
                url = f"naval_transit://{date_csv}"

            article = {
                "category": "Foreign_battleship",
                "is_relevant": True,
                "country1": country_code,
                "country2": "",
                "sentiment_score": 0,
                "sentiment_label": "neutral",
                "extracted_data": {
                    "Foreign_battleship": ships,
                },
                "confidence": 1.0,
                "original_article": {
                    "date": date_iso,
                    "title": ships,
                    "content": ships,
                    "url": url,
                    "source": "naval_transits",
                },
            }
            articles.append(article)

        print(f"[NavalTransitUpdater] Converted {len(articles)} CSV rows to JSON articles.")
        return articles
