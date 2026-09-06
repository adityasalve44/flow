"""
app/channel/debounce.py — inbound message debouncer for WhatsApp and chat channels.

Buffers rapid consecutive messages from the same sender (e.g. 2-4 WhatsApp bubbles
sent within a few seconds) and merges them into a single consolidated turn before
invoking the agent pipeline, preventing the assistant from interrupting the candidate.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.api.schemas import InboundEvent

logger = logging.getLogger(__name__)


@dataclass
class _SenderBuffer:
    events: list[InboundEvent] = field(default_factory=list)
    task: asyncio.Task[Any] | None = None
    last_received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MessageDebouncer:
    """
    Debounces inbound messages per sender phone number.

    When a message arrives:
    1. It is appended to the sender's pending buffer.
    2. Any existing debounce countdown task for this sender is cancelled.
    3. A new timer task is scheduled for `window_seconds`.
    4. When the timer expires without new messages, all buffered messages
       are merged into one InboundEvent and passed to `on_dispatch`.
    """

    def __init__(
        self,
        on_dispatch: Callable[[InboundEvent], Awaitable[Any]],
        window_seconds: float = 4.0,
    ):
        self.on_dispatch = on_dispatch
        self.window_seconds = window_seconds
        self._buffers: dict[str, _SenderBuffer] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, event: InboundEvent) -> None:
        """Enqueue an incoming message for debouncing."""
        if self.window_seconds <= 0:
            # Immediate dispatch (e.g. in test environments)
            await self.on_dispatch(event)
            return

        phone = event.phone_number
        async with self._lock:
            buf = self._buffers.get(phone)
            if buf is None:
                buf = _SenderBuffer()
                self._buffers[phone] = buf

            buf.events.append(event)
            buf.last_received_at = datetime.now(UTC)

            # Cancel previous pending timer task
            if buf.task and not buf.task.done():
                buf.task.cancel()

            # Schedule new delayed dispatch
            buf.task = asyncio.create_task(
                self._delayed_dispatch(phone, self.window_seconds)
            )

    async def _delayed_dispatch(self, phone: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # Pop buffer and dispatch merged event
        events_to_process: list[InboundEvent] = []
        async with self._lock:
            buf = self._buffers.pop(phone, None)
            if buf:
                events_to_process = buf.events

        if not events_to_process:
            return

        merged_event = self._merge_events(events_to_process)
        try:
            await self.on_dispatch(merged_event)
        except Exception as err:
            logger.error(
                "Error in debouncer dispatch for phone %s: %s",
                phone,
                err,
                exc_info=True,
            )

    @staticmethod
    def _merge_events(events: list[InboundEvent]) -> InboundEvent:
        """Merge multiple InboundEvents from the same sender into one."""
        if len(events) == 1:
            return events[0]

        first = events[0]
        last = events[-1]

        # Combine text bodies in arrival order
        combined_text = "\n".join(
            (e.message or "").strip() for e in events if (e.message or "").strip()
        )

        return InboundEvent(
            phone_number=first.phone_number,
            contact_name=first.contact_name or last.contact_name,
            channel_message_id=last.channel_message_id,
            message=combined_text,
            media=last.media,
            timestamp=first.timestamp,
        )

    async def flush_all(self) -> None:
        """Immediately flush all pending buffers (useful for shutdown / tests)."""
        async with self._lock:
            for phone, buf in list(self._buffers.items()):
                if buf.task and not buf.task.done():
                    buf.task.cancel()
                if buf.events:
                    merged = self._merge_events(buf.events)
                    try:
                        await self.on_dispatch(merged)
                    except Exception as err:
                        logger.error("Error flushing buffer for %s: %s", phone, err)
            self._buffers.clear()
