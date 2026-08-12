"""Declarative catalog of every user-tunable setting.

One definition per setting drives all of: the API schema, server-side
validation, and the settings UI. Adding a knob means adding an entry here and
a field on `Settings` — never touching the frontend.

Three things every entry must be honest about:

  * `active`     — False means the setting is stored but nothing reads it yet
                   (Milestone 3+ features). The UI greys these out and labels
                   the milestone rather than pretending they work.
  * `restart`    — changing it needs a process restart to take effect.
  * `sensitive`  — the value is never sent to the browser, only whether it is
                   set. Writes are accepted; reads are not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

FieldType = Literal["string", "text", "int", "float", "bool", "select", "secret", "url", "model"]


@dataclass(frozen=True)
class SettingDef:
    key: str  # must match an attribute on backend.config.Settings
    label: str
    description: str
    category: str
    type: FieldType = "string"

    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    options: Optional[List[str]] = None
    placeholder: Optional[str] = None
    unit: Optional[str] = None

    #: Which Ollama host this model field is served from, for the model picker.
    model_host_key: Optional[str] = None

    advanced: bool = False
    restart: bool = False
    sensitive: bool = False
    active: bool = True
    milestone: int = 1
    warning: Optional[str] = None
    examples: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    description: str
    icon: str = ""


CATEGORIES: List[Category] = [
    Category(
        "connection",
        "Inference host",
        "Where your model actually runs. This can be this machine, or any other "
        "computer on your network — a desktop with a big GPU, for example.",
    ),
    Category(
        "models",
        "Models",
        "Which model handles which kind of work. Gary uses a large model for "
        "reasoning and a small fast one for classification and extraction, so "
        "routine work does not tie up your biggest model.",
    ),
    Category(
        "context",
        "Context & memory",
        "How much conversation and retrieved data fits in the prompt, and what "
        "gets dropped first when it does not fit.",
    ),
    Category(
        "retrieval",
        "Search & retrieval",
        "How Gary finds the right messages before answering. Combines keyword "
        "search, semantic similarity, recency, and importance.",
    ),
    Category(
        "sync",
        "Sync & ingestion",
        "How often Gary checks your accounts. Ingestion is live-only: a "
        "watermark is set when you connect an account and only what arrives "
        "after it is stored. Nothing historical is imported.",
    ),
    Category(
        "notifications",
        "Proactive alerts",
        "When Gary should interrupt you. Thresholds here are the difference "
        "between a useful assistant and a notification firehose.",
    ),
    Category(
        "interface",
        "Interface",
        "Chat UI behaviour and defaults.",
    ),
    Category(
        "server",
        "Server & security",
        "Network binding and API authentication. The defaults keep Gary "
        "reachable only from this machine.",
    ),
    Category(
        "storage",
        "Storage",
        "Where your data lives on disk.",
    ),
]


CATALOG: List[SettingDef] = [
    # ------------------------------------------------------------ connection
    SettingDef(
        key="ollama_base_url",
        label="Ollama URL",
        description=(
            "The Ollama server that runs chat and reasoning. Point this at any "
            "machine on your network to offload inference.\n\n"
            "Important: on the machine that serves the model, Ollama must be "
            "started with OLLAMA_HOST=0.0.0.0 ollama serve — the default binds "
            "to loopback only, so nothing else on your network can reach it. "
            "This is the single most common reason a remote URL fails to connect."
        ),
        category="connection",
        type="url",
        placeholder="http://localhost:11434",
        examples=[
            "http://localhost:11434",
            "http://192.168.1.42:11434",
            "http://desktop.local:11434",
        ],
    ),
    SettingDef(
        key="ollama_embed_base_url",
        label="Embeddings URL",
        description=(
            "Optional. Run embeddings on a different machine than chat — useful "
            "when one box has the big GPU and another is idle. Leave blank to "
            "use the same host as chat."
        ),
        category="connection",
        type="url",
        placeholder="(same as Ollama URL)",
        advanced=True,
    ),
    SettingDef(
        key="ollama_timeout",
        label="Request timeout",
        description=(
            "How long to wait for a generation before giving up. A large model "
            "on a remote machine over WiFi can take a while to produce its first "
            "token; raise this if you see timeouts on long answers."
        ),
        category="connection",
        type="float",
        minimum=5,
        maximum=3600,
        step=5,
        unit="seconds",
        advanced=True,
    ),
    SettingDef(
        key="ollama_keep_alive",
        label="Keep model loaded",
        description=(
            "How long Ollama holds the model in memory after a request. Longer "
            "means faster follow-up questions but the VRAM stays occupied. Use "
            "'-1' to keep it resident permanently, '0' to unload immediately."
        ),
        category="connection",
        type="select",
        options=["0", "30s", "5m", "30m", "1h", "-1"],
        advanced=True,
    ),
    # ---------------------------------------------------------------- models
    SettingDef(
        key="llm_model_large",
        label="Large model",
        description=(
            "Handles complex questions, multi-step reasoning, cross-source "
            "analysis, and agent planning. This is the one that answers you in "
            "chat when 'Deep' is selected.\n\n"
            "Must be a tag that exists on the inference host — run `ollama list` "
            "there to see what is installed."
        ),
        category="models",
        type="model",
        model_host_key="ollama_base_url",
        examples=["gemma3:27b", "qwen3:32b", "llama3.3:70b"],
    ),
    SettingDef(
        key="llm_context_large",
        label="Large model context window",
        description=(
            "Maximum prompt size in tokens for the large model, passed to Ollama "
            "as num_ctx. Larger windows hold more conversation and more retrieved "
            "email, but cost memory on the serving machine.\n\n"
            "32768 is comfortable for a 27B model on 32GB+. If the model spills "
            "out of VRAM and generation slows to a crawl, lower this first."
        ),
        category="models",
        type="int",
        minimum=2048,
        maximum=1048576,
        step=1024,
        unit="tokens",
    ),
    SettingDef(
        key="llm_temperature_large",
        label="Large model temperature",
        description=(
            "Sampling randomness for the large model. Low values keep answers "
            "grounded and repeatable, which is what you want when the model is "
            "reporting facts from your email. Leave blank to use the global "
            "default below."
        ),
        category="models",
        type="float",
        minimum=0.0,
        maximum=2.0,
        step=0.05,
        placeholder="(use global default)",
        advanced=True,
    ),
    SettingDef(
        key="llm_model_fast",
        label="Fast model",
        description=(
            "Handles classification, entity and deadline extraction, and short "
            "summaries — the high-volume background work. Also used for chat when "
            "'Fast' is selected.\n\n"
            "Using a smaller model here is the difference between classifying a "
            "day of email in a minute versus an hour."
        ),
        category="models",
        type="model",
        model_host_key="ollama_base_url",
        examples=["gemma3:12b", "qwen3:8b", "llama3.1:8b"],
    ),
    SettingDef(
        key="llm_context_fast",
        label="Fast model context window",
        description=(
            "Prompt size for the fast model. Classification works on one message "
            "at a time, so this rarely needs to be large."
        ),
        category="models",
        type="int",
        minimum=2048,
        maximum=1048576,
        step=1024,
        unit="tokens",
    ),
    SettingDef(
        key="llm_temperature_fast",
        label="Fast model temperature",
        description=(
            "Sampling randomness for the fast model. Keep this near zero: "
            "classification and extraction should be deterministic. Leave blank "
            "for the global default."
        ),
        category="models",
        type="float",
        minimum=0.0,
        maximum=2.0,
        step=0.05,
        placeholder="(use global default)",
        advanced=True,
    ),
    SettingDef(
        key="llm_model_router",
        label="Router model",
        description=(
            "Decides whether a question needs a database search at all, and "
            "rewrites vague follow-ups like 'tell me more' into standalone "
            "queries. Runs on every message, so it should be small and fast. "
            "Its temperature is pinned to 0 — it emits JSON, not prose."
        ),
        category="models",
        type="model",
        model_host_key="ollama_base_url",
        advanced=True,
        examples=["gemma3:12b", "llama3.1:8b"],
    ),
    SettingDef(
        key="llm_context_router",
        label="Router context window",
        description=(
            "Prompt size for the router model. It only ever sees the current "
            "question plus a little conversation context, so this can stay small."
        ),
        category="models",
        type="int",
        minimum=2048,
        maximum=131072,
        step=1024,
        unit="tokens",
        advanced=True,
    ),
    SettingDef(
        key="embedding_model",
        label="Embedding model",
        description=(
            "Turns message text into vectors for semantic search.\n\n"
            "Changing this after you have indexed data invalidates every existing "
            "embedding — different models produce incompatible vector spaces, and "
            "of different dimensions. Everything must be re-embedded, which for a "
            "large mailbox can take hours."
        ),
        category="models",
        type="model",
        model_host_key="ollama_embed_base_url",
        milestone=3,
        active=False,
        warning="Changing this requires re-embedding your entire index.",
        examples=["qwen3-embedding:4b", "nomic-embed-text", "mxbai-embed-large"],
    ),
    SettingDef(
        key="llm_temperature",
        label="Default temperature",
        description=(
            "Global fallback sampling temperature, used by any role without its "
            "own override. 0.0 is fully deterministic; above ~1.0 answers get "
            "erratic. For an assistant reporting facts from your data, low is right."
        ),
        category="models",
        type="float",
        minimum=0.0,
        maximum=2.0,
        step=0.05,
    ),
    # --------------------------------------------------------------- context
    SettingDef(
        key="llm_generation_buffer",
        label="Reply headroom",
        description=(
            "Tokens held back from the context window for the model's own reply. "
            "If this is too small the model runs out of room mid-answer and gets "
            "cut off; too large and you waste window that could hold more of your "
            "email."
        ),
        category="context",
        type="int",
        minimum=256,
        maximum=32768,
        step=256,
        unit="tokens",
    ),
    SettingDef(
        key="max_history_turns",
        label="Conversation history depth",
        description=(
            "How many past messages from the current conversation are considered "
            "for the prompt. The context budget may still evict some of these if "
            "they do not fit — this is the ceiling, not a guarantee."
        ),
        category="context",
        type="int",
        minimum=2,
        maximum=200,
        step=2,
        unit="messages",
    ),
    SettingDef(
        key="context_safety_margin",
        label="Context safety margin",
        description=(
            "Tokens kept free as a cushion against tokeniser disagreement between "
            "Gary's count and the model's actual count. Raise it if you ever see "
            "context-overflow errors from Ollama."
        ),
        category="context",
        type="int",
        minimum=0,
        maximum=8192,
        step=50,
        unit="tokens",
        advanced=True,
    ),
    # ------------------------------------------------------------- retrieval
    SettingDef(
        key="retrieval_top_k",
        label="Sources per search",
        description=(
            "How many message chunks each search returns. More gives the model "
            "better coverage but fills the context window faster and slows "
            "generation."
        ),
        category="retrieval",
        type="int",
        minimum=1,
        maximum=50,
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="retrieval_dense_threshold",
        label="Semantic similarity floor",
        description=(
            "Minimum cosine similarity for a semantic match to count. Too low and "
            "unrelated messages pad the context; too high and paraphrased matches "
            "get missed."
        ),
        category="retrieval",
        type="float",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="retrieval_max_chars_per_source",
        label="Max characters per source",
        description=(
            "Each retrieved message is truncated to this length before entering "
            "the prompt, so one enormous newsletter cannot crowd out everything else."
        ),
        category="retrieval",
        type="int",
        minimum=500,
        maximum=100000,
        step=500,
        unit="characters",
        milestone=3,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="hybrid_weight_keyword",
        label="Keyword weight",
        description=(
            "Importance of exact keyword matching (BM25) in ranking. Raise it if "
            "searches for names, invoice numbers, or exact phrases are ranking too "
            "low."
        ),
        category="retrieval",
        type="float",
        minimum=0.0,
        maximum=5.0,
        step=0.1,
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="hybrid_weight_semantic",
        label="Semantic weight",
        description=(
            "Importance of meaning-based similarity. Raise it if you often "
            "describe an email in your own words rather than quoting it."
        ),
        category="retrieval",
        type="float",
        minimum=0.0,
        maximum=5.0,
        step=0.1,
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="hybrid_weight_recency",
        label="Recency weight",
        description=(
            "How much newer messages are favoured. Higher values help with "
            "'what happened today' and hurt when you are digging through archives."
        ),
        category="retrieval",
        type="float",
        minimum=0.0,
        maximum=5.0,
        step=0.1,
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="hybrid_weight_importance",
        label="Importance weight",
        description=(
            "How much the classifier's importance score boosts ranking, so "
            "actionable mail surfaces above newsletters."
        ),
        category="retrieval",
        type="float",
        minimum=0.0,
        maximum=5.0,
        step=0.1,
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="chunk_target_chars",
        label="Chunk target size",
        description=(
            "Preferred size of each indexed text chunk. Chunks are split on "
            "paragraph boundaries where possible, so actual sizes vary around this "
            "target."
        ),
        category="retrieval",
        type="int",
        minimum=200,
        maximum=8000,
        step=100,
        unit="characters",
        milestone=3,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="chunk_ceiling_chars",
        label="Chunk hard ceiling",
        description=(
            "Absolute maximum chunk size. Chunking stretches past the target up to "
            "this limit rather than cutting a paragraph in half."
        ),
        category="retrieval",
        type="int",
        minimum=300,
        maximum=16000,
        step=100,
        unit="characters",
        milestone=3,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="chunk_overlap_sentences",
        label="Chunk overlap",
        description=(
            "Complete sentences repeated from the end of one chunk at the start of "
            "the next, so meaning spanning a boundary is not lost."
        ),
        category="retrieval",
        type="int",
        minimum=0,
        maximum=10,
        unit="sentences",
        milestone=3,
        active=False,
        advanced=True,
    ),
    # ------------------------------------------------------------------ sync
    SettingDef(
        key="gmail_poll_interval_seconds",
        label="Gmail check interval",
        description=(
            "How often to ask Gmail what has changed since the last check.\n\n"
            "This is one cheap API call against a watermark that returns nothing "
            "when your inbox is idle, so a short interval costs almost nothing. "
            "Gary polls rather than using Gmail push because push requires a "
            "public HTTPS endpoint Google can reach — which on a laptop means "
            "running a tunnel and letting a third party see your notification "
            "traffic. Polling is simpler, more private, and under a minute behind."
        ),
        category="sync",
        type="int",
        minimum=15,
        maximum=3600,
        step=15,
        unit="seconds",
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="seed_window_days",
        label="Initial seed window",
        description=(
            "Gary is live-only: it records a watermark when you connect an "
            "account and ingests what arrives after it. Nothing historical.\n\n"
            "That means day one is empty, and questions about last month have no "
            "answer until those conversations happen again. Setting this to 7 "
            "pulls one recent week on first connect so the app is useful "
            "immediately. It runs once, is capped by the setting below, and is "
            "not a backlog import. 0 disables it entirely."
        ),
        category="sync",
        type="int",
        minimum=0,
        maximum=90,
        unit="days",
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="seed_max_messages",
        label="Seed message cap",
        description=(
            "Hard ceiling on the one-off seed, so a busy week cannot turn a "
            "bootstrap into a multi-hour import."
        ),
        category="sync",
        type="int",
        minimum=0,
        maximum=5000,
        step=50,
        unit="messages",
        milestone=2,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="calendar_poll_interval_seconds",
        label="Calendar check interval",
        description=(
            "How often to check Google Calendar for changes, using a sync token "
            "so only what actually changed comes back."
        ),
        category="sync",
        type="int",
        minimum=30,
        maximum=86400,
        step=30,
        unit="seconds",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="calendar_future_days",
        label="Calendar look-ahead",
        description=(
            "How far forward to sync events.\n\n"
            "Calendar is the one source where 'only new things from now on' is "
            "the wrong rule: a calendar's value is in the future, and tomorrow's "
            "meeting was probably created last week. So Gary syncs a window "
            "rather than a watermark, and this is its forward edge."
        ),
        category="sync",
        type="int",
        minimum=1,
        maximum=730,
        step=7,
        unit="days",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="calendar_past_days",
        label="Calendar look-back",
        description=(
            "How far back to sync past events. A small window is enough to "
            "answer 'when did I last meet Sarah?' without importing years."
        ),
        category="sync",
        type="int",
        minimum=0,
        maximum=365,
        step=7,
        unit="days",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="imessage_poll_interval_seconds",
        label="iMessage check interval",
        description=(
            "How often to read new rows from the local Messages database. This "
            "is a local file read against a row-ID watermark, so it is cheap and "
            "can be frequent."
        ),
        category="sync",
        type="int",
        minimum=5,
        maximum=3600,
        step=5,
        unit="seconds",
        milestone=7,
        active=False,
    ),
    SettingDef(
        key="imessage_session_gap_minutes",
        label="Message session gap",
        description=(
            "Consecutive messages in one chat closer together than this are "
            "treated as a single conversation session.\n\n"
            "Sessions, not individual messages, are what get summarised and "
            "embedded. A text saying 'friday works' means nothing on its own — "
            "it means something alongside the three messages before it. "
            "Grouping also keeps 500 messages a day down to roughly 20 units of "
            "work rather than 500."
        ),
        category="sync",
        type="int",
        minimum=1,
        maximum=1440,
        step=5,
        unit="minutes",
        milestone=7,
        active=False,
    ),
    SettingDef(
        key="gmail_label_mode",
        label="Which mail to sync",
        description=(
            "'default' keeps your Inbox and Sent mail and skips the Promotions, "
            "Social and Forums tabs — typically half an inbox, and the main "
            "source of noise in search.\n\n"
            "Updates is deliberately kept: Gmail files receipts, bills, shipping "
            "and appointment reminders there, and those carry real deadlines. "
            "'all_mail' takes everything except Spam and Trash. 'custom' uses "
            "exactly the labels you list below — useful if your filters "
            "auto-archive things you still read."
        ),
        category="sync",
        type="select",
        options=["default", "all_mail", "custom"],
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="gmail_include_labels",
        label="Labels to include",
        description=(
            "Comma-separated Gmail labels to sync when the mode above is "
            "'custom'. Both system labels (INBOX, SENT, STARRED) and your own "
            "work.\n\n"
            "A misspelled label matches nothing and silently loses that mail, so "
            "the status page flags any label here that does not exist on your "
            "account."
        ),
        category="sync",
        type="string",
        placeholder="INBOX,SENT",
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="gmail_exclude_labels",
        label="Labels to exclude",
        description=(
            "Comma-separated labels to skip, applied after the include list. "
            "Spam, Trash and Drafts are always excluded regardless."
        ),
        category="sync",
        type="string",
        milestone=2,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="mirror_upstream_deletions",
        label="Mirror deletions from Gmail",
        description=(
            "When you delete a message in Gmail, delete Gary's copy too.\n\n"
            "The deletion cascades: the message, its chunks, its embeddings, its "
            "search-index rows, any deadline extracted from it, and any "
            "attachment files downloaded from it. Nothing is left behind on "
            "disk.\n\n"
            "Turning this off means Gary keeps a copy of mail your mailbox no "
            "longer has — searchable, and surprising later."
        ),
        category="sync",
        type="bool",
        milestone=2,
        active=False,
        warning="Deletions are permanent and cascade to extracted deadlines.",
    ),
    SettingDef(
        key="attachment_download",
        label="Download attachments",
        description=(
            "Fetch attachment files, not just their names. Required for "
            "searching inside documents.\n\n"
            "Files are stored under a generated ID, never the sender-supplied "
            "filename — a name like '../../.ssh/authorized_keys' must never "
            "become a path. Identical files are stored once by content hash, so "
            "a PDF forwarded five times costs one copy."
        ),
        category="storage",
        type="bool",
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="attachment_max_mb",
        label="Attachment size limit",
        description=(
            "Skip attachments larger than this. Their metadata is still "
            "recorded, so Gary knows the file exists and can name it."
        ),
        category="storage",
        type="float",
        minimum=0.1,
        maximum=200.0,
        step=0.5,
        unit="MB",
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="attachment_total_budget_gb",
        label="Attachment disk budget",
        description=(
            "Hard ceiling on total attachment storage. On reaching it Gary stops "
            "downloading and says so, rather than quietly filling the disk — "
            "SQLite can corrupt on a full volume."
        ),
        category="storage",
        type="float",
        minimum=0.1,
        maximum=500.0,
        step=0.5,
        unit="GB",
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="attachment_index_text",
        label="Search inside documents",
        description=(
            "Extract text from PDFs and Office documents so their contents are "
            "searchable — 'what did the contract say about termination?'.\n\n"
            "Extraction is reliable on documents produced digitally and "
            "unreliable on scans and photographs, which contain no text layer at "
            "all. Where nothing can be extracted Gary records that the document "
            "is unreadable rather than treating it as empty."
        ),
        category="storage",
        type="bool",
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="attachment_ocr_scanned",
        label="OCR scanned documents",
        description=(
            "Run optical character recognition on documents with no text layer. "
            "Makes scans and photographed paperwork searchable.\n\n"
            "Considerably slower than text extraction and produces errors on "
            "poor scans, so OCR'd text is marked as such and ranked below "
            "extracted text."
        ),
        category="storage",
        type="bool",
        milestone=3,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="image_embedding_enabled",
        label="Understand images",
        description=(
            "Index photos so you can find them by describing them — 'the photo "
            "of the whiteboard', 'the receipt from the restaurant'.\n\n"
            "Images are routed rather than uniformly processed: photographs get "
            "a visual embedding, while screenshots and scans go to text "
            "recognition instead, because an image embedding captures that "
            "something IS a screenshot but not what it says."
        ),
        category="storage",
        type="bool",
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="image_embedding_model",
        label="Image model",
        description=(
            "Vision model used to encode photos. CLIP ViT-B/32 is the "
            "well-understood default at 512 dimensions — about 2KB per image.\n\n"
            "Note this model's vectors live in their own space and cannot be "
            "compared with text embeddings, so images use a separate index and "
            "your query is encoded twice. Results from both are merged by rank, "
            "which works fine across incompatible score scales."
        ),
        category="storage",
        type="string",
        milestone=3,
        active=False,
        advanced=True,
        examples=["clip-ViT-B-32", "clip-ViT-L-14", "nomic-embed-vision-v1.5"],
        warning="Changing this invalidates every existing image vector.",
    ),
    SettingDef(
        key="image_embed_imessage",
        label="Index iMessage photos",
        description=(
            "Include photos sent and received in Messages. This is where most "
            "personal photos actually are, and the highest-value source for "
            "'find that picture Sarah sent'. Also the highest volume."
        ),
        category="storage",
        type="bool",
        milestone=7,
        active=False,
    ),
    SettingDef(
        key="image_embed_inline_email",
        label="Index inline email images",
        description=(
            "Images embedded in message bodies rather than attached.\n\n"
            "Off by default and worth leaving off: these are logos, banners and "
            "signature graphics almost without exception, and indexing them "
            "returns brand assets for every image query."
        ),
        category="storage",
        type="bool",
        milestone=3,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="image_ocr_text_heavy",
        label="Read text in screenshots",
        description=(
            "Route screenshots, scans and documents to text recognition instead "
            "of visual embedding, so their contents become searchable.\n\n"
            "Detection is metadata-only and costs nothing: camera EXIF marks a "
            "photograph, exact device screen dimensions mark a screenshot, and "
            "a mostly-white frame marks a page. Turning this off makes 'the "
            "screenshot where Alex sent the address' unfindable."
        ),
        category="storage",
        type="bool",
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="image_keep_location_exif",
        label="Keep photo location data",
        description=(
            "Photos routinely carry precise GPS coordinates. Keeping them would "
            "make 'photos from Paris' possible.\n\n"
            "Off by default, deliberately. An indexed archive of coordinates is "
            "a record of everywhere you have been — a much larger disclosure "
            "than the photos themselves, and not something anyone expects a mail "
            "assistant to build."
        ),
        category="storage",
        type="bool",
        milestone=3,
        active=False,
        warning="Enabling this builds a searchable history of your locations.",
    ),
    SettingDef(
        key="track_own_promises",
        label="Track promises you make",
        description=(
            "Pull commitments out of your own sent mail. 'I'll send the report "
            "Friday' becomes something Gary knows you owe.\n\n"
            "This is the only way anything tracks what you said you would do — "
            "incoming mail only ever shows what others asked of you."
        ),
        category="notifications",
        type="bool",
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="remind_own_promises",
        label="Remind me about my promises",
        description=(
            "Surface your own promises before they come due, alongside tasks "
            "other people gave you. Turning this off keeps them searchable but "
            "silent."
        ),
        category="notifications",
        type="bool",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="own_promise_min_confidence",
        label="Promise confidence floor",
        description=(
            "How certain the extractor must be before something you wrote counts "
            "as a promise. Set higher than the general extraction floor on "
            "purpose.\n\n"
            "A throwaway 'I'll take a look' should not become a reminder, while "
            "'I'll have it to you by Friday' should. Raise this if Gary nags you "
            "about things you did not really commit to."
        ),
        category="notifications",
        type="float",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        milestone=3,
        active=False,
    ),
    SettingDef(
        key="identity_merge_policy",
        label="Merging people",
        description=(
            "When Gary spots that two handles (a work email and a personal "
            "Gmail, say) look like the same person, how eagerly should it "
            "combine them?\n\n"
            "'always_ask' means no merge ever happens without your approval, "
            "however certain the evidence — a wrong merge quietly mixes two "
            "people's history together, and one tap is cheaper than unpicking "
            "that later. Weak guesses are still filtered out entirely rather "
            "than queued for review."
        ),
        category="sync",
        type="select",
        options=["always_ask", "conservative", "moderate"],
        milestone=2,
        active=False,
    ),
    SettingDef(
        key="triage_audit_rate",
        label="Triage audit sample",
        description=(
            "Fraction of messages that triage skipped which get re-run through "
            "the model anyway, to measure what the rules are missing.\n\n"
            "Triage saves most of the GPU time by skipping bulk mail, but rules "
            "are occasionally wrong. Sampling makes the miss rate visible "
            "instead of assumed. The sample is deterministic, so re-running an "
            "audit compares like with like."
        ),
        category="sync",
        type="float",
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        milestone=3,
        active=False,
        advanced=True,
    ),
    SettingDef(
        key="importance_prior_weight",
        label="Weight of your habits",
        description=(
            "How much your own behaviour towards a sender can move their "
            "importance score, versus the model's reading of the message.\n\n"
            "A fresh model cannot know your advisor outranks a newsletter, but "
            "your reply rate and reply speed already say so. This caps that "
            "influence: at 0.45 behaviour shades the judgement without "
            "overriding it, so a genuinely urgent first message from a stranger "
            "still gets through. Set to 0 to use the model's score alone."
        ),
        category="notifications",
        type="float",
        minimum=0.0,
        maximum=0.9,
        step=0.05,
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="user_timezone",
        label="Your timezone",
        description=(
            "IANA timezone name, used to turn relative deadlines into real "
            "dates.\n\n"
            "'The report is due Friday' has no meaning without knowing when the "
            "message was sent and where you are. Extraction anchors relative "
            "dates to the message's own timestamp in this zone. Getting it wrong "
            "shifts every extracted deadline."
        ),
        category="sync",
        type="string",
        placeholder="America/New_York",
        milestone=3,
        active=False,
        examples=["UTC", "America/New_York", "America/Los_Angeles", "Europe/London"],
    ),
    # --------------------------------------------------------- notifications
    SettingDef(
        key="classify_new_messages",
        label="Classify incoming messages",
        description=(
            "Run the fast model over each new message to extract importance, "
            "deadlines, people, and suggested actions. This is what powers alerts "
            "and the daily briefing. Turning it off saves compute but leaves Gary "
            "purely reactive.\n\n"
            "Historical backfill is never classified — only messages arriving "
            "after the initial sync — so switching this on does not trigger hours "
            "of GPU work on old mail."
        ),
        category="notifications",
        type="bool",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="notify_importance_threshold",
        label="Alert threshold",
        description=(
            "Minimum importance score, 0 to 1, before Gary raises an alert. "
            "Around 0.7 surfaces genuinely urgent mail; below 0.5 you will hear "
            "about newsletters."
        ),
        category="notifications",
        type="float",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="notify_requires_action_only",
        label="Only alert when action is needed",
        description=(
            "Require both a high importance score and the classifier judging that "
            "you need to do something. Recommended — an important message you have "
            "already handled is not worth an interruption."
        ),
        category="notifications",
        type="bool",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="notify_max_per_hour",
        label="Alert rate limit",
        description=(
            "Hard cap on notifications per hour, regardless of threshold. The "
            "backstop that keeps a bad classification run from becoming a "
            "notification storm."
        ),
        category="notifications",
        type="int",
        minimum=0,
        maximum=60,
        unit="per hour",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="notify_quiet_hours_start",
        label="Quiet hours start",
        description="Hour of day (0–23) when alerts stop. Alerts still appear in-app.",
        category="notifications",
        type="int",
        minimum=0,
        maximum=23,
        unit="o'clock",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="notify_quiet_hours_end",
        label="Quiet hours end",
        description=(
            "Hour of day (0–23) when alerts resume. Anything held back during "
            "quiet hours is still waiting for you in the app."
        ),
        category="notifications",
        type="int",
        minimum=0,
        maximum=23,
        unit="o'clock",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="notify_browser",
        label="Browser notifications",
        description="Show desktop notifications through the browser. Requires granting permission.",
        category="notifications",
        type="bool",
        milestone=6,
        active=False,
    ),
    SettingDef(
        key="notify_macos",
        label="Native macOS notifications",
        description=(
            "Post to macOS Notification Centre directly. Works even when the "
            "browser tab is closed, but only on macOS."
        ),
        category="notifications",
        type="bool",
        milestone=6,
        active=False,
    ),
    # ------------------------------------------------------------- interface
    SettingDef(
        key="ui_default_role",
        label="Default model",
        description=(
            "Which model new conversations start with. 'Deep' is the large model, "
            "'Fast' the small one."
        ),
        category="interface",
        type="select",
        options=["large", "fast"],
    ),
    SettingDef(
        key="ui_stream_responses",
        label="Stream responses",
        description=(
            "Show the reply token by token as it is generated. Turning this off "
            "waits for the complete answer, which some people find less distracting."
        ),
        category="interface",
        type="bool",
    ),
    SettingDef(
        key="ui_show_context_meter",
        label="Show context meter",
        description="Display live context-window utilisation in the status bar.",
        category="interface",
        type="bool",
    ),
    SettingDef(
        key="ui_show_tool_calls",
        label="Show tool activity",
        description=(
            "Display which searches the agent ran while answering. Useful for "
            "understanding why you got a given answer."
        ),
        category="interface",
        type="bool",
        milestone=5,
        active=False,
    ),
    # ---------------------------------------------------------------- server
    SettingDef(
        key="app_host",
        label="Bind address",
        description=(
            "Which network interface the backend listens on. 127.0.0.1 means only "
            "this machine can reach Gary — the right answer for an app holding "
            "your entire mailbox.\n\n"
            "Setting 0.0.0.0 exposes Gary to your whole network and requires an "
            "API token; Gary refuses to start otherwise."
        ),
        category="server",
        type="select",
        options=["127.0.0.1", "0.0.0.0"],
        restart=True,
        warning="0.0.0.0 exposes Gary to your local network. Set an API token first.",
    ),
    SettingDef(
        key="app_port",
        label="Port",
        description=(
            "TCP port the backend listens on. Change it if something else on "
            "this machine already owns 8000."
        ),
        category="server",
        type="int",
        minimum=1,
        maximum=65535,
        restart=True,
    ),
    SettingDef(
        key="api_auth_token",
        label="API token",
        description=(
            "Bearer token required on every API request. Optional while bound to "
            "127.0.0.1, mandatory otherwise.\n\n"
            "This is Gary's own API token — it is never a Google credential. "
            "OAuth tokens stay in the backend and are never sent to the browser."
        ),
        category="server",
        type="secret",
        sensitive=True,
        placeholder="(not set)",
    ),
    SettingDef(
        key="cors_origins_raw",
        label="Allowed origins",
        description=(
            "Comma-separated browser origins permitted to call the API. The Vite "
            "dev server needs to be listed here during development."
        ),
        category="server",
        type="string",
        restart=True,
        advanced=True,
    ),
    SettingDef(
        key="log_level",
        label="Log level",
        description=(
            "Backend logging verbosity. DEBUG logs every prompt assembly and "
            "context-eviction decision, which is invaluable when retrieval is "
            "returning the wrong thing."
        ),
        category="server",
        type="select",
        options=["DEBUG", "INFO", "WARNING", "ERROR"],
    ),
    # --------------------------------------------------------------- storage
    SettingDef(
        key="database_url",
        label="Database URL",
        description=(
            "SQLAlchemy URL for the message database. The default keeps "
            "everything in a single SQLite file under ./data.\n\n"
            "Changing this points Gary at a different database — it does not move "
            "your existing data."
        ),
        category="storage",
        type="string",
        restart=True,
        advanced=True,
        warning="Changing this does not migrate existing data.",
    ),
    SettingDef(
        key="data_dir",
        label="Data directory",
        description="Where the database, attachments, and vector index are stored.",
        category="storage",
        type="string",
        restart=True,
        advanced=True,
    ),
]


BY_KEY: Dict[str, SettingDef] = {d.key: d for d in CATALOG}


def definition(key: str) -> Optional[SettingDef]:
    return BY_KEY.get(key)


def editable_keys() -> List[str]:
    return [d.key for d in CATALOG]


def coerce(defn: SettingDef, raw: Any) -> Any:
    """Turn a JSON value from the UI into the type `Settings` expects.

    Empty string means "unset" — the field falls back to its .env or default
    value rather than being stored as an empty override.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None

    if defn.type in ("int",):
        return int(raw)
    if defn.type in ("float",):
        return float(raw)
    if defn.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return str(raw).strip() if isinstance(raw, str) else raw


class SettingValidationError(ValueError):
    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.message = message


def validate(defn: SettingDef, value: Any) -> Any:
    """Range and option checks beyond what pydantic gives us."""
    if value is None:
        return None

    if defn.type in ("int", "float"):
        if defn.minimum is not None and value < defn.minimum:
            raise SettingValidationError(
                defn.key, f"{defn.label} must be at least {defn.minimum}."
            )
        if defn.maximum is not None and value > defn.maximum:
            raise SettingValidationError(
                defn.key, f"{defn.label} must be at most {defn.maximum}."
            )

    if defn.type == "select" and defn.options and str(value) not in defn.options:
        raise SettingValidationError(
            defn.key, f"{defn.label} must be one of: {', '.join(defn.options)}."
        )

    if defn.type == "url" and value:
        if not str(value).startswith(("http://", "https://")):
            raise SettingValidationError(
                defn.key, f"{defn.label} must start with http:// or https://."
            )

    return value
