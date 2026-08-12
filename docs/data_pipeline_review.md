# 資料流檢視與改善方案：爬蟲 → 資料表 → 預測

本文檢視 `pla-data-dashboard` 從資料蒐集到預測輸出的完整流程，列出目前的實際
行為、已在本次一併修掉的問題，以及建議後續處理的項目。

檢視日期：2026-07-26。所有數字都是對 repo 內當下資料實測得出，不是估計。

---

## 1. 現況：資料流長什麼樣

沒有資料庫。全 repo 沒有 SQLite / Postgres / Supabase / ORM / migration，
`requirements.txt` 也沒有任何 DB driver。**「資料表」就是 `data/` 底下的 CSV 與
JSON，由 GitHub Actions 每天 commit 進 git**，靜態 HTML 儀表板直接讀這些檔案。
git history 本身就是交易紀錄——`scripts/analysis/build_pit_features.py` 正是靠
`git` 快照重建 point-in-time 特徵。

| 來源 | 入口 | 產出 | 排程 (UTC) |
|---|---|---|---|
| 國防部 共機架次 | `scraper.py` (Selenium) | `data/JapanandBattleship.csv` | `update_data.yml` `0 0` |
| 日本防衛省 統幕 | `scraper_japan_mod.py` (PyPDF2) | 同上 ＋ `data/pdf_texts/`, `data/logs/strait_conflicts.json` | `scrape_japan_mod.yml` `0 6` |
| MSA 12 個海事局 航警 | `scripts/scrape_nav_warnings.py` | `data/navigation_warnings/*` | `scrape_nav_warnings.yml` `15 2` |
| 中央社 / 新華社 / 微博 | `scripts/main.py` + `scripts/scrapers/` | `data/news_classified.json` | `daily_update.yml` `15 0` 等 |
| Windy 機場天氣 | `scripts/scrapers/weather_scraper.py` | `data/airport_weather_forecast.csv` | `weather.yml` `30 0,12` |
| 跨檔同步 | `scripts/sync_pipeline.py` | `merged_comprehensive_data_M.csv`, `naval_transits.csv` | `sync_pipeline.yml` `0 8` |
| 預測 | `predict_surge_daily.py` (SurgeForecaster) | `data/predictions/latest_prediction.csv` | `daily_prediction.yml` `0 2` |
| LINE 推播 | `scripts/send_message.py` | 兩張 PNG ＋ 一則文字 | `LINEcron.yml` `0 23` |

這個「CSV as DB」架構本身是合理取捨：零運維成本、資料可直接被 GitHub Pages
讀取、改動全都有 diff 可追。以下問題都不是「該換成資料庫」，而是這個架構下
可以修好的具體缺陷。

---

## 2. 本次已修正的項目

### 2.1 MSA 起訖日從未被結構化（造成 LINE 推播斷字）

`NavigationWarning_scraper.parse_time_period()` 跑六條互相重疊的 regex，卻用
`m.group()` 而非 `m.groups()`，capture group 全丟掉，最後 `return list(set(times))`。
後果：

- 同一則公告產生多個重疊片段，例如
  `7月20日0600时至22日1200时; 2026年7月20日0600时至22日1200时`（45 字元）
- 部分片段沒有月份（`24日0600时至1800时`），而 `set()` 沒有順序，殘缺片段可能排在最前
- `military_exercises.csv` **沒有 start_date / end_date 欄位**，真正的日期解析
  重複實作在 `pla_7day_predictor._parse_navwarn_window`
- 下游 `send_message.py` 再套 `[:40]`（文字）與 `[:16]`（地圖圖例），必定切在迄日中間

**已修**：新增 `scripts/nav_warning_dates.py` 集中解析，回傳結構化 `Period`
（起日、迄日、時刻、是否為「每日」時段），並補上舊版完全沒有的英文格式
`FROM 16 TO 17 MAR` / `... UTC TO ... UTC DAILY`。CSV/JSON 新增 `start_date`、
`end_date` 欄位，`time_periods` 改存格式化後的短字串。`pla_7day_predictor` 端的
重複實作應在後續改為呼叫同一模組。

