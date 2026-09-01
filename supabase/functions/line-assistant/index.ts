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
import { addDays, isoDate, parse, parseNoteQuery, taipeiParts } from "./lib/parser_zh_tw.ts";
import {
  addMinutes,
  confirmCard,
  datePickCard,
  describeEvent,
  describeWhen,
  editActionCard,
  editConfirmCard,
  eventPickerCarousel,
  displayName,
  formatDate,
  hashActor,
  hhmm,
  describeLeave,
  isAddressedToBot,
  leaveReasonCard,
  leaveShapeCard,
  mainMenuCard,
  renderDayView,
  reply,
  replyText,
  stripMentions,
  taipeiDateIso,
  textMessage,
  timePickCard,
  verifySignature,
} from "./lib/line.ts";
import type { DayFilter } from "./lib/line.ts";
import type {
  CalendarRequestRow,
  DraftRow,
  LineEvent,
  LineWebhookBody,
  ParsedNote,
  ParsedSchedule,
  SourceType,
} from "./lib/types.ts";

const HELP = [
  "📅 行程與記事小助手",
  "",
  "【行程】會先回確認卡，按下「確認建立」才寫進共用行事曆",
  "· 8/28 14:00-16:00 部務會議 地點：第一會議室",
  "· 8/28 1400-1600 部務會議（四位數寫法，不用冒號）",
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
  "· 查記事 8/28　／　查記事 8/28-8/31（日期區間）",
  "· 查記事 本週　本月　上週　最近7天",
  "· 查記事 本週 報告（區間＋關鍵字）",
  "· 選單 →「選日期區間查記事」可以用選的",
  "· 刪記事 3",
  "",
  "【請假】選單 →「我要請假」，全程用選的",
  "· 選日期 → 全天／指定時段／跨多天 → 寫事由（可不填）",
  "· 事由只寫進行事曆說明欄，標題只顯示「請假（姓名）」",
  "· 要銷假：選單 →「取消請假」→ 挑日期 → 只列當天請假 → 選一筆",
  "",
  "【不想打字】選單 →「選日期建立行程」",
  "· 選日期 → 選開始 → 選結束 → 打標題，全程用選的",
  "",
  "【修訂既有行程】選單 →「修訂／刪除既有行程」",
  "· 挑日期 → 從當天清單選一筆 → 改時間／標題／地點、複製或刪除",
  "· 任何更動都會先給你「原本 → 改成」的確認卡",
  "",
  "【看某一天有什麼】行程、請假、記事分區列出",
  "· 明天　／　明天行程　／　查 8/28",
  "· 只看請假：誰請假　／　明天誰請假　／　查請假 8/28",
  "· 只看會議：明天會議",
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
    const draft = await db.getDraft(scopeKey, userId, config.pendingTtlMinutes);
    if (draft && draftAwaitsText(draft)) {
      if (text === "取消") {
        await db.deleteDraft(scopeKey, userId);
        await replyText(replyToken, "已取消，沒有更動任何行程。");
        return;
      }
      await consumeDraftText(event, draft, text, userId, groupId, replyToken);
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
      await showDayView(parsed.value.date, parsed.value.filter, userId, groupId, replyToken);
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
  filter: DayFilter,
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

  // 只查請假時不必撈記事，少一次往返
  const notes = filter === "all"
    ? await db.listNotesForDate(db.scopeKeyFor(groupId, userId), dateIso)
    : [];

  let text = renderDayView(dateIso, events, notes, filter);
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
  const sourceType: SourceType = groupId ? "group" : "user";

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
    // reset：如果上一輪留著沒走完的修訂草稿，這裡要整個清掉再開新局，
    // 否則 mode 會殘留成 edit_*，最後一步的文字會被當成改標題。
    await db.upsertDraft({
      scope_key: scopeKey, user_id: userId, group_id: groupId, source_type: sourceType,
      date, mode: "create", reset: true,
    });
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

/** 這張草稿正在等使用者打字嗎？ */
function draftAwaitsText(d: DraftRow): boolean {
  if (d.mode === "edit_title" || d.mode === "edit_location") return true;
  // 請假：end_date 有值代表方式已選定，接下來等事由
  if (d.mode === "leave") return !!d.end_date;
  // create 要三個時間欄位都選完才輪到打標題；copy 用原標題，不需要打字
  return d.mode === "create" && !!d.target_date && !!d.start_time && !!d.end_time;
}

/** 依草稿的 mode 決定這則文字是標題、還是新的標題／地點。 */
async function consumeDraftText(
  event: LineEvent,
  draft: DraftRow,
  text: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const scopeKey = db.scopeKeyFor(groupId, userId);

  if (draft.mode === "edit_title" || draft.mode === "edit_location") {
    const current = draft.event_id ? await gcal.getEvent(draft.event_id) : null;
    if (!current) {
      await db.deleteDraft(scopeKey, userId);
      await replyText(replyToken, "找不到那筆行程，可能已經被刪掉了。請從選單重新開始。");
      return;
    }
    const isTitle = draft.mode === "edit_title";
    await db.upsertDraft({
      scope_key: scopeKey, user_id: userId, group_id: groupId,
      source_type: groupId ? "group" : "user",
      title: isTitle ? text : null,
      location: isTitle ? null : text,
    });
    await reply(replyToken, [editConfirmCard(
      isTitle ? "確認改標題" : "確認改地點",
      (isTitle ? current.summary : current.location) || "（未設定）",
      text,
    )]);
    return;
  }

  if (draft.mode === "leave") {
    await finishLeave(event, draft, text, userId, groupId, replyToken);
    return;
  }

  await finishDraft(event, draft, text, userId, groupId, replyToken);
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

// --- 引導式請假 --------------------------------------------------------------

/**
 * 選單「我要請假」按下去之後的步驟。
 *
 * 選日期 → 選方式（全天／指定時段／跨多天）→ 寫事由 → 標準確認卡。
 * 最後一步刻意接回既有的 createRequest + confirmCard，
 * 所以去重、原子確認、Google 冪等這些保護一個都沒少。
 */
async function handleLeaveStep(
  action: string,
  params: URLSearchParams,
  event: LineEvent,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const scopeKey = db.scopeKeyFor(groupId, userId);
  const sourceType: SourceType = groupId ? "group" : "user";
  const base = { scope_key: scopeKey, user_id: userId, group_id: groupId, source_type: sourceType };

  if (action === "lv_cancel") {
    await db.deleteDraft(scopeKey, userId);
    await replyText(replyToken, "已取消，沒有送出請假。");
    return;
  }

  // 1. 選起始日期 → 問請假方式
  if (action === "lv_date") {
    const date = event.postback?.params?.date ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      await replyText(replyToken, "日期格式不正確，請重新選一次。");
      return;
    }
    await db.upsertDraft({ ...base, mode: "leave", date, reset: true });
    await reply(replyToken, [leaveShapeCard(date)]);
    return;
  }

  // 取消請假：挑日期 → 只列當天請假 → 選一筆 → 確認刪除。
  // 這兩步不需要既有草稿，所以放在下面的守衛之前。
  if (action === "lv_del_date") {
    const date = event.postback?.params?.date ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      await replyText(replyToken, "日期格式不正確，請重新選一次。");
      return;
    }
    let events: Awaited<ReturnType<typeof gcal.listEvents>>;
    try {
      events = await gcal.listEvents(date, config.timezone);
    } catch (err) {
      await replyText(replyToken, `讀取行事曆失敗：${(err as Error).message}`);
      return;
    }
    const leaves = events.filter((e) => e.id && e.reqType === "leave");
    if (leaves.length === 0) {
      await replyText(replyToken, `${formatDate(date)} 沒有請假紀錄。`);
      return;
    }
    await reply(replyToken, [eventPickerCarousel(date, leaves, {
      action: "lv_del_pick", label: "取消這筆", verb: "取消請假",
    })]);
    return;
  }

  if (action === "lv_del_pick") {
    const eventId = params.get("e") ?? "";
    if (!eventId) return;
    const target = await gcal.getEvent(eventId);
    if (!target) {
      await replyText(replyToken, "找不到那筆請假，可能已經被刪掉了。");
      return;
    }
    // 借用修訂流程的刪除機制：寫一張 mode=delete 的草稿，
    // 確認卡的 act=ed_apply 就會走到同一個 applyEdit。
    await db.upsertDraft({ ...base, mode: "delete", event_id: eventId, reset: true });
    await reply(replyToken, [editConfirmCard(
      "確認取消請假", describeEvent(target), target.summary, true,
    )]);
    return;
  }

  const draft = await db.getDraft(scopeKey, userId, config.pendingTtlMinutes);
  if (!draft?.target_date || draft.mode !== "leave") {
    await replyText(replyToken, "這個請假流程已經逾時，請從選單重新開始。");
    return;
  }
  const startDate = draft.target_date;

  switch (action) {
    // 2-a. 全天：迄日就是起日，直接問事由
    case "lv_allday":
      await db.upsertDraft({ ...base, end_date: startDate });
      await reply(replyToken, [leaveReasonCard(describeLeave(startDate, startDate, null, null))]);
      return;

    // 2-b. 指定時段：先問開始時間
    case "lv_timed":
      await reply(replyToken, [timePickCard(
        "① 開始時間",
        `${formatDate(startDate)}\n請假從幾點開始？`,
        "act=lv_s", "09:00", "選開始時間",
      )]);
      return;

    // 2-c. 跨多天：問迄日
    case "lv_multi":
      await reply(replyToken, [datePickCard(
        "請假到哪一天",
        `${formatDate(startDate)} 起\n請選最後一天（含當天）`,
        "act=lv_edate",
        startDate,
        "選迄日",
      )]);
      return;

    case "lv_s": {
      const picked = event.postback?.params?.time ?? "";
      if (!/^\d{2}:\d{2}$/.test(picked)) {
        await replyText(replyToken, "時間格式不正確，請重新選一次。");
        return;
      }
      await db.upsertDraft({ ...base, start: picked });
      await reply(replyToken, [timePickCard(
        "② 結束時間",
        `${formatDate(startDate)} ${picked} 開始\n幾點結束？`,
        "act=lv_e", addMinutes(picked, 60), "選結束時間",
      )]);
      return;
    }

    case "lv_e": {
      const picked = event.postback?.params?.time ?? "";
      if (!/^\d{2}:\d{2}$/.test(picked)) {
        await replyText(replyToken, "時間格式不正確，請重新選一次。");
        return;
      }
      // 同時寫 end_date，代表方式已選定，接下來等事由
      const d = await db.upsertDraft({ ...base, end: picked, end_date: startDate });
      if (!d.start_time) {
        await replyText(replyToken, "找不到剛才選的開始時間，請從選單重新開始。");
        return;
      }
      await reply(replyToken, [leaveReasonCard(
        describeLeave(startDate, startDate, hhmm(d.start_time), picked),
      )]);
      return;
    }

    case "lv_edate": {
      const endDate = event.postback?.params?.date ?? "";
      if (!/^\d{4}-\d{2}-\d{2}$/.test(endDate)) {
        await replyText(replyToken, "日期格式不正確，請重新選一次。");
        return;
      }
      if (endDate < startDate) {
        await replyText(replyToken, `迄日不能早於起日（${formatDate(startDate)}）。請重新選一次。`);
        return;
      }
      await db.upsertDraft({ ...base, end_date: endDate });
      await reply(replyToken, [leaveReasonCard(describeLeave(startDate, endDate, null, null))]);
      return;
    }

    // 3. 不填事由 → 直接送確認卡
    case "lv_skip":
      await finishLeave(event, draft, null, userId, groupId, replyToken);
      return;
  }
}

/**
 * 請假流程收尾：組成待確認請求並送出標準確認卡。
 *
 * 事由走 note 欄位 —— insertEvent 只會把它寫進 Google 的 description，
 * summary 仍然是「請假（姓名）」，日曆格線上看不到事由（計畫書 §5.4）。
 */
async function finishLeave(
  event: LineEvent,
  draft: DraftRow,
  reason: string | null,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const startDate = draft.target_date!;
  const endDate = draft.end_date ?? startDate;
  const timed = !!draft.start_time && !!draft.end_time;

  let startAt: string | null = null;
  let endAt: string | null = null;
  if (timed) {
    const start = hhmm(draft.start_time!);
    const end = hhmm(draft.end_time!);
    // 結束不晚於開始就視為跨日到隔天，跟其他流程的規則一致
    const endYmd = end <= start ? isoDate(addDays(ymdOf(startDate), 1)) : startDate;
    startAt = `${startDate}T${start}:00+08:00`;
    endAt = `${endYmd}T${end}:00+08:00`;
  }

  const created = await db.createRequest({
    webhook_event_id: event.webhookEventId,
    source_type: groupId ? "group" : "user",
    group_id: groupId,
    user_id: userId,
    // 原文只留流程與日期，不含事由 —— 事由是個資，只進 note 欄位
    original_text: `[請假選單] ${startDate}${endDate !== startDate ? `~${endDate}` : ""}`,
    req_type: "leave",
    title: "請假",
    location: null,
    note: reason,
    is_all_day: !timed,
    start_at: startAt,
    end_at: endAt,
    start_date: timed ? null : startDate,
    end_date: timed ? null : endDate,
    expires_at: new Date(Date.now() + config.pendingTtlMinutes * 60_000).toISOString(),
  });

  await db.deleteDraft(db.scopeKeyFor(groupId, userId), userId);

  if (created.status !== "pending") {
    console.info(`redelivered leave finish, status ${created.status}`);
    return;
  }
  await db.audit({ request_id: created.id, action: "request_create_leave", actor_hash: await hashActor(userId), result: "ok" });
  await reply(replyToken, [confirmCard(created, await displayName(event))]);
}

// --- 修訂／複製／刪除既有行程 ------------------------------------------------

/**
 * 修訂流程的所有步驟。
 *
 * 入口是選單的「修訂／刪除既有行程」：挑日期 → 從當天事件挑一筆 → 決定要做什麼。
 * 之後的中間狀態都存在同一張草稿裡（mode + event_id），所以 postback 不必
 * 一路夾帶 event id。任何寫入 Google 之前一律要經過確認卡。
 */
async function handleEditStep(
  action: string,
  params: URLSearchParams,
  event: LineEvent,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const scopeKey = db.scopeKeyFor(groupId, userId);
  const sourceType: SourceType = groupId ? "group" : "user";
  const base = { scope_key: scopeKey, user_id: userId, group_id: groupId, source_type: sourceType };

  // 1. 挑日期 → 列出當天事件讓使用者選
  if (action === "ed_date") {
    const date = event.postback?.params?.date ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      await replyText(replyToken, "日期格式不正確，請重新選一次。");
      return;
    }
    let events: Awaited<ReturnType<typeof gcal.listEvents>>;
    try {
      events = await gcal.listEvents(date, config.timezone);
    } catch (err) {
      await replyText(replyToken, `讀取行事曆失敗：${(err as Error).message}`);
      return;
    }
    const pickable = events.filter((e) => e.id);
    if (pickable.length === 0) {
      await replyText(replyToken, `${formatDate(date)} 沒有行程可以修訂。`);
      return;
    }
    await reply(replyToken, [eventPickerCarousel(date, pickable)]);
    return;
  }

  // 2. 選定一筆 → 問要做什麼
  if (action === "ed_pick") {
    const eventId = params.get("e") ?? "";
    if (!eventId) return;
    const target = await gcal.getEvent(eventId);
    if (!target) {
      await replyText(replyToken, "找不到那筆行程，可能已經被刪掉了。");
      return;
    }
    // 先把 event_id 記進草稿，後面幾步的 postback 就不必再夾帶
    await db.upsertDraft({ ...base, mode: "edit_time", event_id: eventId, reset: true });
    await reply(replyToken, [editActionCard(target)]);
    return;
  }

  if (action === "ed_cancel") {
    await db.deleteDraft(scopeKey, userId);
    await replyText(replyToken, "已取消，沒有更動任何行程。");
    return;
  }

  // 以下每一步都需要草稿裡的 event_id
  const draft = await db.getDraft(scopeKey, userId, config.pendingTtlMinutes);
  if (!draft?.event_id) {
    await replyText(replyToken, "這個修訂流程已經逾時，請從選單重新開始。");
    return;
  }
  const target = await gcal.getEvent(draft.event_id);
  if (!target) {
    await db.deleteDraft(scopeKey, userId);
    await replyText(replyToken, "找不到那筆行程，可能已經被刪掉了。");
    return;
  }

  switch (action) {
    case "ed_title":
      await db.upsertDraft({ ...base, mode: "edit_title" });
      await replyText(replyToken, `目前標題：${target.summary}\n\n請直接輸入新的標題。\n（輸入「取消」可放棄）`);
      return;

    case "ed_loc":
      await db.upsertDraft({ ...base, mode: "edit_location" });
      await replyText(replyToken, `目前地點：${target.location ?? "（未設定）"}\n\n請直接輸入新的地點。\n（輸入「取消」可放棄）`);
      return;

    case "ed_del":
      await db.upsertDraft({ ...base, mode: "delete" });
      await reply(replyToken, [editConfirmCard(
        "確認刪除行程", describeEvent(target), target.summary, true,
      )]);
      return;

    case "ed_time":
    case "ed_copy": {
      const mode = action === "ed_copy" ? "copy" : "edit_time";
      await db.upsertDraft({
        ...base, mode, event_id: draft.event_id, reset: true,
        title: target.summary, location: target.location,
      });
      await reply(replyToken, [datePickCard(
        mode === "copy" ? "複製成新的一筆" : "改時間／日期",
        mode === "copy"
          ? `會沿用標題「${target.summary}」，先選新日期`
          : `原本：${describeEvent(target)}\n請選新的日期`,
        "act=et_d",
        eventDateIso(target),
        "選日期",
      )]);
      return;
    }

    // 3-1. 新日期 → 問開始時間
    case "et_d": {
      const date = event.postback?.params?.date ?? "";
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
        await replyText(replyToken, "日期格式不正確，請重新選一次。");
        return;
      }
      await db.upsertDraft({ ...base, date });
      await reply(replyToken, [timePickCard(
        "① 開始時間", `${formatDate(date)}\n幾點開始？`, "act=et_s", "09:00", "選開始時間",
      )]);
      return;
    }

    // 3-2. 開始時間 → 問結束時間
    case "et_s": {
      const picked = event.postback?.params?.time ?? "";
      if (!/^\d{2}:\d{2}$/.test(picked)) {
        await replyText(replyToken, "時間格式不正確，請重新選一次。");
        return;
      }
      const d = await db.upsertDraft({ ...base, start: picked });
      if (!d.target_date) {
        await replyText(replyToken, "找不到剛才選的日期，請從選單重新開始。");
        return;
      }
      await reply(replyToken, [timePickCard(
        "② 結束時間", `${formatDate(d.target_date)} ${picked} 開始\n幾點結束？`,
        "act=et_e", addMinutes(picked, 60), "選結束時間",
      )]);
      return;
    }

    // 3-3. 結束時間 → 確認卡
    case "et_e": {
      const picked = event.postback?.params?.time ?? "";
      if (!/^\d{2}:\d{2}$/.test(picked)) {
        await replyText(replyToken, "時間格式不正確，請重新選一次。");
        return;
      }
      const d = await db.upsertDraft({ ...base, end: picked });
      if (!d.target_date || !d.start_time) {
        await replyText(replyToken, "草稿不完整，請從選單重新開始。");
        return;
      }
      const after = describeSpan(d.target_date, hhmm(d.start_time), picked);
      await reply(replyToken, [editConfirmCard(
        d.mode === "copy" ? "確認複製成新的一筆" : "確認改時間",
        d.mode === "copy" ? `原行程：${describeEvent(target)}` : describeEvent(target),
        d.mode === "copy" ? `新增一筆：${after}` : after,
      )]);
      return;
    }

    // 4. 套用
    case "ed_apply":
      await applyEdit(event, draft, target, userId, groupId, replyToken);
      return;
  }
}

