"""In-process pub/sub event bus.

A :class:`Subscriber` owns a single bounded :class:`asyncio.Queue` keyed on a
topic. The :class:`EventBus` keeps weakref-tracked subscriber sets per topic
so an iterator GC'd without an explicit ``aclose()`` still drops out of the
fan-out loop. ``publish()`` snapshots the live WeakSet into a list before
iterating so concurrent garbage collection during a publish does not raise
``RuntimeError``.

Slow-consumer policy: when a subscriber's queue is full, the oldest item is
discarded (one ``get_nowait`` then one ``put_nowait``) and a structured
``WARNING`` is emitted under ``event_bus.slow_consumer_drop`` with the topic
and current queue size as fields. The trade-off is documented in the
architect plan: the consumer briefly behind sees gaps; the underlying JSONL
log is the durable copy.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = structlog.get_logger(__name__)

_DEFAULT_QUEUE_MAXSIZE = 256


class Subscriber:
    """One bounded queue keyed on a topic.

    Held by weakref in :class:`EventBus._subs`. The async generator returned
    by :meth:`EventBus.subscribe` keeps a strong reference for the duration
    of iteration; when the consumer breaks / cancels / GC's the generator
    the strong reference drops and the WeakSet entry vanishes on the next
    publish.
    """

    __slots__ = ("__weakref__", "queue", "topic")

    def __init__(self, topic: str, maxsize: int) -> None:
        self.topic = topic
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)


class EventBus:
    """Fan-out pub/sub bus with bounded subscriber queues.

    Multiple concurrent subscribers per topic are supported. There is no
    bounded subscriber count.
    """

    def __init__(self, *, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        self._subs: dict[str, weakref.WeakSet[Subscriber]] = {}
        self._queue_maxsize = queue_maxsize

    async def publish(self, topic: str, payload: Any) -> None:
        """Deliver ``payload`` to every subscriber on ``topic``.

        Iterates a snapshot of the WeakSet so concurrent subscriber GC does
        not raise ``RuntimeError``. On a full queue the oldest entry is
        evicted and a structured WARN is logged.
        """
        for sub in list(self._subs.get(topic, ())):
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover — race guard
                    sub.queue.get_nowait()
                sub.queue.put_nowait(payload)
                log.warning(
                    "event_bus.slow_consumer_drop",
                    topic=topic,
                    qsize=sub.queue.qsize(),
                )

    async def subscribe(self, topic: str) -> AsyncIterator[Any]:
        """Async generator yielding payloads published to ``topic``.

        Use as ``async for payload in bus.subscribe(topic): ...``. The
        generator holds a strong reference to its :class:`Subscriber` for
        the duration of iteration; on cancellation, ``break``, or
        ``aclose()`` the ``finally`` discards the entry from the set
        immediately so the next ``publish`` skips it.
        """
        sub = Subscriber(topic=topic, maxsize=self._queue_maxsize)
        self._subs.setdefault(topic, weakref.WeakSet()).add(sub)
        try:
            while True:
                yield await sub.queue.get()
        finally:
            self._subs.get(topic, weakref.WeakSet()).discard(sub)


__all__ = ["EventBus", "Subscriber"]