實測：86 筆歷史航警有時間資訊者 **51 → 68**。

### 2.2 歷史資料不會自癒

`merge_warnings()` 用 `drop_duplicates(subset='url', keep='first')`，既有列永遠
不會被新抓的同一則覆蓋。這對保存歷史是對的（MSA 每個海事局只留最新一頁），
但代表解析規則改善後舊資料永遠停在舊值。

**已修**：新增 `redrive_periods()`，每次執行都對全部列從 `content_preview`
重算起訖日；並提供 `--redrive-only` 讓規則改善後可以離線補資料——
msa.gov.cn 自 2026-03 起對境外 IP 回 403，重爬這條路在 CI 上走不通。

### 2.3 `content_preview` 被截斷在 500 字，連同日期一起丟掉

`extract_core_content()` 找不到航警編號時直接 `return text[:500]`，而 `text` 是整頁
body（含「English 首页 机构职能…」導覽列），公告本體被擠到截斷點之後。

實測：86 筆中有 **24 筆長度剛好 500 且都含導覽列文字**，起訖日全數遺失。

**已修**：加入正文錨點（`发布时间`／`来源`／`文号`／`航行警告`）先剝掉導覽列，
上限由 500 提高到 1500（含座標列表的公告，座標本身就佔三四百字）。
已經存下來的那 18 筆解不出來的仍留空——不猜。

### 2.4 `naval_transits.csv` 每次新聞管線執行就掉 7 個欄位

`NavalTransitUpdater.FIELDNAMES` 只列 9 欄，`_load_existing()` 依它逐 index 讀、
`_save()` 依它覆寫整個檔案。CSV 實際有 16 欄，於是 `Ship_Type`、`Hull_Number`、
`Mission_Note`、`Date_Precision`、`Date_Note`、`Source`、`Country_Confidence`
**每跑一次就被清空**，隔天 `sync_pipeline.step_enrich_naval` 再補回來，補了又掉。

**已修**：改以檔案實際表頭為準，並聯集所有列出現過的欄位。實測 round-trip 後
`Ship_Type` 保留 59/71 筆（原本 0 筆）。

### 2.5 防衛省資料在日期列不存在時被丟棄

`scraper_japan_mod.update_csv()` 遇到 CSV 沒有該日期時印 `找不到日期` 並
`return False`，當天的解析結果直接消失。`JapanandBattleship.csv` 的日期列由
`scraper.py`（國防部架次）建立，只要國防部當天沒發布或 Selenium 爬蟲失敗，
防衛省的艦艇通報就永遠寫不進去。兩個來源不保證同日都有資料，不該互為前提。

**已修**：改為補一列（架次欄留 NA，代表未觀測而非 0）並依日期排序。

### 2.6 艦型辨識：字典未收錄一律變成「未提及」

`_extract_ship_classes()` 只做 50 筆手工字典的子字串比對，未命中回「未提及」。
實測 152 份 PDF 中，`ジャンカイⅢ級`（江凱III/054A）、`ユーシェン級`（玉申/075
兩棲攻擊艦）、`クズネツォフ級`（庫茲涅佐夫級航艦）等 8 種艦級原文明明寫了
卻被記成「未提及」。

**已修**：補上 12 筆字典條目，並加入 `〈片假名〉級〈艦種〉` 的原文回退（標 `※`
表示未翻譯）。回退對每一份文件都執行，不是只在字典全數落空時——一份編隊
通報常同時提到多艘不同艦級，只要一艘在字典裡，其餘就會被整批吞掉。
目前僅剩 2 種艦級需要原文標示。

### 2.7 `military_exercises.json` 不是合法 JSON

