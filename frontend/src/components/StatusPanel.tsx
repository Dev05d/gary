import { useState } from "react";
import { getToken, setToken, type Status } from "../lib/api";

interface Props {
  status: Status | null;
  error: string | null;
  onClose: () => void;
  onRefresh: () => void;
}

/** The observability page from spec §19. */
export function StatusPanel({ status, error, onClose, onRefresh }: Props) {
  const [token, setLocalToken] = useState(getToken());

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>System status</h2>
          <div>
            <button className="link-btn" onClick={onRefresh}>
              Refresh
            </button>
            <button className="link-btn" onClick={onClose}>
              Close
            </button>
          </div>
        </header>

        {error && <div className="error-box">{error}</div>}

        {status && (
          <div className="modal-body">
            <Section title="Inference backends">
              {status.llm_backends.map((b) => (
                <Row
                  key={b.base_url}
                  label={b.base_url}
                  value={
                    b.connected
                      ? `CONNECTED · v${b.version ?? "?"} · ${b.latency_ms ?? "?"}ms`
                      : "DISCONNECTED"
                  }
                  ok={b.connected}
                  note={b.error ?? undefined}
                />
              ))}
            </Section>

            <Section title="Models">
              {status.models.map((m) => (
                <Row
                  key={m.role}
                  label={`${m.role} · ${m.model}`}
                  value={`ctx ${m.num_ctx.toLocaleString()}`}
                  ok={m.available}
                  note={m.note ?? undefined}
                />
              ))}
            </Section>

            <Section title="Data sources">
              {status.sources.map((s) => (
                <Row
                  key={s.kind}
                  label={s.display_name}
                  value={s.implemented ? s.status.toUpperCase() : "NOT YET BUILT"}
                  ok={s.implemented}
                />
              ))}
            </Section>

            <Section title="Index">
              <Row label="Conversations" value={String(status.counts.conversations)} ok />
              <Row label="Chat turns" value={String(status.counts.chat_turns)} ok />
              <Row
                label="Messages indexed"
                value={String(status.counts.messages_indexed)}
                ok={status.counts.messages_indexed > 0}
              />
              <Row
                label="Embeddings"
                value={String(status.counts.embeddings)}
                ok={status.counts.embeddings > 0}
              />
              <Row label="Background jobs pending" value={String(status.counts.pending_jobs)} ok />
            </Section>

            <Section title="Storage">
              <Row
                label="Database"
                value={status.database_connected ? "CONNECTED" : "ERROR"}
                ok={status.database_connected}
                note={status.database_path ?? undefined}
              />
              <Row
                label="Permissions"
                value={status.read_only ? "READ-ONLY" : "WRITE ENABLED"}
                ok={status.read_only}
                note="Gary cannot send email, delete messages, or modify your calendar."
              />
              <Row label="Uptime" value={`${Math.round(status.uptime_seconds)}s`} ok />
            </Section>

            <Section title="API token">
              <p className="hint">
                Only needed if you exposed the backend beyond localhost. This is
                Gary's own API token — never a Google credential.
              </p>
              <div className="token-row">
                <input
                  type="password"
                  value={token}
                  placeholder="unset"
                  onChange={(e) => setLocalToken(e.target.value)}
                />
                <button
                  className="link-btn"
                  onClick={() => {
                    setToken(token);
                    onRefresh();
                  }}
                >
                  Save
                </button>
              </div>
            </Section>
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="status-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function Row({
  label,
  value,
  ok,
  note,
}: {
  label: string;
  value: string;
  ok: boolean;
  note?: string;
}) {
  return (
    <div className="status-row">
      <span className="status-label">{label}</span>
      <span className={`status-value ${ok ? "ok" : "bad"}`}>{value}</span>
      {note && <span className="status-note">{note}</span>}
    </div>
  );
}
