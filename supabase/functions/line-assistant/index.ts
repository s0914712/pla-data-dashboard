/**
 * LINE 行程 + 記事小助手 — webhook 進入點。
 *
 * 流程（計畫書 §4.1）：接收 → 驗簽 → 白名單/mention 過濾 → 去重 → 解析
 *                     → 確認 → 寫入 Google Calendar → 稽核
 *
 * 部署時必須 verify_jwt = false：LINE 不會附 Supabase JWT，
 * 真正的來源驗證是 X-Line-Signature 的 HMAC-SHA256。
 */

import { config, env, hasEnv, OPTIONAL_ENV, REQUIRED_ENV } from "./lib/env.ts";
import * as db from "./lib/db.ts";
import * as gcal from "./lib/google_calendar.ts";
import { addDays, isoDate, parse, taipeiParts } from "./lib/parser_zh_tw.ts";
import {
  addMinutes,
  confirmCard,
  describeWhen,
  displayName,
  formatDate,
  hashActor,
  hhmm,
  isAddressedToBot,
  mainMenuCard,
  renderDayView,
  reply,
  replyText,
  stripMentions,
  textMessage,
  timePickCard,
  verifySignature,
} from "./lib/line.ts";
import type {
  CalendarRequestRow,
  LineEvent,
  LineWebhookBody,
  ParsedNote,
  ParsedSchedule,
} from "./lib/types.ts";

const HELP = [
  "📅 行程與記事小助手",
  "",
  "【行程】會先回確認卡，按下「確認建立」才寫進共用行事曆",
  "· 8/28 14:00-16:00 部務會議 地點：第一會議室",
  "· 明天 09:00-10:30 週報會議",
  "· 下週一 下午2點-4點 專案檢討",
  "",
  "【請假】",
  "· 請假 8/29 全天",
  "· 請假 8/29 13:30-17:30",
  "· 請假 8/29-8/31（跨日）",
  "",
  "【記事】直接記錄，不需確認",
  "· 記事 下週要交防務報告 #報告",
  "· 查記事 ／ 查記事 報告（關鍵字）",
  "· 刪記事 3",
  "",
  "【不想打字】選單 →「選日期建立行程」",
  "· 選日期 → 選開始 → 選結束 → 打標題，全程用選的",
  "",
  "【看某一天有什麼】行程與記事一起列出",
  "· 明天　／　明天行程　／　查 8/28",
  "· 只 @ 我不打其他字 → 跳出功能選單",
  "",
  "【其他】/help 說明　/bind 綁定本群組（管理員）",
  "",
  "群組中請 @ 我才會處理；沒 @ 到的對話一律不讀取、不留存。",
].join("\n");

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method !== "POST") return new Response("ok", { status: 200 });

  // 驗簽必須用原始 body 字串，不能經過 JSON.parse 再重組（計畫書 §6.1）
  const rawBody = await req.text();
  const signature = req.headers.get("x-line-signature");

  if (!await verifySignature(rawBody, signature)) {
    console.warn("rejected: invalid signature");
    return new Response("invalid signature", { status: 401 });
  }

  // 緊急停用：驗簽後直接回 200，不做任何寫入
  if (!config.botEnabled) {
    console.info("BOT_ENABLED=false, skipping all writes");
    return new Response("ok", { status: 200 });
  }

  let body: LineWebhookBody;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return new Response("bad json", { status: 400 });
  }

  // 逐筆處理；任何一筆失敗都不能讓整體變成非 200，否則 LINE 會重送整批
  for (const event of body.events ?? []) {
    try {
      await handleEvent(event);
    } catch (err) {
      console.error(`event ${event.type} failed:`, (err as Error).message);
    }
  }

  return new Response("ok", { status: 200 });
});

async function handleEvent(event: LineEvent): Promise<void> {
  switch (event.type) {
    case "message":
      if (event.message?.type === "text") await handleText(event);
      return;
    case "postback":
      await handlePostback(event);
      return;
    case "join":
      if (event.replyToken) {
        await replyText(event.replyToken, "你好，我是行程與記事小助手。\n請管理員輸入「@我 /bind」完成本群組綁定後才會開始運作。");
      }
      return;
    case "follow":
      if (event.replyToken) await replyText(event.replyToken, HELP);
      return;
    default:
      return;
  }
}

