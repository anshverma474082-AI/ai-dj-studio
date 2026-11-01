"""§9.4's batcher must not lose what it drained.

The hot path never writes to Postgres: every queue event, chat line and exposure row is buffered
and written by one background pass. To keep `record()` off the database, `flush()` takes the
buffers *out* under the lock and then writes outside it — and the write is the part that can fail.
A connection that drops, a transaction Postgres rejects, or a cancellation that lands on the await
(which is how #49's shutdown path is wired) all leave the drained rows held only by local
variables that go out of scope. They are gone: not delayed, gone.

`_run_flusher` catches the exception and logs "queue event flush failed; buffer will retry". The
buffer is empty by then. That message has been describing a guarantee the code does not make since
the batcher was written, and it is the leading explanation for the intermittent
`only 2 of 3 chat rows were written` failure this suite has carried since #28.

What these tests ask for is the smallest thing that makes the sentence true: on any failure, the
batch goes back into the buffer it came from, in front of whatever arrived while the write was
in flight, so the next pass writes it. Two failure shapes get tested because they reach the code
from different sides — a transaction that refuses to open, and one that runs and is rejected on
commit — plus cancellation, which is not something an `except Exception` would catch.

Mocking the session here is unavoidable rather than convenient: nothing available to a test can
make a real Postgres drop a connection mid-transaction, and a transient failure is precisely the
case §9.4's design is for. Everything else is the real registry, the real engine and the real
database, including the recovery pass — the assertion that matters is that the row is in Postgres
afterwards, not that a private list was restored.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

import app.gateway.state as state
from app.db.models import ChatMessage, QueueEvent, QueueEventType
from app.db.session import SessionLocal
from app.gateway.state import RoomRegistry

REAL_SESSION_LOCAL = state.SessionLocal


class _Unreachable:
    """A session whose transaction fails, in whichever way is being simulated.

    `at="connect"` refuses before the body runs (the database is down); `at="commit"` runs the
    body and raises on exit (it refused the write); `at="hang"` never returns, so the caller can
    cancel while the pass is sitting on the await.

    `add_all` is a no-op on purpose: the `commit` shape is only worth testing if the batch pass
    gets to build its objects and hand them over, which is where a real rejection happens. It
    costs a stub here and saves the test failing on the mock instead of on the code.
    """

    def __init__(self, *, at: str, error: BaseException) -> None:
        self._at = at
        self._error = error

    def begin(self) -> "_Unreachable":
        return self

    def add_all(self, objects: Any) -> None:
        for _ in objects:
            pass

    async def __aenter__(self) -> "_Unreachable":
        if self._at == "connect":
            raise self._error
        if self._at == "hang":
            await asyncio.Event().wait()  # cancelled by the test, never returns on its own
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._at == "commit" and exc_type is None:
            raise self._error
        return False


def broken_session_factory(*, at: str, error: BaseException):
    def factory() -> _Unreachable:
        return _Unreachable(at=at, error=error)

    return factory


async def presence_payloads(lounge_id: str) -> list[dict[str, Any]]:
    """Read the lounge's log back in the order Postgres stamped it (`seq`)."""
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(QueueEvent.payload).where(QueueEvent.lounge_id == lounge_id).order_by(
                    QueueEvent.seq
                )
            )
        ).all()
        return list(rows)


async def chat_bodies(message_id: str) -> list[str]:
    async with SessionLocal() as session:
        rows = (
            await session.scalars(select(ChatMessage.body).where(ChatMessage.id == message_id))
        ).all()
        return list(rows)


async def staged_registry(lounge_id: str, message_id: str) -> RoomRegistry:
    """Three buffered rows across two tables — the batch pass writes both in one transaction."""
    registry = RoomRegistry()
    await registry.record(lounge_id, QueueEventType.PRESENCE, {"userId": "u_one", "at": 1})
    await registry.record(lounge_id, QueueEventType.PRESENCE, {"userId": "u_two", "at": 2})
    await registry.record_chat(
        lounge_id,
        message_id=message_id,
        user_id=None,
        body="the room is loud tonight",
        sentiment_score=0.4,
        ts_seconds=12,
    )
    return registry


@pytest.mark.parametrize("at", ["connect", "commit"])
def test_a_batch_postgres_refused_is_still_written_by_the_next_pass(demo_lounge, monkeypatch, at):
    """The whole claim of this slice, in one test.

    The failure is asserted first (a pass that swallowed it would look like success and quietly
    drop the batch), then the recovery, then the count — twice over, because "written" and
    "written once" are different promises, and a fix that re-queued without clearing the drained
    batch would satisfy the first and fail the second.
    """
    lounge_id = demo_lounge
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    error = RuntimeError("postgres is not listening")

    async def scenario():
        registry = await staged_registry(lounge_id, message_id)
        monkeypatch.setattr(state, "SessionLocal", broken_session_factory(at=at, error=error))
        with pytest.raises(RuntimeError, match="not listening"):
            await registry.flush()
        # Nothing reached Postgres, and nothing is allowed to have evaporated either.
        assert await presence_payloads(lounge_id) == []
        monkeypatch.setattr(state, "SessionLocal", REAL_SESSION_LOCAL)
        assert await registry.flush() == 3
        assert [p["userId"] for p in await presence_payloads(lounge_id)] == ["u_one", "u_two"]
        assert await chat_bodies(message_id) == ["the room is loud tonight"]

    asyncio.run(scenario())


