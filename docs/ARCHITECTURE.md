# Architecture

Design decisions behind Gary, the problems found in the original
specification, and the data model that later milestones build out.

**Revision 2** reflects two decisions made after the first pass:

- **Live-only ingestion.** No historical backfill. Gary starts recording when
  you connect an account.
- **Three storage planes instead of one.** Embeddings are for prose. Structured
  facts and extracted commitments get real columns and real indexes.

Scope for the next phase is deliberately narrow: **Gmail, Google Calendar, and
iMessage.** Nothing else. All three are now implemented, ahead of the
Milestone 3–5 work this document was originally written before — see
`backend/connectors/{gmail,calendar,imessage}/` and `backend/workers/`. The
design below was written before the code; it held up unchanged through
implementation.

Companions: [DATA-MODEL.md](DATA-MODEL.md) for storage and retrieval detail,
[EDGE-CASES.md](EDGE-CASES.md) for the failure register.

---

## 1. Should email be stored as semantic vectors?

Partly. Embedding a whole email is the wrong default, and it is worth being
precise about why, because it determines the entire data model.

An email contains at least three kinds of information, and they want three
different storage strategies:

| Kind | Example | Right storage | Why |
|---|---|---|---|
| **Structured facts** | sender, timestamp, thread, labels, read state | SQL columns + indexes | Exact, filterable, sortable. A vector cannot answer "before Tuesday". |
| **Extracted commitments** | "proposal due Friday 5pm" | SQL rows with a real `due_at` column | You need to *query by date*, sort by urgency, and mark done. |
| **Prose** | "let's find a time to chat about the internship" | Vectors + FTS5 | Fuzzy recall. This is what embeddings are actually good at. |

### Why metadata must not be embedded

Embeddings destroy exactly the properties metadata is useful for:

- **Dates blur.** "August 15" and "August 25" are near-identical vectors.
  "Last Tuesday" has no stable representation at all. Any question with a time
  bound — *most* questions about your own mail — degrades to guessing.
- **Names collide.** "Sarah Chen" and "Sara Chen" and "Sarah Chan" sit almost
  on top of each other. For a filter you need exact identity, not proximity.
- **No aggregation.** "How many unread from my advisor this week" is a
  `COUNT(*)` with a `WHERE`. Vector search cannot count, and top-k means it
  cannot even see the whole set.
- **Signal dilution.** Embedding `From: prof@uni.edu | Aug 12 | Subject: Re:` +
  a 900-word quoted thread produces a vector dominated by boilerplate. The one
  sentence that matters is averaged into noise.

So: **structured fields become indexed columns; only the cleaned prose body is
embedded.**

### What is worth embedding

- The **cleaned body** — quoted replies, signatures, legal footers, and
  tracking pixels stripped. Chunked, roughly a paragraph at a time.
- **Calendar title + description**, so "the meeting about the budget" resolves.
  The times and attendees stay in columns.
- **Conversation sessions** for iMessage, not individual messages (see §5).

### What is not worth embedding

- **Anything under ~15 tokens.** "ok", "sounds good", "thanks" produce vectors
  that match everything and mean nothing. They stay in SQL and FTS5.
- **Automated mail.** Receipts, CI notifications, newsletters, no-reply alerts.
  On a typical account this is 60–80% of volume, and embedding it is how
  semantic search gets poisoned — you ask about a flight and get twelve
  promotional fares. Classify first, embed selectively.
- **Attachments**, until a milestone actually needs document search.

The rule: *embed what you would recognise but could not quote.*

---

## 2. Live-only ingestion

No backfill. On connect, Gary records a watermark and only ingests what
arrives after it.

**What this buys:**

- No multi-hour first sync, no pagination through 20,000 messages, no rate-limit
  choreography, no "is it done yet" progress UI.
- Every message can get the **full** treatment — clean, classify, extract,
  embed — because the volume is ~50–100/day, not 20,000 at once.
- No risk of a backfill triggering thousands of notifications.
- The database stays small enough that SQLite and an embedded vector store are
  comfortably the right tools.
- Failure recovery is trivial: the watermark is the only state.

**What it costs, stated plainly:**

Gary is empty on day one and knows nothing about last month. "What did Professor
Smith say about my project?" fails until Professor Smith emails you again. The
system becomes useful over days, not minutes.

