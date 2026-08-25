// 刻意不依賴外部測試套件。
function assert(cond: unknown, msg = "assertion failed"): asserts cond {
  if (!cond) throw new Error(msg);
}
function assertEquals<T>(actual: T, expected: T, msg = ""): void {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${msg}\n  actual:   ${a}\n  expected: ${b}`);
}

const SECRET = "test-channel-secret";
Deno.env.set("LINE_CHANNEL_SECRET", SECRET);

const {
  verifySignature, stripMentions, isAddressedToBot, describeWhen, formatDate, mainMenuCard,
  timePickCard, hhmm, addMinutes,
  datePickCard, eventPickerCarousel, editActionCard, editConfirmCard, describeEvent,
  renderDayView, leaveShapeCard, leaveReasonCard, describeLeave,
} = await import("./line.ts");

async function sign(body: string, secret = SECRET): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  return btoa(String.fromCharCode(...new Uint8Array(mac)));
}

Deno.test("驗簽：正確簽章通過", async () => {
  const body = JSON.stringify({ events: [] });
  assertEquals(await verifySignature(body, await sign(body)), true);
});

Deno.test("驗簽：錯誤密鑰不通過", async () => {
  const body = JSON.stringify({ events: [] });
  assertEquals(await verifySignature(body, await sign(body, "wrong-secret")), false);
});

Deno.test("驗簽：body 被竄改一個字元就不通過", async () => {
  const body = JSON.stringify({ events: [], destination: "U1" });
  const signature = await sign(body);
  assertEquals(await verifySignature(body.replace("U1", "U2"), signature), false);
});

Deno.test("驗簽：缺 header 不通過", async () => {
  assertEquals(await verifySignature("{}", null), false);
});

Deno.test("驗簽：非法 base64 不通過而不丟例外", async () => {
  assertEquals(await verifySignature("{}", "!!!not-base64!!!"), false);
});

Deno.test("stripMentions：依 index 由後往前切", () => {
  // "@小助手 8/28 開會" —— mention 佔前 4 個 UTF-16 單元
  const text = "@小助手 8/28 開會";
  const out = stripMentions({ text, mention: { mentionees: [{ index: 0, length: 4 }] } });
  assertEquals(out, "8/28 開會");
});

Deno.test("stripMentions：多個 mention 都要移除", () => {
  // "@bot 開會 @someone 記得參加" —— "@bot" 佔 0-3，"@someone" 佔 8-15
  const text = "@bot 開會 @someone 記得參加";
  const out = stripMentions({
    text,
    mention: { mentionees: [{ index: 0, length: 4 }, { index: 8, length: 8 }] },
  });
  assert(!out.includes("@"), out);
  assert(!out.includes("someone"), out);
  // 殘留的多餘空白由 parser 的 normalize() 收斂，這裡不強求
  assertEquals(out.replace(/\s+/g, " "), "開會 記得參加");
});

Deno.test("群組必須真的 @ 到 bot；靠 isSelf 而不是名稱字串", () => {
  const base = { type: "message", webhookEventId: "e", timestamp: 0 };
  // 群組中提及了別人，不是我 → 不處理
  assertEquals(
    isAddressedToBot({
      ...base,
      source: { type: "group", groupId: "G1", userId: "U1" },
      message: { id: "m", type: "text", text: "@someone 開會", mention: { mentionees: [{ index: 0, length: 9, isSelf: false }] } },
    // deno-lint-ignore no-explicit-any
    } as any),
    false,
  );
  // 群組中提及了我 → 處理
  assertEquals(
    isAddressedToBot({
      ...base,
      source: { type: "group", groupId: "G1", userId: "U1" },
      message: { id: "m", type: "text", text: "@bot 開會", mention: { mentionees: [{ index: 0, length: 4, isSelf: true }] } },
    // deno-lint-ignore no-explicit-any
    } as any),
    true,
  );
  // 群組中完全沒 mention → 不處理（隱私最小化）
  assertEquals(
    isAddressedToBot({
      ...base,
      source: { type: "group", groupId: "G1", userId: "U1" },
      message: { id: "m", type: "text", text: "今天天氣真好" },
    // deno-lint-ignore no-explicit-any
    } as any),
    false,
  );
  // 1:1 私訊不需要 @
  assertEquals(
    isAddressedToBot({
      ...base,
      source: { type: "user", userId: "U1" },
      message: { id: "m", type: "text", text: "8/28 14:00-16:00 開會" },
    // deno-lint-ignore no-explicit-any
    } as any),
    true,
  );
});

Deno.test("formatDate 帶星期", () => {
  assertEquals(formatDate("2026-08-28"), "2026/08/28（週五）");
});

Deno.test("describeWhen：資料庫讀回來的是 UTC，必須換算成台北", () => {
  // 這是 PostgREST 實際回傳的樣子：寫入 +08:00，讀出 +00:00。
  // 之前直接 slice 字串，14:00 的行程會顯示成 06:00。
  assertEquals(
    describeWhen({ is_all_day: false, start_at: "2026-08-25T06:00:00+00:00", end_at: "2026-08-25T07:00:00+00:00", start_date: null, end_date: null }),
    "2026/08/25（週二） 14:00-15:00",
  );
  // 跨過台北午夜：UTC 的 8/28 16:00 = 台北 8/29 00:00
  assertEquals(
    describeWhen({ is_all_day: false, start_at: "2026-08-28T15:00:00+00:00", end_at: "2026-08-28T16:30:00+00:00", start_date: null, end_date: null }),
    "2026/08/28（週五） 23:00 至 2026/08/29（週六） 00:30",
  );
});

Deno.test("describeWhen：時段、全天、跨日", () => {
  assertEquals(
    describeWhen({ is_all_day: false, start_at: "2026-08-28T14:00:00+08:00", end_at: "2026-08-28T16:00:00+08:00", start_date: null, end_date: null }),
    "2026/08/28（週五） 14:00-16:00",
  );
  assertEquals(
    describeWhen({ is_all_day: true, start_at: null, end_at: null, start_date: "2026-08-29", end_date: "2026-08-29" }),
    "2026/08/29（週六） 全天",
  );
  // 確認卡顯示的是「含首尾」的迄日，不是 Google 的排除式 end.date
  assertEquals(
    describeWhen({ is_all_day: true, start_at: null, end_at: null, start_date: "2026-08-29", end_date: "2026-08-31" }),
    "2026/08/29（週六） 至 2026/08/31（週一） 全天",
  );
});

Deno.test("postback data 只接受 UUID 形式的 rid", () => {
  const ok = new URLSearchParams("act=confirm&rid=0f8fad5b-d9cb-469f-a165-70867728950e");
  assert(/^[0-9a-f-]{36}$/i.test(ok.get("rid")!));
  const bad = new URLSearchParams("act=confirm&rid=../../etc/passwd");
  assert(!/^[0-9a-f-]{36}$/i.test(bad.get("rid")!));
});

// --- 只 @ 小助手、後面沒打字 --------------------------------------------

Deno.test("bare @mention 剝完後是空字串（這是回功能選單的觸發條件）", () => {
  // LINE 常帶一個尾空白，兩種寫法都要歸為空
  assertEquals(
    stripMentions({ text: "@課表小助手", mention: { mentionees: [{ index: 0, length: 6 }] } }),
    "",
  );
  assertEquals(
    stripMentions({ text: "@課表小助手 ", mention: { mentionees: [{ index: 0, length: 6 }] } }),
    "",
  );
  // 有打字就不是空，不走選單
  assertEquals(
    stripMentions({ text: "@課表小助手 明天", mention: { mentionees: [{ index: 0, length: 6 }] } }),
    "明天",
  );
});

Deno.test("功能選單卡的按鈕與 postback data", () => {
  const card = mainMenuCard("2026-08-23", "2026-08-24");
  assertEquals(card.type, "flex");
  const json = JSON.stringify(card);
  // 四個入口都要在
  assert(json.includes("act=day&d=2026-08-23"), "今天");
  assert(json.includes("act=day&d=2026-08-24"), "明天");
  assert(json.includes("datetimepicker"), "日期選擇器");
  assert(json.includes("act=notes"), "查記事");
  assert(json.includes("act=help"), "使用說明");
  // 選擇器的 data 不帶 d=，日期走 postback.params.date
  const picker = JSON.parse(json).contents.body.contents.find(
    // deno-lint-ignore no-explicit-any
    (c: any) => c.action?.type === "datetimepicker",
  );
  assertEquals(picker.action.data, "act=day");
  assertEquals(picker.action.mode, "date");
  assertEquals(picker.action.initial, "2026-08-23");
});

// --- 引導式建立行程 ----------------------------------------------------------

Deno.test("hhmm：Postgres 的 time 是 HH:MM:SS，要切成 HH:MM", () => {
  assertEquals(hhmm("14:00:00"), "14:00");
  assertEquals(hhmm("09:30"), "09:30");
});

Deno.test("addMinutes：結束時間的預設值", () => {
  assertEquals(addMinutes("09:00", 60), "10:00");
  assertEquals(addMinutes("14:30:00", 60), "15:30");
  // 跨過午夜要繞回去，不能變成 24:xx
  assertEquals(addMinutes("23:30", 60), "00:30");
  assertEquals(addMinutes("23:00", 90), "00:30");
});

Deno.test("選單有「選日期建立行程」入口", () => {
  const json = JSON.stringify(mainMenuCard("2026-08-23", "2026-08-24"));
  assert(json.includes("act=new_date"), "缺少引導流程入口");
  // 這顆按鈕本身就是日期選擇器，按一下就進流程
  const flat = JSON.stringify(JSON.parse(json)).match(/\{[^{}]*act=new_date[^{}]*\}/)![0];
  assert(flat.includes("datetimepicker"), flat);
  assert(flat.includes('"mode":"date"'), flat);
});

Deno.test("時間選擇卡：picker 與取消鍵", () => {
  const card = timePickCard("① 開始時間", "副標", "act=new_start", "09:00", "選開始時間");
  const json = JSON.stringify(card);
  assertEquals(card.type, "flex");
  assert(json.includes('"mode":"time"'), "應為時間選擇器");
  assert(json.includes("act=new_start"), json);
  assert(json.includes('"initial":"09:00"'), json);
  assert(json.includes("act=new_cancel"), "每一步都要能取消");
});

Deno.test("跨日判斷：結束不晚於開始就是隔天", () => {
  // 這是 finishDraft 用的規則，跟手打訊息的解析器一致
  assert("01:00" <= "22:00", "字串比較足以判斷同日先後");
  assert(!("15:00" <= "14:00"));
  assert("14:00" <= "14:00", "相同時間視為跨日，避免產生零長度事件");
});

// --- 修訂既有行程 ------------------------------------------------------------

const EVT = {
  id: "abc123xyz", summary: "兵力協調會", location: "第一會議室", isAllDay: false,
  // 資料庫／Google 回來的都可能是 UTC，這裡刻意用 UTC 當 fixture
  startAt: "2026-08-25T06:00:00+00:00", endAt: "2026-08-25T07:00:00+00:00",
  startDate: null, endDate: null,
};

Deno.test("describeEvent：UTC 進來也要顯示成台北時間", () => {
  assertEquals(describeEvent(EVT), "2026/08/25（週二） 14:00-15:00");
  assertEquals(
    describeEvent({ ...EVT, isAllDay: true, startAt: null, endAt: null, startDate: "2026-08-29", endDate: "2026-08-30" }),
    "2026/08/29（週六） 全天",
  );
});

Deno.test("事件挑選輪播：帶得出 event id，且不超過 12 顆", () => {
  const many = Array.from({ length: 20 }, (_, i) => ({
    id: `evt-${i}`, summary: `會議 ${i}`, location: null,
    isAllDay: false, startTime: "09:00", endTime: "10:00", reqType: "meeting" as const,
  }));
  const card = eventPickerCarousel("2026-08-25", many);
  const bubbles = JSON.parse(JSON.stringify(card)).contents.contents;
  assertEquals(bubbles.length, 12, "LINE carousel 上限是 12");
  assert(JSON.stringify(card).includes("act=ed_pick&e=evt-0"));
});

Deno.test("事件挑選輪播：event id 有做 URL 編碼", () => {
  // Google 的 id 通常是英數，但含特殊字元時不能污染 postback 的 query
  const card = eventPickerCarousel("2026-08-25", [{
    id: "a&b=c", summary: "測試", location: null, isAllDay: true,
    startTime: null, endTime: null, reqType: "meeting" as const,
  }]);
  const json = JSON.stringify(card);
  assert(json.includes("act=ed_pick&e=a%26b%3Dc"), json);
  assert(!json.includes("e=a&b=c"), "未編碼會把 postback 參數切斷");
});

Deno.test("修訂動作卡：五種操作都在", () => {
  const json = JSON.stringify(editActionCard(EVT));
  for (const a of ["act=ed_time", "act=ed_title", "act=ed_loc", "act=ed_copy", "act=ed_del", "act=ed_cancel"]) {
    assert(json.includes(a), `缺少 ${a}`);
  }
  assert(json.includes("兵力協調會"), "要顯示目前的標題");
  assert(json.includes("14:00-15:00"), "要顯示目前的時間（台北）");
});

Deno.test("修訂確認卡：一定有 原本 → 改成 兩行與確認鍵", () => {
  const json = JSON.stringify(editConfirmCard("確認改時間", "舊時間", "新時間"));
  assert(json.includes("原本") && json.includes("舊時間"), json);
  assert(json.includes("改成") && json.includes("新時間"), json);
  assert(json.includes("act=ed_apply"), "缺少套用鍵");
  assert(json.includes("act=ed_cancel"), "缺少取消鍵");
});

Deno.test("刪除確認卡走破壞性樣式，文案不同", () => {
  const json = JSON.stringify(editConfirmCard("確認刪除行程", "8/25 14:00-15:00", "兵力協調會", true));
  assert(json.includes("將刪除"), "刪除卡的標籤要是「將刪除」而不是「改成」");
  assert(json.includes("確認刪除"), json);
  assert(json.includes("#c62828"), "破壞性操作要用紅色");
});

Deno.test("選單有修訂入口", () => {
  const json = JSON.stringify(mainMenuCard("2026-08-23", "2026-08-24"));
  assert(json.includes("act=ed_date"), "缺少修訂入口");
});

Deno.test("日期選擇卡", () => {
  const json = JSON.stringify(datePickCard("改時間", "副標", "act=et_d", "2026-08-25", "選日期"));
  assert(json.includes('"mode":"date"'), json);
  assert(json.includes("act=et_d"), json);
  assert(json.includes('"initial":"2026-08-25"'), json);
});

// --- 行程與請假分區 ----------------------------------------------------------

const MEETING = {
  id: "m1", summary: "兵力協調會", location: "第一會議室", isAllDay: false,
  startTime: "14:00", endTime: "15:00", reqType: "meeting" as const,
};
const LEAVE = {
  id: "l1", summary: "請假（陳彥名）", location: null, isAllDay: true,
  startTime: null, endTime: null, reqType: "leave" as const,
};
const NOTE = {
  id: "n1", seq: 3, source_type: "group" as const, group_id: "G", user_id: "U",
  scope_key: "g:G", content: "帶識別證", tags: [], target_date: "2026-08-25",
  created_at: "2026-08-23T00:00:00+00:00", deleted_at: null,
};

Deno.test("日檢視：行程與請假各自成區", () => {
  const out = renderDayView("2026-08-25", [MEETING, LEAVE], [NOTE]);
  assert(out.includes("【行程】"), out);
  assert(out.includes("【請假】"), out);
  assert(out.includes("【記事】"), out);
  // 請假不能混在行程區裡
  const 行程區 = out.slice(out.indexOf("【行程】"), out.indexOf("【請假】"));
  assert(行程區.includes("兵力協調會"), 行程區);
  assert(!行程區.includes("陳彥名"), "請假跑進行程區了");
  assert(out.includes("【請假】1 人"), "請假區要標人數");
});

Deno.test("日檢視：只查請假時不列行程與記事", () => {
  const out = renderDayView("2026-08-25", [MEETING, LEAVE], [NOTE], "leave");
  assert(out.includes("【請假】"), out);
  assert(!out.includes("【行程】"), out);
  assert(!out.includes("【記事】"), out);
  assert(out.includes("陳彥名"), out);
  assert(!out.includes("兵力協調會"), out);
});

Deno.test("日檢視：沒有人請假時講清楚", () => {
  const out = renderDayView("2026-08-25", [MEETING], [], "all");
  assert(out.includes("【請假】沒有人請假"), out);
});

Deno.test("日檢視：只查請假且真的沒人 → 空的一天", () => {
  const out = renderDayView("2026-08-25", [MEETING], [NOTE], "leave");
  assert(out.includes("沒有人請假"), out);
  assert(out.includes("這一天目前是空的"), out);
});

Deno.test("選單有「誰請假」快捷鍵，且帶 f=leave", () => {
  const json = JSON.stringify(mainMenuCard("2026-08-23", "2026-08-24"));
  assert(json.includes("act=day&f=leave&d=2026-08-23"), json);
  assert(json.includes("act=day&f=leave&d=2026-08-24"), json);
});

// --- 引導式請假 --------------------------------------------------------------

Deno.test("describeLeave：全天單日／跨多天／時段", () => {
  assertEquals(describeLeave("2026-08-29", "2026-08-29", null, null), "2026/08/29（週六） 全天");
  assertEquals(
    describeLeave("2026-08-29", "2026-08-31", null, null),
    "2026/08/29（週六） 至 2026/08/31（週一） 全天",
  );
  assertEquals(
    describeLeave("2026-08-29", "2026-08-29", "13:30", "17:30"),
    "2026/08/29（週六） 13:30-17:30",
  );
});

Deno.test("請假方式卡：三種方式加取消", () => {
  const json = JSON.stringify(leaveShapeCard("2026-08-29"));
  for (const a of ["act=lv_allday", "act=lv_timed", "act=lv_multi", "act=lv_cancel"]) {
    assert(json.includes(a), `缺少 ${a}`);
  }
  assert(json.includes("2026/08/29"), "要顯示選到的日期");
});

Deno.test("事由卡：可以不填，而且說清楚事由不會出現在標題", () => {
  const json = JSON.stringify(leaveReasonCard("2026/08/29（週六） 全天"));
  assert(json.includes("act=lv_skip"), "事由必須是選填");
  assert(json.includes("act=lv_cancel"), json);
  assert(json.includes("選填"), json);
  // 計畫書 §12.2：請假理由可能涉及個資，要讓使用者知道它會寫到哪裡
  assert(json.includes("說明欄"), "要說明事由寫進哪裡");
  assert(json.includes("請假（姓名）"), "要說明標題只顯示什麼");
});

Deno.test("選單有請假入口", () => {
  const json = JSON.stringify(mainMenuCard("2026-08-23", "2026-08-24"));
  assert(json.includes("act=lv_date"), "缺少請假入口");
});

// --- 取消請假 ----------------------------------------------------------------

Deno.test("挑選卡預設仍是修訂用（回歸保護）", () => {
  const json = JSON.stringify(eventPickerCarousel("2026-08-25", [MEETING]));
  assert(json.includes("act=ed_pick&e=m1"), json);
  assert(json.includes("選這筆"), json);
});

Deno.test("挑選卡可換成取消請假用", () => {
  const card = eventPickerCarousel("2026-08-29", [LEAVE], {
    action: "lv_del_pick", label: "取消這筆", verb: "取消請假",
  });
  const json = JSON.stringify(card);
  assert(json.includes("act=lv_del_pick&e=l1"), json);
  assert(json.includes("取消這筆"), json);
  assert(!json.includes("act=ed_pick"), "不該還帶著修訂的 postback");
  assertEquals(card.altText, "2026/08/29（週六） 有 1 筆，請選一筆取消請假");
});

Deno.test("選單有取消請假入口", () => {
  const json = JSON.stringify(mainMenuCard("2026-08-23", "2026-08-24"));
  assert(json.includes("act=lv_del_date"), "缺少取消請假入口");
  // 四個請假／行程入口彼此不能撞
  for (const a of ["act=lv_date", "act=lv_del_date", "act=new_date", "act=ed_date"]) {
    assert(json.includes(a), `缺少 ${a}`);
  }
});

Deno.test("取消請假的確認卡走破壞性樣式", () => {
  const json = JSON.stringify(editConfirmCard(
    "確認取消請假", "2026/08/29（週六） 全天", "請假（陳彥名）", true,
  ));
  assert(json.includes("將刪除"), json);
  assert(json.includes("act=ed_apply"), "要接回既有的套用流程");
  assert(json.includes("#c62828"), "破壞性操作要用紅色");
});
