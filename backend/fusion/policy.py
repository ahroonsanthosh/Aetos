"""Fusion tuning, and the reasoning behind each default.

Follows the same shape as the risk, alerting and auth policies: a frozen model with
`extra="forbid"`, a `notes` field because that forbids an informal `_comment` key, and a loader
reading `config/fusion/policy.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.common.config import ConfigError, ConfigPaths

DEFAULT_POLICY_FILE = "policy.json"


class FusionPolicy(BaseModel):
    """When two detections are treated as observations of one object.

    The statistical gate itself is deliberately **not** here. Fusion must decide "same object"
    on exactly the criterion the tracker uses to decide "same track", or tracks flicker between
    fused and split from cycle to cycle. Two copies of one threshold, each configured on its own,
    is the arrangement in which they drift apart — so the reconciler is handed the tracker's
    gate (`TrackManager.association_gate_threshold`) rather than holding a value of its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    notes: str = ""
    """Free text for whoever edits the file. Declared because `extra="forbid"` would otherwise
    reject the informal comment key people reach for first."""

    enabled: bool = True
    """Whether reconciliation runs at all.

    Present so a site can turn fusion off without redeploying — and so the effect of fusion on a
    live picture can be demonstrated by toggling it, which is the only honest way to show an
    operator what it is doing to their display.
    """

    max_fusable_uncertainty_m: float = Field(default=400.0, gt=0.0)
    """Positional uncertainty beyond which a detection is not fused with anything.

    This is the bound that does the real work, and it constrains the *measurement* rather than
    the distance. The statistical gate scales with reported uncertainty, so a sensor claiming an
    enormous covariance produces a gate wide enough to swallow genuinely separate contacts —
    fusion at its most aggressive exactly where the measurements are least trustworthy.

    Capping the uncertainty rather than the separation is what keeps the gate statistically
    honest. A flat distance ceiling has to be set below the legitimate spread of good sensors to
    catch bad ones, at which point it starts refusing correct fusions: the site's own radars
    report sigma around 45-75 m, so two true observations of one object are routinely 200-300 m
    apart, and a ceiling tight enough to stop a pathological covariance would split them.

    A detection too vague to place within 400 m is not evidence about which object it is. It is
    left as its own track, which over-counts — the visible error, and the correct direction.
    """

    max_pair_separation_m: float = Field(default=750.0, gt=0.0)
    """Absolute backstop on the separation of two fusable detections.

    Generous, and deliberately not the primary mechanism: at C-UAS ranges two detections three
    quarters of a kilometre apart are not one object under any covariance worth believing. It
    exists to bound the arithmetic, not to make the decision.
    """

    require_distinct_sensors: bool = True
    """Whether two detections from the same sensor may ever be fused.

    They may not, and this exists to be read rather than changed. A sensor reports each object
    once per scan; two returns from one sensor in one cycle are two objects, or they are a
    duplicate — and duplicates are already suppressed at ingestion by `detection_id` (FM-013).
    Fusing them would mean concluding that one sensor saw one object twice, which is the one
    thing the input structurally cannot mean.
    """


def policy_path(paths: ConfigPaths | None = None) -> Path:
    """Where the policy is read from. Exposed for diagnostics and tests."""
    return (paths or ConfigPaths.default()).config_root / "fusion" / DEFAULT_POLICY_FILE


def load_fusion_policy(paths: ConfigPaths | None = None) -> FusionPolicy:
    """Read the fusion policy from `config/fusion/`.

    A missing file is an error rather than a silent fall back to defaults, on the same terms as
    the alerting and system-health loaders: defaults that apply invisibly are discovered during
    an incident. Every failure is a `ConfigError` naming the file, which startup reports as a
    clean refusal rather than a traceback.
    """
    file = policy_path(paths)

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{file}: fusion policy not found") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: could not be read: {exc}") from exc

    try:
        return FusionPolicy.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid fusion policy: {exc}") from exc