// --- 訊息 --------------------------------------------------------------------

async function handleText(event: LineEvent): Promise<void> {
  const userId = event.source.userId;
  const groupId = event.source.groupId ?? null;
  const replyToken = event.replyToken;
  if (!userId || !replyToken) return;

  // 多人聊天室（room）第一版不支援
  if (event.source.type !== "user" && event.source.type !== "group") return;

  // 群組中沒 @ 到我 → 不解析、不入庫、不回覆（計畫書 §1.2 隱私最小化）
  if (!isAddressedToBot(event)) return;

  const text = stripMentions(event.message!);

  // 只 @ 了小助手、後面沒有內容 → 回功能選單。
  // 群組白名單在這之前先擋掉，未綁定的群組仍然完全靜默。
  if (text === "") {
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await showMenu(replyToken);
    return;
  }

  const parsed = parse(text, new Date());

  // /bind 必須在白名單檢查之前處理，否則新群組永遠綁不起來
  if (parsed.kind === "command" && (parsed.value.name === "bind" || parsed.value.name === "unbind")) {
    await handleBind(parsed.value.name, userId, groupId, replyToken);
    return;
  }

  // 白名單：1:1 私訊一律放行；群組必須已綁定，未綁定則靜默略過
  if (groupId && !await db.isGroupAllowed(groupId)) {
    console.info("skipped: group not allowlisted");
    return;
  }

  // 引導流程走到最後一步時，這則訊息就是標題。
  // 斜線指令仍然放行，免得使用者被草稿困住。
  if (!text.startsWith("/")) {
    const scopeKey = db.scopeKeyFor(groupId, userId);
    const draft = await db.takeReadyDraft(scopeKey, userId, config.pendingTtlMinutes);
    if (draft) {
      if (text === "取消") {
        await db.deleteDraft(scopeKey, userId);
        await replyText(replyToken, "已取消，沒有建立任何行程。");
        return;
      }
      await finishDraft(event, draft, text, userId, groupId, replyToken);
      return;
    }
  }

  switch (parsed.kind) {
    case "command":
      await handleCommand(parsed.value.name, parsed.value.arg, userId, groupId, replyToken);
      return;

    case "note":
      await handleNote(event.webhookEventId, parsed.value, userId, groupId, replyToken);
      return;

    case "menu":
      await showMenu(replyToken);
      return;

    case "day_query":
      await showDayView(parsed.value.date, userId, groupId, replyToken);
      return;

    case "schedule":
      await handleSchedule(event, parsed.value, text, userId, groupId, replyToken);
      return;

    case "incomplete":
      // 資料不完整只補問，絕不建立事件（計畫書 §3.1）
      await replyText(replyToken, `⚠️ ${parsed.reason}\n\n輸入 /help 看更多範例。`);
      return;

    case "unknown":
      // 有 @ 到我但看不懂 —— 回選單比丟一大串說明好用
      await reply(replyToken, [
        textMessage("看不懂這則訊息，這是我會的事："),
        mainMenuCard(isoDate(taipeiParts(new Date())), isoDate(addDays(taipeiParts(new Date()), 1))),
      ]);
      return;
  }
}

// --- 綁定 --------------------------------------------------------------------

async function isAdmin(userId: string): Promise<boolean> {
  const configured = env("ADMIN_LINE_USER_ID");
  if (configured && userId === configured) return true;
  return await db.isAdminUser(userId);
}

async function handleBind(
  name: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  if (!groupId) {
    await replyText(replyToken, "這個指令只能在群組中使用。");
    return;
  }
  if (!await isAdmin(userId)) {
    await replyText(replyToken, "只有管理員可以綁定或解除綁定群組。");
    return;
  }

  const actorHash = await hashActor(userId);
  if (name === "bind") {
    await db.bindGroup(groupId, userId, null);
    await db.audit({ action: "bind_group", actor_hash: actorHash, result: "ok" });
    await replyText(replyToken, "✅ 本群組已綁定，可以開始使用了。\n\n" + HELP);
  } else {
    await db.unbindGroup(groupId);
    await db.audit({ action: "unbind_group", actor_hash: actorHash, result: "ok" });
    await replyText(replyToken, "✅ 已解除本群組綁定，之後的訊息我不會再處理。");
  }
}

