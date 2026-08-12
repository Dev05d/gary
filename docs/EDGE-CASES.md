# Edge cases

Failure register for the Gmail / Calendar / iMessage architecture. Each entry:
what breaks, why, and what to do about it.

Ordered roughly by how likely each is to bite, and how quietly. The quiet ones
matter most — a sync that crashes gets fixed, a sync that silently drops 3% of
your mail does not.

Legend: **⚠ silent** = produces wrong answers without any error.

---

## 1. Gmail sync

### 1.1 ⚠ Advancing the watermark before finishing pagination
`history.list` paginates with `nextPageToken`. Storing the returned `historyId`
after processing only the first page permanently skips everything on pages 2+.
Nothing errors; the messages simply never exist.

**Fix:** treat the whole paginated walk as one transaction. Accumulate every
page, persist the messages, and only then advance the watermark. On crash
mid-walk, the old watermark is still current and the next run re-reads — which
is safe because ingest is idempotent.

### 1.2 `historyId` expired (HTTP 404)
Google guarantees history availability for "typically at least one week", and
explicitly warns it can be **as little as a few hours**. A laptop closed over a
holiday will hit this.

**Fix:** catch 404 *specifically* — a 429 must retry, not trigger recovery. But
do **not** do the documented "full sync": that would import the entire mailbox,
which is exactly what live-only ingestion exists to avoid. Instead bridge the
gap with a bounded query:

```
messages.list(q="after:<last_successful_sync_date>")
```

then take a fresh `historyId` from the result and resume the delta path. The
gap is bounded by how long we were offline, not by mailbox size.

### 1.3 `historyId` of 0 is invalid
There is no "sync from the beginning" sentinel; passing 0 returns 404.

**Fix:** a null watermark means "not yet initialised" and routes to the connect
path (`users.getProfile`), never to `history.list`.

### 1.4 History IDs are not contiguous
They increase chronologically but with arbitrary gaps. Arithmetic on them —
"resume from lastId + 1", "estimate volume from the delta" — produces garbage.

**Fix:** treat as an opaque token. Never compute with it.

### 1.5 Rate limits and quota
`history.list` costs 2 quota units, `messages.list` costs 5. Budget is per-user
per-minute. A recovery sync (1.2) spikes usage precisely when several sources
may be recovering together.

**Fix:** token-bucket limiter around the client; exponential backoff with
jitter on 403 `rateLimitExceeded` and 429; batch `messages.get` calls; queue
recovery syncs rather than firing them concurrently.

### 1.6 ⚠ Message mutated after ingest
Labels change, read state flips, a message is archived or starred. The message
body is immutable but its metadata is not.

**Fix:** `history.list` reports `labelsAdded` / `labelsRemoved` / `messagesDeleted`
as distinct change types. Handle each — do not treat every history record as
"new message". Metadata updates are an UPDATE, not an INSERT.

### 1.7 Message deleted upstream
The user deleted it in Gmail. Keeping a local copy is a privacy violation of
the user's clear intent.

**Fix:** `messagesDeleted` → soft-delete locally (`deleted_at`), exclude from
all retrieval, and hard-delete on a schedule. Cascade to chunks, embeddings,
FTS rows, and any commitments sourced from it. A deleted email must not leave a
phantom deadline behind.

### 1.8 OAuth token revoked or expired
The user revokes access in their Google account, or the refresh token expires
after prolonged disuse.

**Fix:** distinguish 401 (refresh and retry once) from `invalid_grant` (dead —
mark the source disconnected, surface a re-auth prompt, stop the worker). Never
retry `invalid_grant` in a loop; it will never succeed.

### 1.9 Multiple Google accounts, same message
Personal and work accounts both receive a message where both addresses are on
the To: line.

**Fix:** `(source_id, source_message_id)` is unique *per source*, so both are
ingested — correctly, because they are genuinely two mailbox entries. Dedupe at
*retrieval* time on `rfc_message_id` so the answer cites it once. Do not dedupe
at ingest; label state differs per account and both matter.

### 1.10 Very large messages
A 25MB email with inline images. Fetching and storing it blocks the pipeline.

**Fix:** cap `body_text` (say 256KB), set `truncated = true`, skip embedding
the tail. Attachments are downloaded (see §13.7) under a per-file cap and a
total disk budget.

### 1.11 Malformed MIME and encodings
Old or broken mail: invalid charsets, mislabelled encodings, nested multiparts,
bare CR line endings.

**Fix:** decode with `errors="replace"` and record `decode_degraded`. Never let
one bad message abort a batch — per-message try/except with the failure logged
to a dead-letter table for inspection.

