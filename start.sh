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
# Find a *working* Python — not merely one that exists on PATH.
#
# `python3` is standard on Linux and macOS; a Windows install from python.org registers
# only `python` (or `py`, via the launcher) unless the installer's "Add python.exe to PATH"
# option was used, which most people never see. `python3` is tried first regardless, since
# some Windows setups do provide it and it is the one guaranteed to be Python 3 when both
# exist.
#
# The harder problem is Windows's own trap: with no Python installed at all, `python` and
# `python3` are STILL present on PATH — as "App execution alias" stub files Windows ships by
# default — and `command -v` cannot tell them apart from the real thing, because they are
# real, executable files. Running one does not run Python; it prints a Microsoft Store
# redirect message and exits. So each candidate is actually invoked and its output checked,
# not just located.
# ---------------------------------------------------------------------------------------

PYBIN=""
PYTHON_OK=""
for candidate in python3 python py; do
  command -v "$candidate" >/dev/null 2>&1 || continue

  probe=$("$candidate" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || true)
  if [[ "$probe" == "0" || "$probe" == "1" ]]; then
    PYBIN="$candidate"
    PYTHON_OK="$probe"
    break
  fi
  # Anything else — including empty output, which is what the Store stub produces once its
  # message and non-JSON exit are swallowed — is not a real interpreter. Move on rather than
  # surfacing whatever it printed as though it were a diagnosis of the actual problem.
done

[[ -n "$PYBIN" ]] || fail "Python was not found. On Windows, 'python' existing on PATH does not \
mean Python is installed — Windows ships a placeholder that redirects to the Microsoft Store \
instead of running anything. Install the real thing from https://python.org (tick 'Add \
python.exe to PATH' during setup), close this window, reopen Git Bash, and run ./start.sh again."

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

# Rebuild when *any* interface source is newer than the build output, not just when
# package.json is.
#
# The earlier check compared against package.json alone, which meant a pull that changed
# twenty components and no dependencies rebuilt nothing: the build output already existed
# and package.json was not newer than it, so the stale interface was served against the new
# backend. Silent, and exactly the kind of mismatch that is miserable to diagnose — the API
# returns fields the page does not render, and nothing anywhere says why.
#
# `find -newer` rather than a glob, so nested sources count. `dist/` itself is not tracked
# by git, so a fresh clone has no build output and takes the first branch anyway.
interface_is_stale() {
  [[ ! -f frontend/dist/index.html ]] && return 0

  local newer
  newer=$(find frontend/src frontend/index.html frontend/package.json frontend/vite.config.ts \
    -newer frontend/dist/index.html -print -quit 2>/dev/null || true)
  [[ -n "$newer" ]]
}

if interface_is_stale; then
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
#
# Deliberately Linux/macOS only (xdg-open, open). A Windows attempt via `cmd.exe /c start`
# was tried here and reverted: spawning a native Windows console process from Git Bash can,
# on some setups, deliver a console control event to every process attached to the same
# console — including this script's own foreground server — which was observed to kill the
# server moments after it started, with no crash and no error, just a clean shutdown that
# looked identical to Ctrl-C. Auto-opening a tab is a convenience; taking the server down to
# get it is not a trade worth making, especially for a mechanism this brittle to verify
# without a real Windows machine. On Windows, the URL printed above is the fallback: it has
# to be opened by hand, which is a minor inconvenience next to the alternative.
(
  for _ in $(seq 1 40); do
    if curl -sf -o /dev/null "${URL}/api/v1/system/mode" 2>/dev/null; then
      command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 && break
      command -v open >/dev/null && open "$URL" >/dev/null 2>&1 && break
      break
    fi
    sleep 0.5
  done
) &

ARGS=(--host "$HOST" --port "$PORT")
[[ -n "$SCENARIO" ]] && ARGS+=(--scenario "$SCENARIO")

exec "$PY" -m backend.main "${ARGS[@]}"