// --- 指令 --------------------------------------------------------------------

async function handleCommand(
  name: string,
  arg: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  switch (name) {
    case "help":
      await replyText(replyToken, HELP);
      return;

    case "menu":
    case "day":
      await showMenu(replyToken);
      return;

    case "note_list":
      await handleNoteList(arg, userId, groupId, replyToken);
      return;

    case "note_delete":
      await handleNoteDelete(Number(arg), userId, groupId, replyToken);
      return;

    case "diag":
      if (!await isAdmin(userId)) {
        await replyText(replyToken, "只有管理員可以執行診斷。");
        return;
      }
      await replyText(replyToken, await diagnose());
      return;

    case "cleanup": {
      if (!await isAdmin(userId)) {
        await replyText(replyToken, "只有管理員可以執行清理。");
        return;
      }
      const r = await db.cleanupExpired();
      await replyText(replyToken, `清理完成：${r.expired} 筆逾時待確認已標記過期，${r.scrubbed} 筆原文已清除。`);
      return;
    }

    default:
      await replyText(replyToken, `不認得指令 /${name}。\n\n${HELP}`);
      return;
  }
}

/**
 * 環境自我診斷。只回報變數「有沒有值」，絕不輸出值本身。
 * client_email 是設定日曆分享時本來就要公開的識別字串，故可顯示。
 */
async function diagnose(): Promise<string> {
  const lines = ["🩺 環境診斷", "", "【必要變數】"];
  for (const key of REQUIRED_ENV) lines.push(`${hasEnv(key) ? "✅" : "❌"} ${key}`);

  lines.push("", "【選配變數】");
  for (const key of OPTIONAL_ENV) lines.push(`${hasEnv(key) ? "✅" : "－"} ${key}`);

  lines.push("", "【連線】");
  lines.push(`${await db.pingDb() ? "✅" : "❌"} Supabase 資料庫`);

  if (hasEnv("GOOGLE_SERVICE_ACCOUNT_JSON") && hasEnv("GOOGLE_CALENDAR_ID")) {
    try {
      const probe = await gcal.probeAccess();
      lines.push(`${probe.ok ? "✅" : "❌"} Google Calendar：${probe.detail}`);
      lines.push(`　 服務帳戶：${gcal.serviceAccountEmail()}`);
      if (!probe.ok) lines.push("　 請把上面這個 email 加入日曆共用，權限選「變更活動」。");
    } catch (e) {
      lines.push(`❌ Google Calendar：${(e as Error).message}`);
    }
  } else {
    lines.push("－ Google Calendar：變數未齊全，略過檢查");
  }

  lines.push("", `【設定】時區 ${config.timezone}　待確認有效 ${config.pendingTtlMinutes} 分　啟用 ${config.botEnabled}`);
  lines.push(`已綁定群組數：${await db.boundGroupCount()}`);
  return lines.join("\n");
}

// --- 日檢視 ------------------------------------------------------------------

