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

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.api.schemas import API_PREFIX

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

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
    <p>The API is available at <a href="{prefix}/docs">{prefix}/docs</a>.</p>
  </div></body>
</html>
"""


def mount_frontend(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built interface at `/`. Returns whether a bundle was found.

    Called after the API routers are registered, so their paths are already claimed and the
    catch-all below can only ever see what they did not match.
    """
    root = dist or FRONTEND_DIST
    index = root / "index.html"

    if not index.is_file():
        @app.get("/", include_in_schema=False)
        async def _not_built() -> HTMLResponse:
            return HTMLResponse(_NOT_BUILT_PAGE.format(prefix=API_PREFIX), status_code=200)

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
