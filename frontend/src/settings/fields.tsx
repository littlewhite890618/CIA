// 欄位元件。世界觀：印好的題目是黑色印刷字，使用者的值是原子筆藍、寫在橫線上；
// 跟平台預設不同的值，底下有一道螢光筆；錯誤是紅筆批改。
// 控制項一律是標準元件（原生 select、input、textarea、button），世界只決定外觀。

import { useId, useState } from "react";
import { ChevronDown, Lock, PenLine, Plus, RotateCcw, X } from "lucide-react";

import { CUSTOM_OPTION, type FieldDef } from "./schema";

function displayOf(def: FieldDef, value: string): string {
  return def.optionLabels?.[value] ?? value;
}

/** 使用者寫的值；改過（與預設不同）時螢光筆畫過它 */
export function Ink({ text, changed }: { text: string; changed: boolean }) {
  return <span className={changed ? "ink is-changed" : "ink"}>{text}</span>;
}

function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <p className="field-error" role="alert">
      <PenLine aria-hidden size={14} strokeWidth={2} />
      {message}
    </p>
  );
}

function DefaultNote({ text, onRestore }: { text: string; onRestore: () => void }) {
  return (
    <p className="default-note">
      <span className="default-note-text">預設：{text}</span>
      <button type="button" className="text-button" onClick={onRestore}>
        <RotateCcw aria-hidden size={13} strokeWidth={2} />
        還原
      </button>
    </p>
  );
}

// ── 唯讀（名字、IG 顯示名稱）────────────────────────

export function ReadonlyField({ def, value }: { def: FieldDef; value: string }) {
  return (
    <div className="field">
      <span className="field-label">{def.label}</span>
      <p className="printed">
        {value}
        <Lock aria-label="不能修改" size={14} strokeWidth={2} className="printed-lock" />
      </p>
    </div>
  );
}

// ── 數字（年齡）──────────────────────────────────

interface ValueProps {
  def: FieldDef;
  value: string;
  defaultValue: string;
  error?: string;
  onChange: (value: string) => void;
}

export function NumberField({ def, value, defaultValue, error, onChange }: ValueProps) {
  const id = useId();
  const changed = value !== defaultValue;
  return (
    <div className="field" data-error={error ? "" : undefined}>
      <label className="field-label" htmlFor={id}>
        {def.label}
      </label>
      <input
        id={id}
        className={changed ? "line-input is-changed" : "line-input"}
        type="text"
        inputMode="numeric"
        pattern="[0-9]*"
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/[^0-9]/g, ""))}
        aria-invalid={error ? true : undefined}
      />
      {changed && <DefaultNote text={defaultValue} onRestore={() => onChange(defaultValue)} />}
      <FieldError message={error} />
    </div>
  );
}

// ── 下拉選單（最後一項為自訂）──────────────────────