function formatDateShort(iso: string): string {
  return `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;
}

async function showMenu(replyToken: string): Promise<void> {
  const today = taipeiParts(new Date());
  await reply(replyToken, [mainMenuCard(isoDate(today), isoDate(addDays(today, 1)))]);
}

/**
 * 某一天的行程 + 記事。
 *
 * 行程直接查 Google Calendar（共用日曆是正式來源，手機上直接新增的事件也算），
 * 記事查 Supabase。Google 掛掉時仍要能列出記事，所以兩邊分開容錯。
 */
async function showDayView(
  dateIso: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  let events: Awaited<ReturnType<typeof gcal.listEvents>> = [];
  let calendarError: string | null = null;
  try {
    events = await gcal.listEvents(dateIso, config.timezone);
  } catch (err) {
    calendarError = (err as Error).message;
    console.error("listEvents failed:", calendarError);
  }

  const notes = await db.listNotesForDate(db.scopeKeyFor(groupId, userId), dateIso);

  let text = renderDayView(dateIso, events, notes);
  if (calendarError) text += `\n\n⚠️ 行事曆讀取失敗：${calendarError}`;
  await replyText(replyToken, text);
}

// --- 引導式建立行程 ----------------------------------------------------------

/**
 * 選單「選日期建立行程」按下去之後的三個步驟。
 *
 * 每一步都把結果寫進草稿再問下一題；三題答完就等使用者打標題
 * （由 handleText 的草稿攔截接手）。
 */
async function handleDraftStep(
  action: string,
  event: LineEvent,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const scopeKey = db.scopeKeyFor(groupId, userId);
  const sourceType = groupId ? "group" : "user";

  if (action === "new_cancel") {
    await db.deleteDraft(scopeKey, userId);
    await replyText(replyToken, "已取消，沒有建立任何行程。");
    return;
  }

  if (action === "new_date") {
    const date = event.postback?.params?.date ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      await replyText(replyToken, "日期格式不正確，請重新選一次。");
      return;
    }
    await db.upsertDraft({ scope_key: scopeKey, user_id: userId, group_id: groupId, source_type: sourceType, date });
    await reply(replyToken, [timePickCard(
      "① 開始時間",
      `${formatDate(date)}\n這筆行程幾點開始？`,
      "act=new_start",
      "09:00",
      "選開始時間",
    )]);
    return;
  }

  const picked = event.postback?.params?.time ?? "";
  if (!/^\d{2}:\d{2}$/.test(picked)) {
    await replyText(replyToken, "時間格式不正確，請重新選一次。");
    return;
  }

  if (action === "new_start") {
    const draft = await db.upsertDraft({
      scope_key: scopeKey, user_id: userId, group_id: groupId, source_type: sourceType, start: picked,
    });
    if (!draft.target_date) {
      await replyText(replyToken, "找不到剛才選的日期，請從選單重新開始。");
      return;
    }
    await reply(replyToken, [timePickCard(
      "② 結束時間",
      `${formatDate(draft.target_date)} ${picked} 開始\n幾點結束？`,
      "act=new_end",
      addMinutes(picked, 60),
      "選結束時間",
    )]);
    return;
  }

  // action === "new_end"
  const draft = await db.upsertDraft({
    scope_key: scopeKey, user_id: userId, group_id: groupId, source_type: sourceType, end: picked,
  });
  if (!draft.target_date || !draft.start_time) {
    await replyText(replyToken, "草稿不完整，請從選單重新開始。");
    return;
  }
  const start = hhmm(draft.start_time);
  const crossDay = picked <= start;
  await replyText(
    replyToken,
    `③ 最後一步\n${formatDate(draft.target_date)} ${start}-${picked}` +
      (crossDay ? "（跨日到隔天）" : "") +
      "\n\n請直接輸入這筆行程的標題，例如：兵力協調會\n（輸入「取消」可放棄）",
  );
}

/**
 * 草稿三步都選完，使用者打了標題 —— 組成行程並送出確認卡。
 *
 * 走的是跟手打訊息完全相同的 createRequest 與確認流程，
 * 所以去重、原子確認、Google 冪等這些保護一個都沒少。
 */
async function finishDraft(
  event: LineEvent,
  draft: { target_date: string | null; start_time: string | null; end_time: string | null },
  title: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const date = draft.target_date!;
  const start = hhmm(draft.start_time!);
  const end = hhmm(draft.end_time!);

  // 結束不晚於開始就視為跨日到隔天，跟手打訊息的規則一致
  const endYmd = end <= start
    ? isoDate(addDays(ymdOf(date), 1))
    : date;

  const created = await db.createRequest({
    webhook_event_id: event.webhookEventId,
    source_type: groupId ? "group" : "user",
    group_id: groupId,
    user_id: userId,
    original_text: `[選單] ${date} ${start}-${end} ${title}`,
    req_type: "meeting",
    title,
    location: null,
    note: null,
    is_all_day: false,
    start_at: `${date}T${start}:00+08:00`,
    end_at: `${endYmd}T${end}:00+08:00`,
    start_date: null,
    end_date: null,
    expires_at: new Date(Date.now() + config.pendingTtlMinutes * 60_000).toISOString(),
  });

  await db.deleteDraft(db.scopeKeyFor(groupId, userId), userId);

  if (created.status !== "pending") {
    console.info(`redelivered draft finish, status ${created.status}`);
    return;
  }
  await db.audit({ request_id: created.id, action: "request_create_guided", actor_hash: await hashActor(userId), result: "ok" });
  await reply(replyToken, [confirmCard(created, await displayName(event))]);
}

function ymdOf(iso: string): { y: number; m: number; d: number } {
  const [y, m, d] = iso.split("-").map(Number);
  return { y, m, d };
}

// --- 記事 --------------------------------------------------------------------

async function handleNote(
  webhookEventId: string,
  value: ParsedNote,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const note = await db.insertNote({
    webhook_event_id: webhookEventId,
    source_type: groupId ? "group" : "user",
    group_id: groupId,
    user_id: userId,
    scope_key: db.scopeKeyFor(groupId, userId),
    content: value.content,
    tags: value.tags,
    target_date: value.targetDate,
  });
  await db.audit({ action: "note_create", actor_hash: await hashActor(userId), result: "ok" });
  const dated = note.target_date ? `\n🗓 歸在 ${formatDateShort(note.target_date)}` : "";
  await replyText(replyToken, `📝 已記錄 #${note.seq}\n${note.content}${dated}`);
}

