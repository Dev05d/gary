# Gary — Local Personal Intelligence Agent

A private, local-first assistant that ingests your digital life, stores it on
your own machine, and answers questions about it — with citations back to the
original message.

**Everything stays local.** Your mail, messages, and calendar live in a SQLite
file on your disk. Inference runs on Ollama, on this machine or another one on
your LAN. Nothing is sent to a cloud API.

> **Status: Milestone 1 of 9.**
> The chat stack works end to end — FastAPI + SQLite + Ollama + a streaming UI.
> Connectors (Gmail, Calendar, iMessage) are Milestone 2+ and are **not built
> yet**. The status page tells you the truth about what is wired up.

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
- **The Settings page in the app** — everything else, 76 settings across 9
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

### 8. Connect Gmail

Not yet — Milestone 2. The OAuth flow, the Google Cloud credential steps, and
the first sync will be documented here when the connector lands.

### 9. Run the first sync

Also Milestone 2.

---

## What works right now

- Streaming chat with your local model, over SSE
- Conversation history persisted in SQLite
- Two-model routing: **Deep** / **Fast** toggle in the UI
- Real token counting and context-window budgeting
- **Settings page**: 76 documented settings, live connection testing, model
  discovery, provenance tracking, and hot reload without a restart
- Status page: backend reachability, per-role model availability, DB, counts
- Optional bearer-token auth for non-localhost access
- Prompt-injection trust boundary and the read-only capability model (the
  scaffolding is in place and tested *before* any external data can arrive)

## What is deliberately not built yet

Gmail, Calendar, iMessage, search, embeddings, the agent tool loop,
notifications, and the daily briefing. See the roadmap below.

---

## Testing it

```bash
.venv/bin/python -m pytest              # 434 tests, no Ollama or accounts needed
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
| 2 | Facts plane | Gmail OAuth, live watermark, polling, normalise + store | next |
| 3 | Commitments plane | Classify, extract tasks/deadlines, ground, reconcile | |
| 4 | Semantic plane | FTS5 + chunking + embeddings + hybrid retrieval | |
| 5 | Agent | Query router + typed read-only tools over all three planes | |
| 6 | Calendar | Windowed sync, link events to commitments | |
| 7 | iMessage | Local read-only connector, session grouping | |
| 8 | Proactive | Notifications and the daily briefing | |

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

The next phase covers **Gmail, Google Calendar, and iMessage** only. Discord and
Instagram are deferred — see below for why they are a different kind of problem.

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
  database/     SQLAlchemy models + async session
  events/       normalised event bus
  llm/          provider abstraction, Ollama impl, model registry, budgeting
  security/     prompt-injection guard, capabilities, API auth
  config.py     all configuration, one place
  main.py       app factory
frontend/       React + Vite + TypeScript
tests/          434 tests, mock connectors, no live accounts required
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
