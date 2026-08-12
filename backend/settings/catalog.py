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
