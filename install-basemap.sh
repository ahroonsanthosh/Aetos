#!/usr/bin/env bash
#
# Install the optional geographic basemap.
#
#   ./install-basemap.sh                 the default site, 50 km around it
#   ./install-basemap.sh --site NAME     a different site from config/sites/
#   ./install-basemap.sh --radius 20     smaller area, smaller download
#   ./install-basemap.sh --no-fonts      terrain without map labels
#   ./install-basemap.sh --check         report what is installed and stop
#
# Everything the manual instructions asked you to work out — where your site is, what bounding
# box covers the display's range, which build of the planet to cut it from, where the files go
# — this works out from `config/sites/`. What it cannot do is invent a network connection: the
# tiles are real geographic data and have to be downloaded once.
#
# Nothing here is required. With no basemap installed the display works exactly as it does
# otherwise and the Terrain button says so (ADR 0010, ADR 0014).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

say()  { printf '\033[36m▸\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!\033[0m %s\n' "$1"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

SITE="fictional_test_range"
RADIUS_KM=50
WANT_FONTS=1
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) SITE="$2"; shift 2 ;;
    --radius) RADIUS_KM="$2"; shift 2 ;;
    --no-fonts) WANT_FONTS=0; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done

PUBLIC_DIR="frontend/public"
TILES="$PUBLIC_DIR/basemap.pmtiles"
FONTS_DIR="$PUBLIC_DIR/basemap-fonts"

# ---------------------------------------------------------------------------------------
# What is already installed
#
# Checked by reading the file's first seven bytes rather than by its presence. A truncated or
# half-downloaded archive is a file too, and the failure it produces in the browser is far
# less obvious than "not installed".
# ---------------------------------------------------------------------------------------

tiles_installed() {
  [[ -f "$TILES" ]] && [[ "$(head -c 7 "$TILES" 2>/dev/null || true)" == "PMTiles" ]]
}

report_state() {
  if tiles_installed; then
    ok "Tiles installed: $TILES ($(du -h "$TILES" | cut -f1))"
  elif [[ -f "$TILES" ]]; then
    warn "$TILES exists but is not a PMTiles archive — probably a failed download. Delete it and re-run."
  else
    say "No tiles installed."
  fi

  if [[ -d "$FONTS_DIR" ]]; then
    ok "Label fonts installed: $FONTS_DIR"
  else
    say "No label fonts — terrain will draw unlabelled, which is fine."
  fi
}

if [[ "$CHECK_ONLY" == "1" ]]; then
  report_state
  exit 0
fi

# ---------------------------------------------------------------------------------------
# Where to cut the map
#
# Derived from the site's own reference point, so nobody has to look up coordinates or hand
# a bounding box to a tool. The radius defaults to the display's maximum range, because
# anything the operator can pan to should have ground under it.
# ---------------------------------------------------------------------------------------

SITE_FILE="config/sites/${SITE}.json"
[[ -f "$SITE_FILE" ]] || fail "No such site: $SITE_FILE. Available: $(ls config/sites/*.json | xargs -n1 basename | sed 's/.json//' | tr '\n' ' ')"

PYBIN=""
for candidate in python3 python py; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then PYBIN="$candidate"; break; fi
done
[[ -n "$PYBIN" ]] || fail "Python is needed to read the site configuration. See ./start.sh for the same requirement."

BBOX="$("$PYBIN" - "$SITE_FILE" "$RADIUS_KM" <<'PY'
import json, math, sys

site = json.load(open(sys.argv[1]))["reference"]
radius_km = float(sys.argv[2])
lat, lon = site["latitude"], site["longitude"]

# A degree of latitude is ~111.32 km everywhere; a degree of longitude shrinks with cos(lat).
# The same term the display's projection turns on, and the reason a fixed degree padding would
# give a box far too narrow at high latitude and needlessly wide near the equator.
d_lat = radius_km / 111.32
d_lon = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))

print(f"{lon - d_lon:.4f},{lat - d_lat:.4f},{lon + d_lon:.4f},{lat + d_lat:.4f}")
PY
)"

say "Site '$SITE' — cutting a ${RADIUS_KM} km box: $BBOX"

# ---------------------------------------------------------------------------------------
# The extract tool
# ---------------------------------------------------------------------------------------

PMTILES_BIN="$(command -v pmtiles || true)"
if [[ -z "$PMTILES_BIN" && -x "$REPO_ROOT/.tools/pmtiles" ]]; then
  PMTILES_BIN="$REPO_ROOT/.tools/pmtiles"
