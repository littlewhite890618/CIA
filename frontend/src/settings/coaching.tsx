// 教練回饋的三頁：回饋看什麼（維度＋規則）、回饋的語氣、回饋的內容。
// persona-authoring spec「教練設定」：維度固定六項；預設規則可逐條停用；
// 自訂規則要填描述、類型、歸屬維度，地雷另填嚴重程度；段落至少留一段。

import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Info, Plus, X } from "lucide-react";

import { SelectField, Ink } from "./fields";
import {
  DEFAULT_SECTIONS,
  DIMENSIONS,
  REPORT_LENGTHS,
  REPORT_TONES,
  RULE_CATALOG,
  SEVERITY_LABEL,
  TYPE_LABEL,
} from "./schema";
import type { Coaching, ReportSection, Rule, Severity, SignalType } from "./types";

interface CoachingProps {
  coaching: Coaching;
  defaults: Coaching;
  errors: Record<string, string>;
  onChange: (coaching: Coaching) => void;
}

// ── 三態勾選框（整個維度）──────────────────────────

function TriCheckbox({
  checked,
  indeterminate,
  label,
  onChange,
}: {
  checked: boolean;
  indeterminate: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input
      ref={ref}
      type="checkbox"
      className="check"
      aria-label={label}
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
    />
  );
}

function TypeTag({ rule }: { rule: Rule }) {
  return (
    <span className={`type-tag type-${rule.type}`}>
      {TYPE_LABEL[rule.type]}
      {rule.severity && rule.type === "landmine" && <span className="type-severity">・{SEVERITY_LABEL[rule.severity]}</span>}
    </span>
  );
}

// ── 回饋看什麼 ────────────────────────────────────

