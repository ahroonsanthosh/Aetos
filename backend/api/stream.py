"""WebSocket event stream.

The frontend never polls (spec §15). It opens one connection and is pushed events as they are
committed.

**The broadcaster reads from the event log by `seq`. It is not fed by producers.** That is the
whole of ADR 0006 expressed in code, and it is worth being explicit about what it rules out:
the tracker does not hand tracks to this module, ingestion does not hand it detections, and no
producer anywhere holds a reference to a connected client. A producer appends to the log; the
log notifies this module that *something* was committed; this module then goes and reads the
log by sequence number, exactly as the replay engine and the track projection do.

The notification carries the committed event, and this module deliberately ignores it and
re-reads. Using the handed-over object would be marginally faster and would make the live
stream a second code path that merely resembles replay. The two would then drift, and a bug
reproduced in replay would no longer be the bug that happened live — which is the entire
reason replay is built at all.

The consequence a client can see: `seq` is a resume token. A client that records the highest
`seq` it processed can reconnect with `from_seq` one past it and continue with no gap and no
duplicate, because the log has not moved and the position means the same thing to both sides.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from backend.api.schemas import StreamedEvent
from backend.auth.policy import SESSION_COOKIE_NAME
from backend.events.store import EventStore
from backend.events.types import Event, EventType

router = APIRouter()

WS_UNAUTHENTICATED = 4401
"""Close code for an upgrade refused for want of a session.

In the 4000-4999 application range, and chosen to echo HTTP 401 so it reads unambiguously in a
packet capture. The client must treat it as terminal and stop reconnecting; every other close
code means retry.
"""

STREAMED_EVENT_TYPES = frozenset(
    {
        EventType.TRACK_CREATED,
        EventType.TRACK_UPDATED,
        EventType.TRACK_LOST,
        EventType.ALERT_CREATED,
    }
)
"""What is pushed, per spec §15.

A deliberately narrow set. The stream is an operator surface, not a firehose of everything the
system records — the log holds every event and the replay engine reads all of them, but a
client watching a map does not need `DETECTION_RECEIVED` at sensor rate. Widening this is a
one-line change when a consumer genuinely needs more.
"""

DEFAULT_BATCH_SIZE = 500
"""How many events are read from the log per pass.

