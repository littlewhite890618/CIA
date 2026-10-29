// 使用者設定頁：替角色填的同學錄。九個區段是九頁，一頁一頁往下填，
// 也可以從頁籤直接跳頁；任何時候都能儲存。
// 設計合約見 .impeccable/surfaces/frontend-src-settings-settingspage-tsx.md。

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, MessageCircle, PenLine, RotateCcw } from "lucide-react";

import { loadSettings, resetSettings, saveSettings, type Failure } from "./api";
import { ReportSectionsPage, ReportStylePage, RulesPage } from "./coaching";
import { ListField, NumberField, ReadonlyField, SelectField, TextField } from "./fields";
import { sampleSettings } from "./sampleData";
import {
  GENERIC_RECENT_LIFE,
  PAGES,
  RECENT_LIFE_BY_OCCUPATION,
  type FieldDef,
  type PageDef,
} from "./schema";
import type { Card, CardValue, Coaching, SettingsData } from "./types";

type Status = "ready" | "saved" | "expired" | "invalid" | "used";

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));

function getValue(card: Card, path: string): CardValue {
  const [section, key] = path.split(".");
  return card[section]?.[key] ?? "";
}

function setValue(card: Card, path: string, value: CardValue): Card {
  const [section, key] = path.split(".");
  return { ...card, [section]: { ...card[section], [key]: value } };
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

const PRESET_RECENT_LIVES = new Set([...Object.values(RECENT_LIFE_BY_OCCUPATION), GENERIC_RECENT_LIFE]);

/** 換職業時，近況若還是系統提供的句子就換成新職業的；使用者自己寫的近況不動 */
function followOccupation(card: Card, occupation: string): Card {
  const current = getValue(card, "background.recent_life") as string;
  if (current.trim() && !PRESET_RECENT_LIVES.has(current.trim())) return card;
  return setValue(card, "background.recent_life", RECENT_LIFE_BY_OCCUPATION[occupation] ?? GENERIC_RECENT_LIFE);
}

// ── 驗證：與後端保存時的驗證一致，後端仍會再驗一次 ───────

function validate(card: Card, coaching: Coaching): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const page of PAGES) {
    for (const field of page.fields ?? []) {
      if (field.kind === "readonly") continue;
      const value = getValue(card, field.path);
      if (Array.isArray(value)) {
        if (value.length === 0) errors[field.path] = "至少留一項。";
      } else if (!value.trim()) {
        errors[field.path] = "這一欄要填。";
      } else if (field.path === "identity.age" && !(Number(value) >= 1 && Number(value) <= 99)) {
        errors[field.path] = "寫一個 1 到 99 之間的數字。";
      } else if (field.path === "language_style.typo_rate" && !(Number(value) >= 0 && Number(value) <= 1)) {
        errors[field.path] = "寫一個 0 到 1 之間的數字，例如 0.03。";
      } else if (field.numeric && field.path.startsWith("runtime.") && !(Number(value) > 0)) {
        errors[field.path] = "寫一個大於 0 的秒數。";
      }
    }
  }
  const min = Number(getValue(card, "runtime.delay_min"));
  const max = Number(getValue(card, "runtime.delay_max"));
  if (!errors["runtime.delay_min"] && !errors["runtime.delay_max"] && min > max) {
    errors["runtime.delay_min"] = "最快不能比最慢還慢。";
  }
  if (coaching.reportSections.length === 0) errors["coaching.reportSections"] = "至少留一個段落。";
  return errors;
}

function pageOfError(path: string): number {
  const byKind = (kind: PageDef["kind"]) => PAGES.findIndex((p) => p.kind === kind);
  if (path === "coaching.reportSections") return byKind("reportSections");
  if (path === "coaching.customRules" || path === "coaching.disabledRuleIds") return byKind("rules");
  if (path.startsWith("coaching.")) return byKind("reportStyle");
  return Math.max(0, PAGES.findIndex((p) => p.fields?.some((f) => f.path === path)));
}

function minutesLeft(expiresAt: string): number {
  return Math.max(0, Math.ceil((new Date(expiresAt).getTime() - Date.now()) / 60000));
}

// 連結打不開時還不知道角色的 IG 帳號，先帶到 Instagram 收件匣
const INBOX_URL = "https://www.instagram.com/direct/inbox/";

// ── 頁面 ──────────────────────────────────────────

