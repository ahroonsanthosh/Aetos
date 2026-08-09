# Aetos — one image serving the API and the operator interface.
#
# Two stages so the Node toolchain used to build the interface does not ship in the runtime
# image: only the built bundle crosses over.

# ---- Stage 1: build the operator interface ----------------------------------------------
FROM node:20-slim AS interface

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

# The event log and the projection live here. Mounted as a volume in compose so a restart
# does not lose the record — the log is the source of truth, and a container that discards
# it on every run cannot demonstrate replay or recovery.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Run as an unprivileged user. Nothing here needs root, and a container that does not need it
# should not have it.
RUN useradd --create-home --uid 10001 aetos && chown -R aetos:aetos /app
USER aetos

EXPOSE 8000

# 0.0.0.0 so the port is reachable from outside the container. The published port is bound to
# localhost in compose — there is no authentication (FM-001), and this must not be reachable
# beyond the machine running it.
ENTRYPOINT ["python", "-m", "backend.main", \
            "--host", "0.0.0.0", "--port", "8000", \
            "--data-dir", "/app/data"]
CMD ["--scenario", "b_approaching_threat"]
