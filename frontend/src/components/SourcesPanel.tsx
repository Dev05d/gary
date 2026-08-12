import { useCallback, useEffect, useState } from "react";
import { sourcesApi, type SourceInfo, type SourcesPayload } from "../lib/api";

interface Props {
  onClose: () => void;
  onChanged: () => void;
}

/** Connecting accounts. Tokens stay in the backend; this only shows state. */
export function SourcesPanel({ onClose, onChanged }: Props) {
  const [data, setData] = useState<SourcesPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await sourcesApi.list());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    // The OAuth round-trip happens in another tab, so poll while this is open.
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  const connect = useCallback(async () => {
    setBusy("connect");
    try {
      const { authorization_url } = await sourcesApi.startGoogleAuth();
      window.open(authorization_url, "_blank", "noopener,noreferrer");
      setToast("Finish signing in on the Google tab, then come back here.");
    } catch (err) {
      const e = err as Error & { detail?: { message: string } };
      setError(e.detail?.message ?? e.message);
    } finally {
      setBusy(null);
    }
  }, []);

  const syncNow = useCallback(
    async (id: string) => {
      setBusy(id);
      try {
        const result = await sourcesApi.syncNow(id);
        setToast(result.error ? `Sync failed: ${result.error}` : `Synced — ${result.summary || "nothing new"}`);
        await load();
        onChanged();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [load, onChanged],
  );

  const disconnect = useCallback(
    async (id: string, name: string) => {
      if (!window.confirm(`Disconnect ${name}? Gary stops receiving new mail from it.`)) return;
      setBusy(id);
      try {
        await sourcesApi.disconnect(id);
        await load();
        onChanged();
      } finally {
        setBusy(null);
      }
    },
    [load, onChanged],
  );

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="settings-modal" onClick={(e) => e.stopPropagation()}>
        <header className="settings-head">
          <div>
            <h2>Data sources</h2>
            <p className="settings-sub">
              Gary records from the moment you connect — nothing historical is
              imported. Tokens stay in the backend and are never sent to this page.
            </p>
          </div>
          <button className="link-btn" onClick={onClose}>Close</button>
        </header>

        {error && <div className="banner error">{error}</div>}
        {toast && <div className="banner ok">{toast}</div>}
        {data?.staleness_warning && <div className="banner warn">{data.staleness_warning}</div>}

        <div className="modal-body">
          {data?.setup_hint && (
            <section className="status-section">
              <h3>Setup needed</h3>
              <p className="setting-description">{data.setup_hint}</p>
            </section>
          )}

          <section className="status-section">
            <h3>Google</h3>
            {data && data.sources.filter((s) => s.kind === "gmail").length === 0 && (
              <p className="setting-description">
                No account connected yet. Connecting grants <strong>read-only</strong>{" "}
                access — Gary cannot send, delete, or modify anything.
              </p>
            )}

            {data?.sources
              .filter((s) => s.kind === "gmail" || s.kind === "gcal")
              .map((s) => (
                <SourceRow
                  key={s.id}
                  source={s}
                  busy={busy === s.id}
                  onSync={() => syncNow(s.id)}
                  onDisconnect={() => disconnect(s.id, s.display_name)}
                />
              ))}

            <div className="setting-control" style={{ marginTop: 14 }}>
              <button
                className="send"
                onClick={connect}
                disabled={!data?.google_configured || busy === "connect"}
              >
                {busy === "connect" ? "Opening Google…" : "Connect a Google account"}
              </button>
            </div>
          </section>

          <section className="status-section">
            <h3>Not built yet</h3>
            <p className="setting-description">
              iMessage arrives in Milestone 7 and reads a read-only copy of this
              Mac's local message database. Discord and Instagram do not expose
              personal messages through any official API, so those will import
              from the data-export archives you download from them.
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}

function SourceRow({
  source,
  busy,
  onSync,
  onDisconnect,
}: {
  source: SourceInfo;
  busy: boolean;
  onSync: () => void;
  onDisconnect: () => void;
}) {
  const needsReauth = source.status === "reauth_required";
  return (
    <div className="setting-row">
      <div className="setting-header">
        <span className="setting-label">{source.display_name}</span>
        <div className="setting-tags">
          <span className={`tag ${source.status === "connected" ? "set" : "soon"}`}>
            {source.status}
          </span>
          {source.consecutive_failures > 0 && (
            <span className="tag dirty">{source.consecutive_failures} failures</span>
          )}
        </div>
      </div>

      {source.recording_since ? (
        <p className="setting-description">
          Recording since {new Date(source.recording_since).toLocaleString()} ·{" "}
          {source.messages_ingested.toLocaleString()} stored
          {source.messages_skipped > 0 &&
            ` · ${source.messages_skipped.toLocaleString()} skipped by label rules`}
          {source.last_success_at &&
            ` · last checked ${new Date(source.last_success_at).toLocaleTimeString()}`}
        </p>
      ) : (
        <p className="setting-description">Connected, nothing ingested yet.</p>
      )}

      {needsReauth && (
        <p className="setting-warning">
          ⚠ {source.last_error ?? "Reconnect required."}
        </p>
      )}
      {!needsReauth && source.last_error && (
        <p className="setting-warning">⚠ {source.last_error}</p>
      )}

      <div className="setting-control" style={{ marginTop: 8 }}>
        <button className="test-btn" onClick={onSync} disabled={busy}>
          {busy ? "Checking…" : "Check now"}
        </button>
        <button className="reset-btn" onClick={onDisconnect} disabled={busy}>
          Disconnect
        </button>
      </div>
    </div>
  );
}
