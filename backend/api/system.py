"""System status: run mode and component health.

Two endpoints the operator interface needs and cannot honestly synthesise for itself.

The governing rule here is the same one that keeps a health field off the sensors endpoint:
**a green light that nothing computes is worse than no light at all.** So every component
reports one of a small set of states, and `NOT_BUILT` is a first-class answer rather than
something dressed up as healthy. An interface showing "Classification: OK" for a module that
does not exist would be actively misleading during the exact demo it was built for.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import OptionalOperatorDep, ServicesDep
from backend.api.services import ApiServices
from backend.common.config import RunMode
from backend.system_health.models import Posture
from sensors.adapters.interface import SensorState

router = APIRouter()
"""Deliberately **not** gated at the router.

`GET /system/mode` and `GET /system/health` stay reachable without a session, because a
monitoring probe, a load balancer, or an operator debugging a login problem has to be able to
tell a live process from a dead one without holding a credential — and an unreachable health
check is indistinguishable from an unhealthy service.

What they may say without one is narrow. The unauthenticated answer is liveness and the
access-control posture; the component detail, which names every subsystem and its state,
requires `viewer` like everything else.
"""


class ComponentState(StrEnum):
    """What is known about one subsystem.

    Deliberately distinguishes "working", "not working" and "does not exist". Collapsing the
    last two into a single unhealthy state would make an unbuilt module look like an outage,
    and collapsing it into OK would be a lie.
    """

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    """Built, but not answering. A genuine fault."""

    NOT_BUILT = "not_built"
    """No implementation exists yet. Not a fault, and must never be shown as healthy."""

    NOT_CONNECTED = "not_connected"
    """Exists as an interface with nothing attached. The response integration's permanent
    state in this build."""


class ComponentHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    component: str
    state: ComponentState
    detail: str
    """Always populated. A state with no explanation cannot be acted on."""


class SystemHealthView(BaseModel):
    """Health of every component the operator interface displays."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    components: list[ComponentHealth]
    latest_event_seq: int
    """Head of the event log — the authoritative position of everything that has happened."""

    projection_seq: int
    """How far the track projection has consumed the log."""

    projection_lag: int = Field(ge=0)
    """Events committed but not yet projected. Non-zero briefly is normal; growing without
    bound means the projection has stalled and the operator's picture is falling behind."""

    posture: Posture | None = None
    """What the watchdog says the system as a whole can currently claim about the sky.

    Separate from the component list because it is not a component: it is the conclusion drawn
    across all of them, and the operator interface treats it differently — a banner rather than
    a row. None when no watchdog is attached, which the `system_health` component explains.

    Absent from the unauthenticated answer. Whether a site is currently blind is exactly the
    fact an attacker would most like to confirm without logging in.
    """


