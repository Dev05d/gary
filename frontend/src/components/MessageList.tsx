import { useEffect, useRef } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatTurn } from "../lib/api";
import type { PendingTurn } from "../App";

interface Props {
  turns: ChatTurn[];
  pending: PendingTurn | null;
}

export function MessageList({ turns, pending }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, pending?.content]);

  const empty = turns.length === 0 && !pending;

  return (
    <div className="messages">
      {empty && <EmptyState />}

      {turns.map((turn) => (
        <Bubble key={turn.id} role={turn.role} meta={<TurnMeta turn={turn} />}>
          {turn.error ? (
            <ErrorBox message={turn.error} />
          ) : (
            <Markdown remarkPlugins={[remarkGfm]}>{turn.content}</Markdown>
          )}
          {turn.citations?.sources?.length ? (
            <Citations sources={turn.citations.sources} />
          ) : null}
        </Bubble>
      ))}

      {pending && (
        <Bubble
          role="assistant"
          meta={
            pending.streaming ? (
              <span className="meta">
                <span className="thinking-dot" /> {pending.model || "thinking"}
              </span>
            ) : null
          }
        >
          {pending.error ? (
            <ErrorBox message={pending.error} />
          ) : pending.content ? (
            <Markdown remarkPlugins={[remarkGfm]}>{pending.content}</Markdown>
          ) : (
            <span className="cursor" />
          )}
        </Bubble>
      )}

      <div ref={endRef} />
    </div>
  );
}

function Bubble({
  role,
  children,
  meta,
}: {
  role: string;
  children: React.ReactNode;
  meta?: React.ReactNode;
}) {
  return (
    <article className={`turn ${role}`}>
      <div className="turn-head">
        <span className="who">{role === "user" ? "You" : "Gary"}</span>
        {meta}
      </div>
      <div className="turn-body">{children}</div>
    </article>
  );
}

function TurnMeta({ turn }: { turn: ChatTurn }) {
  if (turn.role !== "assistant") return null;
  const bits: string[] = [];
  if (turn.model) bits.push(turn.model);
  if (turn.latency_ms) bits.push(`${(turn.latency_ms / 1000).toFixed(1)}s`);
  if (turn.completion_tokens) bits.push(`${turn.completion_tokens} tok`);
  return bits.length ? <span className="meta">{bits.join(" · ")}</span> : null;
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="error-box">
      <strong>Something went wrong</strong>
      <p>{message}</p>
    </div>
  );
}

function Citations({ sources }: { sources: NonNullable<ChatTurn["citations"]>["sources"] }) {
  if (!sources?.length) return null;
  return (
    <div className="citations">
      <div className="citations-label">Sources</div>
      {sources.map((s) => (
        <button key={s.ref} className="citation" title={`Open ${s.ref}`}>
          <span className="citation-source">{s.source}</span>
          <span className="citation-title">{s.title ?? s.ref}</span>
          {s.author && <span className="citation-author">{s.author}</span>}
          {s.timestamp && <span className="citation-date">{s.timestamp}</span>}
        </button>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="empty">
      <h2>Ask me anything</h2>
      <p className="empty-note">
        Milestone 1: I'm talking to your local model, but I can't see your email
        or calendar yet — those connectors land in Milestone 2.
      </p>
      <div className="suggestions">
        {[
          "Are you running locally?",
          "Explain what you'll be able to do once Gmail is connected.",
          "What are the privacy tradeoffs of a local LLM?",
        ].map((s) => (
          <div key={s} className="suggestion">
            {s}
          </div>
        ))}
      </div>
    </div>
  );
}