`json.dump` 把 pandas 的 NaN 寫成裸的 `NaN`，任何非 Python 的消費端（含瀏覽器
`fetch`）都會解析失敗。**已修**：文字欄位缺值統一寫空字串。

---

## 3. 建議後續處理（本次未動）

依「影響 × 風險」排序。

### 3.1 ~~預測用遞迴多步~~ — 已處理，模型已換

**2026-07-26 更新：預測核心已由 CatBoost 換成 `pla_surge_model.SurgeForecaster`**
（進入點 `predict_surge_daily.py`，`daily_prediction.yml` 已改指向它；
`pla_7day_predictor.py` 原封保留，改回一行即可回滾）。

依據是修好後的 `backtest_predictor.py`（`--days 120 --horizons 1 --refit 28`，
2026-03-28~07-26，121 天、15 個 surge 日）。同一支腳本同時跑樸素基準線與線上模型，
所有數字都是同一個窗口、同一套指標：

| model | MAE | pin90 | bias | cov90 | PR-AUC | ROC-AUC | Brier | recall |
|---|---|---|---|---|---|---|---|---|
| baseline persistence | 8.07 | 4.03 | +0.02 | – | – | – | – | – |
| baseline EMA-14 | 6.56 | 3.37 | −0.21 | – | – | – | – | – |
| baseline median-28 | 6.18 | 4.21 | −2.81 | – | – | – | – | – |
| **surge model h=1** | 6.17 | **3.03** | **+0.13** | **97%** | **0.329** | **0.647** | **0.1062** | **47%** |
| deployed（舊 CatBoost） | **5.87** | 3.97 | −2.59 | 54% | 0.141 | 0.555 | 0.1410 | 20% |

（`Brier0` = 全押 base rate = 0.1086；`recall` 是 20% 告警預算下的召回率。
`bias` 在這支腳本裡是 `預測−實際`，與本文其他處的符號相反。）

新模型在**除了 MAE 以外的每一項都贏**，包含贏過三條樸素基準線的 `pin90`。
舊模型只贏 MAE —— 而 MAE 正是獎勵低估的那個指標（見下）。
`Brier 0.1062 < Brier0 0.1086`，代表校準後的機率終於比「全押 base rate」有價值。

`cov90 = 97%` 略高於名目 90%，區間偏保守；這比舊模型的 54% 好得多，但仍應持續監看。

**MAE 變差是預期的取捨，不是退步。** 架次分布右偏（mean 7.2 / median 4 / q90 22），
MAE 的最佳解是條件中位數，所以最小化 MAE 等於訓練模型系統性低估——
舊模型 160 天裡點預測從未超過 17.4（實際最高 36），18 次 surge 一次都沒抓到。
判定成敗請看 `cov90` / `pin90` / `PR_AUC`。

一併處理掉的：遞迴餵回（改 direct multi-horizon）、SMOTE、recency 複製列、
區間事後加寬、天氣調整（稽核增益 `+0.011 [-0.076,+0.143]`，跨 0）。

門檻由 25 降到 20：thr=25 的 ROC-AUC 只有 0.547（正樣本 11 個），與亂猜無法區分。

機率校準用 **Platt（logistic on log-odds）而非 isotonic**——這是實測選擇。
校準窗僅約 180 天、正樣本 20 上下，isotonic 會產生大片平坦區間把不同預測壓成同值，
實測 PR-AUC 因此由 0.259 掉到 0.168、ROC-AUC 0.636 → 0.565。Platt 嚴格單調，
在 160 天窗上把平均輸出由 22.3%（實際 11.2%，膨脹 1.98×）拉到 17.8%（1.58×），
Brier 0.1230 → 0.1007，排序幾乎無損（PR-AUC 0.259 → 0.238）。

**仍需監看**：
- 校準後仍有 1.58× 的殘餘膨脹，機率偏高的方向沒有完全消除
- 樣本量不足：15~18 個正樣本，ROC-AUC 95% CI 約 [0.49, 0.78]。
  方向明確、精度不足，需要累積更多事件才能確認
