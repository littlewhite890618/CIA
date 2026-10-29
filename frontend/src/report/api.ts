// 教練回饋頁與後端的溝通（app/web/report_api.py）。token 就是憑證，放在網址的 t 參數。

export type SessionStatus =
  | "ready"
  | "not_generated"
  | "analysis_pending"
  | "analysis_failed"
  | "generation_failed";

export interface Section {
  title: string;
  body: string;
}

export interface ReportSession {
  id: number;
  /** ISO 字串 */
  startedAt: string | null;
  endedAt: string | null;
  status: SessionStatus;
  sections: Section[];
}

export interface ReportData {
  personaName: string;
  /** 連結到期時間（ISO 字串） */
  expiresAt: string;
  /** 回到私訊的連結 */
  chatUrl: string;
  sessions: ReportSession[];
}

export type Failure = "expired" | "invalid" | "used" | "unavailable" | "network";

export type LoadResult = { ok: true; data: ReportData } | { ok: false; failure: Failure };
export type GenerateResult =
  | { ok: true; status: SessionStatus; sections: Section[] }
  | { ok: false; failure: Failure };

async function failureOf(resp: Response): Promise<Failure> {
  if (resp.status === 401) {
    const body = await resp.json().catch(() => ({}));
    return body.error === "expired" || body.error === "used" ? body.error : "invalid";
  }
  if (resp.status === 404) return "unavailable";
  return "network";
}

export async function loadReport(token: string): Promise<LoadResult> {
  try {
    const resp = await fetch(`/api/report?t=${encodeURIComponent(token)}`, { cache: "no-store" });
    if (!resp.ok) return { ok: false, failure: await failureOf(resp) };
    return { ok: true, data: (await resp.json()) as ReportData };
  } catch {
    return { ok: false, failure: "network" };
  }
}

export async function generateSession(token: string, sessionId: number): Promise<GenerateResult> {
  try {
    const resp = await fetch(
      `/api/report/sessions/${sessionId}/generate?t=${encodeURIComponent(token)}`,
      { method: "POST" },
    );
    if (!resp.ok) return { ok: false, failure: await failureOf(resp) };
    const body = await resp.json();
    return { ok: true, status: body.status, sections: body.sections ?? [] };
  } catch {
    return { ok: false, failure: "network" };
  }
}