async function handleNoteList(
  keyword: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const notes = await db.listNotes(db.scopeKeyFor(groupId, userId), keyword);
  if (notes.length === 0) {
    await replyText(replyToken, keyword ? `找不到含「${keyword}」的記事。` : "目前沒有記事。試試：記事 明天要帶識別證");
    return;
  }
  const header = keyword ? `📝 含「${keyword}」的記事（${notes.length} 筆）` : `📝 最近 ${notes.length} 筆記事`;
  const lines = notes.map((n) => {
    const date = n.created_at.slice(5, 10).replace("-", "/");
    return `#${n.seq}　${date}　${n.content}`;
  });
  await replyText(replyToken, [header, "", ...lines, "", "刪除請輸入：刪記事 <編號>"].join("\n"));
}

async function handleNoteDelete(
  seq: number,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  if (!Number.isInteger(seq) || seq <= 0) {
    await replyText(replyToken, "編號格式不對。請輸入：刪記事 3");
    return;
  }
  const note = await db.findNoteBySeq(db.scopeKeyFor(groupId, userId), seq);
  if (!note) {
    await replyText(replyToken, `找不到記事 #${seq}。輸入「查記事」看目前有哪些。`);
    return;
  }
  if (note.user_id !== userId && !await isAdmin(userId)) {
    await replyText(replyToken, `記事 #${seq} 是別人建立的，只有原作者或管理員可以刪除。`);
    return;
  }
  await db.softDeleteNote(note.id);
  await db.audit({ action: "note_delete", actor_hash: await hashActor(userId), result: "ok" });
  await replyText(replyToken, `🗑️ 已刪除記事 #${seq}`);
}

// --- 行程 --------------------------------------------------------------------

async function handleSchedule(
  event: LineEvent,
  schedule: ParsedSchedule,
  originalText: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const expiresAt = new Date(Date.now() + config.pendingTtlMinutes * 60_000).toISOString();

  const created = await db.createRequest({
    webhook_event_id: event.webhookEventId,
    source_type: groupId ? "group" : "user",
    group_id: groupId,
    user_id: userId,
    original_text: originalText,
    req_type: schedule.reqType,
    title: schedule.title,
    location: schedule.location ?? null,
    note: schedule.note ?? null,
    is_all_day: schedule.isAllDay,
    start_at: schedule.startAt ?? null,
    end_at: schedule.endAt ?? null,
    start_date: schedule.startDate ?? null,
    end_date: schedule.endDate ?? null,
    expires_at: expiresAt,
  });

  // webhook 重送時 createRequest 回傳既有那筆；已處理過就不再發確認卡
  if (created.status !== "pending") {
    console.info(`redelivered event for request in status ${created.status}, not re-prompting`);
    return;
  }

  await db.audit({ request_id: created.id, action: "request_create", actor_hash: await hashActor(userId), result: "ok" });
  await reply(replyToken, [confirmCard(created, await displayName(event))]);
}

// --- 確認 / 取消 -------------------------------------------------------------

