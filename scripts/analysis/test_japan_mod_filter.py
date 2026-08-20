#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scraper_japan_mod.py 回歸測試 —— 動向通報的篩選與同日合併。

當初的症狀：CSV 上最後一筆有艦型的中俄艦艇資料停在 2026/07/21，
之後每天都是「未提及／未知海軍N艘艦艇航行」，但防衛省網站上明明還有
p20260730_01.pdf（中国海軍ジャンカイⅡ級・レンハイ級通過宮古海峽）。

兩個原因，這裡各測一段：

1. is_target_navy_pdf() 只看單詞（'艦艇'、'補給艦'…），把熊本地震
   （2026/07/28）之後每天發的災害派遣速報、日米比共同訓練、海賊対処
   部隊出入港通報全都當成中俄艦艇動向。
2. 同一天多份通報時，update_csv() 是後寫覆蓋前寫。災害派遣速報編號
   通常排在最後，於是把當天真正的艦艇通報整列蓋掉。

樣本全部取自 data/pdf_texts/japan_mod_texts.json（專案內的離線快取），
所以這支測試不需要網路。

用法:
    python3 scripts/analysis/test_japan_mod_filter.py
"""

import json
import os
import sys

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, REPO_ROOT)

import scraper_japan_mod as S  # noqa: E402

CACHE_PATH = os.path.join(REPO_ROOT, S.PDF_TEXT_CACHE)

# 真實樣本。左為檔名，右為 is_target_navy_pdf() 應有的判定。
SAMPLES = [
    # 真正的動向通報 —— 必須收
    ("p20260730_01.pdf", True),   # 中国海軍艦艇の動向について（本次問題的主角）
    ("p20260721_01.pdf", True),   # 中国及びロシア海軍艦艇の動向について
    ("p20260812_03.pdf", True),   # 中国軍機の動向について
    ("p20260817_02.pdf", True),   # ロシア海軍艦艇の動向について
    # 非動向通報 —— 必須排除
    ("p20260730_02.pdf", False),  # 熊本地震の災害派遣（與 _01 同一天，先前把 _01 蓋掉）
    ("p20260819_01.pdf", False),  # 熊本地震の災害派遣
    ("p20260725_02.pdf", False),  # 日米比共同訓練の実施について
    ("p20260721_02.pdf", False),  # 第５３次派遣海賊対処行動水上部隊の帰港について
]

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}\n     got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def contains(name, got, needles):
    missing = [n for n in needles if n not in got]
    ok = not missing
    print(f"  {'✅' if ok else '❌'} {name}\n     got={got!r}")
    if not ok:
        print(f"     缺少: {'、'.join(missing)}")
        FAILURES.append(name)


def main():
    with open(CACHE_PATH, encoding="utf-8") as fh:
        cache = json.load(fh)["texts"]
    print("scraper_japan_mod.py 回歸測試\n" + "=" * 60)
    print(f"文字快取: {CACHE_PATH}（{len(cache)} 份）")

    print("\n[1] is_target_navy_pdf() 的收與棄")
    for filename, want in SAMPLES:
        if filename not in cache:
            print(f"  ⚠️ 快取無 {filename}，略過")
            continue
        check(filename, S.is_target_navy_pdf(cache[filename]["text"]), want)

    print("\n[2] 災害派遣速報一律排除")
    quake = [k for k, v in cache.items() if "災害派遣" in v["text"]]
    wrong = [k for k in quake if S.is_target_navy_pdf(cache[k]["text"])]
    check(f"{len(quake)} 份災害派遣速報全被排除", wrong, [])

    print("\n[3] 同一天多份通報要合併，不是後寫覆蓋前寫")
    # 2026/07/30：_01 是中国海軍動向、_02 是災害派遣。
    # 修好之前 _02 會把 _01 的結果整列蓋成「未知海軍4艘艦艇航行」。
    day = S.collect_day_texts("2026/07/30", cache)
    check("2026/07/30 只留下 1 份動向通報", len(day), 1)
    result = S.analyze_with_rules("\n".join(day), "2026/07/30")
    check("2026/07/30 國家", result["國家"], "中國")
    check("2026/07/30 宮古海峽", result["宮古"], 1)
    contains("2026/07/30 艦型", result["艦型"], ["江凱II級護衛艦", "南昌級驅逐艦"])

    # 2026/08/17：四份通報，_01 是災害派遣、_02~_04 是ロシア海軍動向。
    # 併起來的艦型必須是三份的聯集，而不是最後一份（或第一份）的內容。
    day = S.collect_day_texts("2026/08/17", cache)
    check("2026/08/17 留下 3 份動向通報", len(day), 3)
    merged_text, merged_count = S.merge_day_text("2026/08/17", cache)
    check("merge_day_text 回報份數", merged_count, 3)
    merged = S.analyze_with_rules(merged_text, "2026/08/17")
    # 合併後仍要能逐份切掉「行動概要」示意圖。若用 '\n' 接起來，
    # split('行動概要')[0] 會把第二份之後整個丟掉，艦型只剩 _02 的一種。
    contains("2026/08/17 艦型為三份的聯集", merged["艦型"],
             ["無畏I級驅逐艦", "巴爾贊姆級情報收集艦", "イゴリ・ベロウソフ級潜水艦救難艦"])

    print("\n[4] 當天只有災害派遣速報時，不該產出任何艦艇資料")
    check("2026/08/19 無動向通報", S.collect_day_texts("2026/08/19", cache), [])

    print("\n[5] 艦型只取本次航跡，不吃過往指涉")
    # p20260730_01：本次是ジャンカイⅡ級（546）與レンハイ級（101）兩艘。
    # フチ級補給艦（902）只出現在末尾「行動概要」示意圖的 6/27~6/28 舊航次裡。
    ships = S._extract_ship_classes(cache["p20260730_01.pdf"]["text"])
    contains("2026/07/30 收錄本次的兩艘", ships, ["江凱II級護衛艦", "南昌級驅逐艦"])
    check("2026/07/30 不收「行動概要」裡的舊航次艦艇",
          "福池級綜合補給艦" in ships, False)
    # p20260817_02：本次只有ウダロイⅠ級一艘；示意圖另標了 8/4~8/5 的「既公表」
    # 船團（スラバ級、ステレグシチー級），不能算進來。
    check("2026/08/17 _02 只收本次的一艘",
          S._extract_ship_classes(cache["p20260817_02.pdf"]["text"]),
          "無畏I級驅逐艦")

    print("\n[6] 艦級名不是純片假名也要抓得到")
    # 「キロ改級」夾漢字、「イゴリ・ベロウソフ級」有中黑點、
    # 「ハイジウ１０１級」編號三位 —— 舊式樣一律漏掉。
    contains("キロ改級潜水艦、イゴリ・ベロウソフ級",
             S._extract_ship_classes(cache["p20260814_04.pdf"]["text"]),
             ["※キロ改級潜水艦", "※イゴリ・ベロウソフ級潜水艦救難艦"])
    contains("ハイジウ１０１級サルベージ救難艦",
             S._extract_ship_classes(cache["p20260709_01.pdf"]["text"]),
             ["※ハイジウ１０１級サルベージ救難艦"])

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 條失敗：{'、'.join(FAILURES)}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
