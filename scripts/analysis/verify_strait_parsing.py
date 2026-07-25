#!/usr/bin/env python3
"""對 PDF 文字快取離線驗證海峽判讀邏輯。

存在的理由：判讀規則改一次就要重抓 152 份 PDF 才知道對不對，而開發環境
的 egress policy 擋掉 www.mod.go.jp，等於每個判斷都要靠 CI 跑一輪再把
結果貼回來。前面已經因此連錯三次方向（幾何規則、句子級規則過嚴、
`なお` 連坐），每次都花掉一整輪往返。

有了 `data/pdf_texts/japan_mod_texts.json`，改規則可以直接對快取重跑，
當場看到每一份 PDF 的判讀結果、抑制原因、日期歸屬與回溯補填。

用法:
    python3 scripts/analysis/verify_strait_parsing.py
    python3 scripts/analysis/verify_strait_parsing.py --pdf p20260721_01.pdf
    python3 scripts/analysis/verify_strait_parsing.py --date 2026-07-21
    python3 scripts/analysis/verify_strait_parsing.py --diff   # 與 CSV 現況比對
    python3 scripts/analysis/verify_strait_parsing.py --show p20260707_01.pdf  # 印原文
"""

import argparse
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import scraper_japan_mod as sjm  # noqa: E402

CSV_FILE = "data/JapanandBattleship.csv"
STRAITS = ["與那國", "宮古", "大禹", "對馬"]


def parse_filename_date(filename):
    m = re.match(r"p(\d{4})(\d{2})(\d{2})_\d+\.pdf", filename)
    if not m:
        return None
    return pd.Timestamp("-".join(m.groups()))


def analyse(cache, pdf_filter=None, date_filter=None):
    """對每份 PDF 跑一次完整判讀，回傳結果清單。"""
    rows = []
    for filename in sorted(cache):
        if pdf_filter and filename != pdf_filter:
            continue
        report_date = parse_filename_date(filename)
        if report_date is None:
            continue
        if date_filter and report_date != pd.Timestamp(date_filter):
            continue

        text = cache[filename].get("text") or ""
        if not text:
            continue

        is_target = sjm.is_target_navy_pdf(text)
        straits = sjm._detect_straits(text) if is_target else {f: 0 for f in STRAITS}
        rows.append({
            "filename": filename,
            "report_date": report_date,
            "is_target": is_target,
            "country": sjm._detect_country(text),
            "straits": [f for f in STRAITS if straits.get(f) == 1],
            "event_dates": sjm.extract_event_dates(text, report_date) if is_target else {},
            "suppressed": sjm.collect_suppressed_mentions(text) if is_target else [],
            "prior": sjm.collect_prior_transits(text, report_date) if is_target else [],
            "conflict": sjm.detect_strait_conflict(straits),
        })
    return rows


def print_rows(rows, verbose=False):
    for r in rows:
        if not r["is_target"] and not verbose:
            continue
        head = f"{r['filename']}  ({r['report_date'].date()})"
        if not r["is_target"]:
            print(f"{head}  — 非艦艇動向")
            continue
        marks = "+".join(r["straits"]) or "無"
        print(f"{head}  海峽={marks}  國家={r['country']}")
        for field, event in sorted(r["event_dates"].items()):
            offset = (event - r["report_date"]).days
            flag = "  <- 與報告日不同" if offset else ""
            print(f"    事件日 {field}: {event.date()} ({offset:+d} 天){flag}")
        for s in r["suppressed"]:
            print(f"    抑制 {s['strait']}: {s['reason']}")
            print(f"        {s['sentence'][:90]}")
        for event, field, _ in r["prior"]:
            print(f"    回溯 {field} @ {event.date()}")
        if r["conflict"]:
            print(f"    ⚠️ 地理衝突: {'、'.join(r['conflict'])}")


def aggregate_by_date(rows):
    """依事件日期彙總，模擬實際會寫進 CSV 的內容。"""
    per_date = {}
    for r in rows:
        if not r["is_target"]:
            continue
        report_key = r["report_date"].strftime("%Y/%m/%d")
        per_date.setdefault(report_key, set())
        for field in r["straits"]:
            event = r["event_dates"].get(field, r["report_date"])
            per_date.setdefault(event.strftime("%Y/%m/%d"), set()).add(field)
        for event, field, _ in r["prior"]:
            per_date.setdefault(event.strftime("%Y/%m/%d"), set()).add(field)
    return per_date


def diff_against_csv(per_date):
    if not os.path.exists(CSV_FILE):
        print(f"找不到 {CSV_FILE}")
        return
    df = pd.read_csv(CSV_FILE, encoding="utf-8-sig")
    current = {}
    for _, row in df.iterrows():
        current[str(row["date"])] = {f for f in STRAITS if row.get(f) == 1}

    added, removed, same = [], [], 0
    for date_key, expected in sorted(per_date.items()):
        actual = current.get(date_key, set())
        for f in expected - actual:
            added.append((date_key, f))
        for f in actual - expected:
            removed.append((date_key, f))
        if expected == actual:
            same += 1

    print(f"\n{'=' * 60}")
    print(f"與 CSV 現況比對（{len(per_date)} 個日期，{same} 個一致）")
    print(f"{'=' * 60}")
    print(f"\n新版會新增 {len(added)} 個標記:")
    for d, f in added:
        print(f"    + {d} {f}")
    print(f"\n新版會移除 {len(removed)} 個標記:")
    for d, f in removed:
        print(f"    - {d} {f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="只看單一 PDF")
    ap.add_argument("--date", help="只看單一報告日 (YYYY-MM-DD)")
    ap.add_argument("--diff", action="store_true", help="與 CSV 現況比對")
    ap.add_argument("--show", help="印出該 PDF 的原文後結束")
    ap.add_argument("--verbose", action="store_true", help="連非艦艇動向的也列出")
    args = ap.parse_args()

    cache = sjm.load_text_cache()
    if not cache:
        raise SystemExit(
            f"文字快取是空的：{sjm.PDF_TEXT_CACHE}\n"
            "需要先在 GitHub Actions 跑一次回填（rebuild_straits），"
            "快取會隨該次執行一併提交。")

    if args.show:
        entry = cache.get(args.show)
        if not entry:
            raise SystemExit(f"快取中沒有 {args.show}")
        print(entry.get("text", ""))
        return

    print(f"文字快取: {len(cache)} 份 PDF")
    rows = analyse(cache, args.pdf, args.date)
    print(f"符合條件: {len(rows)} 份\n")
    print_rows(rows, args.verbose)

    targets = [r for r in rows if r["is_target"]]
    conflicts = [r for r in targets if r["conflict"]]
    print(f"\n{'=' * 60}")
    print(f"艦艇動向 {len(targets)} 份 / 地理衝突 {len(conflicts)} 份 / "
          f"回溯補述 {sum(len(r['prior']) for r in targets)} 筆")

    if args.diff:
        diff_against_csv(aggregate_by_date(rows))


if __name__ == "__main__":
    main()
