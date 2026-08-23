/**
 * 大小寫兼容的環境變數讀取。
 *
 * 計畫書 §7：Supabase 環境變數名稱區分大小寫，而既有專案已用混合大小寫設過
 * 幾個變數（LINE_UserID、LINE_Channel_access_token）。為避免重設造成中斷，
 * 一律先讀標準名稱，找不到再依序試舊別名。穩定後可刪掉別名。
 */

const ALIASES: Record<string, string[]> = {
  LINE_CHANNEL_ACCESS_TOKEN: ["LINE_Channel_access_token", "LINE_CHANNEL_ACCESSTOKEN"],
  LINE_CHANNEL_SECRET: ["LINE_Channel_secret"],
  LINE_CHANNEL_ID: ["LINE_Channel_id"],
  ADMIN_LINE_USER_ID: ["LINE_USER_ID", "LINE_UserID", "LINE_UserId"],
  GOOGLE_CALENDAR_ID: ["Google_Calendar_ID"],
  GOOGLE_SERVICE_ACCOUNT_JSON: ["Google_Service_Account_JSON"],
};

/** 讀取變數；標準名稱優先，其次別名。回傳 trim 過的值，空字串視同未設定。 */
export function env(name: string): string | undefined {
  for (const key of [name, ...(ALIASES[name] ?? [])]) {
    const raw = Deno.env.get(key);
    if (raw !== undefined && raw.trim() !== "") return raw.trim();
  }
  return undefined;
}

/** 讀取必要變數；缺少時丟出不含值的錯誤。 */
export function requireEnv(name: string): string {
  const value = env(name);
  if (value === undefined) throw new Error(`missing required env: ${name}`);
  return value;
}

/** 回報某個變數是否有值 —— 只回 boolean，永遠不回值本身（供 /diag 使用）。 */
export function hasEnv(name: string): boolean {
  return env(name) !== undefined;
}

export const config = {
  /** 緊急停用開關。設為 false 時仍驗簽並回 200，但不做任何寫入。 */
  get botEnabled(): boolean {
    return (env("BOT_ENABLED") ?? "true").toLowerCase() !== "false";
  },
  /** 待確認行程的有效時間（分鐘）。 */
  get pendingTtlMinutes(): number {
    const parsed = Number(env("PENDING_TTL_MINUTES"));
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 15;
  },
  /** 全系統統一時區。 */
  get timezone(): string {
    return env("DEFAULT_TIMEZONE") ?? "Asia/Taipei";
  },
};

/** 必要變數清單，供 /diag 逐項回報有無。 */
export const REQUIRED_ENV = [
  "LINE_CHANNEL_SECRET",
  "LINE_CHANNEL_ACCESS_TOKEN",
  "GOOGLE_SERVICE_ACCOUNT_JSON",
  "GOOGLE_CALENDAR_ID",
  "ADMIN_LINE_USER_ID",
] as const;

export const OPTIONAL_ENV = [
  "BOT_ENABLED",
  "PENDING_TTL_MINUTES",
  "DEFAULT_TIMEZONE",
  "LINE_CHANNEL_ID",
] as const;