**One mitigation, off by default.** A `seed_window_days` setting pulls a small
recent window on first connect — 7 days is enough to make day one feel alive.
This is a bootstrap, not a backlog: it is bounded, it runs once, and it is
capped. Default `0` (pure live) per your call; set it to `7` if the empty first
day bothers you.

### Calendar is the exception, and it matters

"From now on" is the wrong framing for a calendar. A calendar's value is in the
**future** — the events you have not attended yet. Ingesting only "events
created from now on" would miss the meeting scheduled last week for tomorrow,
which is precisely the thing you want to ask about.

So Calendar syncs a **window**, not a watermark:

```
[ now − 7 days ]  ──────────────►  [ now + 90 days ]
   recent past                        the useful part
```

The small past window supports "when did I last meet Sarah?". Both bounds are
settings. This is not a backlog — it is what a calendar *is*.

### Per-source watermark mechanics

| Source | Watermark | Incremental mechanism | Failure mode |
|---|---|---|---|
| Gmail | `historyId` from `users.getProfile` at connect | `users.history.list(startHistoryId=…)` | History IDs expire after ~7 days idle → 404. Fall back to `messages.list(q=after:<last_seen>)` to bridge the gap, then resume. |
| Calendar | `syncToken` from the first windowed list | `events.list(syncToken=…)` | Token invalidated (410) → re-list the window, get a fresh token. |
| iMessage | `MAX(ROWID)` in `chat.db` at connect | `WHERE ROWID > watermark` | Row IDs are monotonic; no expiry. Schema changes between macOS versions are the real risk. |

### Polling, not Pub/Sub

Gmail push requires a public HTTPS endpoint Google can reach. On a laptop
behind NAT that means running a tunnel — real operational weight for a
local-first app, and a dependency on a third party seeing your notification
traffic.

`users.history.list` against a watermark is one cheap call that returns nothing
when idle. **Polling every 60s is the default.** Push stays available as an
advanced option for anyone who already runs a tunnel. This is simpler, more
private, and the latency difference is under a minute.

---

## 3. Three storage planes

```
                    ┌──────────────────────────────────────────┐
                    │  Gmail   ·   Calendar   ·   iMessage     │
                    └────────────────────┬─────────────────────┘
                                         │  live, past the watermark
                    ┌────────────────────▼─────────────────────┐
                    │  normalise → clean → classify → extract  │
                    └────────────────────┬─────────────────────┘
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        │                                │                                │
┌───────▼─────────┐            ┌─────────▼────────┐            ┌──────────▼────────┐
│ FACTS           │            │ COMMITMENTS      │            │ SEMANTIC          │
│ SQL + indexes   │            │ SQL + indexes    │            │ vectors + FTS5    │
│                 │            │                  │            │                   │
│ messages        │            │ commitments      │            │ chunks            │
│ threads         │            │  due_at ────────►│            │ embeddings        │
│ contacts        │            │  owner           │            │ messages_fts      │
│ calendar_events │            │  status          │            │                   │
│ attachments     │            │  evidence_quote  │            │ cleaned prose     │
│                 │            │  source_message  │            │ only              │
└───────┬─────────┘            └─────────┬────────┘            └──────────┬────────┘
        │                                │                                │
        └────────────────────────────────┼────────────────────────────────┘
                                         │
                          ┌──────────────▼───────────────┐
                          │  query router picks a plane  │
                          └──────────────┬───────────────┘
                                         │
                              typed, read-only agent tools
```

The router is the precision win. Most questions about your own data are **not**
semantic:

| Question | Plane | Actual operation |
|---|---|---|
| "What do I have tomorrow?" | facts | `calendar_events WHERE starts_at::date = ?` |
| "What's due this week?" | commitments | `WHERE due_at BETWEEN ? AND ? AND status='open'` |
| "Who haven't I replied to?" | facts | threads where last message `is_from_me = false` |
| "What did Prof Smith send last week?" | facts | `WHERE sender_contact_id = ? AND timestamp > ?` |
| "Find the email about the interview" | semantic | vector + FTS5 hybrid |
| "Catch me up" | composite | several structured queries, then summarise |

Only one of those is a vector search. A design that routes everything through
embeddings gets five of the six wrong, which is why "semantic search over my
email" products feel unreliable.

**Filter before you search, never after.** "Emails from Sarah last month about
the lease" applies `sender` and date as *payload filters inside* the vector
query. Searching first and filtering the top-k afterwards throws away recall —
if Sarah's email ranked 40th globally, post-filtering never sees it.

