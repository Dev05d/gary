# Architecture

This document covers the design decisions behind Gary, the problems found in
the original specification, and the target schema for later milestones.

---

## 1. Problems with the original spec

The spec is strong — the core principle ("the LLM is not the database") is
exactly right. These are the places where following it literally would cause
trouble.

### 1.1 Two of the requested connectors cannot be built as described

The opening request asks for Gmail, iMessage, Instagram DMs, and Discord DMs.
Section 2 then says "do NOT scrape services when an official API exists" and
"do NOT implement anything that bypasses authentication or platform
restrictions". Those two statements conflict for half the list:

- **Discord DMs.** The bot API has no access to a *user's* private DMs. The
  only way to read them live is a self-bot driving a user token, which is
  explicitly against Discord's ToS and is enforced with account termination.
- **Instagram DMs.** The Messaging API is a customer-service product: it covers
  Business/Creator accounts receiving messages *from customers*, requires a
  linked Facebook Page and app review, and does not expose personal DM history.

Both services do offer **official data exports** (GDPR downloads). That path is
legitimate, requires no risky credentials, and yields the full conversation
history. It is a periodic snapshot, not a live stream — that is a real
limitation and the UI will say so rather than implying live sync.

- **iMessage** is the opposite case, and the spec is too pessimistic about it.
  `~/Library/Messages/chat.db` is a SQLite database of your own messages on
  your own Mac. Reading it with Full Disk Access that you grant is not an auth
  bypass. The connector opens an immutable read-only copy so it can never
  corrupt the live database. Constraints worth knowing up front: macOS only,
  only this machine's history, and it breaks if Apple changes the schema.

**Decision:** four connector *classes*, not one.

| Class | Mechanism | Freshness | Examples |
|---|---|---|---|
| `PushConnector` | Webhook / Pub/Sub | seconds | Gmail |
| `PollConnector` | Incremental sync token | minutes | Calendar, Outlook |
| `LocalConnector` | Local file/DB tail | seconds | iMessage, files |
| `ImportConnector` | One-shot archive | manual | Discord, Instagram |

They share the normalisation and processing pipeline; only acquisition differs.

### 1.2 "SQLite FTS5 + ChromaDB or Qdrant" needs care in a multi-worker app

Qdrant in embedded mode (`QdrantClient(path=...)`, as Megamind uses) takes an
exclusive lock on its directory. A FastAPI server plus separate worker
processes all opening it will fail. Chroma's persistent client has the same
issue.

**Decision:** everything runs in **one process** with asyncio workers.
Milestone 1 through 8 need no more than that — a personal mailbox is ~10⁵
messages, not 10⁹. The vector store is behind a `VectorStore` interface so that
moving to a Qdrant server (`docker compose up qdrant`) is a config change if
the single-process model is ever outgrown. WAL mode is on so SQLite readers and
writers don't block each other.

### 1.3 Embedding whole emails destroys retrieval quality

Spec §6 says "implement embeddings for messages". Embedding an entire email as
one vector fails on long threads: quoted history and signatures dominate the
vector and the actual content gets averaged away.

**Decision:** chunk before embedding, and store chunks separately from
messages. Megamind's `core/chunker.py` already solves this well — paragraph
grouping with an elastic ceiling and sentence-boundary overlap — and it ports
over nearly unchanged. Email needs one addition: strip quoted replies and
signatures before chunking.

### 1.4 Prompt injection is under-specified as a prompt problem

Spec §15 asks the agent to "distinguish between USER INSTRUCTION and UNTRUSTED
CONTENT". Doing that with prompt wording alone is not sufficient — a 12B local
model is considerably easier to talk out of its instructions than a frontier
model.

**Decision:** three layers, with the load-bearing one being capabilities.
1. Fenced, labelled untrusted blocks whose delimiters the content cannot forge.
2. System-prompt trust rules.
3. **No dangerous tools exist to hijack.** A successful injection can make the
   model say something wrong; it cannot make it send mail, because there is no
   send tool and the capability check would reject one.

This is why `permissions.py` and `prompt_guard.py` are in Milestone 1, before
any external data can arrive, rather than bolted on at Milestone 5.

### 1.5 The importance classifier will spam you

Spec §10 wants an LLM importance score on every message; §11 wants
notifications above a threshold. Run naively, the first Gmail sync classifies
20,000 historical emails — hours of GPU time — and then notifies on the
backlog.

