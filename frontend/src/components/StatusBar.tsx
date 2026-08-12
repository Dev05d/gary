import type { Status } from "../lib/api";

interface Props {
  status: Status | null;
  statusError: string | null;
  contextUse: { used: number; limit: number } | null;
  role: "large" | "fast";
}

export function StatusBar({ status, statusError, contextUse, role }: Props) {
  const model = status?.models.find((m) => m.role === role);
  const backend = status?.llm_backends[0];

  if (statusError) {
    return (
      <div className="statusbar error">
        Backend unreachable — {statusError}. Is <code>./start.sh</code> running?
      </div>
    );
  }

  const pct = contextUse ? Math.min(100, (contextUse.used / contextUse.limit) * 100) : 0;

  return (
    <div className="statusbar">
      <span className="sb-item">
        <span className={`dot ${backend?.connected ? "ok" : "bad"}`} />
        {backend?.connected ? "Ollama" : "Ollama offline"}
      </span>

      {model && (
        <span className="sb-item" title={`Serving from ${model.base_url}`}>
          {model.model}
          {!model.available && <span className="warn"> (not installed)</span>}
        </span>
      )}

      {model && <span className="sb-item dim">ctx {(model.num_ctx / 1024).toFixed(0)}k</span>}

      {contextUse && (status?.ui?.show_context_meter ?? true) && (
        <span className="sb-item ctx" title={`${contextUse.used} / ${contextUse.limit} tokens`}>
          <span className="ctx-bar">
            <span
              className="ctx-fill"
              style={{
                width: `${pct}%`,
                background: pct > 85 ? "var(--red)" : pct > 60 ? "var(--amber)" : "var(--green)",
              }}
            />
          </span>
          {Math.round(pct)}%
        </span>
      )}

      <span className="sb-spacer" />
      <span className="sb-item dim" title="Gary cannot send email or modify your data">
        read-only
      </span>
    </div>
  );
}
