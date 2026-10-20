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