- `cov90` 97% 偏保守，區間可能過寬

<details><summary>原始問題描述（已修）</summary>

`pla_7day_predictor.predict_7_days()` 第 1176 行 `current_window.append(pred_final)`
把預測值餵回特徵窗，D+2..D+7 的 lag/rolling 特徵是建立在預測值而非實測值上。
repo 內另一版模型 `pla_surge_model.py` 的 docstring 明確記錄了這件事：
「遞迴會讓預測變異數逐日塌陷」，並改用 direct multi-horizon（每個 horizon 各訓
一個模型）。

同一份 docstring 還記錄：surge 機率只在 h=1 有鑑別力（ROC-AUC 0.764），h≥2 掉到
0.45–0.57 等同亂猜；`scripts/analysis/backtest_predictor.py` 的檔頭則記錄現行
上線模型「MAE 5.86，但 18 次 surge 一次都沒抓到」。

**建議**：把 `pla_surge_model.SurgeForecaster` 接上線，或至少把
`predict_7_days` 改成 direct multi-horizon。本次已先在 LINE 端把 D+2/D+3 的機率
拿掉（只顯示 D+1 並標示基準發生率），避免把兩個亂數與一個有訊號的數字並排展示。

</details>

### 3.2 ~~預測器從 `raw.githubusercontent.com` 讀資料~~ — 新進入點已修

`predict_surge_daily.load_sorties()` 改為優先讀本機 checkout，讀不到才回退遠端。
舊的 `pla_7day_predictor.py:112-116` 仍是原樣（該檔已不在排程路徑上）。

<details><summary>原始問題描述（新進入點已修）</summary>

`pla_7day_predictor.py:112-116` 的 `DATA_SOURCES` 全部指向 main 分支的 raw URL。
`daily_prediction.yml` 明明已經 checkout 了 repo，卻讀網路上的版本。兩個後果：

- **時序競爭**：預測排在 `0 2`，`scrape_japan_mod` 在 `0 6`、`sync_pipeline` 在 `0 8`，
  但 raw URL 有 CDN 快取，讀到的可能是更舊的版本，且無從得知讀到哪一版
- **無法離線重現**：本地跑預測拿到的資料跟 CI 不同，回測結果無法對照

**建議**：預設讀本機路徑，raw URL 只當 fallback。

</details>

### 3.3 push 用 `git pull --rebase -X theirs`，可能丟掉本次抓到的列 — 中影響

每個 workflow 的 push retry 都是：

```bash
git pull --rebase --autostash -X theirs origin main
```

rebase 期間 `-X theirs` 解在 **incoming upstream** 那一側，CSV 起衝突時會捨棄
本次執行剛爬到的列。目前靠 `concurrency: group: csv-battleship-writers`
（`update_data.yml`、`scrape_japan_mod.yml`、`sync_pipeline.yml` 三者皆已加入）
降低碰撞機率，但 retry 路徑本身仍有損失資料的可能。

**建議**：CSV 這種累積型檔案不要用 `-X` 自動解衝突，改成 rebase 失敗時重新讀取
最新檔案、重跑一次 merge 邏輯（各 writer 的 merge 都已是冪等的）。

### 3.4 `naval_transits` 只以日期去重，一天只能記一筆 — 中影響

`NavalTransitUpdater._is_duplicate()` 只比對 `Date`，同一天有兩艘不同國家軍艦
通過台海時，第二筆會被當成重複而丟棄。`add_naval_transit.py` 的 upsert 邏輯
是對的，兩者行為不一致。

**建議**：去重鍵改為 `(Date, Country, Hull_Number)`。

### 3.5 航警地理圍籬只有 6/80 落在台灣海峽 — 中影響

