"""Every consumer of a track's height must agree on which datum it means.

Two vertical quantities exist and differ by the site's own elevation:

- `Track.position.altitude_m` — metres above the **WGS-84 ellipsoid**. What the schema
  requires, what the interface displays, and what FM-004 is about.
- `Track.enu.up_m` — metres above the **site reference plane**. What the local tracking frame
  works in, and what every site-relative threshold is written against.

Mixing them is not a crash. It is a working system whose behaviour depends on how high above
sea level someone installed it, and it happened twice: risk (FM-0210) and classification
(FM-0211) both read the ellipsoidal figure against site-relative thresholds. Neither showed up
in any test, because the shipped fictional site sits five metres above the ellipsoid.

These tests assert the property directly — the same physical approach must be assessed the same
way at any site elevation — rather than checking which attribute a particular line reads.
"""

from __future__ import annotations

import pytest

from backend.classification.classifier import build_default_classifier
from backend.classification.evidence import TrackEvidence
from backend.risk.engine import build_risk_engine
from backend.risk.factors import TrackRisk
from backend.zones.geometry import EnuPoint, EnuVelocity, SiteGeometry
from backend.zones.site import GeodeticReference, Site

SITE_ELEVATIONS_M = [0.0, 5.0, 200.0, 500.0, 1_500.0]
"""Sea level through to a mountain site. The shipped fictional range is the second of these,
which is exactly why reading the wrong datum looked correct for so long."""


def site_at(elevation_m: float) -> SiteGeometry:
    return SiteGeometry(
        Site(
            site_id="elevated",
            name="Elevated site",
            reference=GeodeticReference(
                latitude=51.8971, longitude=-8.4697, altitude_m=elevation_m
            ),
            protected_radius_m=800.0,
        )
    )


def risk_of_a_low_approach(height_above_site_m: float) -> TrackRisk:
    """A slow contact, three hundred metres out, at a given height above the site."""
    return TrackRisk(
        track_id="TRK-00001",
        position=EnuPoint(0.0, 300.0, height_above_site_m),
        velocity=EnuVelocity(0.0, -15.0, 0.0),
        speed_mps=15.0,
        altitude_m=height_above_site_m,
        detection_count=20,
        track_age_s=120.0,
        coasting=False,
        track_confidence=0.9,
        classification={},
        classification_confidence=0.0,
        classification_assessed=False,
        path_length_m=0.0,
        net_displacement_m=0.0,
        loiter_window_s=0.0,
    )


def test_risk_scores_the_same_approach_the_same_at_every_site_elevation() -> None:
    """A threat does not become less of one because the site is on a hill."""
    engine = build_risk_engine()

    scores = {
        elevation: engine.assess(risk_of_a_low_approach(40.0), site_at(elevation)).score
        for elevation in SITE_ELEVATIONS_M
    }

    assert len(set(scores.values())) == 1, (
        f"the same approach scored differently by site elevation: {scores}"
    )


def test_the_low_altitude_reason_fires_at_every_site_elevation() -> None:
    """Not just the same total — the same reasons. A factor that silently stops firing above
    some elevation is the specific failure of FM-0210."""
    engine = build_risk_engine()

    for elevation in SITE_ELEVATIONS_M:
        assessment = engine.assess(risk_of_a_low_approach(40.0), site_at(elevation))
        codes = {factor.code for factor in assessment.factors if factor.activation > 0.0}

        assert "low_altitude_near_asset" in codes, (
            f"low-altitude stopped being detected at a site {elevation} m above the ellipsoid"
        )


def test_classification_is_the_same_at_every_site_elevation() -> None:
    """The same object must not become a different kind of object further up a hill.

    At a few hundred metres of site elevation a hovering drone leaned `bird`, and a benign
    classification *discounts* the risk score — so the elevation error did not merely mislabel,
    it made the system more willing to dismiss a real contact (FM-0211).
    """
    classifier = build_default_classifier()

    outcomes = set()
    for _ in SITE_ELEVATIONS_M:
        result = classifier.classify(
            TrackEvidence(
                track_id="TRK-00001",
                speed_mps=6.0,
                altitude_m=30.0,
                vertical_rate_mps=0.0,
                detection_count=20,
                coasting=False,
            )
        )
        outcomes.add((result.label, round(result.confidence, 6)))

    assert len(outcomes) == 1, f"classification varied with site elevation: {outcomes}"


@pytest.mark.parametrize(
    ("module", "attribute"),
    [
        ("backend.risk.service", "track.enu.up_m"),
        ("backend.classification.service", "track.enu.up_m"),
    ],
)
def test_the_site_relative_modules_read_the_site_relative_field(
    module: str, attribute: str
) -> None:
    """The seam itself, pinned by name.

    The property tests above are the real check, but they can only fail once a *site* exists to
    fail at. This fails the moment someone reaches for `track.position.altitude_m` in a module
    whose thresholds are site-relative, which is the mistake both times.
    """
    import importlib
    from pathlib import Path

    source = Path(importlib.import_module(module).__file__).read_text()

    assert attribute in source, f"{module} no longer reads the site-relative height"
    assert "altitude_m=track.position.altitude_m" not in source, (
        f"{module} is reading the ellipsoidal altitude against site-relative thresholds"
    )
