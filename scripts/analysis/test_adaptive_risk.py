#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SurgeForecaster 3.1 自適應風險層的回歸測試。

這一層決定使用者看到什麼顏色、以及要不要推播警報，但它沒有任何外部依賴 ——
純函數、輸入是機率與一組參考數字。也就是說整層都能測，沒有藉口不測。

重點在三件事，因為這三件錯了不會炸，只會安靜地給出錯誤的警報：

  1. 降級路徑（參考不足／未校準／h>=2）必須退回與 3.0 完全相同的行為
  2. 未校準的機率不可以與校準後的機率混在同一個參考分布
  3. ALERT_BUDGET 與 PERCENTILE_LADDER 的 MEDIUM-HIGH 切點必須是同一個數字

用法:
    python3 scripts/analysis/test_adaptive_risk.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from pla_surge_model import (  # noqa: E402
    ADAPTIVE_RISK_ENABLED,
    ALERT_BUDGET,
    MIN_REFERENCE_N,
    PERCENTILE_LADDER,
    RISK_LADDER,
    adaptive_risk,
    combine_risk_levels,
    percentile_risk_level,
    percentile_threshold,
    risk_level,
    risk_percentile,
)

FAILURES = []

# 一組夠大的參考分布：0.000, 0.002, ... 0.998（500 列，遠超 MIN_REFERENCE_N）。
# 均勻分布讓百分位可以手算 —— p 的百分位就約等於 p。
UNIFORM_REF = np.linspace(0.0, 0.998, 500)
BASE_RATE = 0.169          # 線上實測值，讓絕對階梯的切點與正式環境一致


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}\n     got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def close(name, got, want, tol=1e-9):
    ok = got is not None and abs(got - want) <= tol
    print(f"  {'✅' if ok else '❌'} {name}\n     got={got!r} want≈{want!r}")
    if not ok:
        FAILURES.append(name)


