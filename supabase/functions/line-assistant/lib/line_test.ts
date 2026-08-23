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
