/**
 * Backend client.
 *
 * The auth token, when configured, lives only here and in localStorage — it is
 * the *API* token, never a Google OAuth token. OAuth credentials stay in the
 * backend and are never sent to the browser.
 */

export interface Conversation {
  id: string;
  title: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ChatTurn {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  model: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number | null;
  citations: { sources?: Citation[] } | null;
  error: string | null;
  created_at: string;
}

export interface Citation {
  ref: string;
  source: string;
  title?: string | null;
  author?: string | null;
  timestamp?: string | null;
}

export interface ConversationDetail extends Conversation {
  messages: ChatTurn[];
}

export interface ModelInfo {
  role: string;
  model: string;
  num_ctx: number;
  base_url: string;
  available: boolean;
  note: string | null;
}

export interface BackendStatus {
  name: string;
  base_url: string;
  connected: boolean;
  version: string | null;
  latency_ms: number | null;
  error: string | null;
  installed_models: string[];
}

export interface SourceStatus {
  kind: string;
  display_name: string;
  status: string;
  enabled: boolean;
  last_sync_at: string | null;
  last_error: string | null;
  implemented: boolean;
}

export interface Status {
  version: string;
  milestone: number;
  database_connected: boolean;
  database_path: string | null;
  llm_backends: BackendStatus[];
  models: ModelInfo[];
  sources: SourceStatus[];
  counts: {
    conversations: number;
    chat_turns: number;
    messages_indexed: number;
    threads_indexed: number;
    embeddings: number;
    pending_jobs: number;
  };
  read_only: boolean;
  uptime_seconds: number;
  ui: {
    default_role: "large" | "fast";
    show_context_meter: boolean;
    show_tool_calls: boolean;
    stream_responses: boolean;
  };
  restart_required: string[];
}

const TOKEN_KEY = "gary.apiToken";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken();
  return {
    ...extra,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

/** Error carrying the backend's structured {key, message} validation detail. */
export class ApiError extends Error {
  detail?: { key: string; message: string };
  status: number;

  constructor(message: string, status: number, detail?: { key: string; message: string }) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { ...init, headers: headers(init.headers as Record<string, string>) });
  if (!res.ok) {
    const raw = await res.text().catch(() => "");
    let detail: { key: string; message: string } | undefined;
    try {
      const parsed = JSON.parse(raw);
      if (parsed?.detail?.message) detail = parsed.detail;
    } catch {
      /* not JSON — fall through to the raw text */
    }
    throw new ApiError(
      detail?.message ?? `${res.status} ${res.statusText}${raw ? ` — ${raw.slice(0, 200)}` : ""}`,
      res.status,
      detail,
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function jsonBody(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export const api = {
  status: () => request<Status>("/api/status"),
  listConversations: () => request<Conversation[]>("/api/conversations"),
  getConversation: (id: string) => request<ConversationDetail>(`/api/conversations/${id}`),
  createConversation: () =>
    request<Conversation>("/api/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }),
  deleteConversation: (id: string) =>
    request<void>(`/api/conversations/${id}`, { method: "DELETE" }),
};

// --------------------------------------------------------------- settings

export interface SettingCategory {
  key: string;
  label: string;
  description: string;
}

export interface SettingField {
  key: string;
  label: string;
  description: string;
  category: string;
  type:
    | "string"
    | "text"
    | "int"
    | "float"
    | "bool"
    | "select"
    | "secret"
    | "url"
    | "model";
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  options: string[] | null;
  placeholder: string | null;
  unit: string | null;
  model_host_key: string | null;
  advanced: boolean;
  restart: boolean;
  sensitive: boolean;
  active: boolean;
  milestone: number;
  warning: string | null;
  examples: string[];
  value: unknown;
  is_set: boolean | null;
  source: "default" | "env" | "database";
  env_value: unknown;
  default_value: unknown;
  overridden: boolean;
}

export interface SettingsPayload {
  categories: SettingCategory[];
  fields: SettingField[];
  restart_required: string[];
}

export interface SettingsUpdateResult {
  changed: string[];
  restart_required: string[];
  applied_live: boolean;
  fields: SettingField[];
}

export interface TestConnectionResult {
  connected: boolean;
  base_url: string;
  version?: string | null;
  latency_ms?: number | null;
  models: string[];
  error?: string | null;
}

export const settingsApi = {
  read: () => request<SettingsPayload>("/api/settings"),
  update: (changes: Record<string, unknown>) =>
    request<SettingsUpdateResult>("/api/settings", jsonBody("PATCH", { changes })),
  resetOne: (key: string) =>
    request<SettingsUpdateResult>(`/api/settings/reset/${encodeURIComponent(key)}`, {
      method: "POST",
    }),
  resetAll: () => request<SettingsUpdateResult>("/api/settings/reset", { method: "POST" }),
  testConnection: (baseUrl: string) =>
    request<TestConnectionResult>(
      "/api/settings/test-connection",
      jsonBody("POST", { base_url: baseUrl }),
    ),
  models: (baseUrl?: string) =>
    request<string[]>(
      `/api/settings/models${baseUrl ? `?base_url=${encodeURIComponent(baseUrl)}` : ""}`,
    ),
};

// --------------------------------------------------------------- SSE chat

export interface StartPayload {
  conversation_id: string;
  message_id: string;
  user_message_id: string;
  model: string;
  role: string;
  context: {
    used_tokens: number;
    limit: number;
    utilisation: number;
    evicted_history: number;
    evicted_context: number;
  };
}

export interface DonePayload {
  message_id: string;
  latency_ms: number;
  usage: { prompt_tokens: number; completion_tokens: number };
  citations: { sources?: Citation[] } | null;
  title: string;
}

export interface ChatHandlers {
  onStart?: (p: StartPayload) => void;
  onToken?: (delta: string) => void;
  onDone?: (p: DonePayload) => void;
  onError?: (message: string) => void;
}

/**
 * Streams a chat reply. Uses fetch + ReadableStream rather than EventSource
 * because we need to POST a body and send an auth header — neither of which
 * EventSource supports.
 */
export async function streamChat(
  body: { message: string; conversation_id?: string | null; role?: "large" | "fast" },
  handlers: ChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        message: body.message,
        conversation_id: body.conversation_id ?? null,
        role: body.role ?? "large",
      }),
      signal,
    });
  } catch (err) {
    handlers.onError?.(
      `Cannot reach the Gary backend. Is it running? (${(err as Error).message})`,
    );
    return;
  }

  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    handlers.onError?.(`${res.status} ${res.statusText}${detail ? ` — ${detail.slice(0, 300)}` : ""}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      dispatchFrame(frame, handlers);
      boundary = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) dispatchFrame(buffer, handlers);
}

function dispatchFrame(frame: string, handlers: ChatHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
  }
  if (!dataLines.length) return;

  let payload: unknown;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    return;
  }

  switch (event) {
    case "start":
      handlers.onStart?.(payload as StartPayload);
      break;
    case "token":
      handlers.onToken?.((payload as { delta: string }).delta);
      break;
    case "done":
      handlers.onDone?.(payload as DonePayload);
      break;
    case "error":
      handlers.onError?.((payload as { message: string }).message);
      break;
  }
}

export function eventSocketUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = getToken();
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${proto}//${window.location.host}/ws/events${qs}`;
}
