// 教練回饋頁：每段對話是一張疊起來的信紙（daily-coach-report-link 任務 7.1、7.2）。
// 打開連結＝表示要看，這時才逐段產生回饋；一段寫好才寫下一段，不同時打出好幾個請求。

import { Fragment, useEffect, useRef, useState } from "react";
import { MessageCircle, PenLine, RotateCcw } from "lucide-react";

import { generateSession, loadReport, type Failure, type ReportData, type ReportSession, type Section } from "./api";
import { expiryNote, letterDate, letterTime } from "./format";
import { sampleReport } from "./sampleData";

const INBOX_URL = "https://www.instagram.com/direct/inbox/";
// 示範句的段落：「」裡是建議的新說法，不是使用者說過的話
const REWRITE_TITLE = "換個說法試試";

/** 畫面上的狀態：伺服器的狀態，加上「正在寫」 */
type LetterStatus = ReportSession["status"] | "writing";
type Letter = Omit<ReportSession, "status"> & { status: LetterStatus; fresh?: boolean };

const needsWriting = (status: LetterStatus) => status === "not_generated" || status === "generation_failed";

export function ReportPage() {
  const params = new URLSearchParams(location.search);
  const token = params.get("t");
  const demo = import.meta.env.DEV && !token;
  const demoState = demo ? params.get("state") : null;

  const [data, setData] = useState<ReportData | null>(null);
  const [letters, setLetters] = useState<Letter[]>([]);
  const [failure, setFailure] = useState<Failure | null>(
    demoState === "expired" || demoState === "used" || demoState === "invalid" ? demoState : !demo && !token ? "invalid" : null,
  );
  const busy = useRef(false);
  const queue = useRef<number[]>([]);

  const patch = (id: number, change: Partial<Letter>) =>
    setLetters((all) => all.map((l) => (l.id === id ? { ...l, ...change } : l)));

  async function write(id: number) {
    patch(id, { status: "writing" });
    if (demo) {
      await new Promise((r) => setTimeout(r, demoState === "writing" ? 600_000 : 2400));
      if (demoState === "failed") return patch(id, { status: "generation_failed" });
      return patch(id, { status: "ready", sections: sampleReport().sessions[0].sections, fresh: true });
    }
    const result = await generateSession(token!, id);
    if (!result.ok) {
      if (result.failure === "network") return patch(id, { status: "generation_failed" });
      return setFailure(result.failure);
    }
    patch(id, { status: result.status, sections: result.sections, fresh: result.status === "ready" });
  }

  /** 排進佇列並依序寫；已經在寫的時候只排隊，由正在跑的迴圈接著處理 */
  async function enqueue(ids: number[]) {
    for (const id of ids) if (!queue.current.includes(id)) queue.current.push(id);
    if (busy.current) return;
    busy.current = true;
    try {
      while (queue.current.length) await write(queue.current.shift()!);
    } finally {
      busy.current = false;
    }
  }

  useEffect(() => {
    if (failure) return;
    let alive = true;
    const loaded = demo ? Promise.resolve({ ok: true as const, data: sampleReport() }) : loadReport(token!);
    loaded.then((result) => {
      if (!alive) return;
      if (!result.ok) return setFailure(result.failure);
      const list: Letter[] = result.data.sessions.map((s) =>
        demoState === "pending" && s.status !== "ready" ? { ...s, status: "analysis_pending" } : s,
      );
      setData(result.data);
      setLetters(list);
      enqueue(list.filter((l) => needsWriting(l.status)).map((l) => l.id));
    });
    return () => {
      alive = false;
    };
    // 只在打開頁面時載入一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (failure) return <LinkProblem kind={failure} />;
  if (!data) {
    return (
      <div className="sheet">
        <main className="notice-page" aria-busy="true">
          <p className="notice-text">正在打開你的回饋…</p>
        </main>
      </div>
    );
  }

  return (
    <div className="letters">
      <header className="cover letters-cover">
        <div className="cover-text">
          <h1 className="cover-title">{data.personaName}的聊天回饋</h1>
          <p className="cover-timer">
            {letters.length} 段對話・{expiryNote(data.expiresAt)}
          </p>
        </div>
        <a className="button button-cover button-cover-quiet" href={data.chatUrl}>
          <MessageCircle aria-hidden size={17} strokeWidth={2} />
          回到聊天
        </a>
      </header>

      <main className="letter-stack">
        {letters.map((letter) => (
          <LetterSheet
            key={letter.id}
            letter={letter}
            onRetry={() => {
              patch(letter.id, { status: "not_generated" }); // 立刻顯示「排隊中」
              enqueue([letter.id]);
            }}
          />
        ))}

        <footer className="letters-close">
          <p className="letters-close-text">讀完了，回到私訊繼續聊吧。</p>
          <a className="button" href={data.chatUrl}>
            <MessageCircle aria-hidden size={18} strokeWidth={2} />
            回到聊天
          </a>
        </footer>
      </main>
    </div>
  );
}

function LetterSheet({ letter, onRetry }: { letter: Letter; onRetry: () => void }) {
  return (
    <article className="letter" aria-busy={letter.status === "writing"}>
      <header className="letterhead">
        <h2 className="letter-date">
          <span>{letterDate(letter.startedAt)}</span>{" "}
          <span className="letter-time">{letterTime(letter.startedAt, letter.endedAt)}</span>
        </h2>
      </header>

      {letter.status === "ready" && (
        <div className={letter.fresh ? "letter-body is-fresh" : "letter-body"}>
          {letter.sections.map((section, i) => (
            <LetterSection key={section.title} section={section} order={i} />
          ))}
        </div>
      )}

      {letter.status === "writing" && (
        <div className="letter-writing" role="status">
          <p className="letter-note">正在整理這段的回饋，大約要十秒…</p>
          <BlankLines writing />
        </div>
      )}

      {letter.status === "not_generated" && (
        <div className="letter-queued">
          <p className="letter-note">等上一段整理好，就會換這一段。</p>
          <BlankLines />
        </div>
      )}

      {letter.status === "analysis_pending" && (
        <p className="letter-note">這段對話的分析還沒完成，晚一點再打開這個連結看看。</p>
      )}

      {letter.status === "analysis_failed" && (
        <p className="letter-note">這段對話沒辦法整理出回饋，其他段落不受影響。</p>
      )}

      {letter.status === "generation_failed" && (
        <div className="letter-retry">
          <p className="letter-note">這段沒有整理成功。</p>
          <button type="button" className="button button-ghost button-small" onClick={onRetry}>
            <RotateCcw aria-hidden size={15} strokeWidth={2} />
            再試一次
          </button>
        </div>
      )}
    </article>
  );
}

/** 空白橫線。writing 時藍筆一行一行寫過去（筆跡是一條手寫的波浪線），減少動態時只留橫線 */
function BlankLines({ writing = false }: { writing?: boolean }) {
  return (
    <div className={writing ? "blank-lines is-writing" : "blank-lines"} aria-hidden>
      {[0, 1, 2, 3, 4].map((i) => (
        <span key={i} className="blank-line" style={{ "--i": i } as React.CSSProperties}>
          {writing && (
            <svg className="pen-stroke" viewBox="0 0 300 12" preserveAspectRatio="none">
              <path
                pathLength={1}
                d={i % 2 ? STROKE_SHORT : STROKE_LONG}
              />
            </svg>
          )}
        </span>
      ))}
    </div>
  );
}

// 兩種長短的筆跡，行尾參差，看起來像手寫的一行字
const STROKE_LONG = "M2 8 C 22 3, 38 11, 60 7 S 98 3, 122 8 S 168 11, 192 6 S 238 4, 262 8 S 286 9, 296 7";
const STROKE_SHORT = "M2 7 C 26 11, 44 3, 70 8 S 112 11, 140 6 S 182 3, 206 8 S 226 9, 232 7";

function LetterSection({ section, order }: { section: Section; order: number }) {
  const rewrite = section.title === REWRITE_TITLE;
  const paragraphs = section.body.split("\n").filter((p) => p.trim());
  return (
    <section className="letter-section" style={{ "--order": order } as React.CSSProperties}>
      <h3 className="letter-section-title">{section.title}</h3>
      {paragraphs.map((p, i) => (
        <p key={i} className="letter-text">
          <Quoted text={p} rewrite={rewrite} />
        </p>
      ))}
    </section>
  );
}

/** 「」裡的句子用藍色原子筆：那是使用者自己說過的話（設定頁「藍筆＝你寫的」）。
 *  示範段落裡的「」是建議的新說法，另外畫上淺藍螢光筆。 */
function Quoted({ text, rewrite }: { text: string; rewrite: boolean }) {
  return (
    <>
      {text.split(/(「[^」]*」)/).map((part, i) =>
        part.startsWith("「") && part.endsWith("」") ? (
          <span key={i} className={rewrite ? "said said-new" : "said"}>
            {part}
          </span>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </>
  );
}

const PROBLEM_TEXT: Record<Failure, { title: string; body: string }> = {
  expired: {
    title: "這份回饋已經過期",
    body: "回饋連結只有 7 天有效。之後聊完的對話，隔天凌晨會再收到新的連結。",
  },
  used: {
    title: "這個連結已經在別的地方打開過了",
    body: "為了保護你的對話內容，每個連結只能在第一次打開它的裝置上看。請回到那支手機或那個瀏覽器再打開一次。",
  },
  invalid: {
    title: "這個連結打不開",
    body: "連結可能沒有複製完整。回到私訊，從原本那則訊息再點一次。",
  },
  unavailable: {
    title: "回饋功能目前沒有開放",
    body: "這段時間看不到回饋，你還是可以照常聊天。",
  },
  network: {
    title: "連不上伺服器",
    body: "確認網路沒問題後，重新整理這一頁再試一次。",
  },
};

function LinkProblem({ kind }: { kind: Failure }) {
  const text = PROBLEM_TEXT[kind];
  return (
    <div className="sheet">
      <main className="notice-page">
        <PenLine aria-hidden size={28} strokeWidth={2.25} className="notice-icon notice-icon-ok" />
        <h1 className="page-title">{text.title}</h1>
        <p className="notice-text">{text.body}</p>
        <div className="notice-actions">
          {kind === "network" ? (
            <button type="button" className="button" onClick={() => location.reload()}>
              重新整理
            </button>
          ) : (
            <a className="button" href={INBOX_URL}>
              <MessageCircle aria-hidden size={18} strokeWidth={2} />
              回到聊天
            </a>
          )}
        </div>
      </main>
    </div>
  );
}
