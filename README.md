# Gary — Local Personal Intelligence Agent

A private, local-first assistant that ingests your digital life, stores it on
your own machine, and answers questions about it — with citations back to the
original message.

**Everything stays local.** Your mail, messages, and calendar live in a SQLite
file on your disk. Inference runs on Ollama, on this machine or another one on
your LAN. Nothing is sent to a cloud API.

> **Status: Milestone 2 of 8, plus Calendar and iMessage (6 and 7) built
> ahead of schedule.**
> Chat works end to end, and **Gmail, Google Calendar, and iMessage all sync
> live** into a structured facts plane. Search, embeddings, commitment
> extraction, and the agent tool loop (Milestones 3–5, 8) are **not built
> yet** — Calendar and iMessage were pulled forward because they are more
> connectors of the same shape Gmail already proved, not new architecture.
> The status page tells you the truth about what is wired up.

---

## Quick start

```bash
git clone <this repo> && cd gary
cp .env.example .env      # then edit: model names + Ollama URL
./start.sh
```

Open <http://localhost:5173>.

That's it — `start.sh` creates the virtualenv, installs dependencies, creates
the database, checks that Ollama is reachable, and starts both servers.

---

## Requirements

| Thing | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended |
| Node.js | 18+ | frontend only |
| Ollama | any recent | local or on another machine |

Apple Silicon is the primary target; the backend is pure Python and the
frontend is plain Vite, so Linux works too.

---

## Setup, step by step

### 1. Install dependencies

`./start.sh` does this for you. Manually:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd frontend && npm install && cd ..
```

### 2. Install Ollama

```bash
brew install ollama        # macOS
ollama serve               # leave running
```

### 3. Download models

Gary uses **two model sizes on purpose** (spec §25): a large one for reasoning
and a small fast one for classification and extraction. Pick what your RAM can
hold:

```bash
# 32GB+ machine
ollama pull gemma3:27b     # large
ollama pull gemma3:12b     # fast