**Decision:** classify only messages newer than the sync watermark; never
notify during a backfill; rate-limit notifications per hour; and require both a
score above threshold *and* `requires_action` before notifying.

### 1.6 `gemma4:26b` may not be a real tag

The spec names Gemma 4 26B/12B. Those tags may not exist in Ollama's registry
under that name. Since the spec also (correctly) requires no hard-coded model
names, this resolves itself: the defaults ship as `gemma3:27b`/`gemma3:12b`,
`start.sh` warns when the configured tag is not installed, and the status page
prints the exact `ollama pull` command. Set it to whatever `ollama list` shows.

### 1.7 "Desktop app" vs. the specified stack

The request opens with "desktop app"; §1 specifies React + Vite, which is a web
app. Milestone 1 ships the web UI bound to localhost. A Tauri wrapper (~200
lines, reuses the same frontend build, gives a real `.app` and a menu-bar icon)
is the right way to make it a genuine desktop app, and is worth doing once the
connectors work.

---

## 2. Final architecture

```
                        ┌──────────────────────────────────────┐
 EXTERNAL               │  Gmail   Calendar   iMessage   files │
                        └───────────────────┬──────────────────┘
                                            │
 ACQUISITION            Push │ Poll │ Local │ Import   ← 4 connector classes
                                            │
 NORMALISATION          ┌───────────────────▼──────────────────┐
                        │  Message / CalendarEvent / Contact   │
                        │  one schema regardless of source     │
                        └───────────────────┬──────────────────┘
                                            │
 EVENT BUS              ──── AgentEvent ────┼──── fan-out to workers
                                            │
 STORAGE                ┌───────────────────▼──────────────────┐
                        │ SQLite (WAL)  ·  FTS5  ·  vectors    │
                        └───────────────────┬──────────────────┘
                                            │
 RETRIEVAL              ┌───────────────────▼──────────────────┐
                        │ hybrid: BM25 + dense + recency +     │
                        │ importance, fused by RRF             │
                        └───────────────────┬──────────────────┘
                                            │
 TRUST BOUNDARY         ─── fence untrusted content ───────────
                                            │
 INFERENCE              ┌───────────────────▼──────────────────┐
                        │ LLMProvider → Ollama (local or LAN)  │
                        │ roles: large / fast / router / embed │
                        └───────────────────┬──────────────────┘
                                            │
 AGENT                  ┌───────────────────▼──────────────────┐
                        │ read-only tools, capability-checked  │
                        └───────────────────┬──────────────────┘
                                            │
 SURFACE                     Chat UI  ·  Notifications  ·  Status
```

### Key seams

**`LLMProvider`** (`backend/llm/base.py`) — the only place that knows how to
talk to an inference backend. `OllamaProvider` uses raw httpx rather than the
`ollama` SDK specifically because the SDK reads a process-global `OLLAMA_HOST`,
which makes per-role base URLs impossible.

**`LLMRegistry`** (`backend/llm/registry.py`) — maps a *role* to a concrete
(provider, model, context window). Callers ask for "the fast model", never a
model name. This is what makes spec §25 a config change.

**`EventBus`** (`backend/events/bus.py`) — connectors publish, workers
subscribe. Subscribers have bounded queues and are dropped-oldest, so a stalled
WebSocket client cannot back-pressure ingestion.

**`prompt_guard`** — the only sanctioned path for external text into a prompt.

---

## 3. Reused from Megamind