export function SettingsPage() {
  const params = new URLSearchParams(location.search);
  const token = params.get("t");
  // 開發模式沒有 token 時用範例資料檢視設計；?state=expired|invalid|saved 檢視各狀態
  const demo = import.meta.env.DEV && !token;
  const [load, setLoad] = useState<{ data: SettingsData } | { failure: Failure; keyword?: string } | null>(() =>
    demo ? { data: sampleSettings() } : token ? null : { failure: "invalid" },
  );

  useEffect(() => {
    if (demo || !token) return;
    let alive = true;
    loadSettings(token).then((result) => {
      if (alive) setLoad(result.ok ? { data: result.data } : { failure: result.failure, keyword: result.keyword });
    });
    return () => {
      alive = false;
    };
  }, [demo, token]);

  if (load === null) {
    return (
      <Sheet>
        <main className="notice-page" aria-busy="true">
          <p className="notice-text">載入中…</p>
        </main>
      </Sheet>
    );
  }
  if ("failure" in load) return <LinkProblem kind={load.failure} chatUrl={INBOX_URL} keyword={load.keyword} />;

  const demoState = demo ? params.get("state") : null;
  return (
    <Editor
      data={load.data}
      token={token}
      initialStatus={demoState === "expired" || demoState === "invalid" || demoState === "saved" ? demoState : "ready"}
    />
  );
}