`pla_7day_predictor.NAVWARN_GEOFENCE = {21.0–28.5°N, 117.0–124.0°E}`，但爬蟲抓的是
全部 12 個海事局（含渤海、南海、北部灣）。`audit_feature_sources.py` 的註解已記錄
只有 6/80 落在圍籬內——`navwarn_active` / `navwarn_pub_3d` 這兩個 v2.8 特徵的
有效樣本數低到難以支撐任何統計宣稱。

**建議**：要嘛把爬蟲收斂到福建/廣東/浙江三個面向台海的海事局以提高訊噪比，
要嘛保留全量但把特徵改成「按海區分開的多個欄位」，不要混成一個。

### 3.6 ~~儀表板門檻不一致~~ — 已統一為 20

`prediction.html` 的 `>= 30` 改為讀預測檔的 `surge_threshold` 欄位；
`send_message.py` 的 `HIGH_SORTIE_THRESHOLD` 由 25 改為 20，與
`pla_surge_model.SURGE_THRESHOLD` 一致。舊版寫死 30 還有個隱藏後果：
點預測從未達到 30，所以「方向準確率」實際上退化成「實際值 <30 的天數比例」＝98%，
看起來很漂亮但沒有任何資訊。

<details><summary>原始問題描述（已修）</summary>

`prediction.html:738-739` 用 `>= 30` 計算 direction accuracy，但模型的
`HIGH_THRESHOLD = 25`（`pla_7day_predictor.py:128`），而未上線的
`pla_surge_model.SURGE_THRESHOLD` 又是 20。三個地方三個數字。

**建議**：統一為單一常數，由模型端輸出到 `latest_prediction.csv` 的 metadata，
前端讀取而不是寫死。本次 `send_message.py` 已把門檻寫成具名常數
`HIGH_SORTIE_THRESHOLD = 25` 並在推播文字中明示（「高架次(≥25)機率」），
使用者至少能知道那個百分比在講什麼。

</details>

### 3.7 `data/JapanandBattleship.csv` 的 `remark` / `備考` 語意分裂 — 低影響

`remark` 原本是布林欄（1439 True / 133 False），後來被拿來存繁中敘述，於是
`scraper_japan_mod.py:801-803` 註解說明新的敘述改寫進 `備考`。目前兩欄都有資料、
語意重疊，`send_message.py` 兩邊都要讀。

**建議**：一次性 migration 把敘述統一到 `備考`，`remark` 保留布林語意或直接移除。

### 3.8 雜項

- `scripts/analysis/backtest_predictor.py` **原本根本無法執行**：
  `main()` 裡 `global REFIT_EVERY` 寫在 `add_argument(default=REFIT_EVERY)` 之後，
  是 `SyntaxError`。也就是說它 docstring 裡記錄的那些數字不可能來自這個版本。
  已修，並補上區間覆蓋率、Brier、pinball 與三條樸素基準線
- `scripts/scrapers/weather_report.csv` 是 1 byte 的空檔，誤 commit 進 scrapers 套件
- `scraper_japan_mod.generate_pdf_urls()` 用暴力猜測 URL（每天試 `_01`..`_10`），
  一天 300 次請求且無法得知是否漏抓。改成解析統幕的發表資料列表頁較可靠。

---

### 3.9 風險階梯不可達 — 3.1 自適應層已實作，**影子運行，未上線**

**2026-08-11：`SurgeForecaster 3.1.0-shadow`。驗收未過，預設關閉。**

#### 問題

`probability_review.json` 的 `ladder_unreachable` 診斷每天都在講同一件事：
base rate 0.169 下 `RISK_LADDER` 的 MEDIUM 切點是 `max(0.20, 1.3×0.169) = 0.22`，
而 3.0 上線 16 天校準後機率最高只有 0.181。階梯整段摸不到，連日顯示 🟢 LOW，
兩次 surge（07-31 實際 27、08-05 實際 21）全數漏報，recall 0。

同時 `backtest_predictor.score()` 用 `budget=0.20`（機率排名前 20%）判定模型的
surge 偵測能力 —— **回測的操作點與線上發警報的操作點是兩套標準。**

