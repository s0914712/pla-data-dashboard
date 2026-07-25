#!/usr/bin/env python3
"""把 naval_transits.csv 的台海通過記錄同步進 merged_comprehensive_data_M.csv。

為什麼需要這支：稽核發現 naval_transits.csv 有 69 筆台海通過記錄，但
comprehensive 的 Foreign_battleship 欄只有 12 筆非空值，**59 筆從未寫入**。
追下去發現不是寫壞，是根本沒有寫入者 ——

  - data_merger.py 的 MERGE_COLUMNS 只複製 pla_aircraft_sorties 與
    china_carrier_present，沒有 Foreign_battleship
  - naval_transit_updater.py 只寫入 naval_transits.csv 本身

comprehensive 裡那 12 筆是早年手動輸入的，之後沒有任何流程接手。

預設為 dry-run，只報告不寫檔。確認無誤後加 --apply。
既有的非空值一律不覆蓋（那些是人工填的，比自動生成的描述更可靠），
只補空缺。

用法:
    python3 scripts/updaters/sync_naval_to_comprehensive.py
    python3 scripts/updaters/sync_naval_to_comprehensive.py --apply
    python3 scripts/updaters/sync_naval_to_comprehensive.py --apply --add-missing-rows
"""

import argparse
import os

import pandas as pd

NAVAL_CSV = "data/naval_transits.csv"
COMPREHENSIVE_CSV = "data/merged_comprehensive_data_M.csv"
TARGET_COL = "Foreign_battleship"


# 新聞版面標籤，由 CNA 爬蟲的標題原封不動帶進 Ships 欄位。
NEWS_SECTION_TAGS = ["| 政治", "｜政治", "| 兩岸", "｜兩岸", "| 軍事", "｜軍事",
                     "| 國際", "｜國際"]


def _clean_ships(text):
    out = str(text or "").strip()
    for tag in NEWS_SECTION_TAGS:
        out = out.replace(tag, "")
    return out.strip()


def _is_pla_activity(row):
    """判斷該列其實是共軍活動，而非外國軍艦通過台海。

    Foreign_battleship 這個欄位的語意是「外國海軍艦艇通過台灣海峽」，
    是一種外部訊號。但 naval_transits.csv 裡有 9 筆 Country=CN 的列，
    內容是「7艘共艦台海周邊活動國軍嚴密監控」這類共軍動態新聞 ——
    那是被預測的對象本身，不是外部訊號。混進來會讓欄位語意崩壞，
    拿去當特徵還會造成標籤洩漏。
    """
    country = str(row.get("Country", "") or "").strip().upper()
    ships = str(row.get("Ships", "") or "")

    if country == "CN":
        return True
    # 已標記為其他國家時，即使文字裡順帶提到共軍也仍是外國艦艇通過。
    # 例：2025-02-26 [UK]「共軍派出32架戰機…同時英國海軍巡邏艦史佩號通過台海」
    # 講的是 HMS Spey 的通過，只是新聞把共軍動態寫在同一句。
    if country and country.upper() not in ("NAN", ""):
        return False
    return any(kw in ships for kw in ("共艦", "中共軍艦", "共軍", "中國海警"))


