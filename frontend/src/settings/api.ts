// 設定頁與後端的溝通（app/web/settings_api.py）。token 就是憑證，放在網址的 t 參數。

import type { Card, Coaching, SettingsData } from "./types";

export type Failure = "expired" | "invalid" | "used" | "unavailable" | "network";

export type LoadResult = { ok: true; data: SettingsData } | { ok: false; failure: Failure };
export type SaveResult =
  | { ok: true }
  | { ok: false; failure: Failure }
  | { ok: false; errors: Record<string, string> };

function url(token: string) {
  return `/api/settings?t=${encodeURIComponent(token)}`;
}

async function failureOf(resp: Response): Promise<Failure> {
  if (resp.status === 401) {
    const body = await resp.json().catch(() => ({}));
    return body.error === "expired" || body.error === "used" ? body.error : "invalid";
  }
  if (resp.status === 404) return "unavailable";
  return "network";
}

export async function loadSettings(token: string): Promise<LoadResult> {
  try {
    const resp = await fetch(url(token), { cache: "no-store" });
    if (!resp.ok) return { ok: false, failure: await failureOf(resp) };
    return { ok: true, data: (await resp.json()) as SettingsData };
  } catch {
    return { ok: false, failure: "network" };
  }
}

export async function saveSettings(token: string, card: Card, coaching: Coaching): Promise<SaveResult> {
  try {
    const resp = await fetch(url(token), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card, coaching }),
    });
    if (resp.ok) return { ok: true };
    if (resp.status === 422) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, errors: body.errors ?? { card: "儲存失敗，請再試一次。" } };
    }
    return { ok: false, failure: await failureOf(resp) };
  } catch {
    return { ok: false, failure: "network" };
  }
}

export async function resetSettings(token: string): Promise<SaveResult> {
  try {
    const resp = await fetch(url(token), { method: "DELETE" });
    if (resp.ok) return { ok: true };
    return { ok: false, failure: await failureOf(resp) };
  } catch {
    return { ok: false, failure: "network" };
  }
}
