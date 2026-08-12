"""Fusion tuning, and the reasoning behind each default.

Follows the same shape as the risk, alerting and auth policies: a frozen model with
`extra="forbid"`, a `notes` field because that forbids an informal `_comment` key, and a loader
reading `config/fusion/policy.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import chi2

from backend.common.config import ConfigPaths

MEASUREMENT_DOF = 3
"""Degrees of freedom of the gate: a three-dimensional position difference."""


class FusionPolicy(BaseModel):
    """When two detections are treated as observations of one object."""

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

    gate_confidence: float = Field(default=0.99, gt=0.0, lt=1.0)
    """Probability mass of the chi-squared distribution the association gate admits.

    Matched to `AssociationConfig.gate_confidence` deliberately. Two detections are candidates
    to be the same object under exactly the criterion the tracker would use to decide they were
    the same track, so fusion cannot merge a pair the tracker would have insisted on separating,
    nor separate a pair it would have merged. Divergence between the two would show up as tracks
    that flicker between fused and split from cycle to cycle.
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

    @property
    def gate_threshold(self) -> float:
        """Squared Mahalanobis distance above which two detections cannot be the same object.

        From the chi-squared quantile rather than hand-tuned, so the number means something
        statistical instead of being a constant someone once found agreeable.
        """
        return float(chi2.ppf(self.gate_confidence, MEASUREMENT_DOF))


def policy_path(paths: ConfigPaths | None = None) -> Path:
    return (paths or ConfigPaths.default()).repo_root / "config" / "fusion" / "policy.json"


def load_fusion_policy(paths: ConfigPaths | None = None) -> FusionPolicy:
    """Load the fusion policy, falling back to the defaults when no file is present.

    A missing file is not an error: the defaults are the documented policy, and refusing to
    start over an absent optional config would make fusion harder to adopt than to skip.
    """
    path = policy_path(paths)
    if not path.exists():
        return FusionPolicy()

    return FusionPolicy.model_validate(json.loads(path.read_text()))
