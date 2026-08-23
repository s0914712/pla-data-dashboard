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

日檢視同時列出兩件事：

- **行程**：直接查 Google Calendar 的 `events.list`（`singleEvents=true`），
  所以在手機日曆 App 裡直接新增、不經過小助手的事件也會列出。
- **記事**：查 Supabase 的 `notes_for_date()`。

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
deno test --allow-env lib/    # 51 個單元測試（解析器 + 驗簽 + 日檢視 + 選單 + 顯示格式）
deno check index.ts           # 型別檢查
deno lint                     # 靜態檢查
```

整個函式**零外部依賴**（不用 supabase-js，直接打 PostgREST；Google JWT 用 Web Crypto 自簽），
所以以上三個指令在任何 Deno 環境都能直接跑，不需要網路。

部署：

```bash
supabase functions deploy line-assistant --project-ref fyvaqwqnwgfutwfaaeei
```

`verify_jwt = false` 已寫在 `supabase/config.toml`，CLI 會自動帶上。

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
