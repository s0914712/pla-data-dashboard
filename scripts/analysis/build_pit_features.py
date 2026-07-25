#!/usr/bin/env python3
"""從 git 歷史重建 point-in-time 特徵表。

天氣預報檔每天被覆寫，磁碟上只留得下最新一次快照 —— 但每次覆寫都有
commit，所以「每一天當下看到的預報」其實完整躺在 git 歷史裡
（airport_weather_forecast.csv 有 373 個快照，回溯到 2026-01-20）。

這件事對回測是關鍵。直接拿今天的天氣觀測值回填歷史，等於讓模型在
預測 t+1 時看到 t+1 的實際天氣 —— 那是洩漏，回測會好看但上線就崩。
用 commit 當時的快照就沒有這個問題：as_of 日期那天，那份預報確實
已經存在。

天氣預報是這個專案裡少數真正前瞻的訊號（公告在事件之前發布），
所以值得花力氣把歷史挖出來。

用法:
    python3 scripts/analysis/build_pit_features.py            # 建天氣特徵
    python3 scripts/analysis/build_pit_features.py --out X.csv
"""

import argparse
import io
import os
import subprocess
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

WEATHER_PATH = "data/airport_weather_forecast.csv"
DEFAULT_OUT = "data/features/weather_pit.csv"

# 只留台海周邊機場。爬蟲抓的機場清單會變動，用 ICAO 白名單比較穩。
# ZSFZ 福州、ZSAM 廈門 正對台灣海峽；ZGGG 廣州、ZSPD 上海 分別是南北兩端。
STRAIT_ICAO = ["ZSFZ", "ZSAM", "ZGGG", "ZSPD", "ZSQZ", "ZGOW"]


def git_snapshots(path, ref="origin/main"):
    """回傳 [(commit_date, blob_text)]，舊到新。"""
    out = subprocess.run(
        ["git", "log", "--format=%H %ad", "--date=short", ref, "--", path],
        capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        return []
    rows = []
    for line in reversed(out.splitlines()):
        sha, date = line.split()
        blob = subprocess.run(["git", "show", f"{sha}:{path}"],
                              capture_output=True, text=True)
        if blob.returncode == 0 and blob.stdout.strip():
            rows.append((pd.Timestamp(date), blob.stdout))
    return rows


def build_weather_pit(ref="origin/main"):
    """每個 as_of 日 × 每個 horizon 一列，值為當天看到的預報。"""
    snapshots = git_snapshots(WEATHER_PATH, ref)
    if not snapshots:
        raise SystemExit(f"取不到 {WEATHER_PATH} 的 git 歷史（是否為 shallow clone？"
                         f"先跑 git fetch --deepen=2000）")
    print(f"讀到 {len(snapshots)} 個快照: "
          f"{snapshots[0][0].date()} ~ {snapshots[-1][0].date()}")

    frames = []
    for as_of, text in snapshots:
        try:
            df = pd.read_csv(io.StringIO(text), encoding="utf-8")
        except Exception:
            continue
        if "date" not in df.columns or "days_ahead" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[df["date"].notna()]
        if "airport_icao" in df.columns:
            sel = df[df["airport_icao"].isin(STRAIT_ICAO)]
            # 白名單全不中時退回全部，避免機場清單改版後整批變空
            df = sel if len(sel) else df
        df["as_of"] = as_of
        frames.append(df)

    if not frames:
        raise SystemExit("所有快照都解析失敗")
    allsnap = pd.concat(frames, ignore_index=True)

    # 同一個 (as_of, 目標日) 可能有多個機場 —— 取跨機場的聚合。
    # 用 min(flight_ok_ratio) 而非 mean：只要有一個關鍵機場飛不了，
    # 整體出動就會受限，取最差值比取平均更貼近實際約束。
    num = {
        "wind_max": "max", "gust_max": "max", "precip_total": "max",
        "cloud_avg": "mean", "temp_min": "min", "temp_max": "max",
        "flight_ok_ratio": "min",
    }
    agg = {k: v for k, v in num.items() if k in allsnap.columns}
    grouped = (allsnap.groupby(["as_of", "date"], as_index=False)
               .agg(agg)
               .rename(columns={"date": "target_date"}))
    grouped["horizon"] = (grouped["target_date"] - grouped["as_of"]).dt.days
    grouped = grouped[(grouped["horizon"] >= 0) & (grouped["horizon"] <= 7)]

    # 同一個目標日會被多個 as_of 預報到；每個 as_of 只保留一份，
    # 由呼叫端依 horizon 取用。
    grouped = grouped.sort_values(["as_of", "horizon"]).reset_index(drop=True)
    return grouped


def to_wide(pit, horizons=(1, 2, 3)):
    """轉成「以 as_of 為索引」的寬表，直接可當特徵 join 進模型。"""
    out = None
    for h in horizons:
        sub = pit[pit["horizon"] == h].set_index("as_of")
        sub = sub.drop(columns=["target_date", "horizon"])
        sub.columns = [f"wx_h{h}_{c}" for c in sub.columns]
        out = sub if out is None else out.join(sub, how="outer")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--ref", default="origin/main")
    args = ap.parse_args()

    pit = build_weather_pit(args.ref)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    pit.to_csv(args.out, index=False, encoding="utf-8")

    print(f"\n寫出 {args.out}  ({len(pit)} 列)")
    print(f"as_of 範圍: {pit['as_of'].min().date()} ~ {pit['as_of'].max().date()}"
          f"  ({pit['as_of'].nunique()} 個不同日期)")
    print("\n每個 horizon 的可用列數:")
    print(pit.groupby("horizon").size().to_string())

    wide = to_wide(pit)
    print(f"\n寬表: {wide.shape[0]} 列 × {wide.shape[1]} 欄")
    print("欄位:", list(wide.columns))


if __name__ == "__main__":
    main()
