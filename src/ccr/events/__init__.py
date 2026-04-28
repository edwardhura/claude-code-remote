"""In-process pub/sub primitive.

The :class:`EventBus` fans out payloads to weakref-tracked subscribers with
bounded queues and drop-oldest backpressure. Downstream consumers (the
Telegram broadcast task, the SSE endpoint, the doctor health endpoint)
import :class:`EventBus` directly via ``from ccr.events import EventBus``.
"""

from __future__ import annotations

from ccr.events.bus import EventBus, Subscriber

__all__ = ["EventBus", "Subscriber"]
