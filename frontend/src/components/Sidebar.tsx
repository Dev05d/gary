import type { Conversation, Status } from "../lib/api";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onOpenStatus: () => void;
  status: Status | null;
}

export function Sidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onDelete,
  onOpenStatus,
  status,
}: Props) {
  return (
    <aside className="sidebar">
      <button className="new-chat" onClick={onNew}>
        + New chat
      </button>

      <nav className="convo-list">
        {conversations.length === 0 && <p className="hint">No conversations yet.</p>}
        {conversations.map((c) => (
          <div
            key={c.id}
            className={`convo ${c.id === activeId ? "active" : ""}`}
            onClick={() => onSelect(c.id)}
          >
            <span className="convo-title">{c.title}</span>
            <button
              className="convo-delete"
              title="Delete conversation"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(c.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <button className="sources-btn" onClick={onOpenStatus}>
          <span>Data sources</span>
          <span className="badge">
            {status?.sources.filter((s) => s.implemented).length ?? 0}/
            {status?.sources.length ?? 0}
          </span>
        </button>
        <div className="milestone">Milestone {status?.milestone ?? "—"} · v{status?.version ?? "—"}</div>
      </div>
    </aside>
  );
}