def test_a_write_cancelled_mid_flight_does_not_take_the_batch_with_it(demo_lounge, monkeypatch):
    """§9.4's rows survive a cancellation, not only an exception.

    `stop()` cancels the flusher task and then awaits its own final pass, and starlette's test
    session cancels the app task at whatever await it is sitting on — the trap that cost #49 a
    cycle. A handler written as `except Exception` passes both tests above and fails this one,
    because `CancelledError` is a `BaseException` and is not an `Exception`.
    """
    lounge_id = demo_lounge
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    async def scenario():
        registry = await staged_registry(lounge_id, message_id)
        monkeypatch.setattr(
            state,
            "SessionLocal",
            broken_session_factory(at="hang", error=RuntimeError("unused")),
        )
        pass_ = asyncio.create_task(registry.flush())
        await asyncio.sleep(0.05)  # let the pass reach the await inside the write
        pass_.cancel()
        try:
            await pass_
        except asyncio.CancelledError:
            pass
        assert await presence_payloads(lounge_id) == []
        monkeypatch.setattr(state, "SessionLocal", REAL_SESSION_LOCAL)
        assert await registry.flush() == 3
        assert await chat_bodies(message_id) == ["the room is loud tonight"]

    asyncio.run(scenario())


def test_the_background_flusher_retries_after_a_failure_instead_of_dying(
    demo_lounge, monkeypatch
):
    """The literal promise of `log.exception("queue event flush failed; buffer will retry")`.

    A fresh registry runs its real flusher task. The first batch trips while Postgres is
    unreachable, the loop swallows that and waits again, and then the same task — woken only by
    its interval, with the size trigger deliberately never set for the second event — writes both
    the failed batch and the line recorded after it. Nothing calls `flush()` by hand here, so a
    loop that exits on the exception fails on the deadline, and a loop that stays alive but lost
    the first batch fails on the count.
    """
    lounge_id = demo_lounge

    async def scenario():
        registry = RoomRegistry()
        monkeypatch.setattr(
            state,
            "SessionLocal",
            broken_session_factory(at="commit", error=RuntimeError("down for a moment")),
        )
        await registry.start()
        try:
            await registry.record(lounge_id, QueueEventType.PRESENCE, {"userId": "u_first"})
            registry._flush_now.set()
            # the failing pass drains the buffer before it writes; wait for that, so this test
            # is about the retry and not about how fast the task started.
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                async with registry._pending_lock:
                    drained = not registry._pending
                if drained:
                    break
                await asyncio.sleep(0.05)

            monkeypatch.setattr(state, "SessionLocal", REAL_SESSION_LOCAL)
            await registry.record(lounge_id, QueueEventType.PRESENCE, {"userId": "u_second"})

            for _ in range(40):
                await asyncio.sleep(0.15)
                if len(await presence_payloads(lounge_id)) == 2:
                    break
            assert [p["userId"] for p in await presence_payloads(lounge_id)] == [
                "u_first",
                "u_second",
            ]
        finally:
            await registry.stop()

    asyncio.run(scenario())


def test_a_retried_batch_stays_in_front_of_what_arrived_during_the_outage(
    demo_lounge, monkeypatch
):
    """The restored batch goes back *ahead* of rows appended while the write was in flight.

    Append rather than prepend satisfies every test above — and still interleaves a room's
    history, which is the order §6.3's analytics read back. So the row that exposes the choice
    has to arrive *during* the failing write, not after it: this pass hangs on the await, the
    buffer is empty, `record()` puts `u_late` in it, and only then does the cancellation return
    the batch. Prepend gives `u_a, u_b, u_late`; append gives `u_late, u_a, u_b`. A version of
    this test that failed the write, then recorded the third line, then retried passed under both
    — the buffer was empty at restore time, so there was nothing to get ahead of.

    `queue_events.seq` is stamped at insert, which is what makes the assertion readable at all:
    the rows come out in the order the retried buffer handed them to the session.
    """
    lounge_id = demo_lounge

    async def read_back() -> list[str]:
        return [p["userId"] for p in await presence_payloads(lounge_id)]

    async def scenario():
        registry = RoomRegistry()
        await registry.record(lounge_id, QueueEventType.PRESENCE, {"userId": "u_a", "at": 1})
        await registry.record(lounge_id, QueueEventType.PRESENCE, {"userId": "u_b", "at": 2})
        monkeypatch.setattr(
            state,
            "SessionLocal",
            broken_session_factory(at="hang", error=RuntimeError("never answers")),
        )
        pass_ = asyncio.create_task(registry.flush())
        await asyncio.sleep(0.05)  # drained, and sitting on the write
        await registry.record(lounge_id, QueueEventType.PRESENCE, {"userId": "u_late", "at": 3})
        pass_.cancel()
        try:
            await pass_
        except asyncio.CancelledError:
            pass
        monkeypatch.setattr(state, "SessionLocal", REAL_SESSION_LOCAL)
        await registry.flush()
        assert await read_back() == ["u_a", "u_b", "u_late"]

    asyncio.run(scenario())