async function handlePostback(event: LineEvent): Promise<void> {
  const userId = event.source.userId;
  const replyToken = event.replyToken;
  if (!userId || !replyToken || !event.postback) return;

  const params = new URLSearchParams(event.postback.data);
  const action = params.get("act");

  // 日檢視：日期可能來自按鈕的 d=，也可能來自 datetimepicker 的 params.date
  if (action === "day") {
    const picked = event.postback.params?.date ?? params.get("d") ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(picked)) {
      await replyText(replyToken, "日期格式不正確，請重新選一次。");
      return;
    }
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await showDayView(picked, userId, groupId, replyToken);
    return;
  }

  // 引導式建立行程：選日期 → 選開始 → 選結束 → 打標題
  if (action === "new_date" || action === "new_start" || action === "new_end" || action === "new_cancel") {
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await handleDraftStep(action, event, userId, groupId, replyToken);
    return;
  }

  if (action === "notes") {
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await handleNoteList("", userId, groupId, replyToken);
    return;
  }

  if (action === "help") {
    await replyText(replyToken, HELP);
    return;
  }

  const requestId = params.get("rid");
  // request_id 一律驗成 UUID，避免被當成任意查詢字串或 log injection
  if (!action || !requestId || !/^[0-9a-f-]{36}$/i.test(requestId)) return;

  const request = await db.getRequest(requestId);
  if (!request) {
    await replyText(replyToken, "找不到這筆待確認的行程，可能已經過期了。請重新輸入。");
    return;
  }

  // 只有原建立者可以確認或取消（計畫書 §12.1）
  if (request.user_id !== userId) {
    await replyText(replyToken, "這筆行程要由原建立者本人確認或取消。");
    return;
  }

  const actorHash = await hashActor(userId);

  if (action === "cancel") {
    const canceled = await db.advanceStatus(requestId, "pending", "canceled");
    await db.audit({ request_id: requestId, action: "cancel", actor_hash: actorHash, result: canceled ? "ok" : "noop" });
    await replyText(replyToken, canceled ? "已取消，沒有建立任何行程。" : statusMessage(request.status));
    return;
  }

  if (action !== "confirm") return;

  if (new Date(request.expires_at) < new Date()) {
    await db.advanceStatus(requestId, "pending", "expired");
    await replyText(replyToken, `這筆待確認已超過 ${config.pendingTtlMinutes} 分鐘失效，請重新輸入。`);
    return;
  }

  // 原子搶鎖：雙擊時只有第一個能把 pending 推進到 processing。
  // failed 也允許重試 —— insertEvent 會先用 request_id 查一次，不會重複建立。
  const claimed = await db.advanceStatus(requestId, "pending", "processing")
    ?? await db.advanceStatus(requestId, "failed", "processing");
  if (!claimed) {
    await replyText(replyToken, statusMessage(request.status));
    return;
  }

  try {
    const eventId = await gcal.insertEvent(claimed, await displayName(event), config.timezone);
    await db.advanceStatus(requestId, "processing", "confirmed", { google_event_id: eventId, error_message: null });
    await db.audit({ request_id: requestId, action: "calendar_insert", actor_hash: actorHash, result: "ok" });
    await replyText(
      replyToken,
      `✅ 已建立行事曆行程\n${claimed.title}\n${describeWhen(claimed)}（${config.timezone}）`,
    );
  } catch (err) {
    const message = (err as Error).message;
    await db.advanceStatus(requestId, "processing", "failed", { error_message: message.slice(0, 500) });
    await db.audit({ request_id: requestId, action: "calendar_insert", actor_hash: actorHash, result: "failed", detail: { message } });
    console.error("calendar insert failed:", message);
    await replyText(replyToken, `❌ 建立失敗：${message}\n\n修正後可以再按一次「確認建立」，不會重複建立事件。`);
  }
}

function statusMessage(status: CalendarRequestRow["status"]): string {
  switch (status) {
    case "confirmed": return "這筆行程已經建立過了，不會重複建立。";
    case "processing": return "正在建立中，請稍候。";
    case "canceled": return "這筆行程已經取消了。";
    case "expired": return "這筆待確認已失效，請重新輸入。";
    default: return "這筆行程目前無法確認，請重新輸入。";
  }
}