[Dev05d/Megamind](https://github.com/Dev05d/Megamind) already solved several of
these problems well. What carries over:

| Megamind | Here | Change |
|---|---|---|
| `core/memory_manager.py` | `backend/llm/context_budget.py` | **Ported.** Cascading eviction — retrieved chunks first, then whole conversation turns — plus exact token counts from Ollama's `/api/tokenize` with a chars/4 fallback. Made async; returns a result object instead of printing. |
| `core/chunker.py` | M3 `backend/embeddings/chunker.py` | **Port nearly as-is.** Elastic paragraph chunking with sentence-boundary overlap is exactly right for email bodies. Needs quoted-reply stripping added. |
| `core/vector_store.py` | M3 `backend/embeddings/store.py` | **Adapt.** The dense+sparse hybrid with RRF fusion is the right retrieval design. Change: embedded Qdrant → single-process ownership (see §1.2), and `uuid5(source_i)` point IDs → stable content-hash IDs so re-ingesting a modified email updates rather than duplicates. |
| `core/router.py` | M5 `backend/agent/planner.py` | **Adapt.** Pydantic `format=` schema for structured output on a small model is the right technique, and the `needs_novel_retrieval` route (excluding already-seen chunk IDs) is a genuinely good idea worth keeping. |
| `core/config.py` | `backend/config.py` | **Superseded** by pydantic-settings, but the `OLLAMA_HOST` indirection was already there and is why remote inference was cheap to support. |

What does not carry over: the `input()`-driven CLI loop, the global mutable
client singletons, and printing to stdout from library code.

One thing worth fixing in Megamind itself: **`.env` is committed to that repo.**
Even with only model names in it today, it is the file that will eventually hold
an API key. Add it to `.gitignore` and commit a `.env.example` instead.

---

## 4. Target database schema (M2–M8)

Milestone 1 ships `conversations`, `chat_turns`, `sources`, `app_settings`.
The rest arrive with the code that fills them.

```
sources           id, kind, account_identifier, status, sync_cursor, config
contacts          id, display_name, emails[], handles[], first_seen, last_seen
message_threads   id, source_id, source_thread_id, subject, participants[],
                  last_message_at, message_count
messages          id, source_id, source_message_id, thread_id, sender_contact_id,
                  recipients[], subject, body_text, body_html_sanitized,
                  timestamp, labels[], is_read, is_from_me, metadata
attachments       id, message_id, filename, mime_type, size_bytes, local_path
calendar_events   id, source_id, source_event_id, title, description, location,
                  starts_at, ends_at, all_day, recurrence, attendees[], status
derived_metadata  message_id, importance, requires_action, category, deadline,
                  summary, people[], suggested_action, model, created_at
chunks            id, message_id, chunk_index, text, token_count
embeddings        chunk_id, vector, model, dim, created_at
memories          id, content, kind(user|derived), source_ref, created_at,
                  confirmed_by_user
notifications     id, kind, title, body, importance, message_ref, created_at,
                  seen_at, dismissed_at
jobs              id, type, payload, status, attempts, last_error,
                  scheduled_for, idempotency_key
agent_runs        id, conversation_id, question, tools_called, sources_used,
                  duration_ms, model, created_at
```

Indexes: `messages(timestamp)`, `messages(thread_id)`, `messages(sender_contact_id)`,
`messages(source_id, source_message_id)` unique, `calendar_events(starts_at)`,
`derived_metadata(importance)`, `derived_metadata(deadline)`, `jobs(status, scheduled_for)`.

FTS5 virtual table over `messages(subject, body_text)`, synced by trigger.

`messages.source_message_id` unique per source is what makes ingestion
idempotent — the requirement in spec §18 that re-running a sync must not
duplicate anything.

---

## 5. Retrieval design (M3)

Four signals, fused rather than picked:

```
score = w_bm25 · BM25(q, d)          keyword — for names, IDs, exact phrases
      + w_dense · cos(q, d)          semantic — for "that interview email"
      + w_recent · decay(age)        recency  — last week usually beats last year
      + w_import · importance(d)     salience — flagged/actionable ranks up
```

Reciprocal Rank Fusion over the BM25 and dense result sets (Megamind's
approach), then re-rank by the recency and importance terms. Weights are
configurable.

Keyword search alone fails on "the email about scheduling an interview" when
the mail says "let's find a time to chat". Vector search alone fails on
"invoice 4417". Both are needed, which is why the spec's "do not rely
exclusively on vector search" is correct.

Every retrieved chunk carries `message_id` through to the answer, which is what
makes citations real rather than the model's recollection of a source.

---

## 6. Threat model

| Threat | Mitigation |
|---|---|
| Injected instructions in an email | Fenced untrusted blocks; forgery-proof delimiters; no write tools exist |
| Exfiltration via a tool | Read-only capabilities; no network-egress tool |
| Malicious HTML/CSS in mail | Sanitize on ingest; store text separately; no remote resource loading in the UI |
| LAN attacker reaching the API | Loopback bind by default; refuses non-loopback without a token |
| OAuth token theft from the browser | Tokens never leave the backend; UI never sees them |
| Token theft from disk | Encrypted at rest with `CREDENTIAL_ENCRYPTION_KEY` |
| Accidental secret commit | `.env` gitignored; `.env.example` carries no values |
| Data leaving the machine | Ollama only; no cloud provider implemented |

Assumption: the machine itself and its user are trusted. Gary does not defend
against someone with local root or physical disk access.
