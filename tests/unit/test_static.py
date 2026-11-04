"""Serving the built interface, and what happens when there isn't one to serve.

The "no frontend built" case is the one this module exists to make harmless — every test and
every API-only use hits it, per the module's own docstring — so it is the one case that must
never be allowed to error. It did: the fallback page is CSS, and CSS's `{ }` are exactly the
characters `str.format()` treats as field markers, so substituting the API prefix into the page
raised `KeyError` on every single request it was meant to serve. Nothing in this repository's
test suite constructed an app with no frontend built and actually requested `/`, so it shipped
unnoticed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.schemas import API_PREFIX
from backend.api.static import mount_frontend


def test_requesting_root_with_no_frontend_built_does_not_error(tmp_path: Path) -> None:
    """The exact request that used to raise `KeyError: ' background'`.

    Not a contrived input — this is what every backend test and every API-only deployment
    does, because none of them run `npm run build` first.
    """
    app = FastAPI()
    found = mount_frontend(app, dist=tmp_path / "dist")

    assert found is False, "there is no index.html under tmp_path; this must report so"

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "AETOS" in response.text


def test_the_not_built_page_names_the_real_api_prefix(tmp_path: Path) -> None:
    """The substitution the page exists to make. A literal `__PREFIX__` or `{prefix}` left in
    the response means the template changed and the replacement no longer matches it."""
    app = FastAPI()
    mount_frontend(app, dist=tmp_path / "dist")

    with TestClient(app) as client:
        text = client.get("/").text

    assert f"{API_PREFIX}/docs" in text
    assert "__PREFIX__" not in text
    assert "{prefix}" not in text


def test_the_page_template_still_contains_literal_css_braces(tmp_path: Path) -> None:
    """Guards the fix itself, not just its symptom.

    A future edit that swapped the CSS for something brace-free would make this test
    meaningless but harmless; one that reintroduced `.format()` on a string containing `{ }`
    would be caught by the test above the moment the CSS has any brace in it at all — which it
    does today. This asserts the hazard is still present, so the other test is still exercising
    the bug it was written against rather than a page that no longer has one.
    """
    from backend.api.static import _NOT_BUILT_PAGE

    assert "{" in _NOT_BUILT_PAGE and "}" in _NOT_BUILT_PAGE


# --------------------------------------------------------------------------------------
# The digital twin at /twin/
# --------------------------------------------------------------------------------------


def _twin_build(root: Path) -> Path:
    (root / "editor").mkdir(parents=True)
    (root / "_next").mkdir()
    (root / "index.html").write_text(
        '<html><script src="/twin/_next/a.js"></script><script>self.x=1</script></html>'
    )
    (root / "editor" / "index.html").write_text("<html><script>self.y=2</script></html>")
    (root / "404.html").write_text("<html>not here</html>")
    (root / "_next" / "a.js").write_text("console.log(1)")
    return root


def test_the_twin_is_served_with_a_csp_naming_exactly_its_inline_scripts(tmp_path: Path) -> None:
    import base64
    import hashlib

    from backend.api.static import mount_twin

    app = FastAPI()
    assert mount_twin(app, dist=_twin_build(tmp_path / "out")) is True

    with TestClient(app) as client:
        response = client.get("/twin/")

    expected = base64.b64encode(hashlib.sha256(b"self.x=1").digest()).decode()
    csp = response.headers["content-security-policy"]
    assert response.status_code == 200
    assert f"'sha256-{expected}'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'self'" in csp
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_twin_routes_answer_head_redirect_and_404(tmp_path: Path) -> None:
    from backend.api.static import mount_twin

    app = FastAPI()
    mount_twin(app, dist=_twin_build(tmp_path / "out"))

    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/twin").status_code == 308
        assert client.head("/twin/editor/").status_code == 200
        assert client.get("/twin/_next/a.js").status_code == 200
        missing = client.get("/twin/nope/")
        assert missing.status_code == 404
        assert "not here" in missing.text
        assert client.get("/twin/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_an_unbuilt_twin_says_so(tmp_path: Path) -> None:
    from backend.api.static import mount_twin

    app = FastAPI()
    assert mount_twin(app, dist=tmp_path / "missing") is False
    with TestClient(app) as client:
        response = client.get("/twin/")
    assert response.status_code == 404
    assert "npm run build" in response.text