export function RulesPage({ coaching, errors, onChange }: CoachingProps) {
  const rules = [...RULE_CATALOG, ...coaching.customRules];
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const enabled = (rule: Rule) => !coaching.disabledRuleIds.includes(rule.id);

  const setRules = (ids: string[], on: boolean) => {
    const disabled = new Set(coaching.disabledRuleIds);
    ids.forEach((id) => (on ? disabled.delete(id) : disabled.add(id)));
    onChange({ ...coaching, disabledRuleIds: [...disabled] });
  };

  return (
    <>
      {DIMENSIONS.map((dimension) => {
        const group = rules.filter((r) => r.dimension === dimension);
        const onCount = group.filter(enabled).length;
        const isOpen = !collapsed.includes(dimension);
        const bodyId = `dim-${dimension}`;
        return (
          <section key={dimension} className="dimension">
            <div className="dimension-head">
              <TriCheckbox
                label={`${dimension}：全部啟用或停用`}
                checked={onCount === group.length && group.length > 0}
                indeterminate={onCount > 0 && onCount < group.length}
                onChange={(on) => setRules(group.map((r) => r.id), on)}
              />
              <h3 className="dimension-name">{dimension}</h3>
              <span className="dimension-count">
                {onCount}／{group.length}
              </span>
              <button
                type="button"
                className="icon-button"
                aria-expanded={isOpen}
                aria-controls={bodyId}
                aria-label={isOpen ? `收合${dimension}` : `展開${dimension}`}
                onClick={() =>
                  setCollapsed(isOpen ? [...collapsed, dimension] : collapsed.filter((d) => d !== dimension))
                }
              >
                <ChevronDown aria-hidden size={18} strokeWidth={2} className={isOpen ? "chevron-open" : undefined} />
              </button>
            </div>
            {onCount === 0 && (
              <p className="dimension-warning">
                <Info aria-hidden size={14} strokeWidth={2} />
                全部停用時，回饋不會談到「{dimension}」。
              </p>
            )}
            <ul className="rule-list" id={bodyId} hidden={!isOpen}>
              {group.map((rule) => (
                <li key={rule.id} className={enabled(rule) ? "rule" : "rule is-off"}>
                  <label className="rule-label">
                    <input
                      type="checkbox"
                      className="check"
                      checked={enabled(rule)}
                      onChange={(e) => setRules([rule.id], e.target.checked)}
                    />
                    <span className="rule-text">
                      {rule.custom ? <Ink text={rule.text} changed /> : rule.text}
                    </span>
                  </label>
                  <TypeTag rule={rule} />
                  {rule.custom && (
                    <button
                      type="button"
                      className="icon-button"
                      aria-label={`刪除自訂規則「${rule.text}」`}
                      onClick={() =>
                        onChange({
                          ...coaching,
                          customRules: coaching.customRules.filter((r) => r.id !== rule.id),
                          disabledRuleIds: coaching.disabledRuleIds.filter((id) => id !== rule.id),
                        })
                      }
                    >
                      <X aria-hidden size={16} strokeWidth={2} />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        );
      })}
      {errors["coaching.customRules"] && (
        <p className="field-error" role="alert">
          {errors["coaching.customRules"]}
        </p>
      )}
      <CustomRuleForm
        onAdd={(rule) => onChange({ ...coaching, customRules: [...coaching.customRules, rule] })}
      />
    </>
  );
}

function CustomRuleForm({ onAdd }: { onAdd: (rule: Rule) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [type, setType] = useState<SignalType>("landmine");
  const [dimension, setDimension] = useState<string>(DIMENSIONS[0]);
  const [severity, setSeverity] = useState<Severity | "">("");
  const [error, setError] = useState("");
  const ids = { text: useId(), type: useId(), dimension: useId(), severity: useId() };

  if (!open) {
    return (
      <button type="button" className="button button-ghost add-rule" onClick={() => setOpen(true)}>
        <Plus aria-hidden size={16} strokeWidth={2} />
        自訂一條規則
      </button>
    );
  }

  const submit = () => {
    if (!text.trim()) return setError("寫下這條規則要看的行為。");
    if (type === "landmine" && !severity) return setError("地雷要選嚴重程度。");
    onAdd({
      id: `custom-${Date.now()}`,
      text: text.trim(),
      type,
      dimension,
      severity: type === "landmine" ? (severity as Severity) : undefined,
      custom: true,
    });
    setText("");
    setSeverity("");
    setError("");
    setOpen(false);
  };

  return (
    <div className="inline-form">
      <h3 className="inline-form-title">自訂一條規則</h3>
      <label className="field-label" htmlFor={ids.text}>
        要看的行為
      </label>
      <input
        id={ids.text}
        className="line-input"
        placeholder="例如：一直問對方有沒有男女朋友"
        value={text}
        autoFocus
        onChange={(e) => setText(e.target.value)}
      />
      <div className="inline-form-row">
        <div>
          <label className="field-label" htmlFor={ids.type}>
            類型
          </label>
          <select id={ids.type} className="plain-select" value={type} onChange={(e) => setType(e.target.value as SignalType)}>
            <option value="landmine">地雷（扣分）</option>
            <option value="killer">句點（讓對話卡住）</option>
            <option value="highlight">亮點（加分）</option>
          </select>
        </div>
        <div>
          <label className="field-label" htmlFor={ids.dimension}>
            歸在哪一項
          </label>
          <select id={ids.dimension} className="plain-select" value={dimension} onChange={(e) => setDimension(e.target.value)}>
            {DIMENSIONS.map((d) => (
              <option key={d}>{d}</option>
            ))}
          </select>
        </div>
        {type === "landmine" && (
          <div>
            <label className="field-label" htmlFor={ids.severity}>
              嚴重程度
            </label>
            <select
              id={ids.severity}
              className="plain-select"
              value={severity}
              onChange={(e) => setSeverity(e.target.value as Severity | "")}
            >
              <option value="" disabled>
                選一個
              </option>
              <option value="high">嚴重</option>
              <option value="medium">中等</option>
              <option value="low">輕微</option>
            </select>
          </div>
        )}
      </div>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      <div className="inline-form-actions">
        <button type="button" className="button button-small" onClick={submit}>
          加入
        </button>
        <button type="button" className="text-button" onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

// ── 回饋的語氣 ────────────────────────────────────

export function ReportStylePage({ coaching, defaults, errors, onChange }: CoachingProps) {
  return (
    <>
      <SelectField
        def={{ path: "coaching.reportTone", label: "語氣", kind: "select", options: REPORT_TONES }}
        value={coaching.reportTone}
        defaultValue={defaults.reportTone}
        error={errors["coaching.reportTone"]}
        onChange={(reportTone) => onChange({ ...coaching, reportTone })}
      />
      <SelectField
        def={{ path: "coaching.reportLength", label: "篇幅", kind: "select", options: REPORT_LENGTHS }}
        value={coaching.reportLength}
        defaultValue={defaults.reportLength}
        error={errors["coaching.reportLength"]}
        onChange={(reportLength) => onChange({ ...coaching, reportLength })}
      />
    </>
  );
}

// ── 回饋的內容 ────────────────────────────────────

export function ReportSectionsPage({ coaching, errors, onChange }: CoachingProps) {
  const custom = coaching.reportSections.filter((s) => s.custom);
  const isOn = (key: string) => coaching.reportSections.some((s) => s.key === key);

  const toggle = (section: ReportSection, on: boolean) => {
    // 依預設順序重排，自訂段落接在最後
    const keys = new Set(coaching.reportSections.map((s) => s.key));
    if (on) keys.add(section.key);
    else keys.delete(section.key);
    onChange({
      ...coaching,
      reportSections: [...DEFAULT_SECTIONS.filter((s) => keys.has(s.key)), ...custom],
    });
  };

  return (
    <>
      <ul className="section-list">
        {DEFAULT_SECTIONS.map((section) => (
          <li key={section.key} className={isOn(section.key) ? "section-item" : "section-item is-off"}>
            <label className="rule-label">
              <input
                type="checkbox"
                className="check"
                checked={isOn(section.key)}
                onChange={(e) => toggle(section, e.target.checked)}
              />
              <span>
                <span className="section-title">{section.title}</span>
                <span className="section-desc">{section.description}</span>
              </span>
            </label>
          </li>
        ))}
        {custom.map((section) => (
          <li key={section.key} className="section-item">
            <span className="rule-label">
              <span className="check-spacer" aria-hidden />
              <span>
                <span className="section-title">
                  <Ink text={section.title} changed />
                </span>
                <span className="section-desc">{section.description}</span>
              </span>
            </span>
            <button
              type="button"
              className="icon-button"
              aria-label={`刪除自訂段落「${section.title}」`}
              onClick={() =>
                onChange({ ...coaching, reportSections: coaching.reportSections.filter((s) => s.key !== section.key) })
              }
            >
              <X aria-hidden size={16} strokeWidth={2} />
            </button>
          </li>
        ))}
      </ul>
      {errors["coaching.reportSections"] && (
        <p className="field-error" role="alert">
          {errors["coaching.reportSections"]}
        </p>
      )}
      <CustomSectionForm
        onAdd={(section) => onChange({ ...coaching, reportSections: [...coaching.reportSections, section] })}
      />
    </>
  );
}

function CustomSectionForm({ onAdd }: { onAdd: (section: ReportSection) => void }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const ids = { title: useId(), description: useId() };

  if (!open) {
    return (
      <button type="button" className="button button-ghost add-rule" onClick={() => setOpen(true)}>
        <Plus aria-hidden size={16} strokeWidth={2} />
        自訂一個段落
      </button>
    );
  }

  return (
    <div className="inline-form">
      <h3 className="inline-form-title">自訂一個段落</h3>
      <label className="field-label" htmlFor={ids.title}>
        段落標題
      </label>
      <input
        id={ids.title}
        className="line-input"
        placeholder="例如：這次的小目標"
        value={title}
        autoFocus
        onChange={(e) => setTitle(e.target.value)}
      />
      <label className="field-label" htmlFor={ids.description}>
        要寫什麼
      </label>
      <input
        id={ids.description}
        className="line-input"
        placeholder="例如：給一個下次聊天可以達成的小目標"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      <div className="inline-form-actions">
        <button
          type="button"
          className="button button-small"
          onClick={() => {
            if (!title.trim()) return setError("寫下段落標題。");
            onAdd({
              key: `custom-${Date.now()}`,
              title: title.trim(),
              description: description.trim() || "（沒有說明）",
              custom: true,
            });
            setTitle("");
            setDescription("");
            setError("");
            setOpen(false);
          }}
        >
          加入
        </button>
        <button type="button" className="text-button" onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}
