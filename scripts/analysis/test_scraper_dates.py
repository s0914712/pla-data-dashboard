#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scraper.py 純函數的回歸測試 —— 日期解析與數字抽取。

為什麼只測純函數：scraper.py 需要 selenium + Chrome，CI 與本機都不容易端到端跑。
但這兩個 bug 都在純函數裡，剛好是能測的部分。

樣本是國防部 115/07/31 的真實發布原文（共機 27 架次、共艦 9 艘），
也就是當初發現問題的那一份。

用法:
    python3 scripts/analysis/test_scraper_dates.py
"""

import os
import re
import sys
import types

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
SCRAPER = os.path.join(REPO_ROOT, "scraper.py")

# ── 真實樣本：國防部 115/07/31 發布 ────────────────────────────
# 涵蓋 7/30 0600 至 7/31 0600。注意內文有「兩個」日期：
# 第一個是區間起點（7/30），第二個才是資料日期（7/31）。
LIST_TITLE = "中共解放軍臺海周邊海、空域動態(115.07.31)"
BODY = """中共解放軍臺海周邊海、空域動態

一、日期：
中華民國115年7月30日（星期四）0600時至115年7月31日（星期五）0600時止。

二、活動動態：
迄0600時止，偵獲共機27架次（逾越中線進入北部、中部、西南及東部空域22架次）、\
共艦9艘及公務船2艘，持續在臺海周邊活動。國軍運用任務機、艦及岸置飛彈系統嚴密監控與應處。"""

EXPECTED_DATE = "2026/07/31"      # 發布日 ＝ 區間結束日，與列表頁標題一致
EXPECTED_COUNTS = (27, 9)         # 共機架次、共艦艘數（公務船不計，見下方測試）


def load_pure_functions():
    """只載入 scraper.py 裡不依賴 selenium 的函數。

    scraper.py 在模組層就 import selenium，直接 import 會炸，
    所以擷取原始碼中的純函數區段單獨 exec。
    """
    src = open(SCRAPER, encoding="utf-8").read()
    start = src.index("def extract_numbers_from_text")
    end = src.index("def get_latest_date_from_csv")
    mod = types.ModuleType("scraper_pure")
    mod.re = re
    exec(compile(src[start:end], SCRAPER, "exec"), mod.__dict__)
    return mod


FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}\n     got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def main():
    m = load_pure_functions()
    print("scraper.py 純函數回歸測試\n" + "=" * 60)

    print("\n[1] 數字抽取")
    check("共機/共艦數量", m.extract_numbers_from_text(BODY), EXPECTED_COUNTS)
    # 公務船刻意不計入共艦：海警等公務船與軍艦性質不同，
    # 混進 plan_vessel_sorties 會污染既有序列。
    check("公務船未被誤計為共艦", m.extract_numbers_from_text(BODY)[1], 9)

    print("\n[2] 列表頁標題（主要路徑）")
    check("標題解析出發布日", m.parse_date_from_text(LIST_TITLE), EXPECTED_DATE)

    print("\n[3] 詳細頁內文（後備路徑）")
    # 診斷用，不是斷言：parse_date_from_text() 對內文會回傳區間「起點」。
    # 它本身沒壞（列表頁標題用它是對的），壞的是拿它去解析內文。
    # 修好之後這行仍會印 2026/07/30 —— 這正是為什麼內文要改用 parse_report_date()。
    print(f"     （診斷）parse_date_from_text(內文) = "
          f"{m.parse_date_from_text(BODY)!r} ← 區間起點，不可當資料日期")
    # 這是核心斷言：內文的第一個日期是區間起點（7/30），不是資料日期。
    # 兩條路徑必須回傳同一天，否則同一份報告會依走哪條路徑被標到不同日期。
    parse_report_date = getattr(m, "parse_report_date", None)
    if parse_report_date is None:
        print("  ❌ 內文解析出資料日期\n     scraper.py 尚未提供 parse_report_date()")
        FAILURES.append("內文解析出資料日期")
    else:
        check("內文解析出資料日期（非區間起點）", parse_report_date(BODY), EXPECTED_DATE)

    print("\n[4] 兩條路徑必須一致")
    if parse_report_date is not None:
        check("標題路徑 == 內文路徑",
              m.parse_date_from_text(LIST_TITLE) == parse_report_date(BODY), True)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 條失敗：{'、'.join(FAILURES)}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
