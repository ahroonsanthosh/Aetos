"""Serving the built operator interface from the backend.

One process, one port, one URL. The alternative — a separate static server proxying `/api` —
means two terminals, a proxy configuration, and a same-origin story that differs between
development and deployment. Serving the bundle from the application that owns the data removes
all three.

Two things this must get right:

1. **API routes win.** The interface uses client-side routing, so unknown paths have to fall
   back to `index.html`. A fallback that swallowed `/api/...` would turn a genuine 404 from
   the API into an HTML page, and the client would try to parse a document as JSON and report
   something misleading.
2. **A missing bundle is not an error.** Running the backend without having built the frontend
   is completely normal — it is what every test and every API-only use does. That case says so
   plainly at `/` rather than failing to start.
"""

from __future__ import annotations

import base64
import hashlib
import re
from functools import cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api.schemas import API_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
TWIN_DIST = REPO_ROOT / "twin" / "out"
TWIN_PREFIX = "/twin"

TWIN_EXTERNAL_ORIGINS = {
    # The site editor geocodes a place name, imports building outlines and draws a map
    # beneath the tracing canvas. These are the three services it calls, and nothing else.
    "connect": (
        "https://nominatim.openstreetmap.org https://overpass-api.de https://tile.openstreetmap.org"
    ),
    "img": "https://tile.openstreetmap.org",
}

_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)

_NOT_BUILT_PAGE = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>AETOS — interface not built</title>
  <style>
    body { background:#0b0e12; color:#dfe6ed; font-family:ui-monospace,monospace;
           display:grid; place-items:center; height:100vh; margin:0; }
    div { max-width:60ch; line-height:1.6; }
    h1 { letter-spacing:.14em; font-size:18px; }
    code { background:#12171d; padding:2px 6px; color:#4ba3d9; }
    a { color:#4ba3d9; }
  </style></head>
  <body><div>
    <h1>AETOS</h1>
    <p>The API is running, but the operator interface has not been built.</p>
    <p>Build it with <code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>, then
       reload — or use <code>./start.sh</code>, which does it for you.</p>
    <p>The API is available at <a href="__PREFIX__/docs">__PREFIX__/docs</a>.</p>
  </div></body>
</html>
"""
# `str.replace`, not `.format()`. The page is CSS, and CSS's `{ }` are exactly the characters
# `.format()` treats as field markers — it previously read `{ background:#0b0e12; ... }` as a
# format field and raised `KeyError` on every request this page was meant to serve, which
# means the one case this module exists to make harmless (no frontend built) crashed instead.
# A marker with no brace in it can't collide with CSS ever again, by construction.
assert "__PREFIX__" in _NOT_BUILT_PAGE  # the substitution this page exists to make


@cache
def _twin_csp(page: Path, mtime_ns: int) -> str:
    """The twin's Content-Security-Policy for one page, allowing exactly its inline scripts.

    A static Next.js export carries its page data in inline `<script>` blocks, which the
    console's `script-src 'self'` refuses. Rather than allow every inline script, each page is
    served with the SHA-256 of the inline scripts it actually contains, so an injected one is
    still refused. Cached per file version; the hashes change only when the twin is rebuilt.

    `frame-ancestors 'self'` rather than `'none'` because the console shows the twin inside
    its own shell. Only this origin may frame it.
    """
    del mtime_ns  # part of the cache key only
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body).digest()).decode() + "'"
        for body in _INLINE_SCRIPT.findall(page.read_bytes())
    )
    return (
        "default-src 'self'; "
        f"script-src 'self' {hashes}; "
        "style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: blob: {TWIN_EXTERNAL_ORIGINS['img']}; "
        "font-src 'self' data:; "
        f"connect-src 'self' {TWIN_EXTERNAL_ORIGINS['connect']}; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'self'"
    )


def mount_twin(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built digital twin at `/twin/`. Returns whether a build was found.

    Must be registered before the console's catch-all, which would otherwise answer every
    `/twin/...` path with the console's own shell.
    """
    root = (dist or TWIN_DIST).resolve()
    if not (root / "index.html").is_file():

        @app.get(TWIN_PREFIX + "/{path:path}", include_in_schema=False)
        async def _twin_not_built(path: str) -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><title>Twin not built</title>"
                "<p>The digital twin has not been built. Build it with "
                "<code>cd twin &amp;&amp; npm ci &amp;&amp; npm run build</code>.</p>",
                status_code=404,
            )

        return False

    # HEAD too: the twin's router prefetches its pages with HEAD requests.
    @app.api_route(TWIN_PREFIX, methods=["GET", "HEAD"], include_in_schema=False)
    async def _twin_root() -> RedirectResponse:
        return RedirectResponse(TWIN_PREFIX + "/", status_code=308)

    @app.api_route(TWIN_PREFIX + "/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _twin(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(status_code=404)
        if candidate.is_dir():
            candidate = candidate / "index.html"
        status_code = 200
        if not candidate.is_file():
            candidate, status_code = root / "404.html", 404
            if not candidate.is_file():
                raise HTTPException(status_code=404)

        headers: dict[str, str] = {}
        if candidate.suffix == ".html":
            headers["Content-Security-Policy"] = _twin_csp(candidate, candidate.stat().st_mtime_ns)
            headers["X-Frame-Options"] = "SAMEORIGIN"
            headers["Cache-Control"] = "no-cache"
        return FileResponse(candidate, status_code=status_code, headers=headers)

    return True


def mount_frontend(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built interface at `/`. Returns whether a bundle was found.

    Called after the API routers are registered, so their paths are already claimed and the
    catch-all below can only ever see what they did not match.
    """
    root = dist or FRONTEND_DIST
    index = root / "index.html"
    # Before the console's catch-all, so `/twin/...` is the twin's and not the console's.
    if dist is None:
        mount_twin(app)

    if not index.is_file():
        not_built_page = _NOT_BUILT_PAGE.replace("__PREFIX__", API_PREFIX)

        @app.get("/", include_in_schema=False)
        async def _not_built() -> HTMLResponse:
            return HTMLResponse(not_built_page, status_code=200)

        return False

    # Hashed asset filenames, so these are safe to cache hard. index.html is served by the
    # route below instead, which does not cache — otherwise a browser would keep loading an
    # old document pointing at assets that no longer exist.
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def _spa(request: Request, path: str) -> FileResponse:
        """Serve a real file when one exists, otherwise the app shell.

        The shell fallback is what makes client-side routes like `/health` work on a hard
        refresh: the browser asks the server for a path only the client knows about.
        """
        if request.url.path.startswith(API_PREFIX):
            # Unreachable in practice — the API routers are registered first and would have
            # matched. Kept as a guard so that a future route ordering change fails loudly
            # rather than silently returning HTML where JSON was expected.
            raise HTTPException(status_code=404, detail="No such API endpoint")

        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root.resolve()):
            return FileResponse(candidate)

        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return True
