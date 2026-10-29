// 日期與時段一律用台灣時間顯示：使用者在台灣聊天，手機時區不一定設對。

const TZ = "Asia/Taipei";
const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

const FORMAT = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ,
  month: "numeric",
  day: "numeric",
  weekday: "short",
  hour: "numeric",
  minute: "numeric",
  hourCycle: "h23",
});
const WEEKDAY_EN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function parts(iso: string) {
  const found = Object.fromEntries(FORMAT.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return {
    month: Number(found.month),
    day: Number(found.day),
    hour: Number(found.hour) % 24,
    minute: Number(found.minute),
    weekday: WEEKDAY_EN.indexOf(found.weekday),
  };
}

/** 「10 月 21 日（二）」 */
export function letterDate(iso: string | null): string {
  if (!iso) return "";
  const p = parts(iso);
  return `${p.month} 月 ${p.day} 日（${WEEKDAYS[p.weekday]}）`;
}

function period(hour: number): string {
  if (hour < 5) return "凌晨";
  if (hour < 11) return "早上";
  if (hour < 13) return "中午";
  if (hour < 18) return "下午";
  return "晚上";
}

function clock(hour: number, minute: number): string {
  const h = hour % 12 === 0 && hour >= 12 ? 12 : hour % 12;
  return `${h}:${String(minute).padStart(2, "0")}`;
}

/** 「晚上 9:40–10:15」；跨到另一個時段時兩邊都寫，例如「晚上 11:40–凌晨 0:15」 */
export function letterTime(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "";
  const s = parts(startIso);
  const start = `${period(s.hour)} ${clock(s.hour, s.minute)}`;
  if (!endIso) return start;
  const e = parts(endIso);
  const endClock = clock(e.hour, e.minute);
  return period(e.hour) === period(s.hour) ? `${start}–${endClock}` : `${start}–${period(e.hour)} ${endClock}`;
}

/** 封面帶的到期說明 */
export function expiryNote(expiresAt: string, now = Date.now()): string {
  const days = Math.floor((new Date(expiresAt).getTime() - now) / 86_400_000);
  if (days >= 1) return `連結 ${days} 天後到期`;
  return "連結今天到期";
}
