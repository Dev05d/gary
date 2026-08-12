# Data model and retrieval design

How Gary stores people, email, messages, calendar events, and derived facts —
and how it gets them back out.

Companion documents: [ARCHITECTURE.md](ARCHITECTURE.md) for the overall shape,
[EDGE-CASES.md](EDGE-CASES.md) for the failure register.

---

## 1. Principles

**1. Store what arrived, separately from what you concluded.**
Raw ingested data and LLM-derived data live in different tables. When you swap
the extraction model — and you will — you re-derive without touching a single
ingested row. Mixing them means a model change costs you your data.

**2. Foreign-key to the immutable thing.**
Messages point at an *identity* (a handle), never a *person* (an opinion about
handles). Chunks point at a message. Re-resolving identities or re-chunking
must never rewrite history.

**3. Never destroy to normalise.**
Keep the raw value beside the normalised one. Keep every display name seen.
Merges are links, not overwrites, and every link records who made it and why.

**4. Structure beats similarity wherever structure exists.**
A date is a column. A sender is a foreign key. A deadline is an indexed
timestamp. Embeddings are for the residue that has no structure — prose.

**5. Every derived claim carries its evidence.**
An extracted deadline stores the verbatim sentence it came from. A fact stores
its source message. Unverifiable claims are discarded, not stored with a shrug.

---

## 2. People: identities, persons, links

The hardest modelling problem here, because the same human is
`prof.smith@university.edu`, `jsmith@gmail.com`, `+1 555 0123`, "John Smith",
"J. Smith", and "Prof Smith" — while the *other* John Smith in your contacts is
a different person entirely.

### Three layers

```sql
identities                          -- raw handles. Never merged, never deleted.
  id, kind,                         -- email | phone | apple_id | handle
  value_normalized,                 -- for matching
  value_raw,                        -- for display and replying
  display_names        JSON,        -- every variant seen, most recent first
  first_seen, last_seen,
  message_count,
  is_role_account      BOOL,        -- noreply@, support@ — not a person
  is_bulk_sender       BOOL,        -- List-Unsubscribe / Precedence: bulk
  UNIQUE (kind, value_normalized)

persons                             -- resolved humans
  id, canonical_name, notes,
  is_me                BOOL,        -- the user's own identities
  created_at, created_by            -- 'user' | 'auto'

identity_links                      -- many-to-many, with provenance
  identity_id, person_id,
  confidence           REAL,
  signal               TEXT,        -- how it was decided
  evidence             TEXT,
  confirmed_by_user    BOOL,
  created_at, unlinked_at           -- soft: withdrawal keeps the history
```

**Messages foreign-key to `identity_id`.** A person is a view over identities.
This is the single most important decision in the model: identity resolution is
a *guess that improves over time*, and improving it must never mean rewriting
the message table.

### Resolution cascade

Deterministic first, probabilistic second, with a review band — the standard
entity-resolution shape, and it is standard because the alternatives are worse.

| Signal | Confidence | Notes |
|---|---|---|
| User confirmed | 1.00 | Terminal. Never re-litigated. |
| System Contacts (macOS / Google) | 0.98 | User-curated, authoritative |
| Identical handle | 1.00 | Trivial |
| Signature block lists the handle | 0.85 | They published it themselves |
| Same name + shared private domain | 0.80 | Not public domains — see below |
| Same distinctive name, different providers | 0.70 | "Aurelio Buendía" |
| Same common name | 0.30 | "John Smith" — below the floor |
| Thread co-occurrence | 0.25 | Weak on its own |

Bands: **≥0.90 auto-link · 0.60–0.90 suggest to the user · <0.60 ignore.**

Two rules that matter more than the numbers:

- **Shared public domain is not evidence.** Two John Smiths on `gmail.com` are
  almost certainly two people. The same two on `university.edu` probably are
  not. `is_public_domain()` gates this.
- **Name rarity is a multiplier.** A shared "John Smith" means nothing; a
  shared "Aurelio Buendía" is strong. Single-token names ("Mom", "Alex") are
  treated as common — they collide constantly and carry no domain signal.

Batch resolution **blocks** on name tokens and private domains rather than
comparing every pair, which keeps it off the O(n²) path as the corpus grows.

### Role accounts are not people

`noreply@`, `support@`, `billing@`, `mailer-daemon@`, and VERP bounce addresses
(`bounce+123@`) get identities but never persons. Without this, "who emails me
most?" answers `noreply@github.com` and your contact list fills with brands.

Detection is header-first where possible — `List-Unsubscribe`, `List-Id`,
`Precedence: bulk`, `Auto-Submitted` — because a message carrying those is
telling you outright, which beats guessing from content.