Bounded because a client resuming from `seq` 1 of a long-running deployment would otherwise
read the entire log into memory in one go and hold the event loop while it did. Reading in
bounded passes is the same shape as the backpressure posture in ADR 0008: work is chunked so
that one demanding consumer degrades its own latency rather than everyone else's.
"""

DEFAULT_IDLE_POLL_SECONDS = 0.25


class EventBroadcaster:
    """Pushes committed events to connected clients by reading the log.

    Holds no events. The only per-client state is a cursor: the next `seq` that client has not
    been sent. That is a fact about the connection, not about the world, which is why this
    class does not violate "the API holds no authoritative state" (spec §14).
    """

    def __init__(
        self,
        store: EventStore,
        batch_size: int = DEFAULT_BATCH_SIZE,
        idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
    ) -> None:
        self._store = store
        self._batch_size = batch_size
        self._idle_poll_seconds = idle_poll_seconds
        self._waiters: set[asyncio.Event] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Begin listening for commits. Called from the application's startup."""
        self._loop = asyncio.get_running_loop()
        self._unsubscribe = self._store.subscribe(self._on_committed)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = None
        self._loop = None

    async def stream(self, websocket: WebSocket, from_seq: int) -> None:
        """Serve one client from `from_seq` onwards until it disconnects."""
        await websocket.accept()

        cursor = max(from_seq, 1)
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        disconnected = asyncio.create_task(_await_disconnect(websocket))

        try:
            while not disconnected.done():
                # Cleared before the read, not after. An event committed while this pass is
                # draining must leave the waiter set, or the client would sleep through it
                # and only see it on the next idle poll.
                waiter.clear()
                cursor = await self._drain(websocket, cursor)
                await self._wait_for_work(waiter, disconnected)
        except WebSocketDisconnect:
            pass
        finally:
            self._waiters.discard(waiter)
            disconnected.cancel()

    async def _drain(self, websocket: WebSocket, cursor: int) -> int:
        """Send everything from `cursor` onwards. Returns the new cursor."""
        while True:
            batch = list(self._store.read_from(cursor, limit=self._batch_size))
            if not batch:
                return cursor

            for event in batch:
                if event.event_type in STREAMED_EVENT_TYPES:
                    await websocket.send_json(
                        StreamedEvent.from_event(event).model_dump(mode="json")
                    )

            # Past everything read, including events filtered out. A cursor that only advanced
            # over sent events would rescan the same unsent ones on every pass, and the cost of
            # that grows with the log.
            cursor = batch[-1].seq + 1

            if len(batch) < self._batch_size:
                return cursor

    async def _wait_for_work(self, waiter: asyncio.Event, disconnected: asyncio.Task) -> None:
        """Sleep until a commit, a disconnect, or the idle poll expires.

        The timeout is not belt-and-braces. Commit notifications only reach this process, so a
        log written by another process — a scenario runner alongside the API — would never wake
        anything. Falling back to a bounded poll means the stream degrades to slightly higher
        latency in that case rather than silently delivering nothing.
        """
        woken = asyncio.create_task(waiter.wait())
        try:
            await asyncio.wait(
                {woken, disconnected},
                timeout=self._idle_poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            woken.cancel()

    def _on_committed(self, event: Event) -> None:
        """Wake every client. The committed event itself is not used — see the module note.

        Appends may come from a thread other than the one running the loop, so the wake is
        marshalled onto the loop rather than touching an `asyncio.Event` from the wrong thread.
        """
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake_all)
        except RuntimeError:
            # The loop is closing. Clients are being torn down anyway, and a subscriber that
            # raised here would be recorded as a SUBSCRIBER_FAILED event for no useful reason.
            pass

    def _wake_all(self) -> None:
        for waiter in self._waiters:
            waiter.set()


async def _await_disconnect(websocket: WebSocket) -> None:
    """Resolve when the client goes away.

    A client that only listens still needs its disconnect noticed. Without something reading
    the socket, a departed client is discovered only when a send fails, which for an idle
    stream may be never.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/stream")
async def stream_events(
    websocket: WebSocket,
    from_seq: int | None = Query(
        default=None,
        ge=1,
        description=(
            "Resume from this sequence number inclusive. Omit to receive only events committed "
            "after connecting. A client holding `as_of_seq` from GET /tracks should pass "
            "as_of_seq + 1 to hand over from the snapshot without a gap or a duplicate."
        ),
    ),
) -> None:
    """Live event stream: TRACK_CREATED, TRACK_UPDATED, TRACK_LOST, ALERT_CREATED.

    Read from the event log by sequence number, which is what makes this the same read path as
    replay rather than a parallel one (ADR 0006).

    **Requires a session**, checked before the upgrade is accepted. This endpoint carries the
    entire live picture, so leaving it open while the REST surface is gated would make the gate
    decorative — anyone who could open a socket would get everything the console shows,
    continuously.

    Authenticated by cookie because that is the only credential a browser can present on a
    WebSocket upgrade: the API cannot set headers on one, and a token in the query string ends
    up in access logs and browser history. Cookies are sent automatically on a same-origin
    upgrade, so this needs no client-side token handling at all.
    """
    services = websocket.app.state.services

    if services.identity.resolve(websocket.cookies.get(SESSION_COOKIE_NAME)) is None:
        # Refused before `accept()`, so no socket is ever established. The close code is in the
        # application range and distinct from anything emitted on a restart, because the
        # client's reconnect loop has to tell "not allowed, stop trying" from "gone away,
        # retry" — retrying an authentication failure forever is how a logged-out tab quietly
        # becomes a denial of service against its own backend.
        await websocket.close(code=WS_UNAUTHENTICATED, reason="Authentication required.")
        return

    broadcaster = websocket.app.state.broadcaster
    start = from_seq if from_seq is not None else services.events.latest_seq() + 1
    await broadcaster.stream(websocket, start)