def load_naval(verbose=True):
    df = pd.read_csv(NAVAL_CSV, encoding="utf-8-sig")
    df["_date"] = pd.to_datetime(df["Date"], errors="coerce", format="mixed")
    df = df[df["_date"].notna()].copy()

    df["_is_pla"] = df.apply(_is_pla_activity, axis=1)
    excluded = df[df["_is_pla"]]
    if verbose and len(excluded):
        print(f"排除 {len(excluded)} 筆共軍活動（非外國軍艦通過）:")
        for _, r in excluded.iterrows():
            print(f"  {r['_date'].date()}  [{r['Country']}] {_clean_ships(r['Ships'])[:56]}")
        print()
    df = df[~df["_is_pla"]]

    def describe(row):
        ships = _clean_ships(row.get("Ships"))
        country = str(row.get("Country", "") or "").strip()
        # Country 對自動新增的列並不可靠（例如 2020-02-15 標成
        # Multi(US+Germany)，但艦名只有美艦 USS Chancellorsville），
        # 所以只在艦名本身缺失時才拿它補位，不主動拼在前面。
        if ships:
            return ships
        return country if country.lower() != "nan" else "軍艦通過台灣海峽"

    df["_desc"] = df.apply(describe, axis=1)
    return df[["_date", "_desc"]].drop_duplicates("_date").sort_values("_date")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="實際寫入（預設只報告）")
    ap.add_argument("--add-missing-rows", action="store_true",
                    help="comprehensive 沒有該日期時新增列（預設只補既有列）")
    args = ap.parse_args()

    for path in (NAVAL_CSV, COMPREHENSIVE_CSV):
        if not os.path.exists(path):
            raise SystemExit(f"找不到 {path}")

    naval = load_naval()
    comp = pd.read_csv(COMPREHENSIVE_CSV, encoding="utf-8-sig")
    comp["_date"] = pd.to_datetime(comp["date"], errors="coerce", format="mixed")
    if TARGET_COL not in comp.columns:
        comp[TARGET_COL] = pd.NA

    comp_dates = set(comp["_date"].dropna())
    filled = comp[comp[TARGET_COL].notna()]["_date"].dropna()
    already = set(filled)

    to_fill, to_add, skipped = [], [], []
    for _, row in naval.iterrows():
        d = row["_date"]
        if d in already:
            skipped.append(d)
        elif d in comp_dates:
            to_fill.append((d, row["_desc"]))
        else:
            to_add.append((d, row["_desc"]))

    print(f"naval_transits: {len(naval)} 筆  "
          f"({naval['_date'].min().date()} ~ {naval['_date'].max().date()})")
    print(f"comprehensive:  {len(comp)} 列  "
          f"({comp['_date'].min().date()} ~ {comp['_date'].max().date()})")
    print(f"  {TARGET_COL} 已有值: {len(already)} 個日期\n")
    print(f"可補入既有列       : {len(to_fill)} 筆")
    print(f"日期不在 comprehensive: {len(to_add)} 筆"
          f"{'（本次會新增）' if args.add_missing_rows else '（本次略過，加 --add-missing-rows 才會新增）'}")
    print(f"已有值不覆蓋       : {len(skipped)} 筆")

    if to_fill:
        print("\n將補入既有列的日期:")
        for d, desc in to_fill[:20]:
            print(f"  {d.date()}  {desc[:60]}")
        if len(to_fill) > 20:
            print(f"  ... 另外 {len(to_fill) - 20} 筆")
    if to_add:
        print("\n不在 comprehensive 的日期"
              f"（comprehensive 目前只到 {comp['_date'].max().date()}）:")
        for d, desc in to_add[:20]:
            print(f"  {d.date()}  {desc[:60]}")
        if len(to_add) > 20:
            print(f"  ... 另外 {len(to_add) - 20} 筆")

    if not args.apply:
        print("\n[dry-run] 未寫入任何檔案。確認後加 --apply。")
        return

    for d, desc in to_fill:
        comp.loc[comp["_date"] == d, TARGET_COL] = desc
    added = 0
    if args.add_missing_rows and to_add:
        new_rows = []
        for d, desc in to_add:
            row = {c: pd.NA for c in comp.columns}
            row["date"] = d.strftime("%Y/%-m/%-d")
            row["_date"] = d
            row[TARGET_COL] = desc
            new_rows.append(row)
        comp = pd.concat([comp, pd.DataFrame(new_rows)], ignore_index=True)
        added = len(new_rows)

    comp = comp.sort_values("_date").drop(columns=["_date"])
    comp.to_csv(COMPREHENSIVE_CSV, index=False, encoding="utf-8-sig")
    print(f"\n已寫入 {COMPREHENSIVE_CSV}: 補入 {len(to_fill)} 筆，新增 {added} 列")


if __name__ == "__main__":
    main()
