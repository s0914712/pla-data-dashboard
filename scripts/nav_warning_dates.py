#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MSA 航行警告的起訖日解析。

這支模組存在的理由是「起訖日必須是結構化的兩個日期，不是一串原始比對片段」。

原本 NavigationWarning_scraper.parse_time_period() 跑六條 regex、只回
`list(set(m.group() for ...))`，capture group 全丟掉。後果有三：

1. 同一則公告會同時命中多條 pattern，產生互相重疊的片段，例如
   「7月20日0600时至22日1200时」與「2026年7月20日0600时至22日1200时」
   兩者都留著，join 起來 45 字元。下游 LINE 推播切 [:40]、地圖圖例切 [:16]，
   一定切在迄日中間 —— 使用者看到的「22日12」就是這樣來的。
2. pattern1 會漏掉月份，產生「24日0600时至1800时」這種沒有月份的片段；
   set() 沒有順序，這個殘缺片段有機會排在第一個。
3. set() 的迭代順序不保證穩定，同樣的輸入每次 join 出來的字串可能不同，
   每天 commit 都出現假 diff。

這裡改成解析出 (start, end, 每日起訖時刻)，格式化交給 format_periods_zh()，
輸出短到根本不需要截斷。年份推斷規則與 pla_7day_predictor._parse_navwarn_window
相同（公告月份遠小於發布月份 → 跨年），該處已改為呼叫本模組。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

# 演習窗最長天數。解析爆掉時（例如把座標的數字當成日期）用來擋下離譜區間。
MAX_WINDOW_DAYS = 30

_EN_MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}


@dataclass(frozen=True)
class Period:
    """一段演習/射擊窗。

    start / end 為日期；start_time / end_time 為 HHMM 時刻字串。

    daily 區分兩種語意完全不同的公告，混為一談會誤導讀者：
      daily=False 「3月18日0800时至25日2400时」— 一段連續封閉的時段
      daily=True  「3月16日至17日，每天1000时至1800时」— 每天只封閉數小時
    """
    start: Optional[date]
    end: Optional[date]
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    daily: bool = False
    raw: str = ''

    def key(self):
        return (self.start, self.end, self.start_time, self.end_time, self.daily)


