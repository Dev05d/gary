import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  streamChat,
  type ChatTurn,
  type Conversation,
  type Status,
} from "./lib/api";
import { Sidebar } from "./components/Sidebar";
import { MessageList } from "./components/MessageList";
import { Composer } from "./components/Composer";
import { StatusPanel } from "./components/StatusPanel";
import { StatusBar } from "./components/StatusBar";
import { SettingsPage } from "./components/SettingsPage";

export type ModelRole = "large" | "fast";

/** A turn that only exists client-side while it streams. */
export interface PendingTurn {
  id: string;
  content: string;
  model: string;
  streaming: boolean;
  error?: string;
}

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [showStatus, setShowStatus] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [role, setRole] = useState<ModelRole>("large");
  const roleTouched = useRef(false);
  const [busy, setBusy] = useState(false);
  const [contextUse, setContextUse] = useState<{ used: number; limit: number } | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.status());
      setStatusError(null);
    } catch (err) {
      setStatus(null);
      setStatusError((err as Error).message);
    }
  }, []);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.listConversations());
    } catch {
      /* the status bar already surfaces backend outages */
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    void refreshConversations();
    const timer = setInterval(refreshStatus, 15000);
    return () => clearInterval(timer);
  }, [refreshStatus, refreshConversations]);

  // Apply the configured default model, but never fight a manual choice.
  useEffect(() => {
    if (!roleTouched.current && status?.ui?.default_role) {
      setRole(status.ui.default_role);
    }
  }, [status?.ui?.default_role]);

  /** Re-read a conversation's turns without disturbing per-turn UI state. */
  const loadTurns = useCallback(async (id: string) => {
    const detail = await api.getConversation(id);
    setTurns(detail.messages);
  }, []);

  const openConversation = useCallback(
    async (id: string) => {
      setActiveId(id);
      setPending(null);
      setContextUse(null); // the meter describes the last send, not this thread
      await loadTurns(id);
    },
    [loadTurns],
  );

  const newConversation = useCallback(() => {
    setActiveId(null);
    setTurns([]);
    setPending(null);
    setContextUse(null);
  }, []);

  const removeConversation = useCallback(
    async (id: string) => {
      await api.deleteConversation(id);
      if (id === activeId) newConversation();
      void refreshConversations();
    },
    [activeId, newConversation, refreshConversations],
  );

  const send = useCallback(
    async (text: string) => {
      if (busy) return;
      setBusy(true);

      const optimisticUser: ChatTurn = {
        id: `local-${Date.now()}`,
        role: "user",
        content: text,
        model: null,
        prompt_tokens: 0,
        completion_tokens: 0,
        latency_ms: null,
        citations: null,
        error: null,
        created_at: new Date().toISOString(),
      };
      setTurns((prev) => [...prev, optimisticUser]);
      setPending({ id: "pending", content: "", model: "", streaming: true });

      const controller = new AbortController();
      abortRef.current = controller;
      let conversationId = activeId;
      const streamToUi = status?.ui?.stream_responses ?? true;
      let buffered = "";

      await streamChat(
        { message: text, conversation_id: activeId, role },
        {
          onStart: (p) => {
            conversationId = p.conversation_id;
            setActiveId(p.conversation_id);
            setPending({ id: p.message_id, content: "", model: p.model, streaming: true });
            setContextUse({ used: p.context.used_tokens, limit: p.context.limit });
          },
          onToken: (delta) => {
            buffered += delta;
            // With streaming off, hold the text back and reveal the whole
            // reply at once — the request still streams over the wire.
            if (streamToUi) {
              setPending((prev) => (prev ? { ...prev, content: prev.content + delta } : prev));
            }
          },
          onDone: () => {
            setPending(null);
            // loadTurns, not openConversation: keep the context meter visible.
            if (conversationId) void loadTurns(conversationId);
            void refreshConversations();
          },
          onError: (message) =>
            setPending((prev) =>
              prev
                ? { ...prev, content: buffered, streaming: false, error: message }
                : { id: "err", content: "", model: "", streaming: false, error: message },
            ),
        },
        controller.signal,
      );

      abortRef.current = null;
      setBusy(false);
    },
    [activeId, busy, role, loadTurns, refreshConversations, status?.ui?.stream_responses],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPending((prev) => (prev ? { ...prev, streaming: false } : prev));
    setBusy(false);
  }, []);

  const live = Boolean(status?.llm_backends.some((b) => b.connected));

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={openConversation}
        onNew={newConversation}
        onDelete={removeConversation}
        onOpenStatus={() => setShowStatus(true)}
        status={status}
      />

      <main className="main">
        <header className="topbar">
          <div className="topbar-title">
            <h1>Gary</h1>
            <span className="subtitle">local personal agent</span>
          </div>
          <div className="topbar-right">
            <div className="role-toggle" role="group" aria-label="Model selection">
              <button
                className={role === "large" ? "active" : ""}
                onClick={() => {
                  roleTouched.current = true;
                  setRole("large");
                }}
                title={status?.models.find((m) => m.role === "large")?.model}
              >
                Deep
              </button>
              <button
                className={role === "fast" ? "active" : ""}
                onClick={() => {
                  roleTouched.current = true;
                  setRole("fast");
                }}
                title={status?.models.find((m) => m.role === "fast")?.model}
              >
                Fast
              </button>
            </div>
            <button className="link-btn" onClick={() => setShowSettings(true)}>
              Settings
            </button>
            <button className="link-btn" onClick={() => setShowStatus(true)}>
              Status
            </button>
            <span className={`live-pill ${live ? "on" : "off"}`}>
              <span className="dot" />
              {live ? "LIVE" : "OFFLINE"}
            </span>
          </div>
        </header>

        <MessageList turns={turns} pending={pending} />

        <Composer
          onSend={send}
          onStop={stop}
          busy={busy}
          disabled={!live && !status}
        />

        <StatusBar
          status={status}
          statusError={statusError}
          contextUse={contextUse}
          role={role}
        />
      </main>

      {showStatus && (
        <StatusPanel
          status={status}
          error={statusError}
          onClose={() => setShowStatus(false)}
          onRefresh={refreshStatus}
          onOpenSettings={() => {
            setShowStatus(false);
            setShowSettings(true);
          }}
        />
      )}

      {showSettings && (
        <SettingsPage
          onClose={() => setShowSettings(false)}
          onSettingsChanged={refreshStatus}
        />
      )}
    </div>
  );
}