### 1.12 Messages with no body
Calendar invites, attachment-only mail, delivery receipts.

**Fix:** valid rows with empty `body_clean`. Skip embedding (nothing to embed),
keep in SQL and FTS on subject. Do not treat as an ingest failure.

### 1.13 Drafts and sent mail
`history.list` surfaces drafts. Ingesting a draft as a real message means the
agent reports things you never sent.

**Fix:** filter on the `DRAFT` label at ingest. `SENT` **is** ingested and
flagged `is_from_me` — it is load-bearing for "who haven't I replied to".

### 1.14 Mailing lists rewrite the sender
`From:` is the list, `Sender:`/`Reply-To:` is the human, or vice versa.

**Fix:** when `List-Id` is present, record the list in `list_id` and prefer
`Reply-To` for the human identity. Do not create a person for the list address.

---

## 2. iMessage

### 2.1 ⚠ Text is not in the `text` column
On macOS Ventura and later — and *universally* on macOS 26 — `message.text` is
`NULL` and the content lives in `attributedBody` as an Apple **typedstream**
archive. This is `NSArchiver` format, not a modern `NSKeyedArchiver` bplist, so
`plistlib` silently fails or returns junk.

A naive connector ingests thousands of empty messages and reports success.

**Fix:** proper typedstream deserialisation (`pytypedstream` in Python). Record
which path produced the text in `text_source` so coverage is measurable. Assert
at startup: if >20% of recent messages yield no text, fail loudly rather than
ingesting emptiness.

### 2.2 NULL text that is *not* hidden text
Photo- or attachment-only messages have `text IS NULL` **and**
`attributedBody IS NULL`, with `cache_has_attachments = 1`.

**Fix:** distinguish before attempting to parse:
```sql
(text IS NULL OR length(text) = 0)
AND attributedBody IS NOT NULL AND length(attributedBody) > 100
AND associated_message_type = 0
AND cache_has_attachments = 0
```
Anything else is attachment-only and joins the attachment tables instead.

### 2.3 ⚠ Tapbacks ingested as messages
`associated_message_type` 2000–2005 = reaction added, 3000–3005 = removed.
Ingesting these produces a corpus full of `Liked "sounds good"`, which pollutes
embeddings and inflates message counts.

**Fix:** route to a `reactions` table keyed on `associated_message_guid`. Never
a message. Never embedded.

### 2.4 Apple epoch, two of them
Modern macOS stores nanoseconds since 2001-01-01; older versions stored
seconds. Misreading puts every message in 1970 or the far future.

**Fix:** detect by magnitude (values > 1e11 are nanoseconds), convert, then
sanity-check the result falls within [2007, now + 1 day]. Reject and log
outliers rather than storing them.

### 2.5 Database locked
Messages.app holds `chat.db` open with `-wal` and `-shm` companions.

**Fix:** copy all three to a temp location and open the copy with
`file:...?immutable=1`. Never open the live file, never write to it under any
circumstance.

### 2.6 Full Disk Access not granted
The read fails with a permission error that looks like a missing file.

**Fix:** detect at connect time, distinguish from "file absent", and give the
exact instruction: System Settings → Privacy & Security → Full Disk Access →
add the app. Never present this as "no messages found".

### 2.7 Schema drift across macOS versions
Column names and semantics change between releases.

**Fix:** probe `PRAGMA table_info` at connect, compare to a known-good column
set per version, and refuse to sync with a clear message on an unrecognised
schema rather than silently mis-mapping columns.

### 2.8 Edited and unsent messages
iOS 16+ allows both. An unsent message should disappear; an edited one should
show its current text.

**Fix:** re-read recently-modified rows rather than treating ROWID as
append-only. Unsent → soft-delete and cascade. Edited → update text, re-embed
the affected session, and re-run extraction (an edit may change a deadline).

### 2.9 Out-of-order arrival via iCloud sync
Messages synced from another device arrive with older timestamps but newer
ROWIDs.

**Fix:** watermark on ROWID (monotonic, guaranteed) but order and query on
timestamp. Never assume ROWID order matches time order.

### 2.10 Group chats
A group chat has many participants and no single "sender" at the chat level.

**Fix:** `chat_id` distinguishes 1:1 from group via participant count. Sessions
carry participant lists. Extraction must attribute a commitment to a specific
person — "can someone bring drinks?" in a group is `owner = unclear`, not
implicitly yours.

### 2.11 Unknown numbers and SMS
Messages from handles with no contact entry, and green-bubble SMS.

**Fix:** identities are created regardless; persons are not. `service`
distinguishes iMessage from SMS. Unknown-handle messages are searchable but the
sender shows as the raw number.

