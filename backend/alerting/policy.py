"""When a risk level becomes an alert, and when it stops being one.

Configuration rather than code, for the same reason the risk weights are: what warrants
interrupting a human is a deployment's judgement, and one made by people who will never read
this file. A site guarding a stadium and one guarding a test range disagree about it, and
neither should have to fork the software to say so.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.common.config import ConfigError, ConfigPaths
from backend.tracking.models import RiskLevel

DEFAULT_POLICY_FILE = "policy.json"


class AlertPolicy(BaseModel):
    """Thresholds and dwell governing alert promotion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notes: str = ""
    """Free text for whoever edits this file. Declared rather than tolerated, because the
    config models forbid extra keys and a `_comment` would otherwise fail validation."""

    raise_at: RiskLevel = RiskLevel.HIGH
    """Lowest risk level that raises an alert.

    HIGH by default, not MEDIUM. An alert is an interrupt, and the cost of raising it too
    readily is not merely noise — it is that operators learn to dismiss the feed, which
    disables the mechanism precisely when it finally matters. MEDIUM already surfaces on the
    track list and the map; it does not need to also demand attention.
    """

    clear_at_or_below: RiskLevel = RiskLevel.MEDIUM
    """Level at or below which an open alert begins clearing.

    Must sit strictly below `raise_at`, leaving a band where an alert neither raises nor
    clears. That band is the hysteresis: without it a track sitting exactly on the boundary
    would raise and clear on alternating cycles, which is the failure the whole mechanism
    exists to prevent (ADR 0007).
    """

    clear_dwell_s: float = Field(default=20.0, ge=0.0)
    """How long the level must stay down before the alert clears.

    Asymmetric with raising, deliberately: alerts rise immediately and fall slowly. A threat
    briefly occluded behind a building, or one whose score dips for a cycle as the filter
    re-converges, must not clear the alert and then raise a *second* one seconds later — the
    operator would read two separate incursions where there was one.
    """

    clear_on_track_lost: bool = True
    """Whether losing the track clears its alert.

    True, but the alert records `track_lost` as the reason rather than `risk_subsided`, and
    the two must never be presented as the same outcome. An aircraft that flew out of
    coverage has not been dealt with.
    """

    def model_post_init(self, _context: object) -> None:
        if self.raise_at is RiskLevel.NONE:
            raise ValueError(
                "raise_at is NONE, which would raise an alert for every track ever assessed"
            )
        if self.clear_at_or_below.severity >= self.raise_at.severity:
            raise ValueError(
                f"clear_at_or_below ({self.clear_at_or_below}) is not below raise_at "
                f"({self.raise_at}); a level that both raises and clears an alert would make "
                "it flap on every cycle"
            )


def load_alert_policy(
    paths: ConfigPaths | None = None, filename: str = DEFAULT_POLICY_FILE
) -> AlertPolicy:
    """Read the alerting policy from `config/alerting/`.

    A missing file is an error rather than a silent fall back to defaults. Defaults that
    apply invisibly are the kind of thing a deployment discovers during an incident.
    """
    resolved = paths or ConfigPaths.default()
    file = resolved.config_root / "alerting" / filename

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{file}: alerting policy not found") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: could not be read: {exc}") from exc

    try:
        return AlertPolicy.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid alerting policy: {exc}") from exc


def policy_path(paths: ConfigPaths | None = None) -> Path:
    """Where the policy is read from. Exposed for diagnostics and tests."""
    resolved = paths or ConfigPaths.default()
    return resolved.config_root / "alerting" / DEFAULT_POLICY_FILE