---

## 4. Tracking tasks and deadlines

This is the part that does not work without structure, and it is the direct
answer to "if it's a Gmail with a task and a deadline, how will we track that?"

### The pipeline, per message

```
raw message
   ↓  normalise           → common schema regardless of source
   ↓  clean               → strip quoted replies, signatures, footers, trackers
   ↓  classify            → category, importance, is_automated   [fast model]
   ↓  extract             → commitments, with evidence + dates    [fast model]
   ↓  validate            → grounding + date sanity              [pure code]
   ↓  reconcile           → against open items in the same thread
   ↓  persist             → structured rows
   ↓  embed               → cleaned prose only, if worth it
```

Classification and extraction are one call with a strict JSON schema
(`MessageAnalysis` in `backend/pipeline/schemas.py`), passed to Ollama's
`format=` structured-output parameter and run on the **fast** model.

### Deadlines become real columns

```sql
commitments (
  id, source_message_id, thread_id,
  kind,             -- task | deadline | meeting_request | question | promise
  title,
  owner,            -- me | them | unclear
  due_at,           -- TIMESTAMP, INDEXED  ← the whole point
  due_precision,    -- exact | day | week | month | vague | none
  status,           -- open | done | cancelled | snoozed | dismissed
  confidence,
  evidence_quote,   -- verbatim span from the source
  created_at, updated_at
)
CREATE INDEX ON commitments (status, due_at);
CREATE INDEX ON commitments (thread_id);
```

`due_at` being an indexed timestamp is what makes "what's due this week" a
range scan rather than a hopeful search. `due_precision` keeps the UI honest:
"sometime next week" and "Friday 5pm" both produce a timestamp, but only one
should drive an alarm.

`owner` splits the two questions people actually have:

- *What do I owe?* → `owner = 'me' AND status = 'open'`
- *What am I waiting on?* → `owner = 'them' AND status = 'open'`

"Can you send the report by Friday?" is mine. "I'll send the report by Friday"
is theirs. Same sentence shape, opposite meaning, and the difference is the
entire value of a follow-up feature.

### Three failure modes, and the defences

**1. The model invents a deadline.** Every commitment must carry a verbatim
`evidence_quote`, and `verify_grounding()` checks the quote actually appears in
the message — normalised for whitespace, case, and smart quotes, with a token
overlap fallback for minor rewording. Ungrounded extractions are discarded
before they reach the database. A quote shorter than three tokens is rejected
outright: `"due"` is a substring of half the emails ever written and verifies
nothing.

**2. Relative dates resolve wrong.** "by Friday" means nothing without an
anchor. The extractor gets the *message's own timestamp* and the user's
timezone, and returns absolute ISO-8601. `check_due_date()` then rejects
anything before the message was sent (with a few hours of grace for timezone
skew) or implausibly far out. A rejected date does not discard the task — it
keeps the task and drops the date, because a wrong deadline is worse than no
deadline.

**3. Threads produce duplicates.** A five-reply thread about one deadline must
not create five tasks, and "actually, let's move it to Monday" must move the
existing task rather than create a second one.

The fix is **thread-aware extraction**: when a message arrives in a thread with
open commitments, those commitments are included in the extractor's input, and
it can return `updates` (change the due date, mark done, cancel) as well as new
items. Reconciliation happens against a known set instead of by guessing at
similarity after the fact.

```
EXISTING OPEN ITEMS IN THIS THREAD:
  [c_7f2a] "Submit research proposal" due 2026-08-15T17:00Z

NEW MESSAGE:
  "Let's push the proposal to Monday the 18th."

→ updates: [{commitment_id: "c_7f2a", new_due_at: "2026-08-18T17:00Z",
             evidence_quote: "push the proposal to Monday the 18th"}]
```

**4. Calendar events are not extracted.** They already have structured start
and end times. Extracting a deadline from "Meeting: Thursday 2pm" would create
a worse duplicate of a fact you already hold. Calendar events link to
commitments; they do not generate them.

### The user stays in control

Extraction is a suggestion, not a verdict. Every commitment can be marked done,
snoozed, or **dismissed** — and a dismissal is remembered, so the same email
does not resurrect the task on the next reconciliation pass. `confidence` below
a threshold surfaces as "possible task" rather than a bare assertion.

---

## 5. iMessage needs different treatment

Applying the email pipeline to iMessage produces noise. The characteristics are
genuinely different:

| | Email | iMessage |
|---|---|---|
| Length | paragraphs | a few words |
| Volume | ~50/day | ~500/day |
| Unit of meaning | one message | a burst of messages |
| Structure | subject, thread, formal | none |
| Timing | hours apart | seconds apart |

**The unit of meaning is a session, not a message.** "friday works" is
meaningless alone; it means something only alongside the three messages before
it. So iMessage is grouped into **sessions** — consecutive messages in one
conversation with no gap longer than ~30 minutes — and the *session* is what
gets summarised, embedded, and extracted from.

This also fixes the volume problem: 500 messages a day becomes maybe 20
sessions, which is a sane amount of LLM work.

Individual messages still land in the facts plane with full fidelity, so
`is_from_me`, timestamps, and per-message search all work exactly as expected.

**Access:** `~/Library/Messages/chat.db` is your own SQLite database on your own
Mac. Gary opens an **immutable read-only copy**, never the live file, so it
cannot corrupt or lock what Messages.app is using. Requires Full Disk Access
that you grant, macOS only, this machine's history only. Timestamps are Apple
epoch (nanoseconds since 2001-01-01) and need conversion.

---

## 6. Revised database schema

Milestone 1 shipped `conversations`, `chat_turns`, `sources`, `app_settings`.
The rest arrives with the code that fills it.

```
FACTS
  contacts          id, display_name, emails[], handles[], first_seen, last_seen
  message_threads   id, source_id, source_thread_id, subject, participants[],
                    last_message_at, last_message_from_me, message_count
  messages          id, source_id, source_message_id UNIQUE, thread_id,
                    sender_contact_id, recipients[], subject,
                    body_clean, body_raw, timestamp, labels[],
                    is_read, is_from_me, session_id, metadata
  attachments       id, message_id, filename, mime_type, size_bytes, local_path
  calendar_events   id, source_id, source_event_id UNIQUE, title, description,
                    location, starts_at, ends_at, all_day, recurrence,
                    attendees[], organizer, status
  sessions          id, source_id, thread_id, started_at, ended_at,
                    message_count, summary          -- iMessage grouping

DERIVED
  message_analysis  message_id, category, importance, requires_action,
                    is_automated, summary, people[], model, created_at
  commitments       (see §4)
  dismissals        commitment_fingerprint, dismissed_at   -- do not resurrect

SEMANTIC
  chunks            id, message_id | session_id | event_id, chunk_index,
                    text, token_count
  embeddings        chunk_id, vector, model, dim, created_at
  messages_fts      FTS5 virtual table over (subject, body_clean)

OPERATIONS
  sync_state        source_id, watermark, last_sync_at, last_error, consecutive_failures
  jobs              id, type, payload, status, attempts, last_error,
                    scheduled_for, idempotency_key
  notifications     id, kind, title, body, importance, commitment_id,
                    created_at, seen_at, dismissed_at
  memories          id, content, kind(user|derived), source_ref,
                    confirmed_by_user, created_at
  agent_runs        id, conversation_id, question, tools_called, sources_used,
                    duration_ms, model, created_at
```

Indexes that matter:

```sql
messages (timestamp DESC)                    -- "what came in today"
messages (sender_contact_id, timestamp)      -- "what did Sarah send"
messages (thread_id, timestamp)
messages (source_id, source_message_id)      -- UNIQUE: idempotent ingest
calendar_events (starts_at)                  -- "what's tomorrow"
commitments (status, due_at)                 -- "what's due this week"
commitments (owner, status)                  -- "what am I waiting on"
message_analysis (importance DESC)
threads (last_message_at, last_message_from_me)  -- "who am I ignoring"
```

`messages(source_id, source_message_id)` being UNIQUE is what makes ingestion
idempotent: replaying a sync is a no-op, which is the spec's §18 requirement
and the thing that makes crash recovery boring.

---

## 7. Retrieval

Hybrid, but only when the question is actually semantic.

```
route(question)
  ├─ structured  → typed SQL tool, exact filters, done
  ├─ semantic    → filter-then-search, hybrid fusion
  └─ composite   → several structured queries, then summarise
```

For the semantic path:

```
score = w_keyword   · BM25(q, chunk)        exact terms, names, invoice numbers
      + w_semantic  · cos(q, chunk)         paraphrase and description
      + w_recency   · decay(age)            last week usually beats last year
      + w_important · importance(msg)       actionable outranks newsletters
```