def main():
    print("SurgeForecaster 3.1 自適應風險層測試\n" + "=" * 62)

    print("\n[1] risk_percentile —— mid-rank 與 tie 處理")
    # 10 個相異值，0.55 大於其中 5 個 → 0.5
    ref10 = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    close("嚴格大於一半", risk_percentile(0.55, ref10, min_n=10), 0.5)
    # 命中既有值時取 mid-rank：小於的 4 個 + 一半的 1 個並列 = 0.45
    close("命中既有值取 mid-rank", risk_percentile(0.5, ref10, min_n=10), 0.45)
    # 大量並列：機率會在少數值上叢集，純 < 比較會把整叢壓到叢集底部
    tied = np.array([0.1] * 5 + [0.2] * 5)
    close("整叢並列取叢中點", risk_percentile(0.2, tied, min_n=10), 0.75)
    check("低於全部 → 0", risk_percentile(0.0001, ref10, min_n=10), 0.0)
    check("高於全部 → 1", risk_percentile(2.0, ref10, min_n=10), 1.0)
    check("樣本不足回 None", risk_percentile(0.5, ref10, min_n=11), None)
    check("空參考回 None", risk_percentile(0.5, np.array([]), min_n=1), None)
    # NaN 不該被算進分母，否則參考列數虛胖、百分位偏低
    close("NaN 被排除", risk_percentile(0.55, np.array([0.1, 0.9, np.nan]), min_n=2), 0.5)

    print("\n[2] PERCENTILE_LADDER —— 順序與邊界")
    check("階梯由高到低", [n for n, _ in PERCENTILE_LADDER],
          ["HIGH", "MEDIUM-HIGH", "MEDIUM"])
    check("恰好 0.95 → HIGH", percentile_risk_level(0.95), "HIGH")
    check("0.9499 → MEDIUM-HIGH", percentile_risk_level(0.9499), "MEDIUM-HIGH")
    check("恰好 0.80 → MEDIUM-HIGH", percentile_risk_level(0.80), "MEDIUM-HIGH")
    check("0.7999 → MEDIUM", percentile_risk_level(0.7999), "MEDIUM")
    check("恰好 0.60 → MEDIUM", percentile_risk_level(0.60), "MEDIUM")
    check("0.5999 → LOW", percentile_risk_level(0.5999), "LOW")
    check("None → UNKNOWN", percentile_risk_level(None), "UNKNOWN")

    print("\n[3] ALERT_BUDGET 與階梯的耦合（防漂移）")
    # 這一條就是 3.1 的論點本身：回測用 budget=0.20 判定模型能力，線上就該用
    # 同一條線發警報。若有人把 budget 調成 0.10 而階梯還寫死 0.80，兩者又會分家。
    mh = dict(PERCENTILE_LADDER)["MEDIUM-HIGH"]
    close("MEDIUM-HIGH 切點 == 1 - ALERT_BUDGET", mh, 1 - ALERT_BUDGET)

    print("\n[4] combine_risk_levels —— 取較高者，UNKNOWN 是「沒意見」")
    check("取較高", combine_risk_levels("LOW", "MEDIUM-HIGH"), "MEDIUM-HIGH")
    check("取較高（反向）", combine_risk_levels("HIGH", "MEDIUM"), "HIGH")
    check("相同", combine_risk_levels("MEDIUM", "MEDIUM"), "MEDIUM")
    check("UNKNOWN 不壓過實際等級", combine_risk_levels("UNKNOWN", "MEDIUM"), "MEDIUM")
    check("UNKNOWN 不壓過實際等級（反向）",
          combine_risk_levels("HIGH", "UNKNOWN"), "HIGH")
    check("兩側都 UNKNOWN", combine_risk_levels("UNKNOWN", "UNKNOWN"), "UNKNOWN")
    # UNKNOWN 不是「比 LOW 低」，但兩者相遇時仍該回 LOW（那一側有明確意見）
    check("UNKNOWN vs LOW → LOW", combine_risk_levels("UNKNOWN", "LOW"), "LOW")

    print("\n[5] percentile_threshold —— 可稽核的絕對切點")
    close("均勻分布的 p80", percentile_threshold(UNIFORM_REF), 0.7984, tol=1e-3)
    check("樣本不足回 None", percentile_threshold(np.array([0.1, 0.2])), None)

    print("\n[6] adaptive_risk —— h>=2 一律 UNKNOWN 且不警示")
    r = adaptive_risk(0.9, signal_valid=False, base_rate=BASE_RATE,
                      reference=UNIFORM_REF)
    check("signal_valid=False → UNKNOWN", r["level"], "UNKNOWN")
    check("signal_valid=False → 不警示", r["alert"], False)
    check("signal_valid=False → 無百分位", r["percentile"], None)

    print("\n[7] 降級路徑必須等同 3.0")
    # 參考缺檔
    p = 0.18
    r = adaptive_risk(p, True, BASE_RATE, reference=None)
    check("無參考 → 等同 risk_level()", r["level"], risk_level(p, True, BASE_RATE))
    check("無參考 → 不警示", r["alert"], False)
    check("無參考 → relative UNKNOWN", r["relative"], "UNKNOWN")
    check("無參考 → reference_n 為 0", r["reference_n"], 0)
    # 參考列數不足
    short = np.linspace(0, 1, MIN_REFERENCE_N - 1)
    r = adaptive_risk(p, True, BASE_RATE, reference=short)
    check("參考不足 → 等同 risk_level()", r["level"], risk_level(p, True, BASE_RATE))
    check("參考不足 → 不警示", r["alert"], False)
    # 未校準：這一條是實質的正確性要求，不是防禦性程式碼。
    # Platt 未啟用時機率是 balanced 分類器的原始輸出（實測膨脹約 1.9 倍），
    # 拿去對校準後的參考分布排名，排出來的是尺度差異而不是風險差異。
    r = adaptive_risk(0.9, True, BASE_RATE, reference=UNIFORM_REF, calibrated=False)
    check("未校準 → 無百分位", r["percentile"], None)
    check("未校準 → 不警示", r["alert"], False)
    check("未校準 → 等同 risk_level()", r["level"], risk_level(0.9, True, BASE_RATE))

    print("\n[8] adaptive_risk —— 3.1 要修的那個情境（強制 enabled）")
    # 機率 18.1%，絕對階梯的 MEDIUM 需要 max(0.20, 1.3*0.169)=0.22，所以 3.0 是 LOW。
    # 若它同時是參考分布的第 96 百分位，3.1 開啟後應該讓它浮上來。
    ref = np.concatenate([np.linspace(0.0, 0.17, 480), np.linspace(0.171, 0.30, 20)])
    r = adaptive_risk(0.181, True, BASE_RATE, reference=ref, enabled=True)
    check("絕對階梯仍是 LOW（未下修）", r["level_absolute"], "LOW")
    check("絕對階梯與 3.0 一致", r["level_absolute"], risk_level(0.181, True, BASE_RATE))
    check("相對階梯浮上 HIGH", r["relative"], "HIGH")
    check("level_adaptive 取較高者", r["level_adaptive"], "HIGH")
    check("enabled=True 時 level 用 adaptive", r["level"], "HIGH")
    check("觸發警示", r["alert"], True)
    ok = r["percentile"] is not None and r["percentile"] >= 0.95
    print(f"  {'✅' if ok else '❌'} 百分位 >= 95%\n     got={r['percentile']!r}")
    if not ok:
        FAILURES.append("百分位 >= 95%")

    print("\n[8b] shadow 模式 —— 使用者看到的東西必須完全不變")
    # 這是本次落地的核心保證：驗收沒過，所以預設 shadow。同一個輸入，
    # level 必須退回絕對階梯，但 level_adaptive / alert / percentile 照樣記錄。
    s = adaptive_risk(0.181, True, BASE_RATE, reference=ref, enabled=False)
    check("shadow：level == 絕對階梯", s["level"], risk_level(0.181, True, BASE_RATE))
    check("shadow：level 不等於 adaptive", s["level"] != s["level_adaptive"], True)
    check("shadow：level_adaptive 仍照算", s["level_adaptive"], "HIGH")
    check("shadow：alert 仍照記錄", s["alert"], True)
    check("shadow：percentile 仍照算", s["percentile"] == r["percentile"], True)
    check("shadow：enabled 旗標誠實回報", s["enabled"], False)
    # 預設值就是 shadow —— 若有人把常數改成 True 而沒更新文件，這條會紅
    d = adaptive_risk(0.181, True, BASE_RATE, reference=ref)
    check("預設走 ADAPTIVE_RISK_ENABLED", d["enabled"], ADAPTIVE_RISK_ENABLED)
    check("預設目前為 shadow", ADAPTIVE_RISK_ENABLED, False)

    print("\n[9] 絕對階梯不會被百分位壓低")
    # 機率 60%（遠超 HIGH 的 0.507），但近期普遍更高所以百分位不突出。
    # 這種日子仍然必須是 HIGH —— 這正是保留絕對階梯的理由。
    busy_ref = np.linspace(0.5, 0.99, 500)
    r = adaptive_risk(0.60, True, BASE_RATE, reference=busy_ref, enabled=True)
    check("絕對 HIGH 不被相對壓低", r["level"], "HIGH")
    check("相對階梯確實不高", r["relative"], "LOW")
    check("相對不高 → 不警示", r["alert"], False)

    print("\n[10] alert 與 relative 必須同源")
    # alert 定義成「百分位進入前 ALERT_BUDGET」，PERCENTILE_LADDER 的
    # MEDIUM-HIGH 切點也是 1 - ALERT_BUDGET，所以兩者恆等。任一邊被改動而
    # 另一邊沒跟上，這條就會紅。
    mismatches = []
    for p in np.linspace(0.0, 0.999, 200):
        r = adaptive_risk(float(p), True, BASE_RATE, reference=UNIFORM_REF)
        if r["alert"] != (r["relative"] in ("MEDIUM-HIGH", "HIGH")):
            mismatches.append(round(float(p), 4))
    check("alert == (relative >= MEDIUM-HIGH)", mismatches, [])

    print("\n[11] 警示率確實等於 budget")
    # 用參考分布自己當查詢集：定義上應該恰好有 ALERT_BUDGET 的比例被警示。
    # 這是「top 20%」這個說法的執行期驗證。
    fired = sum(adaptive_risk(float(p), True, BASE_RATE, reference=UNIFORM_REF)["alert"]
                for p in UNIFORM_REF)
    rate = fired / len(UNIFORM_REF)
    ok = abs(rate - ALERT_BUDGET) <= 0.01
    print(f"  {'✅' if ok else '❌'} 自我警示率 ≈ ALERT_BUDGET\n"
          f"     got={rate:.3f} want≈{ALERT_BUDGET}")
    if not ok:
        FAILURES.append("自我警示率 ≈ ALERT_BUDGET")

    print("\n[12] RISK_LADDER 未被 3.1 改動")
    check("絕對階梯原封不動", RISK_LADDER,
          (("HIGH", 0.40, 3.0), ("MEDIUM-HIGH", 0.30, 2.0), ("MEDIUM", 0.20, 1.3)))

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 項失敗：")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("✅ 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