# 16GB machine
ollama pull gemma3:12b     # large
ollama pull qwen3:8b       # fast
```

Then set the tags in `.env`. **Check the tag actually exists first** — model
names change between releases:

```bash
ollama list                # what you have
```

Nothing in the source hard-codes a model name. If `ollama list` shows it, Gary
can use it.

### 4. Running the model on another machine

This is a first-class setup, not an afterthought. On the machine with the GPU:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

(The default binds to loopback only, so the LAN cannot reach it. This is the
single most common reason "it won't connect".)

Then in `.env` on the machine running Gary:

```env
OLLAMA_BASE_URL=http://192.168.1.42:11434
```

Verify before starting Gary:

```bash
curl http://192.168.1.42:11434/api/version
```

You can also split roles across machines — chat on the desktop, embeddings on
the laptop:

```env
OLLAMA_BASE_URL=http://192.168.1.42:11434
OLLAMA_EMBED_BASE_URL=http://192.168.1.99:11434
```

Gary opens one connection pool per distinct URL and the status page shows the
health of each.

### 5. Configure

There are two places to configure Gary, and the difference matters:

- **`.env`** — bootstrap only. Enough to get the backend up: where Ollama is,
  which models to try, where the database lives. Read once at startup.
- **The Settings page in the app** — everything else, 78 settings across 9
  categories, each with an explanation of what it does and what happens if you
  change it. Open it from the top bar.

Settings changed in the app are stored in Gary's database and layer on top of
`.env`. **Your `.env` file is never modified.** Every setting shows where its
current value came from — built-in default, `.env`, or the settings page — and
has a Reset button to drop back a layer.

Most changes apply immediately, including swapping models or repointing at a
different Ollama host: the connection pool is rebuilt and the next message uses
the new setting. The handful that genuinely cannot change at runtime (bind
address, port, database URL) are tagged `restart` and the page tells you.

What's in there:

| Category | Covers |
|---|---|
| Inference host | Ollama URL, separate embeddings host, timeout, keep-alive — with a **Test** button that probes a host and lists its installed models before you commit |
| Models | Model + context window + temperature for each of the four roles |
| Context & memory | Reply headroom, history depth, safety margin |
| Search & retrieval | top-k, similarity floor, chunking, and the four hybrid-ranking weights |
| Sync & ingestion | Check intervals, seed window, timezone, how eagerly people are merged |
| Proactive alerts | Importance threshold, weight of your habits, rate limit, quiet hours |
| Interface | Default model, streaming, context meter |
| Server & security | Bind address, port, API token, CORS, log level |
| Storage | Database URL, data directory |

Settings for features that aren't built yet (retrieval, sync, alerts) are shown
greyed out and labelled with the milestone that activates them. They save now
and take effect when that milestone lands — nothing pretends to work early.

The `.env` settings that matter most:

| Variable | What it does |
|---|---|
| `OLLAMA_BASE_URL` | Where inference runs. Any host on your LAN. |
| `LLM_MODEL_LARGE` / `LLM_MODEL_FAST` | Model tags from `ollama list`. |
| `LLM_CONTEXT_LARGE` | Context window in tokens. Default **32768**. |
| `LLM_GENERATION_BUFFER` | Tokens reserved for the reply (default 2048). |
| `APP_HOST` | `127.0.0.1`. Changing this requires `API_AUTH_TOKEN`. |

**About context length.** `LLM_CONTEXT_*` is passed to Ollama as `num_ctx`, and
Gary budgets the prompt to fit inside it — measuring real tokens via Ollama's
tokenizer and evicting the oldest content when needed, retrieved context first,
then whole conversation turns. The system prompt and your actual question are
never evicted. The meter in the status bar shows live utilisation.

Bigger windows cost memory. 32k is comfortable for a 27B model on 32GB+. If the
model gets evicted to CPU and crawls, drop to 8192.

### 6. Start the backend

```bash
./start.sh --api          # backend only, http://127.0.0.1:8000
```

### 7. Start the frontend

```bash
./start.sh                # both, UI at http://localhost:5173
./start.sh --build        # build the UI and serve everything from port 8000
```

### 8. Create Google OAuth credentials

You need a Google Cloud project. This is free and takes about five minutes.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a project (any name).
2. **APIs & Services → Library** → enable **Gmail API** and **Google Calendar
   API**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** (unless you have a Workspace account).
   - Fill in app name and your email. No logo or verification needed.
   - Add yourself under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorised redirect URI: `http://127.0.0.1:8000/api/auth/google/callback`
     — this must match `GOOGLE_REDIRECT_URI` exactly, port included.
5. Copy the client ID and secret into `.env`.
6. Generate an encryption key for the stored tokens:

```bash
python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

```env
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
CREDENTIAL_ENCRYPTION_KEY=...
```

Restart Gary after editing `.env`.

> **The 7-day gotcha.** While your project's publishing status is **Testing**,
> Google expires refresh tokens after 7 days and you will have to reconnect
> weekly. To stop that, set the OAuth consent screen to **In production**. Gary
> requests only read-only scopes; you may still see an "unverified app" warning
> on the consent screen, which you can click through via *Advanced → Go to
> (app name)*. Verification is only needed to distribute the app to others.

Gary requests **read-only** scopes (`gmail.readonly`, `calendar.readonly`). It
is not technically capable of sending or deleting anything — that guarantee is
enforced by Google, not just by Gary's own tool list.

### 9. Connect the account and start syncing

Open **Data sources** in the sidebar → **Connect a Google account**. A Google
tab opens; approve, and it closes itself. One consent screen covers both —
the OAuth scopes requested include `calendar.readonly` alongside
`gmail.readonly`, so Gmail and Calendar both start syncing from this one step.

From that moment Gary records new mail and calendar changes. **Nothing
historical is imported** for Gmail — the first sync stores a watermark and
ingests nothing, so it completes instantly. Mail arriving afterwards shows up
within a minute. Calendar is the one exception to live-only: it syncs a
*window* (7 days back, 90 forward) from the moment you connect, because a
calendar's value is in the future and tomorrow's meeting was probably created
last week.

If you would rather not start from an empty mailbox, set `seed_window_days`
to `7` in Settings before connecting. It pulls one recent week, once, capped.

Check progress under **Status → Records begin** and **Index**.

### 10. iMessage (macOS only, optional)

Unlike Gmail and Calendar, there is no account to sign into — iMessage reads
a read-only copy of this Mac's own `~/Library/Messages/chat.db`, so it only
does anything useful when Gary is running directly on a Mac with a Messages
history.

It is **off by default**, deliberately more so than the OAuth-based sources:
connecting Gmail is a consent screen you actively drive, while this is a
background process reading a database that holds every message on the
machine, so it does nothing until you explicitly turn it on.

1. Grant **Full Disk Access** to whatever process runs Gary: System Settings
   → Privacy & Security → Full Disk Access → add Terminal (or the Gary app),
   then restart it.
2. Open **Data sources** → **Enable iMessage**.

That click doubles as the test: it tries a real read immediately rather than
waiting for the next poll, so a missing Full Disk Access grant fails right
there with a specific message instead of silently in the background. As with
every other source, nothing historical is imported — only messages from the
moment it is switched on.

---

## What works right now

- **Gmail sync** — live-only, watermark-based, idempotent, with label scoping,
  mirrored deletions, and recovery from expired history without a full import
- **Google Calendar sync** — windowed rather than watermarked, sync-token
  deltas within that window, recurring events expanded to instances at ingest
  so "what's on Tuesday" is an indexed range scan, cancellations kept as
  tombstones rather than deleted
- **iMessage sync** — reads a read-only copy of `chat.db` on this Mac,
  decodes the `attributedBody` typedstream archive modern macOS stores text
  in, groups messages into conversation-burst sessions, routes tapbacks away
  from the message stream, off by default and opt-in per the trust model
  above
- **Structured facts plane** — messages, threads, identities, calendar
  events, chats and chat sessions, with the denormalised columns behind
  "who haven't I replied to"
- **Data horizon** — Gary knows when its records begin and says so, rather than
  reporting a coverage gap as "nothing happened"
- **Live settings, not just live chat** — every sync worker re-reads its
  settings on each poll tick rather than the values captured at boot, so a
  poll-interval change or flipping `imessage_enabled` takes effect within one
  tick, no restart
- Streaming chat with your local model, over SSE
- Conversation history persisted in SQLite
- Two-model routing: **Deep** / **Fast** toggle in the UI
- Real token counting and context-window budgeting
- **Settings page**: 78 documented settings, live connection testing, model
  discovery, provenance tracking, and hot reload without a restart
- Status page: backend reachability, per-role model availability, DB, counts
- Optional bearer-token auth for non-localhost access
- Prompt-injection trust boundary and the read-only capability model (the
  scaffolding is in place and tested *before* any external data can arrive)

## What is deliberately not built yet

Search, embeddings, commitment/task extraction, the agent tool loop,
notifications, and the daily briefing. See the roadmap below.

---

## Testing it

```bash
.venv/bin/python -m pytest              # 528 tests, no Ollama or accounts needed
```

Manual smoke test:

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/status | python3 -m json.tool

curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Are you running locally?"}'
```

You should see an SSE stream: `start`, then `token` events, then `done`.

In the UI, check that:
1. The pill top-right reads **● LIVE** (green) — the backend can reach Ollama.
2. A reply streams in word by word.
3. The status bar shows the model name, `ctx 32k`, and a context meter.
4. **Status** lists your models with `available: true`.
5. Killing Ollama (`pkill ollama`) turns the pill grey and produces a readable
   error in chat rather than a spinner that never resolves.

---

## Roadmap

| # | Milestone | Contents | Status |
|---|---|---|---|
| 1 | Foundation | FastAPI + SQLite + Ollama + chat UI + settings | **done** |
| 2 | Facts plane | Gmail OAuth, live watermark, polling, normalise + store | **done** |
| 3 | Commitments plane | Classify, extract tasks/deadlines, ground, reconcile | next |
| 4 | Semantic plane | FTS5 + chunking + embeddings + hybrid retrieval | |
| 5 | Agent | Query router + typed read-only tools over all three planes | |
| 6 | Calendar | Windowed sync, link events to commitments | **done**\* |
| 7 | iMessage | Local read-only connector, session grouping | **done**\* |
| 8 | Proactive | Notifications and the daily briefing | |