Reciprocal Rank Fusion across the BM25 and dense result sets, then re-rank by
recency and importance. All four weights are settings.

Keyword alone fails on "the email about scheduling an interview" when the mail
says "let's find a time to chat". Vector alone fails on "invoice 4417". Both
are needed — which is why the spec's "do not rely exclusively on vector search"
is right.

Every chunk carries `message_id` through to the answer, so citations point at a
stored message you can open, not at the model's recollection.

---

## 8. Problems found in the original spec

Kept from revision 1; the connector analysis is unchanged and still governs.

### 8.1 Two requested connectors cannot be built as described

The request named Gmail, iMessage, Instagram DMs, and Discord DMs. §2 of the
spec also says never to scrape where an official API exists and never to bypass
platform restrictions. Those conflict for half the list:

- **Discord DMs** — the bot API has no access to a user's private DMs. The only
  live path is a self-bot on a user token, against ToS and enforced with
  account termination.
- **Instagram DMs** — the Messaging API is a customer-service product for
  Business/Creator accounts, requires app review, and does not expose personal
  DM history.

Both offer official **data exports**, which is the legitimate path — a periodic
snapshot, not a live feed. Out of scope for this phase either way.

- **iMessage** — the spec is too pessimistic. Reading your own local database
  with access you grant is not an auth bypass. See §5.

### 8.2 Embedded vector stores lock their directory — resolved by choosing sqlite-vec

`QdrantClient(path=…)` takes an exclusive lock, so a server and separate worker
processes cannot both open it. Worse, a separate store means **no transactional
deletion**: removing a message and removing its vectors are two operations that
can diverge, leaving orphaned vectors that surface in search results for mail
the user deleted.

**Decision: sqlite-vec.** Vectors live in the same SQLite file as everything
else. See §12 for the measurements behind this.

### 8.3 Prompt injection is not a prompt problem

A 12B local model is much easier to talk out of its instructions than a
frontier model. Three layers, and the load-bearing one is not the prompt:

1. Fenced untrusted blocks whose delimiters content cannot forge.
2. System-prompt trust rules.
3. **No dangerous tools exist to hijack.** A successful injection can make the
   model say something wrong; it cannot make it send mail, because there is no
   send tool and the capability check would reject one.

This is why `permissions.py` and `prompt_guard.py` shipped in Milestone 1.

### 8.4 Notification spam

Live-only ingestion removes the backfill-storm risk entirely. The remaining
guards: require importance above threshold **and** `requires_action`; suppress
anything `is_automated`; hard rate limit per hour; quiet hours.

### 8.5 `gemma4:26b` may not exist under that tag

Unverified. Since nothing hard-codes a model name this resolves itself:
defaults ship as `gemma3:27b`/`gemma3:12b`, and the settings page prints the
exact `ollama pull` command when a configured tag is not installed.

### 8.6 "Desktop app" vs. React + Vite

Milestone 1 ships the web UI on localhost. A Tauri wrapper reuses the same
frontend build and gives a real `.app` — worth doing once the connectors work.

---

## 9. Reused from Megamind