/** 事件目前落在哪一天（台北），拿來當日期選擇器的預設值。 */
function eventDateIso(target: gcal.FullEvent): string {
  if (target.startDate) return target.startDate;
  if (target.startAt) {
    const ms = Date.parse(target.startAt);
    if (!Number.isNaN(ms)) {
      const t = new Date(ms + 8 * 60 * 60_000);
      const pad = (n: number) => String(n).padStart(2, "0");
      return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`;
    }
  }
  return isoDate(taipeiParts(new Date()));
}

/** 把日期與起訖時分渲染成跟 describeEvent 一致的字串。 */
function describeSpan(dateIso: string, start: string, end: string): string {
  const endDate = end <= start ? isoDate(addDays(ymdOf(dateIso), 1)) : dateIso;
  return describeWhen({
    is_all_day: false,
    start_at: `${dateIso}T${start}:00+08:00`,
    end_at: `${endDate}T${end}:00+08:00`,
    start_date: null,
    end_date: null,
  });
}

/**
 * 按下確認之後真正寫進 Google。
 *
 * 每一種 mode 都寫 audit_logs；改時間用 PATCH 只送 start/end，
 * 不會把使用者在日曆 App 上另外填的參加者、提醒洗掉。
 */
async function applyEdit(
  event: LineEvent,
  draft: DraftRow,
  target: gcal.FullEvent,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const scopeKey = db.scopeKeyFor(groupId, userId);
  const actorHash = await hashActor(userId);
  const tz = config.timezone;

  try {
    let done: string;

    switch (draft.mode) {
      case "edit_title": {
        if (!draft.payload_title) throw new Error("新標題是空的");
        await gcal.patchEvent(target.id, { summary: draft.payload_title });
        done = `✅ 標題已改為\n${draft.payload_title}`;
        break;
      }
      case "edit_location": {
        if (!draft.payload_location) throw new Error("新地點是空的");
        await gcal.patchEvent(target.id, { location: draft.payload_location });
        done = `✅ 地點已改為\n${draft.payload_location}`;
        break;
      }
      case "delete": {
        await gcal.deleteEvent(target.id);
        done = `🗑️ 已刪除\n${target.summary}\n${describeEvent(target)}`;
        break;
      }
      case "edit_time": {
        const { date, start, end, endDate } = draftSpan(draft);
        await gcal.patchEvent(target.id, {
          start: { dateTime: `${date}T${start}:00+08:00`, timeZone: tz },
          end: { dateTime: `${endDate}T${end}:00+08:00`, timeZone: tz },
        });
        done = `✅ 時間已改為\n${target.summary}\n${describeSpan(date, start, end)}`;
        break;
      }
      case "copy": {
        const { date, start, end, endDate } = draftSpan(draft);
        const created = await db.createRequest({
          webhook_event_id: event.webhookEventId,
          source_type: groupId ? "group" : "user",
          group_id: groupId,
          user_id: userId,
          original_text: `[複製自 ${target.id}] ${date} ${start}-${end}`,
          req_type: "meeting",
          title: draft.payload_title ?? target.summary,
          location: draft.payload_location ?? target.location,
          note: null,
          is_all_day: false,
          start_at: `${date}T${start}:00+08:00`,
          end_at: `${endDate}T${end}:00+08:00`,
          start_date: null,
          end_date: null,
          expires_at: new Date(Date.now() + config.pendingTtlMinutes * 60_000).toISOString(),
        });
        const gid = await gcal.insertEvent(created, await displayName(event), tz);
        await db.advanceStatus(created.id, "pending", "confirmed", { google_event_id: gid });
        done = `✅ 已新增一筆\n${created.title}\n${describeSpan(date, start, end)}`;
        break;
      }
      default:
        await replyText(replyToken, "這個修訂流程還沒完成，請從選單重新開始。");
        return;
    }

    await db.deleteDraft(scopeKey, userId);
    await db.audit({ action: `edit_${draft.mode}`, actor_hash: actorHash, result: "ok" });
    await replyText(replyToken, done);
  } catch (err) {
    const message = (err as Error).message;
    console.error(`edit ${draft.mode} failed:`, message);
    await db.audit({ action: `edit_${draft.mode}`, actor_hash: actorHash, result: "failed", detail: { message } });
    await replyText(replyToken, `❌ 修訂失敗：${message}\n\n行程沒有被更動。請從選單重新開始。`);
  }
}

/** 從草稿取出起訖，套用「結束不晚於開始就跨日」的共同規則。 */
function draftSpan(draft: DraftRow): { date: string; start: string; end: string; endDate: string } {
  const date = draft.target_date!;
  const start = hhmm(draft.start_time!);
  const end = hhmm(draft.end_time!);
  const endDate = end <= start ? isoDate(addDays(ymdOf(date), 1)) : date;
  return { date, start, end, endDate };
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

/**
 * 記事查詢。參數可以是關鍵字、單一日期、日期區間或期間詞，也可以混用：
 *
 *   查記事            → 最近 10 筆
 *   查記事 報告        → 關鍵字
 *   查記事 8/28       → 那一天
 *   查記事 8/28-8/31  → 區間
 *   查記事 本週 報告   → 區間 + 關鍵字
 */
async function handleNoteList(
  arg: string,
  userId: string,
  groupId: string | null,
  replyToken: string,
): Promise<void> {
  const q = parseNoteQuery(arg, new Date());
  const scopeKey = db.scopeKeyFor(groupId, userId);

  if (q.from && q.to) {
    await replyNoteRange(q.from, q.to, q.keyword, scopeKey, replyToken);
    return;
  }

  const notes = await db.listNotes(scopeKey, q.keyword);
  if (notes.length === 0) {
    await replyText(
      replyToken,
      q.keyword ? `找不到含「${q.keyword}」的記事。` : "目前沒有記事。試試：記事 明天要帶識別證",
    );
    return;
  }
  const header = q.keyword
    ? `📝 含「${q.keyword}」的記事（${notes.length} 筆）`
    : `📝 最近 ${notes.length} 筆記事`;
  await replyText(replyToken, renderNotes(header, notes));
}

async function replyNoteRange(
  fromIso: string,
  toIso: string,
  keyword: string,
  scopeKey: string,
  replyToken: string,
): Promise<void> {
  const span = fromIso === toIso
    ? formatDateShort(fromIso)
    : `${formatDateShort(fromIso)}～${formatDateShort(toIso)}`;
  const notes = await db.listNotesInRange(scopeKey, fromIso, toIso, keyword);
  if (notes.length === 0) {
    const suffix = keyword ? `含「${keyword}」的` : "";
    await replyText(replyToken, `${span} 沒有${suffix}記事。`);
    return;
  }
  const header = keyword
    ? `📝 ${span} 含「${keyword}」的記事（${notes.length} 筆）`
    : `📝 ${span} 的記事（${notes.length} 筆）`;
  await replyText(replyToken, renderNotes(header, notes));
}

/**
 * 記事列表的共用排版。
 *
 * 日期優先用 target_date（記事內容提到的那一天），沒有才退回建立日期 ——
 * 而 created_at 從 PostgREST 讀回來是 UTC，要換算成台北才不會差一天。
 */
function renderNotes(header: string, notes: { seq: number; content: string; target_date: string | null; created_at: string }[]): string {
  const lines = notes.map((n) => {
    const iso = n.target_date ?? taipeiDateIso(n.created_at);
    return `#${n.seq}　${formatDateShort(iso)}　${n.content}`;
  });
  return [header, "", ...lines, "", "刪除請輸入：刪記事 <編號>"].join("\n");
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
    const raw = params.get("f");
    const filter: DayFilter = raw === "leave" || raw === "meeting" ? raw : "all";
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await showDayView(picked, filter, userId, groupId, replyToken);
    return;
  }

  // 引導式建立行程：選日期 → 選開始 → 選結束 → 打標題
  if (action === "new_date" || action === "new_start" || action === "new_end" || action === "new_cancel") {
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await handleDraftStep(action, event, userId, groupId, replyToken);
    return;
  }

  // 引導式請假：選日期 → 選方式 → 寫事由
  if (action?.startsWith("lv_")) {
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await handleLeaveStep(action, params, event, userId, groupId, replyToken);
    return;
  }

  // 修訂／複製／刪除既有行程
  if (action?.startsWith("ed_") || action?.startsWith("et_")) {
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    await handleEditStep(action, params, event, userId, groupId, replyToken);
    return;
  }

  if (action === "notes") {
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;
    // q= 讓「本週記事」「本月記事」直接重用文字版的查詢語法
    await handleNoteList(params.get("q") ?? "", userId, groupId, replyToken);
    return;
  }

  // 記事區間查詢：選起日 → 選迄日。起日直接編進第二張卡的 postback，不用存草稿。
  if (action === "nt_cancel") {
    await replyText(replyToken, "已取消。");
    return;
  }

  if (action === "nt_from" || action === "nt_to") {
    const groupId = event.source.groupId ?? null;
    if (groupId && !await db.isGroupAllowed(groupId)) return;

    const picked = event.postback.params?.date ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(picked)) {
      await replyText(replyToken, "日期格式不正確，請重新選一次。");
      return;
    }

    if (action === "nt_from") {
      await reply(replyToken, [datePickCard(
        "查記事：選迄日",
        `起日 ${formatDateShort(picked)}，接著選要查到哪一天。`,
        `act=nt_to&s=${picked}`,
        picked,
        "選迄日",
        "act=nt_cancel",
      )]);
      return;
    }

    const start = params.get("s") ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) {
      await replyText(replyToken, "找不到起日，請從選單重新選一次。");
      return;
    }
    // 選反了就對調，不要回一個空清單讓人以為沒記事
    const [from, to] = start <= picked ? [start, picked] : [picked, start];
    await replyNoteRange(from, to, "", db.scopeKeyFor(groupId, userId), replyToken);
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
