# LINE 行程與記事小助手 — 部署與維運手冊

MVP 版。既有的每日 PLA 分析推播（`scripts/send_message.py` + `.github/workflows/LINEcron.yml`）
**完全不受影響**：那條路走 LINE push API，新功能走 webhook，兩者不交集。

| 項目 | 值 |
|---|---|
| Supabase 專案 | `fyvaqwqnwgfutwfaaeei`（ap-southeast-2）|
| Webhook URL | `https://fyvaqwqnwgfutwfaaeei.supabase.co/functions/v1/line-assistant` |
| Edge Function | `line-assistant`（`verify_jwt = false`）|
| 原始碼 | `supabase/functions/line-assistant/` |
| Migration | `supabase/migrations/20260823000000_line_assistant.sql` |

---

## 1. 首次設定

四組設定，缺一不可。做完前三組就能用 `/diag` 自我驗證。

### A. LINE Developers Console

1. Messaging API → **Webhook settings → Webhook URL** 填上面那個 URL
2. **Use webhook** → ON
3. 按 **Verify** → 應顯示 Success（函式對空 `events` 陣列回 200）
4. LINE Official Account Manager → 回應設定：
   - 「自動回應訊息」→ **停用**（否則會同時回罐頭訊息）
   - 「Webhook」→ **啟用**
   - 「允許加入群組／多人聊天室」→ **啟用**（否則 bot 進不了群組）

### B. Supabase Secrets

Dashboard → Project Settings → Edge Functions → Secrets。名稱**大小寫敏感**，
完整清單見 `supabase/.env.example`。