function Editor({ data, token, initialStatus }: { data: SettingsData; token: string | null; initialStatus: Status }) {
  const [status, setStatus] = useState<Status>(initialStatus);
  const [done, setDone] = useState<"saved" | "reset">("saved");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [card, setCard] = useState<Card>(() => clone(data.current.card));
  const [coaching, setCoaching] = useState<Coaching>(() => clone(data.current.coaching));
  const [saved, setSaved] = useState(() => clone(data.current));
  const [pageIndex, setPageIndex] = useState(0);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmReset, setConfirmReset] = useState(false);
  const [minutes, setMinutes] = useState(() => minutesLeft(data.expiresAt));

  const page = PAGES[pageIndex];
  const isLast = pageIndex === PAGES.length - 1;
  const unsaved = !same(card, saved.card) || !same(coaching, saved.coaching);
  const tabsRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setMinutes(minutesLeft(data.expiresAt)), 30_000);
    return () => window.clearInterval(timer);
  }, [data.expiresAt]);

  useEffect(() => {
    window.scrollTo({ top: 0 });
    tabsRef.current
      ?.querySelector<HTMLElement>('[aria-current="page"]')
      ?.scrollIntoView({ inline: "center", block: "nearest" });
  }, [pageIndex]);

  const changedPages = useMemo(() => {
    return new Set(
      PAGES.filter((p) => {
        if (p.kind === "fields") {
          return p.fields!.some(
            (f) => f.kind !== "readonly" && !same(getValue(card, f.path), getValue(data.defaults.card, f.path)),
          );
        }
        if (p.kind === "rules") {
          return coaching.disabledRuleIds.length > 0 || coaching.customRules.length > 0;
        }
        if (p.kind === "reportStyle") {
          return (
            coaching.reportTone !== data.defaults.coaching.reportTone ||
            coaching.reportLength !== data.defaults.coaching.reportLength
          );
        }
        return !same(coaching.reportSections, data.defaults.coaching.reportSections);
      }).map((p) => p.id),
    );
  }, [card, coaching, data.defaults]);

  const errorPages = useMemo(
    () => new Set(Object.keys(errors).map((path) => PAGES[pageOfError(path)]?.id)),
    [errors],
  );

  const updateCard = (path: string, value: CardValue) => {
    setCard((c) => {
      const next = setValue(c, path, value);
      return path === "identity.occupation" ? followOccupation(next, value as string) : next;
    });
    if (errors[path]) setErrors(({ [path]: _gone, ...rest }) => rest);
  };

  const updateCoaching = (next: Coaching) => {
    setCoaching(next);
    if (Object.keys(errors).some((k) => k.startsWith("coaching."))) {
      setErrors((e) => Object.fromEntries(Object.entries(e).filter(([k]) => !k.startsWith("coaching."))));
    }
  };

  const showErrors = (found: Record<string, string>) => {
    setErrors(found);
    const first = Object.keys(found)[0];
    if (first) setPageIndex(pageOfError(first));
  };

  const handleFailure = (failure: Failure) => {
    if (failure === "expired" || failure === "invalid" || failure === "used") setStatus(failure);
    else setNotice("儲存失敗：連不上伺服器。確認網路後再按一次儲存，你的修改還在。");
  };

  const save = async () => {
    setNotice(null);
    const found = validate(card, coaching);
    if (Object.keys(found).length > 0) return showErrors(found);
    setErrors({});

    if (token) {
      setBusy(true);
      const result = await saveSettings(token, card, coaching);
      setBusy(false);
      if (!result.ok) {
        if ("errors" in result) return showErrors(result.errors);
        return handleFailure(result.failure);
      }
    }
    setSaved({ card: clone(card), coaching: clone(coaching) });
    setDone("saved");
    setStatus("saved");
  };

  const resetAll = async () => {
    setNotice(null);
    if (token) {
      setBusy(true);
      const result = await resetSettings(token);
      setBusy(false);
      if (!result.ok) {
        setConfirmReset(false);
        if ("failure" in result) return handleFailure(result.failure);
        return;
      }
    }
    setCard(clone(data.defaults.card));
    setCoaching(clone(data.defaults.coaching));
    setSaved(clone(data.defaults));
    setErrors({});
    setConfirmReset(false);
    setDone("reset");
    setStatus("saved");
  };

  if (status === "expired" || status === "invalid" || status === "used") {
    return <LinkProblem kind={status} chatUrl={data.chatUrl} keyword={data.keyword} />;
  }

  if (status === "saved") {
    return (
      <Sheet>
        <main className="notice-page">
          <Check aria-hidden size={28} strokeWidth={2.25} className="notice-icon notice-icon-ok" />
          <h1 className="page-title">{done === "reset" ? "已恢復預設" : "已儲存"}</h1>
          <p className="notice-text">
            {done === "reset"
              ? `從你傳的下一則訊息開始，${data.personaName}會用原本的設定回覆你。`
              : `從你傳的下一則訊息開始，${data.personaName}會照新的設定回覆你。`}
          </p>
          <div className="notice-actions">
            <a className="button" href={data.chatUrl}>
              <MessageCircle aria-hidden size={18} strokeWidth={2} />
              回到聊天
            </a>
            <button type="button" className="button button-ghost" onClick={() => setStatus("ready")}>
              繼續修改
            </button>
          </div>
        </main>
      </Sheet>
    );
  }

  const linkExpired = minutes === 0;

  return (
    <Sheet>
      <header className="cover">
        <div className="cover-text">
          <h1 className="cover-title">{data.personaName}的設定</h1>
          <p className={minutes <= 5 ? "cover-timer is-urgent" : "cover-timer"}>
            {linkExpired ? "連結已過期" : `連結 ${minutes} 分鐘後失效`}
          </p>
        </div>
        <button type="button" className="button button-cover" onClick={save} disabled={linkExpired || busy}>
          {busy ? "儲存中…" : "儲存"}
          {unsaved && !busy && <span className="unsaved-dot" aria-label="有尚未儲存的修改" />}
        </button>
      </header>

      {linkExpired && (
        <p className="banner" role="alert">
          <PenLine aria-hidden size={16} strokeWidth={2} />
          連結已過期，這次的修改存不了。回到私訊再傳一次「{data.keyword}」，拿新的連結。
        </p>
      )}
      {(notice || errors.card) && (
        <p className="banner" role="alert">
          <PenLine aria-hidden size={16} strokeWidth={2} />
          {notice ?? errors.card}
        </p>
      )}

      <nav className="tabs" aria-label="設定頁" ref={tabsRef}>
        {PAGES.map((p, i) => (
          <button
            key={p.id}
            type="button"
            className="tab"
            aria-current={i === pageIndex ? "page" : undefined}
            onClick={() => setPageIndex(i)}
          >
            {p.title}
            {errorPages.has(p.id) ? (
              <span className="tab-mark tab-mark-error" aria-label="有要修正的欄位" />
            ) : changedPages.has(p.id) ? (
              <span className="tab-mark" aria-label="改過" />
            ) : null}
          </button>
        ))}
      </nav>

      <main className="page" key={page.id}>
        <div className="page-head">
          <h2 className="page-title">{page.title}</h2>
          <p className="page-meta">
            第 {pageIndex + 1} 頁／共 {PAGES.length} 頁
          </p>
          <p className="page-desc">{page.description}</p>
        </div>

        <PageBody
          page={page}
          card={card}
          defaults={data.defaults.card}
          baseline={data.baselineNeverEngages}
          coaching={coaching}
          defaultCoaching={data.defaults.coaching}
          errors={errors}
          onCard={updateCard}
          onCoaching={updateCoaching}
        />

        {isLast && (
          <div className="reset-zone">
            {confirmReset ? (
              <div className="reset-confirm" role="group" aria-label="確認恢復預設">
                <p>九頁的設定都會回到預設，包括你改過的回饋方式。確定嗎？</p>
                <div className="inline-form-actions">
                  <button
                    type="button"
                    className="button button-danger button-small"
                    onClick={resetAll}
                    disabled={busy}
                  >
                    全部恢復預設
                  </button>
                  <button type="button" className="text-button" onClick={() => setConfirmReset(false)}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <button type="button" className="text-button" onClick={() => setConfirmReset(true)}>
                <RotateCcw aria-hidden size={14} strokeWidth={2} />
                全部恢復預設
              </button>
            )}
          </div>
        )}
      </main>

      <footer className="pager">
        <button
          type="button"
          className="button button-ghost"
          onClick={() => setPageIndex((i) => i - 1)}
          disabled={pageIndex === 0}
        >
          <ArrowLeft aria-hidden size={18} strokeWidth={2} />
          上一頁
        </button>
        {isLast ? (
          <button type="button" className="button" onClick={save} disabled={linkExpired || busy}>
            {busy ? "儲存中…" : "儲存設定"}
          </button>
        ) : (
          <button type="button" className="button" onClick={() => setPageIndex((i) => i + 1)}>
            下一頁
            <ArrowRight aria-hidden size={18} strokeWidth={2} />
          </button>
        )}
      </footer>
    </Sheet>
  );
}

