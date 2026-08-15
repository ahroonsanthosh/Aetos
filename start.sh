#!/usr/bin/env bash
#
# Start Aetos: one command, one URL.
#
#   ./start.sh                          run with the default scenario
#   ./start.sh h_track_crossing         run a specific scenario
#   ./start.sh --list                   show the available scenarios
#   ./start.sh --port 9000              serve somewhere else
#   ./start.sh --no-scenario            serve an idle system, no synthetic data
#   ./start.sh --live                   run against real hardware (see docs/live_mode.md)
#   ./start.sh --site my_site           use a different site configuration
#   ./start.sh --no-auth                no login, localhost only (see ADR 0016)
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
NO_AUTH=0
MODE="simulation"
SITE=""
# --live defaults to the hardware testbed site, because the shipped fictional site has no
# hardware fitted and would be refused. An explicit --site always wins.
LIVE_DEFAULT_SITE="hardware_testbed"

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
    --live) MODE="live"; SCENARIO=""; shift ;;
    --hybrid) MODE="hybrid"; shift ;;
    --site) SITE="$2"; shift 2 ;;
    --no-auth) NO_AUTH=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) SCENARIO="$1"; shift ;;
  esac
done

# Resolved here rather than just before launch, because the banner prints it. A value the
# banner reads must be settled before the banner runs, or it prints a blank where the site
# should be — which is how "Site" came to be empty on every --live run.
if [[ "$MODE" != "simulation" && -z "$SITE" ]]; then
  SITE="$LIVE_DEFAULT_SITE"
fi

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
# Live mode needs the serial library; simulation does not, and installing it for everyone
# would make a laptop demo depend on a package it never calls. The stamp records which of the
# two was installed, so switching to --live installs the extra rather than failing at the port.
STAMP=".venv/.aetos-installed"
EXTRAS="."
WANTED="base"
if [[ "$MODE" != "simulation" ]]; then
  EXTRAS=".[hardware]"
  WANTED="hardware"
fi

if [[ ! -f "$STAMP" || pyproject.toml -nt "$STAMP" || "$(cat "$STAMP" 2>/dev/null)" != "$WANTED" ]]; then
  say "Installing backend dependencies"
  if command -v uv >/dev/null; then
    uv pip install --quiet -e "$EXTRAS"
  else
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -e "$EXTRAS"
  fi
  echo "$WANTED" > "$STAMP"
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
# `frontend/public` is in the list because Vite copies it into `dist/` at build time and the
# basemap installer writes there. Leaving it out meant installing or re-cutting tiles after a
# build changed nothing the browser could see: `dist/` kept whatever copy it was built with.
# Missing tiles at least make the Terrain button say so. A *stale* extract is worse — re-cut
# the map for a new site and the display draws the old site's ground under the new one's
# tracks, confidently and with no complaint.
interface_is_stale() {
  [[ ! -f frontend/dist/index.html ]] && return 0

  local sources=(frontend/src frontend/index.html frontend/package.json frontend/vite.config.ts)
  [[ -d frontend/public ]] && sources+=(frontend/public)

  local newer
  newer=$(find "${sources[@]}" -newer frontend/dist/index.html -print -quit 2>/dev/null || true)
  [[ -n "$newer" ]]
}

if interface_is_stale; then
  say "Building the operator interface (a minute the first time)"
  (
    cd frontend
    # Reinstall whenever package.json is newer than the last install, not merely when
    # node_modules is absent. The absent-only check meant a pull that added a dependency —
    # exactly what the map's basemap layer did — left an existing checkout building against
    # packages it never installed, with a TypeScript error as the only symptom and no
    # indication that the fix was `npm install` rather than something in the new code. Mirrors
    # the same staleness fix already applied to the frontend/dist rebuild check below.
    if [[ ! -d node_modules || package.json -nt node_modules/.package-lock.json ]]; then
      npm ci --silent 2>/dev/null || npm install --silent
    fi
    npm run build --silent
  ) || fail "The interface failed to build. Run 'cd frontend && npm install && npm run build' to see why."