fi

if [[ -z "$PMTILES_BIN" ]]; then
  say "Fetching the pmtiles tool"
  mkdir -p .tools

  case "$(uname -s)" in
    Linux)   OS="Linux" ;;
    Darwin)  OS="Darwin" ;;
    MINGW*|MSYS*|CYGWIN*) OS="Windows" ;;
    *) fail "Unrecognised system $(uname -s). Install the pmtiles tool by hand from https://github.com/protomaps/go-pmtiles/releases and re-run." ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) ARCH="x86_64" ;;
    arm64|aarch64) ARCH="arm64" ;;
    *) fail "Unrecognised architecture $(uname -m). Install the pmtiles tool by hand and re-run." ;;
  esac

  # Asked for by name from the releases API rather than hardcoding a version, so this does not
  # rot the first time upstream cuts a release.
  # curl's own diagnostics are suppressed: this step has a good failure message of its own,
  # and a bare "curl: (22) ... 403" above it tells the reader nothing they can act on.
  ASSET="$(curl -fsSL https://api.github.com/repos/protomaps/go-pmtiles/releases/latest 2>/dev/null \
    | grep -o "https://[^\"]*${OS}_${ARCH}\.tar\.gz" | head -1 || true)"

  [[ -n "$ASSET" ]] || fail "Could not find a pmtiles release for ${OS}/${ARCH}. Download it from https://github.com/protomaps/go-pmtiles/releases, put it on your PATH, and re-run."

  curl -fsSL "$ASSET" 2>/dev/null | tar -xz -C .tools pmtiles 2>/dev/null || fail "The pmtiles download failed. Fetch it by hand from https://github.com/protomaps/go-pmtiles/releases and re-run."
  chmod +x .tools/pmtiles
  PMTILES_BIN="$REPO_ROOT/.tools/pmtiles"
  ok "pmtiles tool ready"
fi

# ---------------------------------------------------------------------------------------
# The extract
#
# Protomaps publishes a dated build of the planet each week rather than a "latest" alias, so a
# hardcoded date rots. Walking back from today finds the newest one that exists.
# ---------------------------------------------------------------------------------------

SOURCE=""
for back in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
  DATE="$("$PYBIN" -c "import datetime,sys; print((datetime.date.today()-datetime.timedelta(days=int(sys.argv[1]))).strftime('%Y%m%d'))" "$back")"
  CANDIDATE="https://build.protomaps.com/${DATE}.pmtiles"
  if curl -fsS -o /dev/null -r 0-1 --max-time 20 "$CANDIDATE" 2>/dev/null; then
    SOURCE="$CANDIDATE"
    break
  fi
done

[[ -n "$SOURCE" ]] || fail "Could not reach a Protomaps planet build. Check your connection, or follow docs/basemap.md to install tiles by hand."

say "Extracting from $SOURCE (a few minutes; only the bytes for your area are downloaded)"
mkdir -p "$PUBLIC_DIR"

# --maxzoom 14 is roughly 10 m per pixel, which is finer than the display's closest range of
# 200 m needs. Deeper zooms multiply the archive for detail a tactical display never uses.
"$PMTILES_BIN" extract "$SOURCE" "$TILES" --bbox="$BBOX" --maxzoom=14 \
  || fail "The extract failed. See docs/basemap.md to do it by hand."

tiles_installed || fail "The extract produced a file that is not a PMTiles archive. Delete $TILES and try again."
ok "Tiles installed ($(du -h "$TILES" | cut -f1))"

# ---------------------------------------------------------------------------------------
# Label fonts — optional, and a failure here is not a failure of the basemap
# ---------------------------------------------------------------------------------------

if [[ "$WANT_FONTS" == "1" && ! -d "$FONTS_DIR" ]]; then
  say "Fetching label fonts"
  TMP="$(mktemp -d)"
  if curl -fsSL -o "$TMP/assets.zip" https://github.com/protomaps/basemaps-assets/archive/refs/heads/main.zip \
     && (cd "$TMP" && unzip -q assets.zip 'basemaps-assets-main/fonts/*'); then
    mv "$TMP/basemaps-assets-main/fonts" "$FONTS_DIR"
    ok "Label fonts installed"
  else
    warn "Label fonts could not be fetched. Terrain will draw unlabelled, which is a cosmetic loss only."
  fi
  rm -rf "$TMP"
fi

echo
report_state
echo
ok "Done. Run ./start.sh and click Terrain on the map."