class ModeView(BaseModel):
    """Whether the operator is looking at simulated or real data.

    The interface must show this unmissably and at all times, so it has to be a fact the
    backend asserts rather than a constant the frontend carries. A frontend-side toggle would
    let synthetic data be presented as real by editing one line.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: RunMode
    real_adapters_connected: int = Field(ge=0)
    """How many non-simulated sensor adapters are currently feeding the pipeline.

    Reported alongside the configured mode rather than folded into it, because the two can
    disagree: an environment declaring LIVE with nothing attached is a misconfiguration the
    operator needs to see, not a claim the API should launder into a mode string."""

    simulated: bool
    """Whether everything on screen is synthetic. True whenever no real adapter is feeding
    the pipeline, regardless of what the configuration declares."""


@router.get("/system/mode", response_model=ModeView, tags=["system"])
async def get_mode(services: ServicesDep) -> ModeView:
    """The run mode, as a fact rather than a frontend constant.

    No real sensor adapter is wired into this build — the testbed radar driver of ADR 0017 is
    constructed by nothing — so `real_adapters_connected` is structurally zero and `simulated`
    is structurally true. Both are computed rather than
    hardcoded, so they will start telling the truth on their own the day an adapter registry
    is wired in.
    """
    real_adapters = services.real_adapter_count()

    return ModeView(
        mode=services.run_mode,
        real_adapters_connected=real_adapters,
        simulated=real_adapters == 0,
    )


def _sensor_health(services: ApiServices) -> ComponentHealth:
    """What the attached sensors say about themselves, summarised into one row.

    The endpoint judges; the adapters only report (FM-010). The judgement here is deliberately
    blunt and pessimistic: **any** sensor not reporting OK degrades the row, and the row names
    which one and why. A site with four sensors and one dead USB cable must not average out to
    healthy, because the operator's question is never "are most of my sensors fine".

    Simulated adapters are included. A scenario that has finished driving them is a legitimate
    reason for silence, and separating the two kinds here would mean the row said nothing at all
    in the mode most people run.
    """
    if services.sensor_health is None:
        return ComponentHealth(
            component="sensors",
            state=ComponentState.NOT_CONNECTED,
            detail="No sensor adapters are attached to this process.",
        )

    reports = services.sensor_health.sensor_health()
    if not reports:
        return ComponentHealth(
            component="sensors",
            state=ComponentState.NOT_CONNECTED,
            detail="No sensor adapters are attached to this process.",
        )

    unhealthy = [report for report in reports if report.state is not SensorState.OK]
    if not unhealthy:
        return ComponentHealth(
            component="sensors",
            state=ComponentState.OK,
            detail=f"{len(reports)} sensor(s) reporting normally.",
        )

    named = "; ".join(f"{r.sensor_id}: {r.state.upper()} — {r.reason}" for r in unhealthy)
    return ComponentHealth(
        component="sensors",
        state=ComponentState.DEGRADED,
        detail=f"{len(unhealthy)} of {len(reports)} sensor(s) not reporting normally. {named}",
    )


@router.get("/system/health", response_model=SystemHealthView, tags=["system"])
async def get_health(services: ServicesDep, operator: OptionalOperatorDep) -> SystemHealthView:
    """Component health, reporting only what is genuinely knowable.

    Components with no implementation report NOT_BUILT rather than being omitted, because an
    operator interface that silently drops a subsystem from its dashboard gives no way to
    tell "healthy" from "forgotten".

    **Without a session this answers liveness only.** The component list names every subsystem
    and says which are unbuilt, which is a map of the software's weak points and not something
    to hand to an unauthenticated caller. A probe gets enough to know the process is alive; an
    operator gets the detail.
    """
    if operator is None:
        return _liveness_only(services)

    components: list[ComponentHealth] = []

    latest_seq, event_store = _event_store_health(services)
    components.append(event_store)

    projection_seq, projection = _projection_health(services, latest_seq)
    components.append(projection)

    components.append(_database_health(services))
    components.append(
        ComponentHealth(
            component="tracking",
            state=ComponentState.OK,
            detail="Association, filtering and lifecycle are running.",
        )
    )

    # Modules whose state this endpoint can actually observe, reported from the handles the
    # API holds rather than from a constant.
    #
    # These were hardcoded NOT_BUILT and stayed that way after the modules landed, so the
    # dashboard went on asserting that "every track scores zero" while the track list beside
    # it showed live scores. A health endpoint is the last place a stale constant belongs: it
    # is the thing an operator consults precisely when they have stopped trusting the rest of
    # the display, and it was the one component reporting on itself without evidence.
    # Access control, reported in the authenticated listing as well as the liveness one.
    #
    # This row exists because the warning in `_liveness_only` is unreachable when the
    # deployment has no authentication: `LocalOperatorIdentityProvider` resolves an operator
    # for everyone, so that branch never runs in the one configuration it was written to warn
    # about. Reporting the posture here too is what makes the claim true in both modes rather
    # than only in the one where it was never needed.
    components.append(
        ComponentHealth(
            component="access_control",
            state=(
                ComponentState.OK
                if services.identity.requires_credential
                else ComponentState.NOT_CONNECTED
            ),
            detail=(
                "Operators authenticate and roles are enforced."
                if services.identity.requires_credential
                else (
                    "NO authentication is configured. Anyone who can reach this port has full "
                    "operator access and the complete event log (FM-001)."
                )
            ),
        )
    )

    components.append(_sensor_health(services))

    components.append(
        _module_health(
            "classification",
            services.classification_running,
            running="Track identity is being assessed from kinematics and sensor evidence.",
            absent="Classification is not running; tracks report no label.",
        )
    )
    components.append(
        _module_health(
            "risk",
            services.situation is not None,
            running="Tracks are scored each cycle, with reason codes.",
            absent="Threat assessment is not running; every track scores zero.",
        )
    )
    components.append(
        _module_health(
            "alerts",
            services.alerts is not None,
            running="Risk levels are being promoted to alerts against the alerting policy.",
            absent="Alert generation is not running; the alert feed is empty.",
        )
    )

    components.append(_watchdog_health(services))

    # Genuinely absent. Listed explicitly so the dashboard shows the shape of the finished
    # system and where the gaps still are.
    components.append(
        ComponentHealth(
            component="fusion",
            state=ComponentState.NOT_BUILT,
            detail="Multi-sensor reconciliation is not implemented.",
        )
    )

    components.append(
        ComponentHealth(
            component="response_integration",
            state=ComponentState.NOT_CONNECTED,
            detail=(
                "Architectural placeholder. No response system is connected and this build "
                "contains no code path capable of activating one."
            ),
        )
    )

    return SystemHealthView(
        components=components,
        latest_event_seq=latest_seq,
        projection_seq=projection_seq,
        projection_lag=max(0, latest_seq - projection_seq),
        posture=services.watchdog.report().posture if services.watchdog else None,
    )


def _watchdog_health(services: ApiServices) -> ComponentHealth:
    """The watchdog's own row: whether anything is judging sensor silence at all.

    Note what this row is *not*. It does not restate the posture — that is `posture` on the
    response, and duplicating it here would be a second representation of one fact, free to
    disagree with the first. This row answers only "is the watchdog running", which is the
    question the rest of the response cannot answer for itself.

    A stalled posture degrades this row, because a watchdog whose last pass has aged out is not
    doing its job even though the object exists.
    """
    if services.watchdog is None:
        return ComponentHealth(
            component="system_health",
            state=ComponentState.NOT_CONNECTED,
            detail=(
                "No watchdog is attached to this process. Sensor silence is not being judged "
                "against any threshold, so a dead sensor and an empty sky look identical "
                "(FM-010)."
            ),
        )

    assessment = services.watchdog.report()
    if assessment.posture is Posture.STALLED:
        return ComponentHealth(
            component="system_health",
            state=ComponentState.UNAVAILABLE,
            detail=assessment.summary,
        )

    return ComponentHealth(
        component="system_health",
        state=ComponentState.OK,
        detail=(
            f"Watching {len(assessment.sensors)} source(s) against the site's silence "
            f"thresholds. {assessment.summary}"
        ),
    )


def _module_health(
    component: str, is_running: bool, *, running: str, absent: str
) -> ComponentHealth:
    """Report a pipeline module from whether the API actually holds a handle to it.

    NOT_BUILT rather than a degraded state when absent: the module is not failing, it is not
    there. An API constructed over a bare projection — which the tests do — genuinely has no
    risk or alerting behind it, and reporting those as healthy would make the endpoint useless
    for the one question it exists to answer.
    """
    return ComponentHealth(
        component=component,
        state=ComponentState.OK if is_running else ComponentState.NOT_BUILT,
        detail=running if is_running else absent,
    )


def _event_store_health(services: ApiServices) -> tuple[int, ComponentHealth]:
    try:
        latest = services.events.latest_seq()
    except Exception as exc:  # noqa: BLE001 - any failure here is the same outage
        return 0, ComponentHealth(
            component="event_store",
            state=ComponentState.UNAVAILABLE,
            detail=f"The event log could not be read: {exc}",
        )

    return latest, ComponentHealth(
        component="event_store",
        state=ComponentState.OK,
        detail=f"Append-only log readable at sequence {latest}.",
    )


def _projection_health(services: ApiServices, latest_seq: int) -> tuple[int, ComponentHealth]:
    try:
        applied = services.projection.last_applied_seq
    except Exception as exc:  # noqa: BLE001
        return 0, ComponentHealth(
            component="track_projection",
            state=ComponentState.UNAVAILABLE,
            detail=f"The track projection could not be read: {exc}",
        )

    lag = max(0, latest_seq - applied)
    if lag == 0:
        return applied, ComponentHealth(
            component="track_projection",
            state=ComponentState.OK,
            detail="Projection is current with the event log.",
        )

    # A lag is normal in the moment between an append and the next catch-up. A large one means
    # the operator's picture is behind what the system actually knows, which is worth saying
    # plainly rather than hiding behind a green light.
    state = ComponentState.OK if lag <= _ACCEPTABLE_PROJECTION_LAG else ComponentState.DEGRADED
    return applied, ComponentHealth(
        component="track_projection",
        state=state,
        detail=f"Projection is {lag} event(s) behind the log.",
    )


def _database_health(services: ApiServices) -> ComponentHealth:
    try:
        services.sites.list_all()
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            component="database",
            state=ComponentState.UNAVAILABLE,
            detail=f"The database could not be read: {exc}",
        )

    return ComponentHealth(component="database", state=ComponentState.OK, detail="Readable.")


_ACCEPTABLE_PROJECTION_LAG = 50
"""Events the projection may trail the log by before the dashboard calls it degraded.

