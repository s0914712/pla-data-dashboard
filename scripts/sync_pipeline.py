#!/usr/bin/env python3
"""把各爬蟲的產出統一同步進 comprehensive 與 naval_transits。

在這支之前，四段管線各寫各的、中間沒人負責搬運，結果是：

- 防衛省解析出的海峽通過與國家判定停在 JapanandBattleship.csv，
  因為 data_merger 的 MERGE_COLUMNS 只複製兩個欄位
- naval_transits.csv 有 69 筆，comprehensive 的 Foreign_battleship 只有
  12 筆 —— 57 筆從未寫入，因為根本沒有寫入者
- 新聞管線寫進 naval_transits 的是新聞標題原文，帶著版面標籤與共軍動態，
  每次都要人工回頭清

本腳本把這些接起來，且每一步都冪等，可以每天無腦跑。

流程：
    1. naval_transits.csv 正規化（清版面標籤、補國家/舷號/艦型）
    2. JapanandBattleship.csv -> comprehensive（架次、航母、四海峽、國家）
    3. naval_transits.csv -> comprehensive 的 Foreign_battleship
    4. 一致性檢查與摘要

用法:
    python3 scripts/sync_pipeline.py            # dry-run，只報告
    python3 scripts/sync_pipeline.py --apply    # 實際寫入
"""

import argparse
import os
import subprocess
import sys

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts", "updaters"))

import naval_transit_enricher as enricher  # noqa: E402

JAPAN_CSV = os.path.join(BASE, "data", "JapanandBattleship.csv")
COMPREHENSIVE_CSV = os.path.join(BASE, "data", "merged_comprehensive_data_M.csv")
NAVAL_CSV = os.path.join(BASE, "data", "naval_transits.csv")

# 從 JapanandBattleship 搬到 comprehensive 的欄位
JAPAN_MERGE_COLUMNS = [
    "pla_aircraft_sorties", "china_carrier_present",
    "與那國", "宮古", "大禹", "對馬", "國家",
]

# 四海峽是二元欄位（0/1），與架次那種計數欄位在報表上要分開處理
STRAIT_COLUMNS = ["與那國", "宮古", "大禹", "對馬"]

# naval_transits 需要的結構化欄位
NAVAL_EXTRA_COLUMNS = [
    "Ship_Type", "Hull_Number", "Mission_Note",
    "Date_Precision", "Date_Note", "Source", "Country_Confidence",
]


def _norm_dates(series):
    return pd.to_datetime(series, errors="coerce", format="mixed")


def step_enrich_naval(apply_changes):
    """步驟 1：naval_transits.csv 正規化。"""
    print("\n[1/4] naval_transits.csv 正規化")
    df = pd.read_csv(NAVAL_CSV, encoding="utf-8-sig")
    before = df.copy()
    for col in NAVAL_EXTRA_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA

    df, stats = enricher.enrich_dataframe(df, verbose=False)
    changed = int((df.fillna("__na__") != before.reindex(columns=df.columns)
                   .fillna("__na__")).any(axis=1).sum())

    print(f"      清版面標籤 {stats['cleaned']} / 補國家 {stats['country']} / "
          f"補舷號 {stats['hull']} / 補艦型 {stats['ship_type']}")
    print(f"      共軍動態（不處理，保留原列）{stats['pla']} 筆；異動 {changed} 列")
    if apply_changes and changed:
        df.to_csv(NAVAL_CSV, index=False, encoding="utf-8-sig")
        print(f"      已寫入 {NAVAL_CSV}")
    return changed


def step_japan_to_comprehensive(apply_changes, fill_missing_dates=False):
    """JapanandBattleship -> comprehensive。

    comprehensive 只有 277 個有效日期，日本防衛省則有 1941 天 —— 預設情況下
    有海峽標記的 135 天裡有 118 天在 comprehensive 根本沒有對應列，資料就是
    進不去。fill_missing_dates 會補上這些列，代價是 comprehensive 會從
    約 1000 列長到約 2700 列。
    """
    print("\n[3/4] JapanandBattleship.csv -> comprehensive")
    japan = pd.read_csv(JAPAN_CSV, encoding="utf-8-sig")
    comp = pd.read_csv(COMPREHENSIVE_CSV, encoding="utf-8-sig")
    japan["_d"] = _norm_dates(japan["date"])
    comp["_d"] = _norm_dates(comp["date"])

    source = {r["_d"]: r for _, r in japan.iterrows() if pd.notna(r["_d"])}
    for col in JAPAN_MERGE_COLUMNS:
        if col not in comp.columns:
            comp[col] = pd.NA

    added_rows = 0
    if fill_missing_dates:
        existing_dates = set(comp["_d"].dropna())
        missing = sorted(d for d in source if d not in existing_dates)
        if missing:
            # 不用 pd.concat —— 整批全是 NA 的新列會觸發 dtype 推斷的
            # FutureWarning，未來版本行為還會改變。逐列以 .loc 附加可以
            # 完全沿用 comp 既有的 dtype，也不需要額外處理。
            start = len(comp)
            for offset, d in enumerate(missing):
                comp.loc[start + offset, "date"] = d.strftime("%Y/%-m/%-d")
                comp.loc[start + offset, "_d"] = d
            added_rows = len(missing)
            print(f"      新增 {added_rows} 個缺少的日期列")

    filled = {c: 0 for c in JAPAN_MERGE_COLUMNS}
    marks = {c: 0 for c in JAPAN_MERGE_COLUMNS}   # 其中值為 1 的（真的有通過）
    for idx, row in comp.iterrows():
        src = source.get(row["_d"])
        if src is None:
            continue
        for col in JAPAN_MERGE_COLUMNS:
            if col not in src.index or pd.isna(src[col]):
                continue
            current = comp.at[idx, col]
            # 只補空值，不覆蓋 —— comprehensive 裡可能有人工整理過的內容
            if pd.isna(current) or str(current).strip() == "":
                comp.at[idx, col] = src[col]
                filled[col] += 1
                # 只有海峽是二元欄位，「值為 1」才有「有通過」的意義。
                # 架次的 1 就只是 1 架，數它沒有意義。
                if col in STRAIT_COLUMNS and str(src[col]).strip() in ("1", "1.0"):
                    marks[col] += 1

    total = sum(filled.values()) + added_rows
    for col, n in filled.items():
        if n:
            # 海峽欄位補進去的多半是 0（當天沒通過），0 同樣是有效資訊，
            # 但「補了幾格」和「其中幾格是真的有通過」意義差很多，分開報。
            detail = f"（其中 {marks[col]} 格為 1）" if marks[col] else ""
            print(f"      {col:<22} 補入 {n} 格{detail}")
    if not total:
        print("      沒有可補的空格（已同步）")

    if apply_changes and total:
        comp.sort_values("_d").drop(columns=["_d"]).to_csv(
            COMPREHENSIVE_CSV, index=False, encoding="utf-8-sig")
        print(f"      已寫入 {COMPREHENSIVE_CSV}")
    return total