---

## 3. Email

```sql
messages
  id,
  source_id, source_message_id,      -- UNIQUE together → idempotent ingest
  rfc_message_id,                    -- RFC 5322 Message-ID, cross-source dedup
  thread_id, source_thread_id,
  in_reply_to, references_ids  JSON, -- threading fallback
  from_identity_id,
  to_ids, cc_ids, bcc_ids      JSON,
  reply_to_id,
  subject, subject_normalized,       -- Re:/Fwd: stripped
  body_text,
  body_html_sanitized,
  body_clean,                        -- quotes + signature stripped ← embedded
  signature_block,                   -- kept separately: identity evidence
  snippet,
  sent_at,                           -- Date header. Spoofable.
  received_at,                       -- server receive time. Authoritative.
  ingested_at,                       -- our clock
  labels                       JSON,
  is_read, is_from_me, is_draft, is_sent,
  has_attachments, size_bytes, truncated BOOL,
  content_hash,                      -- near-duplicate detection
  list_id,                           -- mailing list, if any
  deleted_at                         -- soft delete, honours upstream deletion
```

### Three timestamps, and which one to trust

`sent_at` comes from the `Date:` header, which is written by the sender and is
therefore **spoofable and frequently wrong** — misconfigured clocks routinely
produce mail dated days out.

`received_at` comes from Gmail's `internalDate`, the server's own receive time.
**This is the ordering key.** Every index, every "what came in today", every
recency signal uses `received_at`.

`sent_at` is kept for display, and when the two disagree by more than a few
days the header is distrusted and the UI shows the received time.

### Body storage

Three forms, because they serve different purposes and none is derivable from
the others after the fact:

- `body_text` — plain text, for search and fallback display
- `body_html_sanitized` — for faithful display, with remote resources stripped
  (tracking pixels are a real exfiltration channel: loading one tells the
  sender you read it, and when)
- `body_clean` — quoted replies, signatures, and legal footers removed. **This
  is what gets embedded and extracted from.**

Raw MIME is not kept. Nothing is re-derivable from it that is not already
captured, and it doubles storage.

Quote stripping is genuinely hard and worth using a library for — Mailgun's
`talon` or GitHub's `email_reply_parser` handle the common patterns
(`On <date>, <person> wrote:`, `>` prefixes, `-----Original Message-----`,
`Sent from my iPhone`). Rule-based rather than ML, for predictability.

The signature block is kept rather than discarded: it is the single richest
source of identity-linking evidence, since people publish their own phone
numbers and alternate addresses in it.

### Threads

```sql
threads
  id, source_id, source_thread_id UNIQUE,
  subject_normalized,
  participant_identity_ids  JSON,
  first_message_at, last_message_at,
  last_inbound_at,                   -- last message NOT from me
  last_outbound_at,                  -- last message from me
  message_count, unread_count,
  is_muted,
  summary, summary_updated_at
```

`last_inbound_at` and `last_outbound_at` are denormalised deliberately. They
turn "who haven't I replied to?" — a question that otherwise needs a correlated
subquery per thread — into an indexed scan:

```sql
SELECT * FROM threads
WHERE last_inbound_at > last_outbound_at
  AND last_inbound_at < :now - INTERVAL 2 DAY
  AND NOT is_muted
ORDER BY last_inbound_at ASC;
```

Gmail's `threadId` is authoritative for Gmail. `In-Reply-To` and `References`
are stored anyway, for cross-source threading later and for robustness when a
client breaks threading.

---

## 4. Messages (iMessage)

Structurally different enough that reusing the email tables would produce
garbage. See [ARCHITECTURE.md §5](ARCHITECTURE.md).

```sql
im_messages
  id, source_id, source_rowid UNIQUE, guid,
  chat_id, from_identity_id,
  text,
  text_source,               -- 'text_column' | 'attributed_body' | 'attachment_only'
  sent_at, is_from_me,
  service,                   -- iMessage | SMS
  is_edited, edited_at, is_unsent,
  has_attachments,
  session_id

reactions                    -- tapbacks are NOT messages
  id, target_guid, from_identity_id, kind, added BOOL, created_at

sessions                     -- the unit of meaning
  id, chat_id, started_at, ended_at, message_count,
  participant_identity_ids JSON, summary, embedded BOOL
```

Three things that will silently corrupt the corpus if missed:

**Text is often not in the `text` column.** On macOS Ventura and later — and
universally on macOS 26 — `message.text` is `NULL` and the content lives in
`attributedBody` as an Apple *typedstream* archive (`NSArchiver`, not a modern
`NSKeyedArchiver` bplist, so `plistlib` is the wrong tool). Proper
deserialisation is required; `pytypedstream` handles it. Note that `NULL` text
with `NULL` attributedBody usually means an attachment-only message, which
needs the attachment join instead.

**Tapbacks are stored as messages.** `associated_message_type` 2000–2005 means
a reaction was added, 3000–3005 removed. Ingesting these as messages fills the
corpus with `Liked "sounds good"`. They belong in `reactions`, keyed to the
target GUID.

**Timestamps are Apple epoch.** Nanoseconds since 2001-01-01 on modern macOS,
*seconds* on older versions. Detect by magnitude and convert.

Access is via an **immutable read-only copy** of `chat.db`, never the live
file, which Messages.app holds open with WAL companions.

---

## 5. Calendar

```sql
calendar_events
  id, source_id, source_event_id UNIQUE, calendar_id,
  ical_uid, recurring_event_id,      -- links an instance to its series
  title, description, location,
  starts_at, ends_at, timezone, all_day BOOL,
  recurrence_rule,
  organizer_identity_id,
  attendees JSON,                    -- handle + response status each
  my_response,                       -- accepted | declined | tentative | needs_action
  status,                            -- confirmed | tentative | cancelled
  is_instance_exception BOOL,
  updated_at, deleted_at
```

Recurring events are stored as **expanded instances within the sync window**,
not as a rule to evaluate at query time. "What do I have Tuesday?" must be an
indexed range scan; expanding RRULEs during retrieval is both slow and a source
of subtle correctness bugs around exceptions and DST.

`my_response` matters: a declined event is not on your calendar in any sense
that should appear in a briefing.

---

## 6. Derived data

Kept strictly separate from ingested data so the extraction model can change
without data loss.

```sql
message_analysis
  message_id PK, category, importance, requires_action, is_automated,
  summary, people JSON, model, prompt_version, created_at

commitments                          -- see ARCHITECTURE.md §4
  id, source_message_id, thread_id, kind, title, owner,
  due_at, due_precision, status, confidence, evidence_quote,
  dismissed_fingerprint, created_at, updated_at

facts                                -- durable statements about people
  id, subject_person_id, predicate, object_text, object_normalized,
  cardinality,                       -- single | multi
  confidence, source_message_id, evidence_quote,
  observed_at, valid_from, valid_to,
  superseded_by_fact_id,
  status                             -- active | superseded | disputed | rejected
```

`model` and `prompt_version` on `message_analysis` are what make re-derivation
tractable: when you change the extractor, you can find exactly which rows were
produced by the old one and re-run only those.

### Facts change, and contradictions are information

"Sarah's number is X" derived from a signature in March. In September a new
signature says Y. Three possibilities: she changed numbers, she has two, or one
extraction is wrong.

Resolution depends on **predicate cardinality**, which is part of the
vocabulary rather than guessed per case:

- `birthday`, `employer`, `job_title` — single-valued. Newer supersedes older;
  the old row stays with `status='superseded'` and a `valid_to`.
- `phone`, `email`, `address` — multi-valued. Both stay active.
- Equal-confidence contradiction on a single-valued predicate → both marked
  `disputed` and surfaced to the user.

**Nothing is ever silently overwritten.** A superseded fact keeps its evidence
and its validity interval, so "what was Sarah's old number?" and "when did she
change jobs?" remain answerable.

---

## 7. Semantic layer

```sql
chunks
  id, kind,                          -- message | session | event
  message_id | session_id | event_id,
  chunk_index, text, token_count,
  content_hash,                      -- stable id: re-ingest updates, not duplicates
  embedded_at, embedding_model

messages_fts                         -- FTS5 virtual table
  subject, body_clean, sender_name   -- synced by trigger
```

### What gets embedded

| Content | Embed? | Why |
|---|---|---|
| Email `body_clean` | Yes | The prose case |
| Session summaries (iMessage) | Yes | The unit of meaning |
| Calendar title + description | Yes | "the meeting about the budget" |
| Anything under ~15 tokens | **No** | "ok", "thanks" — matches everything, means nothing |
| Automated / bulk mail | **No** by default | 60–80% of volume; embedding it poisons search |
| Message metadata | **Never** | It has columns |
| Attachments | Not yet | Milestone TBD |

Everything skipped is still fully present in SQL and FTS5. Skipping the
embedding removes it from *semantic* recall, not from the system.

Point IDs are **content hashes**, not positional. Re-ingesting an edited
message updates the affected chunk rather than appending a duplicate.

---

## 8. Retrieval

### Classify before you search