\* Built out of order, at the user's request: Calendar and iMessage reuse the
same connector/worker/sync-state shape Gmail already validated in Milestone 2,
so pulling them forward did not require Milestones 3–5 first. Linking calendar
events *to* commitments (the second half of Milestone 6's original scope)
still depends on Milestone 3's extraction pipeline and remains open.

**Ingestion is live-only.** Gary records a watermark when you connect an account
and stores what arrives after it — no historical backfill. That keeps the first
sync instant and lets every message get full processing, at the cost of an empty
day one. A bounded `seed_window_days` setting pulls one recent week if you'd
rather not start from nothing.

Calendar is the deliberate exception: it syncs a *window* (7 days back, 90
forward) because a calendar's value is in the future, and tomorrow's meeting was
created last week.

---

## Scope

**Gmail, Google Calendar, and iMessage** are the full set of live connectors
for this phase, and all three are now built. Discord and Instagram are
deferred — see below for why they are a different kind of problem. The next
phase is Milestone 3: turning what these three connectors ingest into
classified, extracted, grounded commitments.

## A note on the connectors you asked for

Not every service can be integrated the same way, and it is worth being clear
about which ones are real before you count on them.

| Source | How | Viable? |
|---|---|---|
| **Gmail** | Official Gmail API, OAuth 2.0, incremental `historyId` sync + Pub/Sub push | Yes — fully |
| **Google Calendar** | Official Calendar API, same OAuth grant | Yes — fully |
| **iMessage** | Read-only copy of your own `~/Library/Messages/chat.db` on this Mac, with Full Disk Access you grant | Yes — local-only, macOS, this machine's history |
| **Discord DMs** | The official bot API cannot read your personal DMs. Self-bots violate the ToS and get accounts banned. | Import from your **Discord Data Export** instead |
| **Instagram DMs** | The Messaging API only covers Business/Creator accounts receiving customer messages, and needs app review. Personal DMs are not available. | Import from your **Instagram Data Export** instead |

For Discord and Instagram, Milestone 9 will ship an importer that reads the
official data-export archives you download from those services. It is a
snapshot rather than a live feed — but it is legitimate, it does not risk your
accounts, and it makes those conversations searchable alongside everything
else. See `docs/ARCHITECTURE.md` for the reasoning in full.

---

## Security

- **Read-only by design.** Gary has no tool that can send email, delete a
  message, or modify your calendar. This is enforced by a capability check, not
  by asking the model nicely. See `backend/security/permissions.py`.
- **Untrusted content is fenced.** Anything retrieved from email or messages is
  wrapped in delimiters the content cannot forge, and the system prompt states
  that fenced text is data, never instruction. See
  `backend/security/prompt_guard.py` and its tests.
- **Localhost only by default.** The app refuses to start on a non-loopback
  host without `API_AUTH_TOKEN` set.
- **OAuth tokens never reach the browser.** Credentials live in the backend,
  encrypted at rest with `CREDENTIAL_ENCRYPTION_KEY`.
- **Secrets are not committed.** `.env` is gitignored; `.env.example` has no
  real values.

---

## Project layout

```
backend/
  api/          HTTP routes + WebSocket event feed
  chat/         conversation persistence, prompt assembly
  connectors/   gmail/, calendar/, imessage/ — one client + sync module each
  database/     SQLAlchemy models, async session, Alembic migrations
  events/       normalised event bus
  llm/          provider abstraction, Ollama impl, model registry, budgeting
  pipeline/     identity resolution, label policy, horizon, image policy
  security/     prompt-injection guard, capabilities, API auth, credential crypto
  settings/     the settings catalog + database-override service
  workers/      one poll loop per source, dispatched by kind
  config.py     all configuration, one place
  main.py       app factory
frontend/       React + Vite + TypeScript
tests/          528 tests, mock connectors, no live accounts required
docs/           architecture notes
```

Design documents:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — overall shape, live-only
  ingestion, the three storage planes, and problems found in the original spec
- [`docs/DATA-MODEL.md`](docs/DATA-MODEL.md) — how people, email, messages,
  calendar events, and derived facts are stored, and how retrieval works
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) — failure register: ~110 cases
  across sync, extraction, identity, retrieval, time, security, and operations,
  each with its fix
