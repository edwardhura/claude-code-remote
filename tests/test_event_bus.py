"""Tests for :mod:`ccr.events.bus`.

Exercises pub/sub semantics, slow-consumer drop-with-WARN, weakref cleanup
on subscriber GC, fan-out to multiple subscribers, and cancel safety of
``subscribe()``.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from ccr.events import EventBus


async def test_publish_to_no_subscribers_is_noop() -> None:
    bus = EventBus()
    # Should not raise even though no subscribers exist.
    await bus.publish("session.event", {"hello": "world"})


async def test_single_subscriber_receives_published_payloads() -> None:
    bus = EventBus()
    received: list[object] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("topic"):
            received.append(payload)
            if len(received) == 2:
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    await bus.publish("topic", "first")
    await bus.publish("topic", "second")
    await asyncio.wait_for(task, timeout=1)
    assert received == ["first", "second"]


async def test_multiple_subscribers_each_receive_every_payload() -> None:
    bus = EventBus()
    a: list[object] = []
    b: list[object] = []

    async def consumer(out: list[object]) -> None:
        async for payload in bus.subscribe("topic"):
            out.append(payload)
            if len(out) == 3:
                break

    ta = asyncio.create_task(consumer(a))
    tb = asyncio.create_task(consumer(b))
    await asyncio.sleep(0)
    for n in range(3):
        await bus.publish("topic", n)
    await asyncio.wait_for(asyncio.gather(ta, tb), timeout=1)
    assert a == [0, 1, 2]
    assert b == [0, 1, 2]


async def test_slow_consumer_drops_oldest_with_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bus = EventBus(queue_maxsize=2)
    # Subscribe but never read — fills the queue and forces drops.
    sub_iter = bus.subscribe("topic")
    # Prime the subscriber so it is registered with the bus.
    consumer_task = asyncio.create_task(_advance_once_then_park(sub_iter))
    await asyncio.sleep(0)

    await bus.publish("topic", "a")
    await bus.publish("topic", "b")
    # Third publish must drop the oldest queued payload.
    await bus.publish("topic", "c")
    await bus.publish("topic", "d")

    captured = capsys.readouterr().out
    # structlog's PrintLoggerFactory writes to stdout; the WARN message id
    # is "event_bus.slow_consumer_drop". Two drops were forced ("c" and "d").
    drop_count = captured.count("event_bus.slow_consumer_drop")
    assert drop_count >= 2

    consumer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer_task


async def _advance_once_then_park(sub_iter):  # type: ignore[no-untyped-def]
    """Iterate the subscriber once (to register it), then park forever."""
    await sub_iter.__anext__()
    forever: asyncio.Future[None] = asyncio.Future()
    await forever


async def test_subscriber_iteration_cleanup_on_break() -> None:
    bus = EventBus()
    received: list[object] = []

    async def consumer() -> None:
        async for payload in bus.subscribe("topic"):
            received.append(payload)
            break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    await bus.publish("topic", 1)
    await asyncio.wait_for(task, timeout=1)

    # Subsequent publish must not raise even though the consumer is gone.
    await bus.publish("topic", 2)
    assert received == [1]


async def test_subscriber_garbage_collected_iterator_drops_from_set() -> None:
    bus = EventBus()
    received: list[object] = []

    async def take_one(it) -> None:  # type: ignore[no-untyped-def]
        async for payload in it:
            received.append(payload)
            break

    iterator = bus.subscribe("topic")
    task = asyncio.create_task(take_one(iterator))
    await asyncio.sleep(0)
    await bus.publish("topic", "x")
    await asyncio.wait_for(task, timeout=1)
    del iterator
    gc.collect()

    # Publishing now should not raise and should not log a warning.
    await bus.publish("topic", "y")


async def test_two_topics_isolated() -> None:
    bus = EventBus()
    a: list[object] = []
    b: list[object] = []

    async def consumer(topic: str, out: list[object]) -> None:
        async for payload in bus.subscribe(topic):
            out.append(payload)
            break

    ta = asyncio.create_task(consumer("alpha", a))
    tb = asyncio.create_task(consumer("beta", b))
    await asyncio.sleep(0)
    await bus.publish("alpha", "A1")
    await bus.publish("beta", "B1")
    await asyncio.wait_for(asyncio.gather(ta, tb), timeout=1)
    assert a == ["A1"]
    assert b == ["B1"]


async def test_subscribe_is_cancel_safe() -> None:
    bus = EventBus()

    async def consumer() -> None:
        async for _payload in bus.subscribe("topic"):
            pass

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Publishing after the cancellation must not raise.
    await bus.publish("topic", "later")
