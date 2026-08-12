import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  settingsApi,
  type SettingCategory,
  type SettingField,
  type TestConnectionResult,
} from "../lib/api";
import { SettingRow } from "./SettingRow";

interface Props {
  onClose: () => void;
  onSettingsChanged: () => void;
}

type Draft = Record<string, unknown>;

export function SettingsPage({ onClose, onSettingsChanged }: Props) {
  const [fields, setFields] = useState<SettingField[]>([]);
  const [categories, setCategories] = useState<SettingCategory[]>([]);
  const [activeCategory, setActiveCategory] = useState<string>("connection");
  const [draft, setDraft] = useState<Draft>({});
  // Default on: hiding two thirds of the knobs behind a checkbox makes the
  // page look emptier than it is. Uncheck to get the short list.
  const [showAdvanced, setShowAdvanced] = useState(true);
  const [showInactive, setShowInactive] = useState(true);
  const [filter, setFilter] = useState("");

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState<string[]>([]);

  // Model tags per host, so the model pickers can offer real choices.
  const [modelsByHost, setModelsByHost] = useState<Record<string, string[]>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, TestConnectionResult>>({});

  const toastTimer = useRef<number | null>(null);

  const flash = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await settingsApi.read();
      setFields(data.fields);
      setCategories(data.categories);
      setRestartRequired(data.restart_required);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const valueOf = useCallback(
    (f: SettingField): unknown => (f.key in draft ? draft[f.key] : f.value),
    [draft],
  );

  /** Fetch installed models for whichever host a model field points at. */
  const refreshModels = useCallback(
    async (hostKey: string) => {
      const hostField = fields.find((f) => f.key === hostKey);
      const fallback = fields.find((f) => f.key === "ollama_base_url");
      const url =
        (hostField ? (valueOf(hostField) as string) : "") ||
        (fallback ? (valueOf(fallback) as string) : "");
      if (!url) return;
      const models = await settingsApi.models(url);
      setModelsByHost((prev) => ({ ...prev, [hostKey]: models }));
    },
    [fields, valueOf],
  );

  useEffect(() => {
    if (!fields.length) return;
    void refreshModels("ollama_base_url");
    void refreshModels("ollama_embed_base_url");
    // Only on initial load — re-fetching on every keystroke would hammer the host.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fields.length]);

  const dirtyKeys = useMemo(
    () =>
      Object.keys(draft).filter((k) => {
        const f = fields.find((x) => x.key === k);
        if (!f) return false;
        const before = f.value ?? "";
        const after = draft[k] ?? "";
        return String(before) !== String(after);
      }),
    [draft, fields],
  );

  const save = useCallback(async () => {
    if (!dirtyKeys.length) return;
    setSaving(true);
    setFieldError(null);
    setError(null);
    try {
      const changes: Draft = {};
      for (const k of dirtyKeys) changes[k] = draft[k];
      const res = await settingsApi.update(changes);
      setFields(res.fields);
      setDraft({});
      setRestartRequired(res.restart_required);
      flash(
        res.restart_required.length
          ? `Saved. ${res.restart_required.length} setting(s) need a restart.`
          : `Saved — ${res.changed.length} setting(s) applied immediately.`,
      );
      onSettingsChanged();
      void refreshModels("ollama_base_url");
    } catch (err) {
      const e = err as Error & { detail?: { key: string; message: string } };
      if (e.detail?.message) {
        setFieldError({ key: e.detail.key, message: e.detail.message });
        setActiveCategoryForKey(e.detail.key);
      } else {
        setError(e.message);
      }
    } finally {
      setSaving(false);
    }
  }, [dirtyKeys, draft, flash, onSettingsChanged, refreshModels]);

  const setActiveCategoryForKey = (key: string) => {
    const f = fields.find((x) => x.key === key);
    if (f) setActiveCategory(f.category);
  };

  const resetOne = useCallback(
    async (key: string) => {
      const res = await settingsApi.resetOne(key);
      setFields(res.fields);
      setDraft((d) => {
        const next = { ...d };
        delete next[key];
        return next;
      });
      flash(`Reset to ${res.fields.find((f) => f.key === key)?.source ?? "default"} value.`);
      onSettingsChanged();
    },
    [flash, onSettingsChanged],
  );

  const resetAll = useCallback(async () => {
    if (!window.confirm("Clear every setting you've changed here and fall back to .env?")) return;
    const res = await settingsApi.resetAll();
    setFields(res.fields);
    setDraft({});
    flash("All overrides cleared.");
    onSettingsChanged();
  }, [flash, onSettingsChanged]);

  const runTest = useCallback(
    async (key: string) => {
      const f = fields.find((x) => x.key === key);
      const url = (valueOf(f!) as string) || "";
      if (!url) return;
      setTesting(key);
      try {
        const result = await settingsApi.testConnection(url);
        setTestResult((prev) => ({ ...prev, [key]: result }));
        if (result.connected) {
          setModelsByHost((prev) => ({ ...prev, [key]: result.models }));
        }
      } catch (err) {
        setTestResult((prev) => ({
          ...prev,
          [key]: {
            connected: false,
            base_url: url,
            models: [],
            error: (err as Error).message,
          },
        }));
      } finally {
        setTesting(null);
      }
    },
    [fields, valueOf],
  );

  const searching = filter.trim().length > 0;
  const needle = filter.trim().toLowerCase();

  const visibleFields = useMemo(() => {
    return fields.filter((f) => {
      if (searching) {
        const haystack = `${f.label} ${f.key} ${f.description}`.toLowerCase();
        if (!haystack.includes(needle)) return false;
      } else if (f.category !== activeCategory) {
        return false;
      }
      if (!showAdvanced && f.advanced) return false;
      if (!showInactive && !f.active) return false;
      return true;
    });
  }, [fields, activeCategory, showAdvanced, showInactive, searching, needle]);

  const countsByCategory = useMemo(() => {
    const map: Record<string, { total: number; changed: number }> = {};
    for (const f of fields) {
      const entry = (map[f.category] ??= { total: 0, changed: 0 });
      entry.total += 1;
      if (f.overridden) entry.changed += 1;
    }
    return map;
  }, [fields]);

  const activeCategoryMeta = categories.find((c) => c.key === activeCategory);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="settings-modal" onClick={(e) => e.stopPropagation()}>
        <header className="settings-head">
          <div>
            <h2>Settings</h2>
            <p className="settings-sub">
              Changes save to Gary's database and layer on top of your <code>.env</code>.
              Your <code>.env</code> file is never modified.
            </p>
          </div>
          <button className="link-btn" onClick={onClose}>
            Close
          </button>
        </header>

        {restartRequired.length > 0 && (
          <div className="banner warn">
            <strong>Restart needed</strong> for: {restartRequired.join(", ")}. Everything
            else is already live.
          </div>
        )}
        {error && <div className="banner error">{error}</div>}
        {toast && <div className="banner ok">{toast}</div>}

        <div className="settings-body">
          <nav className="settings-nav">
            <input
              className="settings-search"
              placeholder="Search all settings…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            {categories.map((c) => {
              const counts = countsByCategory[c.key];
              return (
                <button
                  key={c.key}
                  className={`settings-nav-item ${
                    !searching && c.key === activeCategory ? "active" : ""
                  }`}
                  onClick={() => {
                    setFilter("");
                    setActiveCategory(c.key);
                  }}
                >
                  <span>{c.label}</span>
                  {counts?.changed ? <span className="badge">{counts.changed}</span> : null}
                </button>
              );
            })}

            <div className="settings-toggles">
              <label>
                <input
                  type="checkbox"
                  checked={showAdvanced}
                  onChange={(e) => setShowAdvanced(e.target.checked)}
                />
                Show advanced
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={showInactive}
                  onChange={(e) => setShowInactive(e.target.checked)}
                />
                Show upcoming
              </label>
              <button className="link-btn danger" onClick={resetAll}>
                Reset everything
              </button>
            </div>
          </nav>

          <div className="settings-content">
            {loading && <p className="hint">Loading…</p>}

            {!loading && !searching && activeCategoryMeta && (
              <div className="category-intro">
                <h3>{activeCategoryMeta.label}</h3>
                <p>{activeCategoryMeta.description}</p>
              </div>
            )}

            {!loading && searching && (
              <div className="category-intro">
                <h3>
                  {visibleFields.length} result{visibleFields.length === 1 ? "" : "s"} for
                  “{filter}”
                </h3>
              </div>
            )}

            {!loading && visibleFields.length === 0 && (
              <p className="hint">
                Nothing here. Try enabling “Show advanced” or “Show upcoming”.
              </p>
            )}

            {visibleFields.map((f) => (
              <SettingRow
                key={f.key}
                field={f}
                value={valueOf(f)}
                dirty={dirtyKeys.includes(f.key)}
                error={fieldError?.key === f.key ? fieldError.message : null}
                models={f.model_host_key ? modelsByHost[f.model_host_key] ?? [] : []}
                testing={testing === f.key}
                testResult={testResult[f.key]}
                onChange={(v) => setDraft((d) => ({ ...d, [f.key]: v }))}
                onReset={() => resetOne(f.key)}
                onTest={f.type === "url" ? () => runTest(f.key) : undefined}
              />
            ))}
          </div>
        </div>

        <footer className="settings-foot">
          <span className="hint">
            {dirtyKeys.length
              ? `${dirtyKeys.length} unsaved change${dirtyKeys.length === 1 ? "" : "s"}`
              : "No unsaved changes"}
          </span>
          <div className="settings-actions">
            <button
              className="link-btn"
              onClick={() => setDraft({})}
              disabled={!dirtyKeys.length}
            >
              Discard
            </button>
            <button className="send" onClick={save} disabled={!dirtyKeys.length || saving}>
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
