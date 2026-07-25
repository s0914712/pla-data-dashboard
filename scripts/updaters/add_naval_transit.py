#!/usr/bin/env python3
"""新增／更新外國軍艦通過台灣海峽的紀錄。

原本的 naval_transits.csv 只有 Date / Ships / Country，資訊太薄 ——
媒體報導裡最有價值的「艦艇類型」與「任務性質」（是例行航行、參加演習，
還是水文測繪）全都塞在 Ships 那一欄的自由文字裡，無法查詢也無法呈現。

本腳本補上結構化欄位，並保留原有欄位不動以維持相容。

用法:
    python3 scripts/updaters/add_naval_transit.py --list
    python3 scripts/updaters/add_naval_transit.py --seed      # 匯入已知紀錄
    python3 scripts/updaters/add_naval_transit.py \\
        --date 2026-04-17 --country JP --ships "雷號（JS Ikazuchi, DD-107）" \\
        --type "村雨級護衛艦" --note "海上自衛隊時隔10個月再度派艦穿越台海" \\
        --source "媒體報導"
"""

import argparse
import os

import pandas as pd

CSV_PATH = "data/naval_transits.csv"

# 後補的結構化欄位。原有欄位（Date/Year/Ships/Country/Sorties_*）維持不動。
EXTRA_COLUMNS = [
    "Ship_Type",       # 艦艇類型：飛彈驅逐艦 / 海洋測量船 …
    "Hull_Number",     # 舷號：DDG-113、T-AGS 65 …
    "Mission_Note",    # 任務性質與備註
    "Date_Precision",  # exact / range / approximate —— 媒體常只寫「7月中旬」
    "Date_Note",       # 原始日期描述，例如「1月16日－17日」「7月中旬」
    "Source",          # 資料來源
]

# 已知紀錄。日期不精確者一律標明，不要假裝知道確切日子。
SEED_RECORDS = [
    {
        "Date": "2026/1/16",
        "Country": "US",
        "Ships": "芬恩號（USS John Finn, DDG-113）、瑪麗西爾斯號（USNS Mary Sears, T-AGS 65）",
        "Ship_Type": "飛彈驅逐艦、海洋測量船",
        "Hull_Number": "DDG-113, T-AGS 65",
        "Mission_Note": "2026 年美軍首度雙艦穿越台海。芬恩號具防空與反潛能力；"
                        "瑪麗西爾斯號執行水下數據與環境監測。",
        "Date_Precision": "range",
        "Date_Note": "1月16日－17日",
        "Source": "媒體報導",
    },
    {
        "Date": "2026/4/17",
        "Country": "JP",
        "Ships": "雷號（JS Ikazuchi, DD-107）",
        "Ship_Type": "村雨級護衛艦（驅逐艦）",
        "Hull_Number": "DD-107",
        "Mission_Note": "海上自衛隊時隔 10 個月再度派艦穿越台海。"
                        "通過後南下前往南海參加美菲聯合軍演。",
        "Date_Precision": "exact",
        "Date_Note": "4月17日",
        "Source": "媒體報導",
    },
    {
        "Date": "2026/7/15",
        "Country": "US",
        "Ships": "亨森號（USNS Henson, T-AGS 63）",
        "Ship_Type": "探路者級海洋調查船",
        "Hull_Number": "T-AGS 63",
        "Mission_Note": "由南向北駛入台灣海峽，搭載多波束聲納等精密儀器蒐集水文數據，"
                        "引發共軍派遣直升機跟監應對。",
        # 來源只寫「7月中旬」，這裡取 7/15 當代表值並標為 approximate。
        # 查到確切日期後請更新 Date 並把 Date_Precision 改成 exact。
        "Date_Precision": "approximate",
        "Date_Note": "7月中旬（確切日期待確認）",
        "Source": "媒體報導",
    },
]


def load():
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    for col in EXTRA_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def normalise(date_str):
    ts = pd.to_datetime(date_str, errors="coerce", format="mixed")
    if pd.isna(ts):
        raise SystemExit(f"無法解析日期: {date_str}")
    return ts


def upsert(df, record):
    """同一天已有紀錄就補欄位，沒有才新增 —— 避免重複列。"""
    target = normalise(record["Date"])
    dates = pd.to_datetime(df["Date"], errors="coerce", format="mixed")
    match = dates == target

    if match.any():
        idx = df.index[match][0]
        filled = []
        for key, value in record.items():
            if key == "Date":
                continue
            existing = df.at[idx, key] if key in df.columns else pd.NA
            # 只補空欄位，不覆蓋既有內容（可能是人工整理過的）
            if pd.isna(existing) or str(existing).strip() in ("", "nan"):
                df.at[idx, key] = value
                filled.append(key)
        return "updated", filled

    row = {c: pd.NA for c in df.columns}
    row.update(record)
    row["Date"] = target.strftime("%Y/%-m/%-d")
    row["Year"] = target.year
    df.loc[len(df)] = row
    return "added", list(record)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="匯入內建的已知紀錄")
    ap.add_argument("--list", action="store_true", help="列出非共軍的通過紀錄")
    ap.add_argument("--date")
    ap.add_argument("--country")
    ap.add_argument("--ships")
    ap.add_argument("--type", dest="ship_type")
    ap.add_argument("--hull")
    ap.add_argument("--note")
    ap.add_argument("--precision", default="exact",
                    choices=["exact", "range", "approximate"])
    ap.add_argument("--date-note")
    ap.add_argument("--source", default="媒體報導")
    args = ap.parse_args()

    if not os.path.exists(CSV_PATH):
        raise SystemExit(f"找不到 {CSV_PATH}")
    df = load()

    if args.list:
        dates = pd.to_datetime(df["Date"], errors="coerce", format="mixed")
        view = df.assign(_d=dates).sort_values("_d")
        view = view[view["Country"].astype(str).str.upper() != "CN"]
        for _, r in view.iterrows():
            if pd.isna(r["_d"]):
                continue
            extra = " | ".join(
                str(r[c]) for c in ("Ship_Type", "Mission_Note")
                if c in r and pd.notna(r[c]))
            print(f"{r['_d'].date()}  [{r['Country']}]  {str(r['Ships'])[:48]}")
            if extra:
                print(f"    {extra[:110]}")
        return

    records = []
    if args.seed:
        records = SEED_RECORDS
    elif args.date:
        records = [{
            "Date": args.date, "Country": args.country, "Ships": args.ships,
            "Ship_Type": args.ship_type, "Hull_Number": args.hull,
            "Mission_Note": args.note, "Date_Precision": args.precision,
            "Date_Note": args.date_note, "Source": args.source,
        }]
    else:
        raise SystemExit("需要 --seed 或 --date（--list 可查詢）")

    for record in records:
        action, fields = upsert(df, {k: v for k, v in record.items() if v is not None})
        print(f"  {action:8s} {record['Date']}  {str(record.get('Ships'))[:44]}")
        if fields:
            print(f"           欄位: {', '.join(fields)}")

    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    print(f"\n已寫入 {CSV_PATH}（{len(df)} 列）")


if __name__ == "__main__":
    main()
