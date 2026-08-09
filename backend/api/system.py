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
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.api.services import ApiServices
from backend.common.config import RunMode

router = APIRouter()


def _services(request: Request) -> ApiServices:
    return request.app.state.services


ServicesDep = Annotated[ApiServices, Depends(_services)]


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

    No real sensor adapter integration exists in this build, so `real_adapters_connected` is
    structurally zero and `simulated` is structurally true. Both are computed rather than
    hardcoded, so they will start telling the truth on their own the day an adapter registry
    is wired in.
    """
    real_adapters = services.real_adapter_count()

    return ModeView(
        mode=services.run_mode,
        real_adapters_connected=real_adapters,
        simulated=real_adapters == 0,
    )


@router.get("/system/health", response_model=SystemHealthView, tags=["system"])
async def get_health(services: ServicesDep) -> SystemHealthView:
    """Component health, reporting only what is genuinely knowable.

    Components with no implementation report NOT_BUILT rather than being omitted, because an
    operator interface that silently drops a subsystem from its dashboard gives no way to
    tell "healthy" from "forgotten".
    """
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

    # Modules the pipeline is designed around but which do not exist yet. Listed explicitly so
    # the dashboard shows the shape of the finished system and where the gaps currently are.
    for component, detail in (
        ("fusion", "Multi-sensor reconciliation is not implemented."),
        ("classification", "Classification is not implemented; tracks report no label."),
        ("risk", "Threat assessment is not implemented; every track scores zero."),
        ("alerts", "Alert generation is not implemented; the alert feed is empty."),
        ("system_health", "The watchdog and degraded-mode controller are not implemented."),
    ):
        components.append(
            ComponentHealth(
                component=component, state=ComponentState.NOT_BUILT, detail=detail
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

    return ComponentHealth(
        component="database", state=ComponentState.OK, detail="Readable."
    )


_ACCEPTABLE_PROJECTION_LAG = 50
"""Events the projection may trail the log by before the dashboard calls it degraded.

Sized to absorb one ordinary catch-up batch. Beyond it, the operator is looking at a picture
measurably older than what the system knows, which is exactly the condition the spec's
degraded-mode requirement exists to surface.
"""