def step_naval_to_comprehensive(apply_changes):
    """步驟 3：naval_transits -> comprehensive 的 Foreign_battleship。"""
    print("\n[2/4] naval_transits.csv -> comprehensive (Foreign_battleship)")
    cmd = [sys.executable,
           os.path.join(BASE, "scripts", "updaters", "sync_naval_to_comprehensive.py")]
    if apply_changes:
        # 一併新增 comprehensive 沒有的日期。不加的話，新發生的通過只要
        # 晚於 comprehensive 現有的最後一天就永遠進不去 —— 自動化就失效了。
        cmd += ["--apply", "--add-missing-rows"]
    result = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.strip() and not line.startswith("  20"):
            print(f"      {line}")
    if result.returncode != 0:
        print(f"      ⚠️ 失敗: {result.stderr[:200]}")
    return result.returncode


def step_verify(apply_changes=True):
    """步驟 4：一致性檢查。

    讀的是磁碟上的現況。dry-run 沒有寫檔，所以這裡看到的是「跑之前」的
    狀態，不是前面幾步算出來的預期結果 —— 標題明講，免得誤以為沒作用。
    """
    label = "一致性檢查" if apply_changes else "一致性檢查（磁碟現況，非本次預期結果）"
    print(f"\n[4/4] {label}")
    japan = pd.read_csv(JAPAN_CSV, encoding="utf-8-sig")
    comp = pd.read_csv(COMPREHENSIVE_CSV, encoding="utf-8-sig")
    naval = pd.read_csv(NAVAL_CSV, encoding="utf-8-sig")

    japan["_d"] = _norm_dates(japan["date"])
    comp["_d"] = _norm_dates(comp["date"])
    naval["_d"] = _norm_dates(naval["Date"])

    straits = ["與那國", "宮古", "大禹", "對馬"]
    have = [c for c in straits if c in comp.columns]
    j_marks = int(sum((japan[c] == 1).sum() for c in straits if c in japan.columns))
    c_marks = int(sum((comp[c] == 1).sum() for c in have)) if have else 0
    print(f"      海峽標記  Japan {j_marks} / comprehensive {c_marks}")

    foreign = naval[~naval.apply(
        lambda r: enricher.is_pla_activity(r.get("Ships"), r.get("Country")), axis=1)]
    fb = comp["Foreign_battleship"].notna().sum() if "Foreign_battleship" in comp else 0
    print(f"      外艦通過  naval_transits {len(foreign)} / comprehensive {fb}")

    missing = set(foreign["_d"].dropna()) - set(
        comp[comp.get("Foreign_battleship").notna()]["_d"].dropna()
        if "Foreign_battleship" in comp else [])
    beyond = {d for d in missing if d > comp["_d"].max()}
    print(f"      尚未寫入 comprehensive 的外艦日期 {len(missing)} 個"
          f"（其中 {len(beyond)} 個超出 comprehensive 日期範圍）")

    if "Country" in naval.columns:
        blank = int(foreign["Country"].isna().sum())
        print(f"      naval_transits Country 空值 {blank} 筆")
    if "Ship_Type" in naval.columns:
        typed = int(foreign["Ship_Type"].notna().sum())
        print(f"      已有艦型的外艦紀錄 {typed}/{len(foreign)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="實際寫入（預設只報告）")
    ap.add_argument("--fill-japan-dates", action="store_true",
                    help="為日本防衛省有資料但 comprehensive 缺少的日期補列"
                         "（會讓 comprehensive 從約 1000 列長到約 2700 列）")
    args = ap.parse_args()

    for path in (JAPAN_CSV, COMPREHENSIVE_CSV, NAVAL_CSV):
        if not os.path.exists(path):
            raise SystemExit(f"找不到 {path}")

    print("=" * 62)
    print("資料同步管線" + ("（實際寫入）" if args.apply else "（dry-run）"))
    print("=" * 62)

    # 順序有意義：step 3 會往 comprehensive 新增列，必須排在 step 2 之前，
    # 否則新列要等下一輪才會被日本防衛省的資料填滿（實測需跑三次才收斂）。
    step_enrich_naval(args.apply)
    step_naval_to_comprehensive(args.apply)
    step_japan_to_comprehensive(args.apply, args.fill_japan_dates)
    step_verify(args.apply)

    print("\n" + "=" * 62)
    if args.apply:
        print("完成。")
    else:
        print("dry-run 結束，未寫入任何檔案。確認後加 --apply。")


if __name__ == "__main__":
    main()
