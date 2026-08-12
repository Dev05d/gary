#!/usr/bin/env bash
#
# Gary — one command to start everything.
#
#   ./start.sh          backend + frontend dev server
#   ./start.sh --build  build the UI and serve it from the backend (one port)
#   ./start.sh --api    backend only
#
set -euo pipefail

cd "$(dirname "$0")"

BLUE=$'\033[34m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf "%s==>%s %s\n" "$BLUE"  "$OFF" "$*"; }
ok()   { printf "%s  ok%s %s\n" "$GREEN" "$OFF" "$*"; }
warn() { printf "%s  !!%s %s\n" "$YELLOW" "$OFF" "$*"; }
die()  { printf "%s error:%s %s\n" "$RED" "$OFF" "$*" >&2; exit 1; }

MODE="dev"
case "${1:-}" in
  --build) MODE="build" ;;
  --api)   MODE="api" ;;
  "")      ;;
  *)       die "unknown option: $1 (use --build, --api, or no argument)" ;;
esac

# --- Python -----------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    ver=$("$c" -c 'import sys;print(f"{sys.version_info.major}{sys.version_info.minor:02d}")')
    if [ "$ver" -ge 311 ]; then PY="$c"; break; fi
  fi
done
[ -n "$PY" ] || die "Python 3.11+ not found. Install it: brew install python@3.12"

if [ ! -d .venv ]; then
  say "Creating virtualenv (.venv)"
  "$PY" -m venv .venv
fi

# Reinstall only when requirements change.
STAMP=.venv/.requirements.sha
NEW_SUM=$(shasum -a 256 requirements.txt 2>/dev/null | cut -d' ' -f1 || sha256sum requirements.txt | cut -d' ' -f1)
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$NEW_SUM" ]; then
  say "Installing Python dependencies"
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
  echo "$NEW_SUM" > "$STAMP"
  ok "dependencies installed"
fi

# --- Config -----------------------------------------------------------------
if [ ! -f .env ]; then
  say "Creating .env from .env.example"
  cp .env.example .env
  warn "Edit .env to set OLLAMA_BASE_URL and your model names, then re-run."
fi

mkdir -p data

# Read a few values for the pre-flight checks below. The app itself parses
# .env properly via pydantic-settings; this is only for friendly messages.
OLLAMA_URL=$(grep -E '^OLLAMA_BASE_URL=' .env | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
OLLAMA_URL=${OLLAMA_URL:-http://localhost:11434}
APP_PORT=$(grep -E '^APP_PORT=' .env | tail -1 | cut -d= -f2- || true)
APP_PORT=${APP_PORT:-8000}
APP_HOST=$(grep -E '^APP_HOST=' .env | tail -1 | cut -d= -f2- || true)
APP_HOST=${APP_HOST:-127.0.0.1}

# --- Ollama reachability (advisory, never fatal) ----------------------------
say "Checking Ollama at $OLLAMA_URL"
if curl -sf --max-time 4 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  ok "Ollama reachable"
  MODEL_LARGE=$(grep -E '^LLM_MODEL_LARGE=' .env | tail -1 | cut -d= -f2- || true)
  if [ -n "$MODEL_LARGE" ] && ! curl -sf --max-time 4 "$OLLAMA_URL/api/tags" | grep -q "\"$MODEL_LARGE\""; then
    warn "Model '$MODEL_LARGE' is not installed on that host."
    warn "  Run there:  ollama pull $MODEL_LARGE"
    warn "  Or list what is available:  curl -s $OLLAMA_URL/api/tags"
  fi
else
  warn "Cannot reach Ollama at $OLLAMA_URL"
  case "$OLLAMA_URL" in
    *localhost*|*127.0.0.1*) warn "  Start it:  ollama serve" ;;
    *) warn "  On that machine run:  OLLAMA_HOST=0.0.0.0 ollama serve"
       warn "  Then check the firewall allows port 11434 on your LAN." ;;
  esac
  warn "Gary will still start; the status page will show the problem."
fi

# --- Frontend ---------------------------------------------------------------
if [ "$MODE" = "build" ]; then
  command -v npm >/dev/null 2>&1 || die "npm not found. Install Node 18+: brew install node"
  say "Building frontend"
  (cd frontend && npm install --silent --no-audit --no-fund && npm run build >/dev/null)
  ok "frontend built into frontend/dist"
fi

cleanup() { [ -n "${VITE_PID:-}" ] && kill "$VITE_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [ "$MODE" = "dev" ]; then
  command -v npm >/dev/null 2>&1 || die "npm not found. Install Node 18+: brew install node"
  if [ ! -d frontend/node_modules ]; then
    say "Installing frontend dependencies"
    (cd frontend && npm install --silent --no-audit --no-fund)
  fi
  say "Starting Vite dev server"
  (cd frontend && VITE_BACKEND_URL="http://127.0.0.1:$APP_PORT" npm run dev -- --host 127.0.0.1) &
  VITE_PID=$!
  sleep 2
  printf "\n%s  Open  →  http://localhost:5173%s\n\n" "$GREEN" "$OFF"
else
  printf "\n%s  Open  →  http://%s:%s%s\n\n" "$GREEN" "$APP_HOST" "$APP_PORT" "$OFF"
fi

printf "%s  API docs → http://%s:%s/api/docs   (Ctrl-C to stop)%s\n\n" "$DIM" "$APP_HOST" "$APP_PORT" "$OFF"

say "Starting backend"
exec .venv/bin/python -m uvicorn backend.main:app --host "$APP_HOST" --port "$APP_PORT"