function PageBody(props: {
  page: PageDef;
  card: Card;
  defaults: Card;
  baseline: string[];
  coaching: Coaching;
  defaultCoaching: Coaching;
  errors: Record<string, string>;
  onCard: (path: string, value: CardValue) => void;
  onCoaching: (coaching: Coaching) => void;
}) {
  const { page, card, defaults, baseline, coaching, defaultCoaching, errors, onCard, onCoaching } = props;
  const coachingProps = { coaching, defaults: defaultCoaching, errors, onChange: onCoaching };

  if (page.kind === "rules") return <RulesPage {...coachingProps} />;
  if (page.kind === "reportStyle") return <ReportStylePage {...coachingProps} />;
  if (page.kind === "reportSections") return <ReportSectionsPage {...coachingProps} />;

  return (
    <>
      {page.fields!.map((def: FieldDef) => {
        const value = getValue(card, def.path);
        const defaultValue = getValue(defaults, def.path);
        const common = { def, error: errors[def.path] };
        switch (def.kind) {
          case "readonly":
            return <ReadonlyField key={def.path} def={def} value={value as string} />;
          case "number":
            return (
              <NumberField
                key={def.path}
                {...common}
                value={value as string}
                defaultValue={defaultValue as string}
                onChange={(v) => onCard(def.path, v)}
              />
            );
          case "select":
            return (
              <SelectField
                key={def.path}
                {...common}
                value={value as string}
                defaultValue={defaultValue as string}
                onChange={(v) => onCard(def.path, v)}
              />
            );
          case "text":
            return (
              <TextField
                key={def.path}
                {...common}
                value={value as string}
                defaultValue={defaultValue as string}
                onChange={(v) => onCard(def.path, v)}
              />
            );
          case "list":
            return (
              <ListField
                key={def.path}
                {...common}
                items={value as string[]}
                defaultItems={defaultValue as string[]}
                locked={def.path === "boundaries.never_engages" ? baseline : []}
                onChange={(v) => onCard(def.path, v)}
              />
            );
        }
      })}
    </>
  );
}

function Sheet({ children }: { children: React.ReactNode }) {
  return <div className="sheet">{children}</div>;
}

// 關鍵字由後端提供；連結無效時後端不會告訴我們（避免亂打一串就能問出關鍵字），只能說「重新要一次連結」
const askAgain = (keyword?: string) =>
  keyword ? `回到私訊再傳一次「${keyword}」，就會拿到新的連結。` : "回到私訊重新要一次連結就好。";

const problemText = (kind: Failure, keyword?: string): { title: string; body: string } =>
  ({
    expired: {
      title: "這個連結已經過期",
      body: `設定連結只有 30 分鐘有效。${askAgain(keyword)}`,
    },
    used: {
      title: "這個連結已經在別的地方打開過了",
      body: `為了保護你的設定，每個連結只能在打開它的那個裝置上使用。${askAgain(keyword)}`,
    },
    invalid: {
      title: "這個連結打不開",
      // 無效時後端不給關鍵字；最常見的原因是連結被截斷，從原本那則訊息再點一次就好
      body: keyword ? `連結可能沒有複製完整。${askAgain(keyword)}` : "連結可能沒有複製完整。回到私訊，從原本那則訊息再點一次。",
    },
    unavailable: {
      title: "設定功能目前沒有開放",
      body: "這段時間沒辦法調整角色的設定，你還是可以照常聊天。",
    },
    network: {
      title: "連不上伺服器",
      body: "確認網路沒問題後，重新整理這一頁再試一次。",
    },
  })[kind];

function LinkProblem({ kind, chatUrl, keyword }: { kind: Failure; chatUrl: string; keyword?: string }) {
  const text = problemText(kind, keyword);
  return (
    <Sheet>
      <main className="notice-page">
        <PenLine aria-hidden size={28} strokeWidth={2.25} className="notice-icon notice-icon-redpen" />
        <h1 className="page-title">{text.title}</h1>
        <p className="notice-text">{text.body}</p>
        <div className="notice-actions">
          {kind === "network" ? (
            <button type="button" className="button" onClick={() => location.reload()}>
              重新整理
            </button>
          ) : (
            <a className="button" href={chatUrl}>
              <MessageCircle aria-hidden size={18} strokeWidth={2} />
              回到聊天
            </a>
          )}
        </div>
      </main>
    </Sheet>
  );
}
