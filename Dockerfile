# Aetos — one image serving the API and the operator interface.
#
# Two stages so the Node toolchain used to build the interface does not ship in the runtime
# image: only the built bundle crosses over.

# ---- Stage 1: build the operator interface ----------------------------------------------
FROM node:22-slim AS interface

WORKDIR /build/frontend

# Dependencies first, and from the lockfile. Copying package files before the source means
# this layer is only rebuilt when the dependencies actually change, not on every code edit.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --silent

COPY frontend/ ./
RUN npm run build

# ---- Stage 2: runtime -------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Never write .pyc files or buffer stdout: the first wastes image space, the second makes
# container logs arrive in unhelpful bursts.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY backend/ ./backend/
COPY sensors/ ./sensors/
COPY scenarios/ ./scenarios/
COPY config/ ./config/

# Installed editable, deliberately. A regular install puts `backend/` in site-packages, and
# `ConfigPaths.default()` derives the repository root from its own `__file__` — so config
# loading would look for `config/` next to site-packages and fail. Editable keeps the package
# at /app, where the config, scenarios and sensor registry actually are, and matches how the
# project is developed and tested.
RUN pip install --no-cache-dir --quiet -e .

COPY --from=interface /build/frontend/dist ./frontend/dist

# The event log, the projection and the account table live here. Mount a volume on it if the
# host offers one; without one (Render's free tier, for example) every restart starts a fresh
# history, and accounts are recreated from the environment (backend/auth/bootstrap.py).
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Run as an unprivileged user. Nothing here needs root, and a container that does not need it
# should not have it.
RUN useradd --create-home --uid 10001 aetos && chown -R aetos:aetos /app
USER aetos

EXPOSE 8000

# This image is for a host that terminates TLS in front of it: Render, Fly.io, Railway, or your
# own Caddy/nginx (docs/deployment.md). That is what makes `--behind-proxy` a true statement
# here, and why it is baked in. Do not publish this container's port directly to a network
# without such a proxy: session cookies would cross it in clear.
#
# 0.0.0.0 so the proxy can reach it from outside the container. $PORT because most platforms
# choose the port and pass it in; 8000 otherwise.
#
# Accounts come from the environment, since a hosted platform usually has no shell to run
# `backend.auth.admin` in. Set AETOS_ADMIN_USERNAME and AETOS_ADMIN_PASSWORD (12+ characters)
# in the platform's secret settings; optionally AETOS_VIEWER_USERNAME and AETOS_VIEWER_PASSWORD
# for a read-only account to share. With none set the guard refuses to start, on purpose.
ENTRYPOINT ["sh", "-c", "exec python -m backend.main --host 0.0.0.0 --port \"${PORT:-8000}\" --data-dir /app/data --behind-proxy \"$@\"", "aetos"]

# Looped, so whoever opens the console hours after it started still sees the scenario rather
# than an empty sky. Override with any other arguments `backend.main` accepts.
CMD ["--scenario", "b_approaching_threat", "--loop"]
