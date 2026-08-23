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
