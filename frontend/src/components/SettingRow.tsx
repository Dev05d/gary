import { useState } from "react";
import type { SettingField, TestConnectionResult } from "../lib/api";

interface Props {
  field: SettingField;
  value: unknown;
  dirty: boolean;
  error: string | null;
  models: string[];
  testing: boolean;
  testResult?: TestConnectionResult;
  onChange: (value: unknown) => void;
  onReset: () => void;
  onTest?: () => void;
}

/** One setting: control, full explanation, provenance, and reset. */
export function SettingRow({
  field,
  value,
  dirty,
  error,
  models,
  testing,
  testResult,
  onChange,
  onReset,
  onTest,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const disabled = !field.active;

  // Long explanations collapse to the first paragraph until asked for.
  const paragraphs = field.description.split("\n\n");
  const summary = paragraphs[0];
  const rest = paragraphs.slice(1);

  return (
    <div className={`setting-row ${disabled ? "inactive" : ""} ${error ? "has-error" : ""}`}>
      <div className="setting-header">
        <label className="setting-label" htmlFor={`f-${field.key}`}>
          {field.label}
          {field.unit && <span className="setting-unit"> ({field.unit})</span>}
        </label>

        <div className="setting-tags">
          {dirty && <span className="tag dirty">unsaved</span>}
          {field.overridden && !dirty && <span className="tag set">customised</span>}
          {field.source === "env" && !field.overridden && <span className="tag env">.env</span>}
          {field.restart && <span className="tag restart">restart</span>}
          {field.advanced && <span className="tag adv">advanced</span>}
          {!field.active && <span className="tag soon">Milestone {field.milestone}</span>}
        </div>
      </div>

      <div className="setting-control">
        <Control
          field={field}
          value={value}
          models={models}
          disabled={disabled}
          onChange={onChange}
        />
        {onTest && (
          <button className="test-btn" onClick={onTest} disabled={testing || disabled}>
            {testing ? "Testing…" : "Test"}
          </button>
        )}
        {field.overridden && (
          <button className="reset-btn" onClick={onReset} title="Revert to .env / default">
            Reset
          </button>
        )}
      </div>

      {testResult && (
        <div className={`test-result ${testResult.connected ? "ok" : "bad"}`}>
          {testResult.connected ? (
            <>
              Connected — Ollama v{testResult.version} in {testResult.latency_ms}ms.{" "}
              {testResult.models.length} model{testResult.models.length === 1 ? "" : "s"}{" "}
              installed
              {testResult.models.length > 0 && `: ${testResult.models.join(", ")}`}
            </>
          ) : (
            <>{testResult.error}</>
          )}
        </div>
      )}

      {error && <div className="setting-error">{error}</div>}

      <p className="setting-description">
        {summary}
        {rest.length > 0 && (
          <button className="more-btn" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "less" : "more"}
          </button>
        )}
      </p>

      {expanded &&
        rest.map((p, i) => (
          <p key={i} className="setting-description extra">
            {p}
          </p>
        ))}

      {field.warning && <p className="setting-warning">⚠ {field.warning}</p>}

      {field.examples.length > 0 && (
        <div className="setting-examples">
          {field.examples.map((ex) => (
            <button
              key={ex}
              className="example-chip"
              disabled={disabled}
              onClick={() => onChange(ex)}
            >
              {ex}
            </button>
          ))}
        </div>
      )}

      <div className="setting-provenance">
        <span>
          from <strong>{sourceLabel(field.source)}</strong>
        </span>
        {field.default_value !== null && field.default_value !== undefined && (
          <span className="mono">default: {String(field.default_value)}</span>
        )}
        <span className="mono dim">{field.key}</span>
      </div>
    </div>
  );
}

function sourceLabel(source: string): string {
  switch (source) {
    case "database":
      return "this settings page";
    case "env":
      return ".env file";
    default:
      return "built-in default";
  }
}

function Control({
  field,
  value,
  models,
  disabled,
  onChange,
}: {
  field: SettingField;
  value: unknown;
  models: string[];
  disabled: boolean;
  onChange: (v: unknown) => void;
}) {
  const id = `f-${field.key}`;

  if (field.type === "bool") {
    return (
      <label className="switch">
        <input
          id={id}
          type="checkbox"
          checked={Boolean(value)}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="switch-track" />
        <span className="switch-text">{value ? "On" : "Off"}</span>
      </label>
    );
  }

  if (field.type === "select") {
    return (
      <select
        id={id}
        value={String(value ?? "")}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {field.options?.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  }

  if (field.type === "model") {
    // Free text plus a datalist of what is actually installed on that host —
    // a hard dropdown would block a valid tag the probe could not see.
    return (
      <>
        <input
          id={id}
          list={`${id}-models`}
          value={String(value ?? "")}
          disabled={disabled}
          placeholder={field.placeholder ?? "model:tag"}
          onChange={(e) => onChange(e.target.value)}
        />
        <datalist id={`${id}-models`}>
          {models.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
      </>
    );
  }

  if (field.type === "int" || field.type === "float") {
    const showSlider =
      field.minimum !== null &&
      field.maximum !== null &&
      field.minimum !== undefined &&
      field.maximum !== undefined &&
      field.type === "float";
    return (
      <>
        {showSlider && (
          <input
            type="range"
            className="slider"
            min={field.minimum ?? 0}
            max={field.maximum ?? 1}
            step={field.step ?? 0.05}
            value={Number(value ?? field.minimum ?? 0)}
            disabled={disabled}
            onChange={(e) => onChange(Number(e.target.value))}
          />
        )}
        <input
          id={id}
          type="number"
          className="numeric"
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          step={field.step ?? (field.type === "int" ? 1 : 0.01)}
          value={value === null || value === undefined ? "" : String(value)}
          placeholder={field.placeholder ?? ""}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        />
      </>
    );
  }

  if (field.type === "secret") {
    return (
      <input
        id={id}
        type="password"
        autoComplete="new-password"
        placeholder={field.is_set ? "•••••••• (set — type to replace)" : "(not set)"}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <input
      id={id}
      type="text"
      value={String(value ?? "")}
      placeholder={field.placeholder ?? ""}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