#### 做法

不動任何機率（Poisson 回歸 / 分類器 / Platt / conformal 一行未改），另外算
「今天的機率排在歷史 walk-forward OOS 分布的第幾位」。`PERCENTILE_LADDER` 的
MEDIUM-HIGH 切點直接寫成 `1 - ALERT_BUDGET`，與 `score()` 的 budget 是同一個常數。

參考分布：`scripts/analysis/build_oos_probabilities.py` →
`data/predictions/oos_probabilities.csv`（1519 列，2022-03-06 → 2026-08-11，92% 已校準）。
`walk_forward()` 本來就在算這個，只是印完就丟。

#### 為什麼增量 append 沒有 leakage

對固定 target date `t`，`train_idx = np.where(target_dates < t - embargo)[0]` 只由 `t`
之前的列決定，`key = len(train_idx) // REFIT_EVERY` 因此也只由過去決定。所以「今天重建」
與「當時建」對舊日期逐位元相同。**實測：增量重跑既有最後 5 列，最大差異 2.8e-17。**

#### 實測結果

尺度一致性（線上每日重訓 vs OOS 每 7 天重訓，同 16 個日期）：
線上平均 0.110／OOS 平均 0.105，平移 **+0.005**，Spearman 0.746 —— 通過。

驗收閘（`--reference-window` 120/180/270/365/545 全掃，結論不隨參數改變）：

| 評估窗 | 警示率 | 召回 | 召回倍數 | 3.0 召回 | ROC-AUC | Brier 技能 | |
|---|---|---|---|---|---|---|---|
| 365 天 | 15.1% | 33.9% | **2.25x** | 30.6% | 0.674 | +4.9% | ✅ |
| 730 天 | 28.7% | 40.1% | 1.40x | 10.5% | 0.608 | +2.3% | ❌ |
| 1095 天 | 19.9% | 27.3% | 1.37x | 6.6% | 0.568 | +0.6% | ❌ |
| 1285 天 | 17.9% | 24.0% | 1.35x | 6.5% | 0.551 | −2.7% | ❌ |

**閘門定義**：核心指標是 `召回倍數 = recall / alert_rate`（無鑑別力恆為 1.00x）。
不用「recall >= 40%」是因為那只在警示率恰為 20% 時等價；自適應門檻的警示率會隨情勢
漂移。另外 `precision = 召回倍數 × base_rate`，所以「precision >= 2x base rate」與
「召回倍數 >= 2x」是同一條件，不是獨立閘門 —— 原本列成兩條是重複計分。

#### 為什麼不上線

**一、4 個窗只有 1 個過。** 擋住的不是決策層 —— 3.1 在每個窗都大幅贏過 3.0
（召回 6.6→27.3%、10.5→40.1%）—— 而是模型鑑別力隨窗口拉長由 ROC-AUC 0.674 掉到
0.551，最長窗的 Brier 技能是負的。§3.1 記載的 0.636~0.764 來自 2026 年的 160 天窗，
那個水準在近一年成立，更早則否。

**二、在目前這段線上資料上，3.1 與 3.0 逐日相同。** 把 16 天線上機率對「該日往前
365 天」的 OOS 分布排名：

| 日期 | P(high) | 實際 | 百分位 | 警示線 | 警示 |
|---|---|---|---|---|---|
| 07-31 | 16.4% | **27** | 37th | 30.5% | — |
| 08-05 | 10.8% | **21** | 18th | 29.8% | — |
| 08-08 | 18.1% | 14 | 46th | 29.6% | — |

**16 天全部零警示。** 直覺上「18.1% 是上線以來最高」是在這 16 天內部排名；對過去
365 天的 OOS 分布（p80 = 29.6%）它只排中段，因為近一年含有活躍得多的時段，
最近幾週是真的安靜。這正是「安靜期警示率自然低於 budget」在運作，方向沒錯，
但代價是提出的症狀在這份資料上沒有被修好。

