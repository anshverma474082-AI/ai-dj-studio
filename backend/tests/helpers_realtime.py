"""Test doubles for the realtime path.

Only the transport is faked here, and only so a test can read back the frames a
given client received in the order it received them. Room state, the registry,
the batched writer and the broadcaster stay real — the §6.1 acceptance criteria
are about agreement between server and client, and a mock broadcaster cannot
disagree with anything.
"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.websockets import WebSocketState

from app.db.models import Role
from app.gateway.state import Participant, QueueSlot, RoomState, install_registry


class Recorder:
    """One client's view of the socket: the frames it received, in order."""

    def __init__(self, app) -> None:
        self.app = app
        self.frames: list[dict] = []
        self.application_state = WebSocketState.CONNECTED

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)

    def events(self, event: str) -> list[dict]:
        return [f for f in self.frames if f["event"] == event]

    @property
    def revisions(self) -> list[int]:
        return [f["payload"]["revision"] for f in self.events("lounge.queue.update")]

    @property
    def last_queue(self) -> list[dict]:
        updates = self.events("lounge.queue.update")
        assert updates, "client received no queue update at all"
        return updates[-1]["payload"]["queue"]

    @property
    def presence(self) -> list[tuple[str, int]]:
        return [
            (f["payload"]["action"], f["payload"]["participantCount"])
            for f in self.events("lounge.presence.update")
        ]


def make_room(*track_ids: str, current: str = "t_2001") -> RoomState:
    room = RoomState(lounge_id="lounge_1", host_user_id="u_host")
    room.current_track_id = current
    room.queue = [QueueSlot(tid, (i + 1) * 1000) for i, tid in enumerate(track_ids)]
    return room


def make_app_and_registry():
    """A bare `app.state` carrier — `install_registry` only needs somewhere to hang."""
    app = SimpleNamespace(state=SimpleNamespace())
    return app, install_registry(app)


def seat(room: RoomState, app, connection_id: str, user_id: str, *, host: bool = False) -> Recorder:
    socket = Recorder(app)
    room.participants[connection_id] = Participant(
        user_id=user_id,
        role=Role.HOST.value if host else Role.LISTENER.value,
        connection_id=connection_id,
        socket=socket,
    )
    return socket