export function SelectField({ def, value, defaultValue, error, onChange }: ValueProps) {
  const id = useId();
  const options = def.options ?? [];
  const isPreset = options.includes(value);
  // 選了「自訂」但還沒輸入時，value 是空字串；用本地狀態記住目前在自訂模式
  const [customMode, setCustomMode] = useState(!isPreset);
  const custom = customMode || !isPreset;
  const changed = value !== defaultValue;

  return (
    <div className="field" data-error={error ? "" : undefined}>
      <label className="field-label" htmlFor={id}>
        {def.label}
      </label>
      <div className="pick">
        <span className="pick-value">
          {custom ? (
            <span className="pick-placeholder">自訂…</span>
          ) : (
            <Ink text={displayOf(def, value)} changed={changed} />
          )}
        </span>
        <ChevronDown aria-hidden size={18} strokeWidth={2} className="pick-chevron" />
        <select
          id={id}
          value={custom ? CUSTOM_OPTION : value}
          aria-invalid={error ? true : undefined}
          onChange={(e) => {
            if (e.target.value === CUSTOM_OPTION) {
              setCustomMode(true);
              onChange("");
            } else {
              setCustomMode(false);
              onChange(e.target.value);
            }
          }}
        >
          {options.map((option) => (
            <option key={option} value={option}>
              {displayOf(def, option)}
            </option>
          ))}
          <option value={CUSTOM_OPTION}>自訂…</option>
        </select>
      </div>
      {custom && (
        <input
          className={changed ? "line-input is-changed" : "line-input"}
          aria-label={`${def.label}（自訂）`}
          placeholder="寫下你想要的"
          inputMode={def.numeric ? "decimal" : undefined}
          value={value}
          autoFocus={!value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {changed && (
        <DefaultNote
          text={displayOf(def, defaultValue)}
          onRestore={() => {
            setCustomMode(false);
            onChange(defaultValue);
          }}
        />
      )}
      <FieldError message={error} />
    </div>
  );
}

// ── 長段落文字（近況、變冷淡時的樣子）──────────────

export function TextField({ def, value, defaultValue, error, onChange }: ValueProps) {
  const id = useId();
  const changed = value !== defaultValue;
  return (
    <div className="field" data-error={error ? "" : undefined}>
      <label className="field-label" htmlFor={id}>
        {def.label}
      </label>
      <textarea
        id={id}
        className={changed ? "ruled-text is-changed" : "ruled-text"}
        value={value}
        rows={Math.max(3, Math.ceil(value.length / 22) + 1)}
        aria-invalid={error ? true : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {changed && (
        <p className="default-note">
          <span className="default-note-text">已改過這一段</span>
          <button type="button" className="text-button" onClick={() => onChange(defaultValue)}>
            <RotateCcw aria-hidden size={13} strokeWidth={2} />
            還原預設
          </button>
        </p>
      )}
      <FieldError message={error} />
    </div>
  );
}

// ── 多項清單 ──────────────────────────────────────

interface ListProps {
  def: FieldDef;
  items: string[];
  defaultItems: string[];
  /** 印好、不可移除的項目（絕不接的系統底線） */
  locked?: string[];
  error?: string;
  onChange: (items: string[]) => void;
}

export function ListField({ def, items, defaultItems, locked = [], error, onChange }: ListProps) {
  const labelId = useId();
  const addId = useId();
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");

  const editable = items.filter((item) => !locked.includes(item));
  const remaining = (def.options ?? []).filter((option) => !items.includes(option));
  const changed =
    editable.length !== defaultItems.filter((i) => !locked.includes(i)).length ||
    editable.some((item) => !defaultItems.includes(item));

  const add = (item: string) => {
    const text = item.trim();
    if (text && !items.includes(text)) onChange([...items, text]);
  };

  return (
    <div className="field" data-error={error ? "" : undefined} role="group" aria-labelledby={labelId}>
      <span className="field-label" id={labelId}>
        {def.label}
      </span>
      {def.hint && <p className="field-hint">{def.hint}</p>}
      <ul className="entries">
        {locked
          .filter((item) => items.includes(item))
          .map((item) => (
            <li key={item} className="entry is-locked">
              <span className="printed">{item}</span>
              <Lock aria-label="系統底線，不能移除" size={14} strokeWidth={2} className="printed-lock" />
            </li>
          ))}
        {editable.map((item) => (
          <li key={item} className="entry">
            <Ink text={item} changed={!defaultItems.includes(item)} />
            <button
              type="button"
              className="icon-button"
              aria-label={`移除「${item}」`}
              onClick={() => onChange(items.filter((i) => i !== item))}
            >
              <X aria-hidden size={16} strokeWidth={2} />
            </button>
          </li>
        ))}
      </ul>

      {adding ? (
        <div className="add-row">
          <input
            className="line-input"
            aria-label={`新增${def.label}`}
            placeholder="寫下要加的項目"
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add(draft);
                setDraft("");
                setAdding(false);
              }
            }}
          />
          <button
            type="button"
            className="button button-small"
            onClick={() => {
              add(draft);
              setDraft("");
              setAdding(false);
            }}
          >
            加入
          </button>
          <button type="button" className="text-button" onClick={() => setAdding(false)}>
            取消
          </button>
        </div>
      ) : (
        <div className="pick pick-add">
          <span className="pick-value pick-placeholder">
            <Plus aria-hidden size={16} strokeWidth={2} />
            加一項
          </span>
          <ChevronDown aria-hidden size={18} strokeWidth={2} className="pick-chevron" />
          <select
            id={addId}
            aria-label={`新增${def.label}`}
            value=""
            onChange={(e) => {
              if (e.target.value === CUSTOM_OPTION) setAdding(true);
              else add(e.target.value);
            }}
          >
            <option value="" disabled>
              加一項…
            </option>
            {remaining.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
            <option value={CUSTOM_OPTION}>自訂…</option>
          </select>
        </div>
      )}

      {changed && (
        <p className="default-note">
          <span className="default-note-text">跟預設不同</span>
          <button type="button" className="text-button" onClick={() => onChange([...defaultItems])}>
            <RotateCcw aria-hidden size={13} strokeWidth={2} />
            還原預設
          </button>
        </p>
      )}
      <FieldError message={error} />
    </div>
  );
}