**三、兩次 surge 的機率本身就低**（16.4% 排 37th、10.8% 排 18th）。任何只改門檻的
方案都救不回它們 —— 那是模型沒看到，不是門檻擋掉。與 §3.1 對 08-05 的結論一致。

#### 目前的落地方式

`pla_surge_model.ADAPTIVE_RISK_ENABLED = False`。百分位照算、照寫進 CSV（新欄
`risk_level_adaptive` / `risk_percentile` / `relative_risk` / `alert` /
`alert_threshold_prob` / `reference_n`），`probability_review` 每天把兩組操作點並列，
但 `risk_level` 仍由絕對階梯決定。

**實測驗證**：重跑後 183 列的 `predicted_sorties` / `lower_bound` / `upper_bound` /
`high_event_probability` / `risk_level` **零格差異** —— 使用者看到的東西完全不變。

要上線只需把該常數改成 `True`，`risk_level` 與 LINE 推播的百分位敘述會一起生效。
判準：累積到 `MIN_N=30` 日／`MIN_POSITIVE=5` 個 surge 後，看 `probability_review.json`
的 `models.new.operating_points.adaptive` 是否穩定達到召回倍數 2x。

#### 順帶修掉的既有 bug（與 3.1 無關，本來就是錯的）

- `prediction.html` 的 `getRiskBadgeClass()` 只認 LOW/MEDIUM/HIGH，
  **`MEDIUM-HIGH` 與 `UNKNOWN` 都 fall through 成綠色 `badge-risk-low`** ——
  D+2~D+7 的「模型說不出話」被畫成「模型說今天安全」
- 同檔 `parseFloat('')` 對 h>=2 的空機率渲染出字面的 `NaN%`；表頭寫 95% CI，實際是 90%
- `index.html` 風險圓環的 `counts` 只有三級，`MEDIUM-HIGH`/`UNKNOWN` 被靜默丟棄。
  由於 D+2~D+7 恆為 UNKNOWN，這張「7 天分布圖」實際只畫了 1 天
- `generate_threads_chart.py` 的 `risk_colors` 缺 `MEDIUM-HIGH`
- `probability_review.py` 自己抄了一份 `ALERT_FLOOR=0.30`/`ALERT_LIFT=2.0`，
  與 `RISK_LADDER` 是兩份定義。已改成 `absolute_alert_threshold()` 單一來源

---

## 4. 驗證方式

```bash
pip install pandas matplotlib pillow requests httpx PyPDF2

# MSA 起訖日：離線重算歷史資料
python3 scripts/scrape_nav_warnings.py --redrive-only

# LINE 推播全文與兩張圖（不實際推送）
python3 scripts/send_message.py --dry-run

# 防衛省解析規則對 PDF 快取重跑（不連線）
python3 scripts/analysis/verify_strait_parsing.py --diff

# 預測模型（新進入點）
python3 predict_surge_daily.py --dry-run

# 回測：含樸素基準線、區間覆蓋率、Brier、pinball
python3 scripts/analysis/backtest_predictor.py --days 365 --horizons 1,3 --refit 28

# 3.1 自適應風險層（§3.9）
python3 scripts/analysis/test_adaptive_risk.py            # 單元測試
python3 scripts/analysis/build_oos_probabilities.py       # 增量 + 因果性自我驗證
python3 scripts/analysis/backtest_adaptive_risk.py --days 365   # 三路操作點 + 閘門
```

回測輸出的判讀順序：
1. `cov90` 應落在 85~95%（名目 90%）。這是換模型最主要的收益
2. `bias` 應在 ±0.5 內
3. `PR_AUC` 要明顯高於 base rate；`Brier` 要低於 `Brier0`（全押 base rate）
4. `MAE` **不是**驗收指標——它在此分布上獎勵低估，看 `pin90`