Most questions about your own data are not semantic. Routing everything through
embeddings gets the majority wrong, which is why "semantic search over my
email" products feel unreliable.

| Class | Example | Plane | Operation |
|---|---|---|---|
| Lookup | "Sarah's number?" | facts | indexed point lookup |
| Temporal | "emails today" | facts | range scan on `received_at` |
| Aggregate | "how many unread this week" | facts | `COUNT` + `GROUP BY` |
| Relational | "who haven't I replied to" | facts | denormalised thread columns |
| Commitment | "what's due Friday" | commitments | range scan on `due_at` |
| Semantic | "the email about the interview" | vectors + FTS | hybrid |
| Composite | "catch me up" | all | orchestrated |

An agent that answers "I found some emails that might be relevant" when the
question was a `COUNT` has failed, regardless of how good the retrieval was.

### The semantic path

```
1. Parse constraints   → person, date range, source, labels, has_attachment
2. Resolve entities    → "Sarah" → identity_ids, via identity resolution
                         "my advisor" → person_id, via user memory
3. Build a predicate   → SQL WHERE / vector payload filter
4. Run BOTH channels, predicate applied INSIDE each:
     FTS5  MATCH ... WHERE <predicate>
     dense search with payload filter <predicate>
5. Fuse with RRF       → score = Σ 1/(k + rank), k = 60
6. Re-rank             → recency decay + importance
7. Thread-collapse     → dedupe chunks from one thread
8. Budget              → fit the context window
```

**Filter inside each channel, before fusion — never after.** This is the single
highest-impact retrieval decision, and it is not a micro-optimisation.

Post-filtering runs the search first and discards non-matching results from the
top-k. For personal email the filters are extremely low-selectivity — "from
Sarah last month" might be 0.1% of the corpus — and low selectivity is exactly
where post-filtering fails hardest: if Sarah's email ranked 200th globally, a
top-50 search never sees it, and no amount of filtering afterwards recovers it.
Post-filtering after fusion also silently shortens the final list below the
requested k.

RRF is rank-based rather than score-based, which is what makes it robust: it
needs no score normalisation between BM25 and cosine similarity, and it is
parameter-free apart from `k` (60 is the standard production value).

**Thread collapse** is worth calling out. If six of eight retrieved chunks come
from one thread, that is one source, not six. Collapsing to the thread plus its
best chunks — and spending the freed budget on other threads — measurably
improves answer diversity.

### Recency: filter or signal, never both

If the question carries an explicit time bound ("today", "last week"), recency
is a **filter** and must not also boost ranking — doing both double-counts and
crowds out the older-but-more-relevant item inside the window.

If the bound is implicit ("recent", "lately"), recency is a **ranking signal**
with exponential decay.

Conflating the two is a common and hard-to-debug source of bad results.

### Always cite, always bound

Every chunk carries `message_id` through to the answer, so a citation points at
a stored message the user can open — not at the model's recollection of one.

And every answer is bounded by the **data horizon** (§ARCHITECTURE, and
`backend/pipeline/horizon.py`): a question reaching before ingestion began gets
an explicit gap warning, because with live-only ingestion an empty result is
otherwise indistinguishable from "it never happened".

---

## Sources

- [Synchronize clients with Gmail — Google](https://developers.google.com/workspace/gmail/api/guides/sync)
- [Gmail API history.list reference](https://googleapis.github.io/google-api-python-client/docs/dyn/gmail_v1.users.history.html)
- [imessage_tools — parsing attributedBody](https://github.com/my-other-github-account/imessage_tools)
- [iMessage database format notes](https://glama.ai/mcp/servers/@jonmmease/jons-mcp-imessage/blob/3af876d28c0efe9f24b18db9936155308736c4b9/docs/IMESSAGE_DATABASE_FORMAT.md)
- [Pre-filtering vs post-filtering in vector search](https://apxml.com/courses/advanced-vector-search-llms/chapter-2-optimizing-vector-search-performance/advanced-filtering-strategies)
- [Reciprocal Rank Fusion for hybrid search](https://apxml.com/courses/advanced-vector-search-llms/chapter-3-hybrid-search-approaches/rrf-fusion-algorithms)
- [Understanding Reciprocal Rank Fusion — Guillaume Laforge](https://glaforge.dev/posts/2026/02/10/advanced-rag-understanding-reciprocal-rank-fusion-in-hybrid-search/)
- [What is entity resolution — RudderStack](https://www.rudderstack.com/blog/what-is-entity-resolution/)
- [Entity resolution — Neo4j](https://neo4j.com/blog/graph-database/what-is-entity-resolution/)
