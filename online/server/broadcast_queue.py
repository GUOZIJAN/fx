"""Coalesce market updates so a burst of trades causes one broadcast."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable


logger = logging.getLogger(__name__)


class BroadcastQueue:
    def __init__(self, send: Callable[[], Awaitable[None]],
                 min_interval: float = 1.0, initial_delay: float = .05):
        self.send = send
        self.min_interval = min_interval
        self.initial_delay = initial_delay
        self.pending = False
        self.task: asyncio.Task[None] | None = None
        self.last_started = 0.0

    def request(self) -> None:
        self.pending = True
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while self.pending:
                delay = max(self.initial_delay,
                            self.last_started + self.min_interval - time.monotonic())
                await asyncio.sleep(delay)
                self.pending = False
                self.last_started = time.monotonic()
                try:
                    await self.send()
                except Exception:
                    logger.exception("Market broadcast failed")
        finally:
            self.task = None

    async def close(self) -> None:
        self.pending = False
        task = self.task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