def _norm_hhmm(value) -> Optional[str]:
    """把 '6' / '06' / '0600' / '600' 一律正規化成 'HHMM'，不合理則回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s.isdigit():
        return None
    if len(s) <= 2:                 # 「6时」= 0600
        hh, mm = int(s), 0
    else:
        s = s.zfill(4)
        hh, mm = int(s[:-2]), int(s[-2:])
    if hh > 24 or mm > 59:
        return None
    return f'{hh:02d}{mm:02d}'


def _make_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _infer_year(month: int, publish: Optional[date]) -> int:
    """公告月份遠小於發布月份 → 跨年（12 月發布隔年 1 月的演習）。"""
    if publish is None:
        return date.today().year
    year = publish.year
    if month < publish.month - 6:
        year += 1
    elif month > publish.month + 6:   # 1 月發布、公告寫 12 月 → 去年的延續公告
        year -= 1
    return year


def _clamp(start: Optional[date], end: Optional[date]):
    """迄日不得早於起日，且窗長不超過 MAX_WINDOW_DAYS。"""
    if start is None:
        return start, end
    if end is None or end < start:
        end = start
    limit = start + timedelta(days=MAX_WINDOW_DAYS)
    if end > limit:
        end = limit
    return start, end


def _parse_publish(publish) -> Optional[date]:
    if publish is None:
        return None
    if isinstance(publish, date):
        return publish
    s = str(publish).strip()[:10]
    m = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if not m:
        return None
    return _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


# ── 中文格式 ────────────────────────────────────────────────
# 一條 regex 吃掉原本 pattern1/2/3/5 的所有變形，避免片段互相重疊。
#
#   自?(YYYY年)?M月D日(HHMM时)? [至到-~] (YYYY年)?(M月)?(D日)?(HHMM时)?
#
# 起日的「月」「日」為必要，其餘皆可選；迄日的日期必須帶「日」字，
# 否則「0800时至1200时」會被誤讀成「至 1200 日」。
#
# 起訖日之間常插入全形逗號（「4月1日，0800时至1200时」），所以時刻前允許標點。
_ZH_RANGE = re.compile(
    r'自?\s*(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日'
    r'(?:\s*[，,、]?\s*(\d{1,4})\s*[时時])?'
    r'(?:\s*[至到\-~－—]\s*'
    r'(?:(\d{4})\s*年)?\s*(?:(\d{1,2})\s*月)?\s*(?:(\d{1,2})\s*日)?'
    r'(?:\s*[，,、]?\s*(\d{1,4})\s*[时時])?'
    r')?'
)

# 「每日/每天 HHMM时至HHMM时」—— 補在日期區間之後的每日時段
_ZH_DAILY = re.compile(
    r'每\s*[日天]\s*(\d{1,4})\s*[时時]\s*[至到\-~]\s*(\d{1,4})\s*[时時]'
)

# ── 英文格式 ────────────────────────────────────────────────
# 1) FROM DDHHMM UTC TO DDHHMM UTC        （日＋時分，月份要靠發布日推）
_EN_UTC = re.compile(
    r'FROM\s+(\d{6})\s*UTC\s+TO\s+(\d{6})\s*UTC', re.IGNORECASE)
# 2) FROM 16 TO 17 MAR / FROM 16 MAR TO 17 MAR / FROM 30 DEC TO 02 JAN
#    這一種在舊版完全沒有 pattern，英文版公告的起訖日一律解析失敗。
_EN_DAY_MON = re.compile(
    r'FROM\s+(\d{1,2})\s*([A-Z]{3})?\s+TO\s+(\d{1,2})\s*([A-Z]{3})',
    re.IGNORECASE)
# 3) 0200 UTC TO 1000 UTC DAILY  —— 每日時段，配合 2) 使用
_EN_DAILY = re.compile(
    r'(\d{4})\s*UTC\s+TO\s+(\d{4})\s*UTC\s+DAILY', re.IGNORECASE)


def _parse_zh(text: str, publish: Optional[date]) -> List[Period]:
    periods = []
    daily = _ZH_DAILY.search(text)
    daily_pair = None
    if daily:
        ds, de = _norm_hhmm(daily.group(1)), _norm_hhmm(daily.group(2))
        if ds and de:
            daily_pair = (ds, de)

    for m in _ZH_RANGE.finditer(text):
        (sy, sm, sd, st, ey, em, ed, et) = m.groups()
        smonth, sday = int(sm), int(sd)
        syear = int(sy) if sy else _infer_year(smonth, publish)
        start = _make_date(syear, smonth, sday)
        if start is None:
            continue

        if ed:
            emonth = int(em) if em else smonth
            eday = int(ed)
            eyear = int(ey) if ey else syear
            end = _make_date(eyear, emonth, eday)
            # 「12月30日至1月2日」：迄日在起日之前代表跨年
            if end is not None and end < start and not ey:
                end = _make_date(eyear + 1, emonth, eday)
        else:
            end = start

        start, end = _clamp(start, end)
        s_time = _norm_hhmm(st)
        e_time = _norm_hhmm(et)
        # 公告本身沒寫時刻，但句尾有「每天HHMM时至HHMM时」→ 套用為每日時段
        is_daily = False
        if daily_pair and not (s_time and e_time):
            s_time, e_time = daily_pair
            is_daily = True
        periods.append(Period(start, end, s_time, e_time, is_daily, m.group().strip()))
    return periods


def _parse_en(text: str, publish: Optional[date]) -> List[Period]:
    periods = []

    for m in _EN_UTC.finditer(text):
        raw_s, raw_e = m.group(1), m.group(2)
        if publish is None:
            continue
        # DDHHMM，月份沿用發布月；迄日的日小於起日代表跨月
        sday, s_time = int(raw_s[:2]), _norm_hhmm(raw_s[2:])
        eday, e_time = int(raw_e[:2]), _norm_hhmm(raw_e[2:])
        start = _make_date(publish.year, publish.month, sday)
        if start is None:
            continue
        end = _make_date(publish.year, publish.month, eday)
        if end is None or eday < sday:
            nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            end = _make_date(nxt.year, nxt.month, eday)
        start, end = _clamp(start, end)
        periods.append(Period(start, end, s_time, e_time, False, m.group().strip()))

    daily = _EN_DAILY.search(text)
    daily_pair = None
    if daily:
        ds, de = _norm_hhmm(daily.group(1)), _norm_hhmm(daily.group(2))
        if ds and de:
            daily_pair = (ds, de)

    for m in _EN_DAY_MON.finditer(text):
        sday, smon, eday, emon = m.groups()
        emonth = _EN_MONTHS.get(emon.upper()) if emon else None
        if emonth is None:
            continue
        smonth = _EN_MONTHS.get(smon.upper()) if smon else emonth
        syear = _infer_year(smonth, publish)
        start = _make_date(syear, smonth, int(sday))
        if start is None:
            continue
        eyear = syear + 1 if emonth < smonth else syear
        end = _make_date(eyear, emonth, int(eday))
        start, end = _clamp(start, end)
        ds, de = (daily_pair if daily_pair else (None, None))
        periods.append(Period(start, end, ds, de, bool(daily_pair), m.group().strip()))

    return periods


def parse_periods(text, publish_date=None) -> List[Period]:
    """從公告文字解析所有演習/射擊窗，依起日排序、去重。

    publish_date 用於推斷公告未寫出的年份與月份（英文 DDHHMM 格式必需）。
    回傳空 list 代表解析不出來 —— 呼叫端應保留原文，不要猜。
    """
    if not text:
        return []
    s = str(text)
    if s.strip().lower() in ('nan', 'none'):
        return []

    publish = _parse_publish(publish_date)
    found = _parse_zh(s, publish) + _parse_en(s, publish)

    seen, out = set(), []
    for p in found:
        if p.start is None:
            continue
        k = p.key()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    # 穩定排序：起日 → 迄日 → 每日起時。不用 set()，避免每天產生假 diff。
    out.sort(key=lambda p: (p.start, p.end or p.start, p.start_time or ''))
    return out


def format_periods_zh(periods, max_periods=3) -> str:
    """格式化成短字串，短到下游不需要截斷。

    單日          → 07/23 0600-1800
    連續跨日      → 07/20 0600 至 07/22 1200
    每日時段      → 07/16-07/17 每日 1000-1800
    """
    if not periods:
        return ''
    chunks = []
    for p in periods[:max_periods]:
        if p.start is None:
            continue
        s = f'{p.start:%m/%d}'
        multi_day = bool(p.end and p.end != p.start)
        e = f'{p.end:%m/%d}' if multi_day else ''

        if p.daily and p.start_time and p.end_time:
            span = f'{s}-{e}' if multi_day else s
            chunks.append(f'{span} 每日 {p.start_time}-{p.end_time}')
        elif multi_day:
            head = f'{s} {p.start_time}' if p.start_time else s
            tail = f'{e} {p.end_time}' if p.end_time else e
            chunks.append(f'{head} 至 {tail}')
        elif p.start_time and p.end_time:
            chunks.append(f'{s} {p.start_time}-{p.end_time}')
        else:
            chunks.append(s)
    text = '；'.join(chunks)
    if len(periods) > max_periods:
        text += f'…等 {len(periods)} 段'
    return text


def period_bounds(periods):
    """回傳所有區間的整體 (最早起日, 最晚迄日)；無資料回 (None, None)。"""
    valid = [p for p in periods if p.start is not None]
    if not valid:
        return None, None
    start = min(p.start for p in valid)
    end = max((p.end or p.start) for p in valid)
    return start, end


def parse_bounds_iso(text, publish_date=None):
    """便利函式：直接拿 ('YYYY-MM-DD', 'YYYY-MM-DD', '顯示字串')。"""
    periods = parse_periods(text, publish_date)
    start, end = period_bounds(periods)
    return (start.isoformat() if start else '',
            end.isoformat() if end else '',
            format_periods_zh(periods))