[Dev05d/Megamind](https://github.com/Dev05d/Megamind) already solved several of
these well.

| Megamind | Here | Change |
|---|---|---|
| `core/memory_manager.py` | `backend/llm/context_budget.py` | **Ported.** Cascading eviction, exact token counts from `/api/tokenize` with a chars/4 fallback. Made async; returns a result object instead of printing. |
| `core/chunker.py` | M3 `backend/pipeline/chunker.py` | **Port nearly as-is.** Elastic paragraph chunking with sentence-boundary overlap is right for email bodies. Add quoted-reply and signature stripping first. |
| `core/vector_store.py` | M3 `backend/embeddings/store.py` | **Adapt.** Dense+sparse hybrid with RRF is the right retrieval design. Changes: single-process ownership (§8.2); content-hash point IDs instead of `uuid5(source_i)` so a re-ingested message updates rather than duplicates; **payload filters applied inside the query** so structured constraints do not lose recall. |
| `core/router.py` | M5 `backend/agent/router.py` | **Adapt and expand.** Pydantic `format=` structured output on a small model is the right technique. Expanded from "retrieve or not" to "which storage plane" (§3). The `needs_novel_retrieval` route excluding already-seen chunk IDs is a good idea worth keeping. |
| `core/config.py` | `backend/config.py` | **Superseded** by pydantic-settings, but the `OLLAMA_HOST` indirection was already there and is why remote inference was cheap to support. |

Not carried over: the `input()` CLI loop, global mutable client singletons, and
printing to stdout from library code.

Worth fixing in Megamind itself: **`.env` is committed to that repo.** Only
model names today, but it is the file that will eventually hold a key.

---

## 10. Vector store: measured, not assumed

The stated objection to sqlite-vec is that it brute-forces rather than building
an ANN index, and struggles past ~1M vectors. That objection does not apply
here, and the numbers are worth writing down.

### Corpus size under live-only ingestion

~100 messages/day, ~60% skipped as bulk, ~2 chunks each ≈ **80 vectors/day**.

| Horizon | Vectors | 1024-dim on disk | Brute-force query |
|---|---|---|---|
| 1 year | 29,200 | 120 MB | 2 ms |
| 3 years | 87,600 | 359 MB | 7 ms |
| 5 years | 146,000 | 598 MB | 14 ms |

We are two orders of magnitude below where brute force becomes a problem.

### Measured with sqlite-vec (30,000 vectors, 1024-dim)

| Operation | Time |
|---|---|
| Insert 30,000 vectors | 2.0 s |
| Unfiltered k-NN (k=10) | 74 ms |
| **Pre-filtered k-NN** (sender + date + not-automated) | **1 ms** |

The last row is the decisive one. sqlite-vec's `vec0` tables support metadata
columns and a `PARTITION KEY`, so a constraint like "from Sarah, last month,
not automated" is applied **inside** the vector query. Partitioning means the
scan touches only that sender's shard — the filtered query is 74× *faster* than
the unfiltered one, not slower.

This is precisely the pre-filtering the retrieval design requires
([DATA-MODEL.md §8](DATA-MODEL.md)), where post-filtering would silently
destroy recall on low-selectivity filters.

### Why not the alternatives

| Option | Why not |
|---|---|
| Embedded Qdrant | Better ANN, irrelevant at 30k vectors. Costs transactional deletes, an exclusive directory lock, and a second thing to back up. |
| LanceDB | Genuinely good embedded option, but its IVF-PQ advantage only pays off past ~1M vectors, and it adds an Arrow/Lance dependency and separate files for no gain here. |
| Qdrant in Docker | Breaks "runs without Docker on macOS" and adds a daemon to a personal app. |
| In-process numpy | Fast, but reimplements persistence, filtering, and crash safety that SQLite already has. |

### What this buys

- **Transactional deletion.** Removing a message removes its chunks, its FTS
  rows, and its vectors in one transaction. No orphans, no reconciliation
  sweep, no deleted mail resurfacing in search.
- **One file.** Backup is `cp gary.db`. So is restore.
- **No second process, no lock contention**, and §8.2 above stops being a
  constraint on the architecture.

### The one real gotcha

sqlite-vec is a loadable extension, and `sqlite3.enable_load_extension` is
**disabled in Apple's system Python**. Homebrew and python.org builds have it.
Startup checks for the capability and fails with the fix rather than a
stack trace deep in a query.

**Dimension note.** `qwen3-embedding:4b` emits 2560 dimensions, which is 1.5 GB
at five years. A 1024-dim model (or a Matryoshka-truncated qwen3) cuts that
2.5× for negligible quality loss on this workload. The default should be
1024-dim; 2560 remains available for anyone who wants it.

---

## 11. Images

Images get their own treatment because neither the text pipeline nor a single
embedding model handles them well.

### Two models, because images are two different things

An image embedding encodes *what a picture looks like*. It does not read. CLIP
sees "a screenshot of a messaging app", not the address written in it — and a
large share of personal images are screenshots and scans, which are text
wearing an image's clothes.

So images are **routed**, not uniformly processed
(`backend/pipeline/image_policy.py`):

| Signal | Route | Why |
|---|---|---|
| Camera EXIF present | embed | Only a camera writes an aperture value |
| Dimensions match a device screen exactly | OCR | No camera produces 1179×2556 |
| Filename says "Screenshot"/"Scan" | OCR | The device already told us |
| >55% near-white | OCR | A page, not a scene |
| <3000 distinct colours | OCR | Flat UI palette; photos are continuous-tone |
| Camera EXIF **and** mostly white | both | A photographed document |
| Ambiguous PNG | both | Cheaper than guessing wrong |
| Under 64px, or inline in email | skip | Tracking pixels, logos, spacers |

Every check is metadata-only. Running OCR on a holiday photo to discover it has
no text is exactly the waste this avoids.

### Scope

Email attachments and iMessage photos. **Inline email images are excluded** —
they are logos, banners and signature graphics almost without exception, and
embedding them returns brand assets for every query.

### CLIP means a second index

CLIP ViT-B/32 was chosen deliberately, and it has a structural consequence
worth stating plainly.

CLIP's vectors live in **their own space**. They cannot be compared with the
text-embedding vectors used for messages. So:

```
query ─┬─► text embedder ──► message/chunk index ──► ranked list A
       └─► CLIP text tower ─► image index (512-dim) ─► ranked list B
                                                          │
                              RRF fuses A and B ◄──────────┘
```

Two indexes, and the query is encoded twice. **RRF rescues this**: fusion is
rank-based, not score-based, so two incomparable score scales merge correctly
anyway. That is the same property that made RRF the right choice for BM25 +
dense fusion (§7), and it is why CLIP remains workable despite the split space.

The alternative — `nomic-embed-vision`, which shares a space with
`nomic-embed-text` — would have collapsed this to one index and one query
encoding, at the cost of pinning the text embedder. That trade was declined.

Cost is small: 512 dimensions is 2KB per image, so even a heavy year of photos
is well under 100MB.

**Dependency note.** CLIP normally arrives via `torch` + `transformers`, which
is roughly 2GB. An ONNX export of ViT-B/32 is ~150MB and runs on
`onnxruntime` with no torch — the right choice for an app whose whole premise
is running comfortably on a personal machine.

### Text extracted by OCR is second-class

OCR output is marked as such and ranked below text that came from a real text
layer, because its error rate is materially higher. A retrieval hit on OCR'd
text says where to look; it is not quotable evidence, and the grounding check
(§4) would rightly reject a quote drawn from it.

### Location data is stripped

Photos routinely carry precise GPS coordinates. Indexing them would make
"photos from Paris" possible — and would also turn the archive into a movement
history, which is a far larger disclosure than the photos themselves and not
something anyone expects from a mail client. GPS EXIF is removed on ingest
unless explicitly opted in.

---

## 12. Revised milestones

| # | Milestone | Contents |
|---|---|---|
| 1 | Foundation ✅ | FastAPI + SQLite + Ollama + chat UI + settings |
| 2 | Facts plane ✅ | Gmail OAuth, live watermark, polling, normalise + store. "What came in today?" |
| 3 | Commitments plane | Clean, classify, extract, ground, reconcile. "What's due this week?" |
| 4 | Semantic plane | FTS5 + chunking + embeddings + hybrid retrieval |
| 5 | Agent | Query router + typed read-only tools over all three planes |
| 6 | Calendar ✅ | Windowed sync ✅. Linking events to commitments waits on Milestone 3. |
| 7 | iMessage ✅ | Local read-only connector, session grouping |
| 8 | Proactive | Notifications and the daily briefing |

Milestone 2 is deliberately boring: get real mail into real rows, correctly and
idempotently, with no LLM in the path. Everything else stacks on that being
right.

6 and 7 landed out of order, before 3–5: both are more connectors of the exact
shape 2 already validated (OAuth or local file → watermark or window → normalise
→ store), not new architecture, so there was nothing about them that depended on
extraction or retrieval existing first. The part of 6 that *does* depend on
Milestone 3 — resolving "am I free Tuesday, and does that clash with anything
I've promised" — is still open.

---

## 13. Threat model

| Threat | Mitigation |
|---|---|
| Injected instructions in an email | Fenced untrusted blocks; forgery-proof delimiters; no write tools exist |
| Fabricated deadlines and tasks | Verbatim evidence quotes verified against the source; date sanity checks |
| Exfiltration via a tool | Read-only capabilities; no network-egress tool |
| Malicious HTML/CSS in mail | Sanitised on ingest; text stored separately; no remote resource loading in the UI |
| LAN attacker reaching the API | Loopback bind by default; refuses non-loopback without a token |
| OAuth token theft from the browser | Tokens never leave the backend |
| Token theft from disk | Encrypted at rest with `CREDENTIAL_ENCRYPTION_KEY` |
| Corrupting the live iMessage DB | Immutable read-only copy, never the live file |
| Accidental secret commit | `.env` gitignored; `.env.example` carries no values |

Assumption: the machine and its user are trusted. Gary does not defend against
local root or physical disk access.