---

## 3. Calendar

### 3.1 Recurring events
A weekly standup is one event with an RRULE, plus exceptions ("this week only,
3pm"), plus cancellations of single instances.

**Fix:** store **expanded instances** within the sync window, not the rule.
"What do I have Tuesday?" must be an indexed range scan. Link instances to the
series via `recurring_event_id`; instance exceptions override.

### 3.2 ⚠ Declined events in the briefing
An event you declined is not on your calendar in any meaningful sense.

**Fix:** `my_response` is stored and filtered. Declined and cancelled events
are excluded from briefings and "what do I have" queries by default.

### 3.3 `syncToken` invalidated (HTTP 410)
Happens after prolonged absence or server-side changes.

**Fix:** re-list the configured window, take a fresh token. Cheap, because the
window is bounded (7 days back, 90 forward) — unlike Gmail, a calendar full
resync is not a mailbox import.

### 3.4 All-day events and timezones
An all-day event on 15 August is 15 August *locally*, not `00:00Z`. Storing it
as UTC midnight puts it on the 14th for anyone west of Greenwich.

**Fix:** store all-day events as a date with an `all_day` flag, and never
convert them to instants. Timed events store an instant plus the originating
timezone.

### 3.5 Event moved
Same event ID, different start time. A stale row means the briefing announces
the wrong hour.

**Fix:** upsert on `source_event_id`; `updated_at` drives re-notification. If
an event within the notification horizon moves, re-notify.

### 3.6 Multiple and shared calendars
Personal, work, a partner's shared calendar, subscribed holiday feeds.

**Fix:** `calendar_id` per event, with per-calendar enable/disable. Holiday and
subscription feeds default to off — they are noise in a briefing.

### 3.7 Events with restricted visibility
Shared calendars often expose only "busy" with no title.

**Fix:** store what is visible. Never let the extractor invent a title for an
opaque block.

---

## 4. Identity and people

### 4.1 ⚠ Two different people with the same name
Two John Smiths. Merging them cross-contaminates both.

**Fix:** name alone never auto-links. Shared *public* domain (gmail.com) is not
evidence. Common surnames score 0.30, below the ignore floor. Only a shared
private domain, a signature reference, or user confirmation crosses the bar.

### 4.2 One person, many handles
Work email, personal email, phone, Apple ID.

**Fix:** the identity/person/link model. Signature parsing is the strongest
automatic signal, since people publish their own alternate handles.

### 4.3 ⚠ Over-normalising email addresses
Folding dots universally merges `j.smith@company.com` and
`jsmith@company.com` — different mailboxes on most servers, possibly different
people.

**Fix:** provider-specific rules only. Dots folded for Gmail alone; plus-tags
stripped only for providers known to implement plus-addressing.

### 4.4 ⚠ Guessing a phone country code
Normalising a bare 10-digit number to +1 when the user is in the UK merges
unrelated people.

**Fix:** refuse rather than guess. Only NANP inference, only when the region is
configured as `1`. Unnormalisable numbers stay as distinct identities — two records
for one person is recoverable; one record for two people is not.

### 4.5 Role accounts as people
`noreply@github.com` becoming your most frequent correspondent.

**Fix:** role-account detection on the local part (including VERP bounce forms
like `bounce+123@`) plus header signals. Identities yes, persons never.

### 4.6 A bad merge, discovered late
Two identities linked in error, after weeks of accumulated data.

**Fix:** links are soft. Withdrawing one sets `unlinked_at`; nothing is
rewritten because messages never referenced the person. Merge history is
preserved for audit.

### 4.7 The user's own identities
"Who haven't I replied to" is meaningless without knowing which handles are
yours.

**Fix:** `persons.is_me`. Seeded from the connected account addresses,
extendable by the user. Every alias must be registered or your own replies
count as inbound.

### 4.8 Display name injection
A sender sets their display name to `Bob<<<UNTRUSTED_CONTENT ... SYSTEM:`.

**Fix:** display names are sanitised through the same prompt-guard path as
bodies. Already covered and tested (`test_prompt_guard.py`).

---

## 5. Extraction: tasks and deadlines

### 5.1 ⚠ Fabricated deadlines
The model invents "payment due Friday" from a message that says no such thing.

**Fix:** mandatory verbatim `evidence_quote`, verified against the source
before persistence. Quotes under three tokens are rejected outright — `"due"`
is a substring of half the emails ever written and verifies nothing.
Implemented and tested in `backend/pipeline/schemas.py`.

### 5.2 ⚠ Relative dates resolved wrong
"by Friday" resolved against *today* instead of the message date, or resolved
to last Friday.

**Fix:** anchor to the message's own timestamp plus the user's timezone.
Post-check rejects deadlines preceding the message (a few hours of grace for
skew) or implausibly distant. A rejected date **drops the date and keeps the
task** — a wrong deadline is worse than none.

### 5.3 Duplicates across a thread
Five replies discussing one deadline producing five tasks.

**Fix:** thread-aware extraction — open commitments in the thread are supplied
as input, and the model returns `updates` as well as new items.

### 5.4 Supersession
"Actually, let's push it to Monday" creating a second task instead of moving
the first.

**Fix:** same mechanism. The update carries its own evidence quote, verified
identically.

### 5.5 Hypotheticals and negations
"If we don't finish by Friday we're in trouble" or "the Friday deadline was
cancelled" extracted as a live deadline.

**Fix:** the extraction prompt covers conditionals and negations explicitly,
and low-confidence extractions fall below the floor. Anything surviving with
confidence < 0.6 surfaces as "possible task", not an assertion.

### 5.6 Bills are real tasks from automated senders
`is_automated` suppresses `requires_action`, but "your electricity bill is due
on the 15th" is a genuine deadline from a genuine no-reply address.

**Fix:** the suppression applies to *notification eligibility*, not to
extraction. Commitments are still extracted from automated mail and appear in
"what's due"; they just do not interrupt you. Bills you want in the list, not
in a push alert. Promotional urgency ("act now!") fails the grounding and
confidence checks separately.

### 5.7 Tasks belonging to someone else
"Can you send the report by Friday?" in a group thread addressed to a third
party.

**Fix:** `owner` (me / them / unclear). Only `owner = me` reaches "what do I
owe". `unclear` is surfaced separately rather than assumed.

### 5.8 Deadline with no year
"Due the 15th" in a December message — 15 December or 15 January?

**Fix:** resolve to the next occurrence after the message date. `due_precision`
records the ambiguity, and the UI shows the inferred date as inferred.

### 5.9 Sender in a different timezone
"5pm Friday" from a colleague in Tokyo.

**Fix:** prefer an explicit timezone in the text; otherwise the sender's
timezone if derivable from headers; otherwise the user's, with precision
downgraded to `day`.

### 5.10 Recurring commitments
"Submit your timesheet every Friday."

**Fix:** out of scope for the first pass. Extract as a single instance with a
`recurrence_hint`, and do not attempt to generate a series — a wrong recurring
task generates wrong alerts indefinitely.

### 5.11 Already-completed tasks
A deadline extracted from a thread where the last message says "sent, thanks!"

**Fix:** extraction runs on the *latest* message with prior thread context, so
completion signals arrive as `updates` closing the item.

### 5.12 Resurrection after dismissal
The user dismisses an extracted task; re-processing recreates it.

**Fix:** `dismissed_fingerprint` — a stable hash over
(thread, normalised title, due date). Re-extraction checks dismissals before
inserting.

### 5.13 Non-English content
Extraction quality degrades; grounding may fail on non-Latin scripts.

**Fix:** grounding normalisation is Unicode-aware (NFKC). Where confidence is
low, store the analysis without commitments rather than guessing.

---

## 6. Retrieval and answering

### 6.1 ⚠⚠ The coverage gap presented as a negative
**The most dangerous failure in this architecture.** With live-only ingestion,
"what did Prof Smith say last month?" returns nothing — and "I found nothing"
is indistinguishable from "he never wrote".

**Fix:** data horizon, enforced in `backend/pipeline/horizon.py`. Every source
records when it started; any query reaching behind that gets an explicit gap
warning, and the system prompt states the rule that a gap is never a negative
finding. Tested.

### 6.2 ⚠ Post-filtering destroying recall
Searching first and filtering by sender/date afterwards. For personal email the
filters are extremely low-selectivity — "from Sarah last month" may be 0.1% of
the corpus — which is exactly where post-filtering fails hardest.

**Fix:** apply the predicate *inside* each channel before RRF. Never filter
after fusion, which also silently shortens the result list.

### 6.3 Ambiguous person reference
"What did Sarah say?" with three Sarahs.

**Fix:** resolve to candidate identities; if more than one is plausible, ask —
with disambiguating context (last contact, domain) rather than raw IDs.

### 6.4 Relationship references
"My advisor", "my landlord", "mom".

**Fix:** user-approved memories and the `facts` table
(`relationship_to_me`). Unresolvable → ask rather than guess.

### 6.5 Empty results
Genuinely nothing matches.

**Fix:** say so plainly, state what was searched and over what range, and
distinguish "nothing matched" from "outside my records" (§6.1). Never pad with
loosely-related results to appear useful.

### 6.6 Token explosion
"Tell me everything about the lease" matching 400 messages.

**Fix:** hard cap on retrieved sources; thread-collapse; summarise-then-answer
for large sets; tell the user the answer is based on the top N of M matches.

### 6.7 Enormous threads
A 200-message thread that cannot fit in context.

**Fix:** thread summaries maintained incrementally, with the most recent and
most relevant messages included verbatim.

### 6.8 Aggregate questions answered by sampling
"How many emails from Sarah this week" answered from a top-k retrieval.

**Fix:** aggregates route to SQL `COUNT`, never to retrieval. This is what the
query classifier is for.

### 6.9 Stale index after a settings change
Changing the embedding model invalidates every vector — different models
produce different dimensions and incompatible spaces.

**Fix:** `embedding_model` recorded per chunk. Changing it flags existing
vectors stale, warns before the change (already in the settings copy), and
queues re-embedding rather than silently mixing spaces.

### 6.10 Contradictory sources
Email says the meeting is Tuesday, a text says Wednesday.

**Fix:** surface both with timestamps and let recency and source speak. Never
silently pick one.

---

## 7. Time

### 7.1 ⚠ Trusting the `Date:` header
Sender-written, spoofable, and frequently wrong from misconfigured clocks.

**Fix:** order and index on `received_at` (Gmail `internalDate`, server-side).
Keep `sent_at` for display; distrust it when the two diverge by more than a few
days.

### 7.2 DST transitions
"Every Tuesday 9am" across a DST boundary; the 2am hour that occurs twice or
not at all.

**Fix:** store instants in UTC plus the originating timezone. Expand recurring
events in the event's timezone, not UTC.

### 7.3 "Today" for a user who is not the server
The backend runs UTC; the user is in UTC-7. At 6pm local it is already
tomorrow in UTC, so "today's email" returns the wrong day.

**Fix:** `user_timezone` is a setting, and every day-boundary query resolves in
it. Never use the server's local date.

### 7.4 Laptop asleep during a scheduled sync
Cron-style scheduling silently skips.

**Fix:** interval-since-last-success, not wall-clock scheduling. On wake,
detect the gap and catch up.

### 7.5 Clock changed after ingestion
System time correction reorders `ingested_at`.

**Fix:** never use `ingested_at` for ordering or recency. It is provenance only.

---

## 8. Security and privacy

### 8.1 Prompt injection from email
Covered: fenced untrusted content, forgery-proof delimiters, and — the
load-bearing defence — no write tools exist to hijack.

### 8.2 Injection via extracted fields
A malicious calendar title or contact name reaching the prompt outside the
fence.

**Fix:** *all* external-origin strings go through `sanitize_untrusted`,
including titles, display names, and filenames — not just bodies.

### 8.3 Tracking pixels
Rendering remote images tells the sender you read it, when, and roughly where.

**Fix:** strip remote resources from sanitised HTML. No network requests from
rendered mail, ever.

### 8.4 Malicious attachment filenames
`../../.ssh/authorized_keys`.

**Fix:** never use the supplied filename as a path. Store under a generated ID;
keep the original as a display label only.

### 8.5 Zip bombs and decompression
Only relevant once attachments are processed.

**Fix:** size and ratio caps; refuse rather than expand.

### 8.6 Deletion must cascade
Deleting a message while its extracted deadline, chunks, embeddings, and FTS
rows survive leaves recoverable content behind.

**Fix:** deletion cascades across all derived tables. Verified by test when the
tables land.

### 8.7 Sensitive categories
Medical, legal, financial mail the user may not want summarised or embedded.

**Fix:** exclusion rules by sender, label, or keyword — ingested to SQL but
excluded from embedding and from proactive surfacing. User-controlled.

### 8.8 Secrets in the database
Tokens and the encryption key sit alongside message content.

**Fix:** OAuth tokens encrypted at rest; never returned to the frontend; the
settings API is write-only for secrets. Already implemented and tested.

---

## 9. Operational

### 9.1 Ollama unavailable mid-pipeline
Classification fails for a batch; the machine serving the model went to sleep.

**Fix:** ingestion and enrichment are separate stages. Messages land in SQL
regardless; enrichment is a retryable job with backoff. Search works on
unenriched messages; only commitments lag.

### 9.2 Partial failure mid-sync
Crash after storing 40 of 100 messages.

**Fix:** idempotent upserts on `(source_id, source_message_id)`; the watermark
advances only on full success. Re-running is a no-op for what already landed.

### 9.3 Disk full
Writes fail; SQLite can corrupt under some failure modes.

**Fix:** check free space before large operations; refuse to sync below a
threshold with a clear message; WAL checkpointing to bound growth.

### 9.4 SQLite lock contention
Workers writing while the API reads.

**Fix:** WAL mode (already on), `busy_timeout`, short write transactions,
single writer via the job queue.

### 9.5 Duplicate job execution
The same enrichment job runs twice after a restart.

**Fix:** `idempotency_key` on jobs; enrichment is idempotent by construction —
re-analysing a message replaces its analysis row rather than appending.

### 9.6 Unbounded growth
Chunks, embeddings, and analyses accumulate.

**Fix:** live-only ingestion bounds the rate. Retention settings for
attachments and old automated mail; `VACUUM` on a schedule.

### 9.7 A model change silently altering behaviour
Extraction quality shifts after switching models; existing rows were produced
by the old one.

**Fix:** `model` and `prompt_version` on every derived row, so affected rows
can be found and re-derived selectively.

---

---

## 11. Found by stress-testing the design (round 2)

These emerged from re-reading the architecture rather than from building it,
which is the cheapest place to find them.

### 11.1 ⚠ Dismissal fingerprint keyed on the due date
I specified `dismissed_fingerprint` as a hash over
(thread, normalised title, **due date**). That is broken: when the deadline
moves, the fingerprint changes and a task the user explicitly dismissed comes
straight back.

**Fix:** fingerprint on (thread_id, normalised title) only. A dismissal means
"not a task, in this thread" and must survive rescheduling. The dismissal
carries the date it was made so the UI can explain why an item is hidden.

### 11.2 ⚠ The query router is a small model, and it will misclassify
The three-plane design depends on routing "what's due Friday" to the
commitments table. A 12B model will sometimes send it to semantic search, which
returns plausible-looking prose and no deadlines — a wrong answer that looks
right.

**Fix:** the LLM is not the first classifier. Deterministic pre-classification
runs first and handles the unambiguous majority: a date expression plus
due/deadline vocabulary routes to commitments; "how many"/"count" routes to
aggregate; a bare person name plus a time bound routes to a filtered fact
query. The LLM router only sees what the rules cannot decide. Tools also return
a typed "wrong plane, try X" rather than empty results, so one bad hop is
recoverable.

### 11.3 ⚠ `is_me` seeded only from connected account addresses
"Who haven't I replied to" is wrong if any of your sending aliases is unknown —
your own replies get counted as inbound and the thread looks unanswered.

**Fix:** derive `is_me` identities from the **SENT folder**. Any address
appearing in `From:` on a message you sent is yours, by construction. This
catches plus-aliases, custom domains, and send-as addresses that no
configuration screen would have surfaced.

### 11.4 Enrichment lag misreported as absence
A message lands at 09:00 and its classification is queued behind twelve others.
At 09:01 the user asks "anything important this morning?". The message is in
the facts plane with no importance score, so it is invisible to an
importance-filtered query.

**Fix:** ingestion and enrichment are separate stages with separate
watermarks. Any query touching derived data reports the enrichment backlog:
"3 messages from the last 10 minutes are still being processed." Same principle
as the data horizon — never present a pipeline state as a finding.

### 11.5 Malformed JSON from the extractor
Ollama's `format=` constrains output, but small models still occasionally emit
something unparseable, especially at longer context.

**Fix:** one retry with a stripped-down prompt, then give up and store the
message with `analysis_failed`. Failure to extract must never block ingestion —
the message is still searchable, it just has no commitments. Persistent
failures surface on the status page with the offending message IDs.

### 11.6 Thread collapse breaks citation granularity
Collapsing six chunks from one thread into a single source improves diversity,
but then a citation points at a thread rather than the sentence that supports
the claim.

**Fix:** citations stay message-level. Collapse affects *ranking and budget*,
not attribution — the thread is retrieved as a unit, and each claim still
cites the specific message it came from.

### 11.7 Three "reach back" mechanisms that should be one
The design accumulated three ways to fetch older data: the optional seed
window, the historyId-expiry bridge query, and the calendar window. Three code
paths doing the same thing is three places for an off-by-one.

**Fix:** one **bounded catch-up query** primitive — fetch source X between
timestamps A and B, capped at N items — with all three as callers passing
different bounds. One implementation, one set of tests.

### 11.8 sqlite-vec extension loading is disabled in Apple's system Python
`sqlite3.enable_load_extension` raises `AttributeError` on the Python that
ships with macOS. The failure surfaces deep inside the first vector query.

**Fix:** probe the capability at startup, before any ingestion, and fail with
the actual remedy ("use Homebrew or python.org Python; Apple's build disables
SQLite extensions") rather than a stack trace.

### 11.9 Embedding model dimension drives storage more than message count
`qwen3-embedding:4b` is 2560-dim: 1.5 GB of vectors at five years, versus
598 MB for a 1024-dim model.

**Fix:** default to a 1024-dim model. The settings page already warns that
changing the embedding model invalidates the index; it should also show the
projected storage for the chosen dimension.

---

---

## 12. Found by testing, not by reading (round 3)

Both of these were live defects in shipped code, found by attacking it rather
than re-reading it.

### 12.1 ⚠⚠ Grounding accepted quotes assembled from scattered words
The anti-hallucination check compared a quote to the message as a **bag of
words**: what fraction of the quote's tokens appear anywhere in the source.
That is trivially defeated.

Given a message reading *"Please review the attached report before the team
meeting. The meeting is Thursday…"*, the fabricated quote **"the report is due
Thursday"** scored 0.80 and was **accepted** — despite the word "due" appearing
nowhere in the message. A model inventing a deadline from words it saw
elsewhere in the email passed the exact check that exists to stop it.

**Fix:** grounding now requires **contiguity**, not mere presence. A fuzzy match
must contain an unbroken run of at least 4 matching tokens (or the whole quote,
if shorter), measured with a sequence diff over token lists rather than set
membership. All four fabrication variants are now rejected with the reason
"words appear in the message but never together", while genuine quotes —
including ones reworded slightly around a real span — still pass.

Two implementation notes: `autojunk` is disabled on the sequence matcher,
because its "ignore frequent elements" heuristic discards exactly the common
words that make a quote a quote; and the source is capped at 6000 tokens to
bound the O(n·m) diff on long threads.

### 12.2 ⚠ Markdown images fired tracking pixels
The chat renderer refuses raw HTML, so `<script>`, `<iframe>`, `onerror` and
`javascript:` URIs were all correctly neutralised — verified in a real browser.

But **plain markdown image syntax is not HTML.** `![x](https://tracker/p.gif)`
rendered an `<img>` and the browser fetched it. A browser-level check confirmed
a real outbound request to the tracker.

This is the tracking-pixel problem from §8.3, arriving through a door that
section did not cover. It is latent today — a local model will not emit a
tracker — but from Milestone 3 this renderer displays text that came out of the
user's inbox, and a pixel embedded there would report back when and where the
mail was read.

**Fix:** images are never loaded. The renderer substitutes a visible "image not
loaded" marker carrying the URL, so nothing is hidden and the user can open it
deliberately. Links additionally get `noopener noreferrer nofollow`, so
following one out of a message hands the target neither a window handle nor a
referrer revealing what was being read. Re-verified in the browser: **zero
external requests**, with bold, code, tables and links still rendering.

**Generalised lesson:** "sanitise the HTML" is not the same as "make no network
requests". The second is the property that actually matters, and it needs
testing at the browser level — no amount of reading the sanitiser would have
found this.

### 12.3 ⚠ Settings save was a check-then-act race
`SettingsService._write` read the row, then inserted if absent or mutated if
present. Two concurrent saves both observe no row, both INSERT, and the second
dies on `UNIQUE constraint failed: app_settings.key`.

Not hypothetical: a double-click on Save, or the UI saving while a background
job persists a derived setting, hits it. A concurrency test caught it failing
on roughly **one run in twelve** — invisible to a single-run suite.

**Fix:** an atomic `INSERT … ON CONFLICT DO UPDATE`. Zero failures in 20
consecutive runs afterwards.

**Generalised lesson:** every read-modify-write against shared state in this
codebase is suspect, and flaky-by-design concurrency tests are the only thing
that surfaces them. The same pattern to audit at Milestone 2: sync watermark
updates, identity upserts, and commitment reconciliation — all read-then-write
against rows that two workers can touch.

---

---

## 13. Consequences of the mail-scope decisions

Four product decisions were settled before Milestone 2. Each brought its own
failure modes.

**Decisions:** default to INBOX + SENT minus Promotions/Social/Forums, with a
custom label picker · mirror upstream deletions · download and index text
documents · track and remind on promises you made.

### 13.1 ⚠ Deleted mail leaving attachment files on disk
Mirroring deletions plus downloading attachments is the risky combination.
Deleting a message row while its downloaded PDF stays on disk leaves the
content of "deleted" mail sitting in the data directory.

**Fix:** deletion cascades to the filesystem, not just the database, and the
file delete happens inside the same unit of work as the row delete. Because
identical attachments are stored once by content hash, a file is only removed
once its **last** referencing message is gone — reference-counted, not
delete-on-first-orphan.

### 13.2 ⚠ CATEGORY_UPDATES must not be treated as noise
The obvious reading of "skip the promotional tabs" is to skip all four category
tabs. Updates is where Gmail files receipts, bills, shipping notices and
appointment reminders — the mail with the most real deadlines in it.

**Fix:** only Promotions, Social and Forums are excluded by default. Encoded in
`label_policy.py` and covered by a test that fails if Updates is ever added to
the exclusion set.

### 13.3 A typo in a custom label silently loses mail
`gmail_include_labels = "INBOX,Wrok"` matches nothing for the misspelled label.
No error — that mail simply never arrives, indefinitely.

**Fix:** configured labels are checked against the account's actual label list
and unknown names are surfaced on the status page. `unknown_labels()` is
tested.

### 13.4 Changing the label scope after the fact
Adding a label to the custom list should arguably pull in that label's recent
mail — but live-only ingestion says no backfill.

**Fix:** treat it as one more caller of the bounded catch-up primitive
(§11.7), bounded by `seed_window_days`. If that is 0, the new label starts from
now and the UI says so rather than implying history appeared.

### 13.5 Archived mail is invisible under the default
Mail auto-archived by a Gmail filter never carries INBOX, so the default scope
never sees it — including filters the user reads regularly.

**Fix:** not a bug, but it must be visible. The status page reports what
fraction of recent mail was skipped and why, so "Gary never sees my newsletter
folder" is discoverable rather than mysterious.

### 13.6 Document text extraction fails silently on scans
A photographed or scanned PDF has no text layer. Extraction returns an empty
string, which is indistinguishable from an empty document.

**Fix:** record extraction status per attachment — `extracted`, `empty`,
`unreadable`, `ocr` — and never treat `unreadable` as "the document says
nothing". OCR is available but off by default, and OCR'd text is marked and
ranked below extracted text because its error rate is much higher.

### 13.7 Attachment storage growth and hostile files
Downloading turns a bounded database into an unbounded one, and attachments are
the most hostile input in the whole system.

**Fix:** a per-file size cap and a total disk budget that stops downloads and
says so rather than filling the volume — SQLite can corrupt on a full disk.
Files are stored under a generated ID, never the sender-supplied filename, so
`../../.ssh/authorized_keys` cannot become a path. Archives are not expanded.
Content-hash storage means a PDF forwarded five times costs one copy.

### 13.8 Throwaway promises becoming nags
"I'll take a look" is not a commitment. Extracting it and reminding the user
about it is exactly how a useful feature becomes an irritant that gets turned
off.

**Fix:** promises extracted from your own sent mail carry a **higher**
confidence floor than incoming commitments (`own_promise_min_confidence`,
default 0.60 versus 0.35). Vague language without a date stays searchable but
never generates a reminder.

### 13.9 Promises in mail you sent before connecting
Sent mail is only ingested from the watermark forward, so a promise made last
week is invisible. The user will reasonably expect Gary to know about it.

**Fix:** the data horizon already covers this, and "what did I promise?" is a
temporal query — it gets the same explicit gap warning as any other question
reaching behind the horizon.

---

## 10. What this changes about the plan

Five items are load-bearing enough to build **with** their milestone rather
than after:

| Item | Milestone | Why it cannot wait |
|---|---|---|
| Data horizon (§6.1) | 2 | Without it the first honest-looking wrong answer ships on day one |
| Watermark-after-pagination (§1.1) | 2 | Silent, permanent data loss |
| `is_me` from the SENT folder (§11.3) | 2 | Wrong from the first message; poisons every follow-up feature |
| Bounded catch-up primitive (§11.7) | 2 | Three copies is three chances to get it wrong |
| Grounding checks (§5.1) | 3 | Fabricated deadlines are worse than no deadlines |
| Dismissal fingerprint (§11.1) | 3 | Dismissed tasks resurrecting destroys trust fast |
| Deterministic routing first (§11.2) | 5 | A misrouted query returns a confident wrong answer |
| Pre-filtering (§6.2) | 4 | Retrofitting means rewriting the retrieval path |
| `attributedBody` parsing (§2.1) | 7 | The connector otherwise "works" and ingests nothing |

Five are already implemented and tested: the data horizon
(`backend/pipeline/horizon.py`), grounding and date sanity
(`backend/pipeline/schemas.py`), identity resolution with always-ask merging
(`backend/pipeline/identity.py`), LLM triage with audit sampling
(`backend/pipeline/triage.py`), and behavioural importance
(`backend/pipeline/importance.py`).