Sized to absorb one ordinary catch-up batch. Beyond it, the operator is looking at a picture
measurably older than what the system knows, which is exactly the condition the spec's
degraded-mode requirement exists to surface.
"""


def _liveness_only(services: ApiServices) -> SystemHealthView:
    """The unauthenticated answer: the process is up, and whether it checks credentials.

    Deliberately not an empty component list, which would read as "no subsystems" rather than
    "not telling you". One component, saying exactly why the rest is absent — the same rule that
    governs every other empty response in this API (ADR 0011).

    The access-control posture is included on purpose and is safe to disclose: it tells an
    operator diagnosing a login problem whether this deployment has authentication at all, and
    it tells anyone scanning the port a fact they could establish in one request regardless.
    Publishing it makes an unauthenticated deployment visible to whoever is auditing it rather
    than only to whoever finds it.
    """
    latest_seq, _ = _event_store_health(services)
    guarded = services.identity.requires_credential

    return SystemHealthView(
        components=[
            ComponentHealth(
                component="api",
                state=ComponentState.OK,
                detail=(
                    "Serving. Component detail requires a session."
                    if guarded
                    else "Serving. This deployment has NO authentication configured (FM-001)."
                ),
            )
        ],
        latest_event_seq=latest_seq,
        projection_seq=0,
        projection_lag=0,
    )
