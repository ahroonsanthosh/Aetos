#!/usr/bin/env bash
# Build Aetos for a host with no shell: the backend, the console and the digital twin.
#
# Run by Render as the build command (render.yaml), from the repository root. Anything else
# that builds from source can run it too.
#
#   ./scripts/render-build.sh
#
# The backend is installed editable on purpose: `ConfigPaths.default()` finds `config/`,
# `scenarios/` and the sensor registry next to the package's own files, so the package has to
# stay in the repository rather than move into site-packages.
set -euo pipefail

cd "$(dirname "$0")/.."

NODE_WANTED=22.11.0
NODE_MIN_MAJOR=20

echo "==> Python dependencies"
python -m pip install --quiet -e .

# The twin's Next.js needs Node 20.9 or newer. Use the host's Node when it is new enough, and
# otherwise fetch a pinned one into .tools/, so the build does not depend on whichever Node a
# host image happens to ship.
node_major() { node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
if ! command -v node >/dev/null 2>&1 || [ "$(node_major)" -lt "$NODE_MIN_MAJOR" ]; then
  echo "==> Node $NODE_WANTED (host has $(node --version 2>/dev/null || echo none))"
  mkdir -p .tools
  curl -fsSL "https://nodejs.org/dist/v${NODE_WANTED}/node-v${NODE_WANTED}-linux-x64.tar.xz" \
    | tar -xJ -C .tools
  export PATH="$PWD/.tools/node-v${NODE_WANTED}-linux-x64/bin:$PATH"
fi
echo "    node $(node --version), npm $(npm --version)"

echo "==> Operator console"
(cd frontend && npm ci --no-audit --no-fund && npm run build)

echo "==> Digital twin"
(cd twin && npm ci --no-audit --no-fund && NEXT_TELEMETRY_DISABLED=1 npm run build)

echo "==> Built. Console in frontend/dist, twin in twin/out."