fi

# ---------------------------------------------------------------------------------------
# Accounts
#
# Aetos requires a login (ADR 0016). On a fresh checkout there are no accounts, which would
# leave someone staring at a sign-in screen they cannot get past with nothing saying why —
# the interface cannot tell "wrong password" from "no accounts exist", and must not, because
# that distinction is exactly the oracle the login endpoint refuses to give an attacker.
#
# So it is answered here instead, where the database can simply be counted.
# ---------------------------------------------------------------------------------------

ACCOUNTS="skipped"
if [[ "$NO_AUTH" != "1" ]]; then
ACCOUNTS=$("$PY" - <<'PY' 2>/dev/null || echo "unknown"
from backend.common.clock import SystemClock
from backend.common.config import ConfigPaths
from backend.database.sqlite import SqliteRepositories

repos = SqliteRepositories(SystemClock(), ConfigPaths.default().repo_root / "data" / "aetos.db")
print(repos.users.count())
repos.close()
PY
)
fi

if [[ "$ACCOUNTS" == "0" ]]; then
  echo
  printf '\033[33m!\033[0m No operator accounts exist yet, so nobody can sign in.\n'
  echo
  printf '  Create one:\n'
  # "$PY", not "python". The dependencies live in .venv, and the bare interpreter on PATH
  # almost certainly cannot import pydantic — on Windows the venv is .venv/Scripts/python.exe,
  # which nobody guesses. This script already resolved the right one; printing anything else
  # hands out a command that cannot work.
  printf '    \033[36m%s -m backend.auth.admin add --username you --role admin\033[0m\n' "$PY"
  echo
  printf '  Or run without authentication, for a machine only you can reach:\n'
  printf '    \033[36m./start.sh --no-auth\033[0m\n'
  echo
  exit 1
fi

URL="http://${HOST}:${PORT}"

echo
printf '\033[1m  AETOS\033[0m  Counter-UAS / Sensor Fusion\n'
echo
printf '  Mode       %s\n' "$MODE"
if [[ -n "$SCENARIO" ]]; then
  printf '  Scenario   %s\n' "$SCENARIO"
elif [[ "$MODE" == "simulation" ]]; then
  printf '  Scenario   none — the picture will stay empty\n'
else
  printf '  Site       %s\n' "$SITE"
fi
printf '  Interface  \033[36m%s\033[0m\n' "$URL"
printf '  API docs   %s/api/v1/docs\n' "$URL"
echo
# This said SIMULATED unconditionally, so a live run announced itself as synthetic before the
# backend could say otherwise — the one claim this system must never get backwards.
if [[ "$MODE" == "simulation" ]]; then
  printf '  Data is \033[33mSIMULATED\033[0m. No real sensor is connected.\n'
else
  printf '  Data is \033[33mLIVE\033[0m, from unvalidated testbed hardware. Its frame layout and\n'
  printf '  baud rate are unconfirmed, and detection of a small airframe is unmeasured\n'
  printf '  (FM-050, FM-052). Treat every track as evidence to check — docs/live_mode.md.\n'
fi
if [[ "$NO_AUTH" == "1" ]]; then
  printf '  \033[33mNo authentication\033[0m — anyone who can reach this port has full access.\n'
else
  printf '  Sign in with an account from \033[36m%s -m backend.auth.admin\033[0m.\n' "$PY"
fi
printf '  Bound to localhost. To deploy to a site, see docs/deployment.md.\n'
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

ARGS=(--host "$HOST" --port "$PORT" --mode "$MODE")
[[ "$NO_AUTH" == "1" ]] && ARGS+=(--no-auth)
[[ -n "$SITE" ]] && ARGS+=(--site "$SITE")
[[ -n "$SCENARIO" ]] && ARGS+=(--scenario "$SCENARIO")

exec "$PY" -m backend.main "${ARGS[@]}"