必要：`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、
`GOOGLE_SERVICE_ACCOUNT_JSON`、`GOOGLE_CALENDAR_ID`、`ADMIN_LINE_USER_ID`

選配：`BOT_ENABLED`、`PENDING_TTL_MINUTES`、`DEFAULT_TIMEZONE`、`LINE_CHANNEL_ID`

> **不要**設定 `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` —— Edge Runtime 自動注入，
> 而且 `SUPABASE_` 前綴是保留字。
>
> 舊的混合大小寫名稱（`LINE_UserID`、`LINE_Channel_access_token`）程式會自動 fallback，
> 可以先留著；穩定後再統一成標準名稱並刪掉舊的。對照表在 `lib/env.ts` 的 `ALIASES`。

### C. Google Cloud + Calendar

1. 啟用 **Google Calendar API**
2. 建立服務帳戶 → 建立 JSON 金鑰 → 貼進 `GOOGLE_SERVICE_ACCOUNT_JSON`
3. **最常見的錯誤在這一步**：目標共用日曆 → 設定與共用 → 「與特定使用者或群組共用」
   → 加入 JSON 裡的 `client_email` → 權限選 **「變更活動」**（只給「查看」會 403）
4. 同一頁的**日曆 ID** 填進 `GOOGLE_CALENDAR_ID`。不是服務帳戶 email，也不是你的 Gmail

不需要每位成員做 OAuth 授權；服務帳戶直接寫入單一共用日曆。

### D. 綁定群組

1. 開專用測試群組，把小助手拉進去
2. 管理員輸入 `@小助手 /bind`
3. 函式從 webhook 取 groupId 寫入 `allowed_groups`

groupId 不需手抄、不會出現在 log、也不用進 Secret。

---

## 2. 使用方式

群組中**必須 @ 到 bot**（以 `mention.isSelf` 判定，不做名稱字串比對）；
1:1 私訊不需要 @。沒 @ 到的群組對話一律不解析、不入庫、不回覆。

### 行程（會先回確認卡，按「確認建立」才寫入日曆）

```
8/28 14:00-16:00 部務會議 地點：第一會議室
明天 09:00-10:30 週報會議
下週一 下午2點-4點半 專案檢討
請假 8/29 全天
請假 8/29 13:30-17:30
請假 8/29-8/31
```

解析規則：

| 情況 | 處理 |
|---|---|
| 有月日無年份 | 選距現在最近且尚未過期的未來日期 |
| 明確年份但已過期 | 不自動改到明年，改為補問 |
| 會議缺結束時間 | **不預設時長**，補問 |
| 請假只有日期 | 全天 |
| 跨日請假 | DB 存含首尾迄日，寫 Google 時才 +1 天（`end.date` 是排除式）|
| 一則訊息多個日期 | 拒絕，請分開輸入 |

隱私預設：請假只把「請假（姓名）」寫進 `summary`；原始訊息與請假理由**不**進 `description`，
只有明確輸入「備註：」才寫入。

### 記事（直接記錄，不需確認）

```
記事 下週要交防務報告 #報告
記事 明天要帶識別證          ← 自動歸在明天
查記事
查記事 報告
刪記事 3
```

記事的編號 scope 是「群組」或「個人私訊」，兩邊各自從 1 開始。
只有原作者或管理員可以刪除，且是軟刪除（`deleted_at`）。

**日期推論**：建立記事時會用行程解析器的同一套日期邏輯掃描內容
（`inferDate()`），抓到就寫入 `target_date`，抓不到就是 `null`。
日檢視據此歸戶：`target_date` 等於當天，或 `target_date` 為 null 且建立當天
（台北時區）等於當天。掃描有防呆——數字前後緊鄰其他數字時不採用，
所以「值班電話 02/2345」不會被當成 2 月 23 日。

### 引導式建立行程（不用打字）

選單 →「選日期建立行程」→ 三步選完再打標題：

```
①  選日期    （datetimepicker，mode=date）
②  選開始時間（mode=time）
③  選結束時間（mode=time，預設是開始 +1 小時）
④  輸入標題  ← 唯一需要打字的一步
```

前三步都是 postback，夾帶不了自由文字，所以中間狀態存在 `schedule_drafts`
（primary key 是 `scope_key + user_id`，同群組不同人可以各自進行）。
草稿 TTL 與待確認一致（`PENDING_TTL_MINUTES`，預設 15 分），
`take_ready_draft()` 每次呼叫順手清掉過期的。

第 ④ 步的攔截點在 `handleText`：白名單通過後，若該使用者有一張三欄位齊全的
草稿，且訊息不是斜線指令，就把整則訊息當標題。輸入「取消」可放棄，
每一張時間選擇卡上也都有取消鍵。

結束時間不晚於開始就視為跨日到隔天，跟手打訊息的規則一致。
送出後走的是**完全相同的** `createRequest` 與確認卡流程，
所以去重、原子確認、Google 冪等這些保護一個都沒少。

### 修訂既有行程

選單 →「修訂／刪除既有行程」→ 挑日期 → 當天事件以輪播卡列出 → 選一筆 → 決定要做什麼：

| 動作 | 行為 |
|---|---|
| 改時間／日期 | 選新日期 → 開始 → 結束 → 確認 → `events.patch` 只送 `start`/`end` |
| 改標題 | 輸入新標題 → 確認 → `events.patch` 只送 `summary` |
| 改地點 | 輸入新地點 → 確認 → `events.patch` 只送 `location` |
| 複製成新的一筆 | 沿用原標題與地點，選新日期時間 → 確認 → 建立**新**事件，原事件不動 |
| 刪除 | 紅色確認卡 → `events.delete` |

**可修訂的對象是日曆上所有事件**，不限小助手建立的 —— 事件清單直接來自
`events.list`，`id` 帶進 postback，所以你在手機日曆 App 裡直接新增的也能改。

**權限與建立一致**：白名單群組的任何成員都能修訂或刪除。每一筆都寫進
`audit_logs`（`edit_edit_time`／`edit_delete` 等 action），`actor_hash` 是
LINE userId 的 SHA-256。

幾個刻意的設計：

- **用 PATCH 不用 PUT** —— 只送要改的欄位，不會把使用者在日曆 App 上另外
  填的參加者、提醒、描述洗掉。
- **中間狀態存在草稿裡**（`mode` + `event_id`），所以 postback 不必一路
  夾帶 event id，也不會超過 300 bytes 的上限。
- **event id 有做 URL 編碼**再放進 postback，含特殊字元時不會把參數切斷。
- **刪除回 410/404 視為成功** —— 對「把它刪掉」這個意圖來說結果相同，
  重複按不會噴錯。
- 建立流程的第一步會 `reset` 草稿，避免上一輪沒走完的修訂殘留 `mode`，
  導致最後一步的文字被誤當成新標題。

### 日檢視：看某一天有什麼

```
@課表小助手             ← 只 @ 不打字，直接跳功能選單
明天                    ← 裸日期即查詢
明天行程
查 8/28
明天有什麼
選單
```

**只 @ 小助手、後面不打任何字**就會回一張功能選單卡：
**今天 / 明天** 快捷鍵、**選其他日期**（LINE 原生 `datetimepicker`，滑選不必打字）、
**查記事**、**使用說明**。看不懂的訊息也回這張卡，而不是丟一大串文字。

日檢視分三區列出：**【行程】【請假】【記事】**。

- **行程／請假**：都來自 Google Calendar 的 `events.list`（`singleEvents=true`），
  所以在手機日曆 App 裡直接新增、不經過小助手的事件也會列出。
- **記事**：查 Supabase 的 `notes_for_date()`。

**怎麼分辨會議與請假**：小助手建立的事件會在
`extendedProperties.private.req_type` 留下類型；手機上直接新增的、以及這個
欄位上線前建立的沒有，就退回看標題（`isLeaveTitle()`，與解析使用者輸入時
用的是同一組關鍵字）。

**只查其中一種**：

```
誰請假 ／ 請假名單 ／ 查請假        ← 預設今天
明天誰請假 ／ 查請假 8/28 ／ 查 8/28 請假
明天會議                            ← 只看會議
```

選單也有「今天誰請假」「明天誰請假」兩顆快捷鍵（postback 帶 `f=leave`）。

> **刻意的取捨**：裸的「…請假」結尾**不會**被當成查詢 —— 計畫書 §5.1 把
> 「請假 8/29」「明天請假」定義成建立語句，若被查詢吃掉，使用者就再也沒辦法
> 用最自然的說法請假。沒有查／看／列出前綴時，必須用「誰請假」「請假名單」
> 這種明確問法才算查詢。這條有測試守著。

Google 讀取失敗時仍會列出記事，並在訊息尾端附上錯誤原因——兩邊分開容錯。

**判斷「查詢」還是「建立」**：剝掉前後綴後，剩下的字必須剛好只有一個日期
才算查詢。所以「明天 09:00-10:30 週報會議」是建立行程（拿掉日期後還有東西），
「明天」「明天行程」是查詢。這條規則有測試守著，是最容易誤判的地方。

### 管理指令

| 指令 | 權限 | 用途 |
|---|---|---|
| `/help` | 所有人 | 指令說明 |
| `/bind` `/unbind` | 管理員 | 綁定／解綁本群組 |
| `/diag` | 管理員 | 環境診斷：列出各變數**有無值**（不顯示值）、DB 連線、Google 權限探測 |
| `/cleanup` | 管理員 | 手動執行保留政策（標記逾時 pending、清除 30 日前原文）|

---

## 3. 資料表

全部 `ENABLE ROW LEVEL SECURITY` 且**不建任何 policy** → anon / authenticated 一律拒絕。
Edge Function 用自動注入的 service role key 繞過 RLS。

| 表 | 用途 |
|---|---|
| `allowed_groups` | 群組白名單（`/bind` 寫入）|
| `allowed_users` | 額外管理員／成員 |
| `calendar_requests` | 待確認、結果、去重鍵 |
| `notes` | 記事（軟刪除）。`target_date` 為推論出的歸屬日期，可為 null |
| `schedule_drafts` | 引導式建立**與修訂**的中間狀態（`mode` + `event_id`），一個人一個 scope 一張，逾時自動清除 |
| `audit_logs` | 稽核。`actor_hash` 是 LINE userId 的 SHA-256，不留原始 ID |

狀態機：`pending → processing → confirmed`；取消 `canceled`；失敗 `failed`；逾時 `expired`。

### 三層冪等

1. `calendar_requests.webhook_event_id` **UNIQUE** → LINE webhook 重送不產生第二筆 pending
2. 確認時條件更新 `WHERE id=$1 AND status='pending'` → 雙擊只有第一個拿到處理權
3. 寫 Google 時帶 `extendedProperties.private.request_id`；重試前先用
   `privateExtendedProperty=request_id=<uuid>` 查一次 → 防止「Google 建了但 DB 沒回寫」造成重複

`failed` 狀態允許再按一次「確認建立」重試，因為第 3 層會擋掉重複建立。

---

## 4. 排錯

| 症狀 | 原因與處置 |
|---|---|
| LINE Verify 失敗 | `LINE_CHANNEL_SECRET` 沒設或設錯。用 `/diag` 確認（1:1 私訊也能下指令）|
| 群組完全沒反應 | 沒 @ 到 bot；或群組沒 `/bind`；或「允許加入群組」沒開 |
| **每則訊息都回「感謝您的訊息！很抱歉，本帳號無法個別回覆用戶的訊息」** | 這**不是本專案的程式**（repo 內找不到這段文字），是 LINE 內建的自動回應。到 LINE Official Account Manager → 回應設定 → 把「自動回應訊息」**停用**。webhook 與它互不影響，關掉不會影響小助手 |
| `Google Calendar 403` | 日曆沒分享給 `client_email`，或權限只給「查看」而非「變更活動」|
| `Google Calendar 404` | `GOOGLE_CALENDAR_ID` 填成服務帳戶 email 或填錯 |
| `google token 400 invalid_grant` | `GOOGLE_SERVICE_ACCOUNT_JSON` 的 `private_key` 換行壞掉（程式已自動處理 `\n` 字面值，若仍失敗請重新複製整份 JSON）|
| 全天事件差一天 | 這是 Google `end.date` 排除式規格。DB 存含首尾，`exclusiveEndDate()` 負責 +1 天 |
| **確認卡的時間比輸入的早 8 小時** | 已修正。成因是 PostgREST 讀 `timestamptz` 以 UTC 輸出（寫進去 `+08:00`，讀回來 `+00:00`），舊版 `splitLocalIso()` 直接切字串就拿到 UTC 的時分。現在一律 `Date.parse` 成瞬間再換算。注意**存進去的瞬間一直是對的**，Google 事件時間沒受影響，錯的只有顯示 |
| 日檢視列不出行程但記事正常 | Google 讀取失敗，訊息尾端會有原因。服務帳戶需要 `calendar.readonly` scope（已內建）與日曆讀取權限 |
| 記事沒歸到預期的日期 | 內容裡的日期沒被辨識，或被防呆擋掉。用「查記事」確認，必要時改寫成「記事 8/28 要交報告」|

查 log：Supabase Dashboard → Edge Functions → `line-assistant` → Logs，
或用 MCP 的 `query_logs`。log 刻意**不輸出**訊息原文、token、private key、原始 userId。

---

## 5. 緊急處置

| 情況 | 動作 |
|---|---|
| 大量失敗或誤建 | `BOT_ENABLED=false` → 仍驗簽並回 200，但停止一切寫入 |
| LINE token 失效 | 輪替 Channel Access Token → 更新 Supabase Secret。**不需重新部署** |
| Google 授權失效 | 檢查日曆共用權限與服務帳戶金鑰。不自動降級到其他日曆 |
| 錯誤版本上線 | Dashboard → Edge Functions → 回滾到前一版 |
| 需要全面停用 | 關閉 LINE 的 Use webhook，或把 bot 移出群組，並撤銷服務帳戶的日曆權限 |

若 token 或服務帳戶 JSON 曾被貼到公開位置，**立即撤銷並輪替**。

---

## 6. 開發

```bash
cd supabase/functions/line-assistant
deno test --allow-env lib/    # 75 個單元測試（解析器 + 驗簽 + 日檢視 + 選單 + 引導流程 + 修訂 + 行程／請假分區 + 顯示格式）
deno check index.ts           # 型別檢查
deno lint                     # 靜態檢查
```

整個函式**零外部依賴**（不用 supabase-js，直接打 PostgREST；Google JWT 用 Web Crypto 自簽），
所以以上三個指令在任何 Deno 環境都能直接跑，不需要網路。

### 部署

**正常情況走 CI**：`.github/workflows/deploy_supabase_function.yml` 會在
`supabase/functions/**` 有變動時自動跑 `deno check` / `lint` / `test`，
全過才部署。需要在 repo 加一個 Secret：

| Secret | 從哪裡拿 |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | https://supabase.com/dashboard/account/tokens |

專案 ref 不是機密，直接寫在 workflow 與 `config.toml` 裡。

手動部署（本機）：

```bash
supabase functions deploy line-assistant --project-ref fyvaqwqnwgfutwfaaeei
```

`verify_jwt = false` 已寫在 `supabase/config.toml`，CLI 會自動帶上。

> 函式原始碼已超過 11 萬字元，不要再用人工複製貼上的方式部署 ——
> 既慢又容易打錯字（實際發生過兩次註解字元被打錯）。CI 部署與 repo 逐位元組相同。

### 資料保留

`original_text` 只留 30 日。`cleanup_expired()` 這支 SQL function 會把逾時的 pending 標成
`expired`、清掉 30 日前的原文。目前由 `/cleanup` 手動觸發；要自動化可掛 `pg_cron`：

```sql
select cron.schedule('line-assistant-cleanup', '0 3 * * *', $$select public.cleanup_expired()$$);
```

---

## 7. 第一版不做的事

| 項目 | 原因 |
|---|---|
| 寫入 TimeTree 共享日曆 | TimeTree 沒有正式寫入 API；Google 日曆只能在 Home Calendar 顯示為外部日曆 |
| 每位成員寫入自己的日曆 | 需各自 OAuth 與 refresh token 管理 |
| 自動邀請與會者 | 服務帳戶新增 attendee 可能涉及 Workspace 網域委派 |
| 一則訊息多筆行程 | 降低解析與確認複雜度 |
| 語音／圖片辨識 | 先驗證文字流程 |
| 無確認直接建立 | 避免自然語言歧義造成錯誤行程 |
| AI 自然語言解析 | MVP 用規則式，成本 0 且錯誤可控 |
| 一次看整週 | 目前一次一天；要看整週得逐日查 |
