#!/usr/bin/env bash
#
# Start Aetos: one command, one URL.
#
#   ./start.sh                          run with the default scenario
#   ./start.sh h_track_crossing         run a specific scenario
#   ./start.sh --list                   show the available scenarios
#   ./start.sh --port 9000              serve somewhere else
#   ./start.sh --no-scenario            serve an idle system, no synthetic data
#
# Sets up the Python environment, builds the operator interface if it is not already built,
# and serves both from a single process. Safe to run repeatedly: everything it does is
# skipped when it has already been done.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

say() { printf '\033[36m▸\033[0m %s\n' "$1"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------------------
# Find a Python. `python3` is standard on Linux and macOS; a Windows install from
# python.org registers only `python` (or `py`, via the launcher) unless the installer's
# "Add python3.exe to PATH" option was used, which most people never see. Trying python3
# first still matters — some Windows setups (WSL, some package managers) do provide it, and
# where both exist `python3` is the one guaranteed to be Python 3.
# ---------------------------------------------------------------------------------------

PYBIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYBIN="$candidate"
    break
  fi
done
if [[ -z "$PYBIN" ]] && command -v py >/dev/null 2>&1; then
  PYBIN="py"   # the Windows Python launcher, present even when 'python' is not on PATH
fi
[[ -n "$PYBIN" ]] || fail "Python is required (3.11 or newer). Install it from python.org, then try again."

PYTHON_OK=$("$PYBIN" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
[[ "$PYTHON_OK" == "1" ]] || fail "Python 3.11 or newer is required; found $("$PYBIN" --version)."

SCENARIO="b_approaching_threat"
PORT=8000
HOST="127.0.0.1"

# ---------------------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)
      echo "Available scenarios:"
      for f in scenarios/*/*.json; do
        printf '  %-28s %s\n' "$(basename "$f" .json)" \
          "$("$PYBIN" -c "import json,sys;print(json.load(open('$f')).get('description','')[:60])" 2>/dev/null || true)"
      done
      exit 0
      ;;
    --no-scenario) SCENARIO=""; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) SCENARIO="$1"; shift ;;
  esac
done

# ---------------------------------------------------------------------------------------
# Prerequisites — checked up front, so a missing tool is reported before anything is built
# ---------------------------------------------------------------------------------------

command -v node >/dev/null || fail "Node.js is required (20 or newer) to build the interface."

# ---------------------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------------------

if [[ ! -d .venv ]]; then
  say "Creating the Python environment"
  if command -v uv >/dev/null; then
    uv venv --quiet
  else
    "$PYBIN" -m venv .venv
  fi
fi

PY=".venv/bin/python"
[[ -x "$PY" ]] || PY=".venv/Scripts/python.exe"   # Git Bash on Windows
[[ -x "$PY" ]] || fail "Could not find the Python interpreter inside .venv."

# Reinstall only when the dependency declaration is newer than the last install, so a repeat
# run starts immediately instead of resolving packages every time.
STAMP=".venv/.aetos-installed"
if [[ ! -f "$STAMP" || pyproject.toml -nt "$STAMP" ]]; then
  say "Installing backend dependencies"
  if command -v uv >/dev/null; then
    uv pip install --quiet -e .
  else
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -e .
  fi
  touch "$STAMP"
fi

# ---------------------------------------------------------------------------------------
# Operator interface
# ---------------------------------------------------------------------------------------

if [[ ! -f frontend/dist/index.html || frontend/package.json -nt frontend/dist/index.html ]]; then
  say "Building the operator interface (a minute the first time)"
  (
    cd frontend
    if [[ ! -d node_modules ]]; then
      npm ci --silent 2>/dev/null || npm install --silent
    fi
    npm run build --silent
  ) || fail "The interface failed to build. Run 'cd frontend && npm run build' to see why."
fi

# ---------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------

URL="http://${HOST}:${PORT}"

echo
printf '\033[1m  AETOS\033[0m  Counter-UAS / Sensor Fusion\n'
echo
if [[ -n "$SCENARIO" ]]; then
  printf '  Scenario   %s\n' "$SCENARIO"
else
  printf '  Scenario   none — the picture will stay empty\n'
fi
printf '  Interface  \033[36m%s\033[0m\n' "$URL"
printf '  API docs   %s/api/v1/docs\n' "$URL"
echo
printf '  Data is \033[33mSIMULATED\033[0m. No real sensor is connected.\n'
printf '  There is no authentication — do not expose this beyond localhost.\n'
echo
printf '  Ctrl-C to stop.\n'
echo

# Open a browser once the server is actually answering, rather than immediately — otherwise
# the tab loads before the port is listening and shows a connection error.
(
  for _ in $(seq 1 40); do
    if curl -sf -o /dev/null "${URL}/api/v1/system/mode" 2>/dev/null; then
      command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 && break
      command -v open >/dev/null && open "$URL" >/dev/null 2>&1 && break
      # Git Bash on Windows has neither of the above, but it does have access to Windows's
      # own launcher via cmd.exe. The empty "" is a required placeholder argument for
      # `start` — without it, `start` treats the URL as a window title instead of a target.
      command -v cmd.exe >/dev/null && cmd.exe /c start "" "$URL" >/dev/null 2>&1 && break
      break
    fi
    sleep 0.5
  done
) &

ARGS=(--host "$HOST" --port "$PORT")
[[ -n "$SCENARIO" ]] && ARGS+=(--scenario "$SCENARIO")

exec "$PY" -m backend.main "${ARGS[@]}"
