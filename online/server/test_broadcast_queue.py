import asyncio
import time
import unittest

from broadcast_queue import BroadcastQueue


class BroadcastQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_burst_is_coalesced_and_later_change_is_delivered(self):
        sent = []
        first = asyncio.Event()
        second = asyncio.Event()

        async def send():
            sent.append(time.monotonic())
            (first if len(sent) == 1 else second).set()

        queue = BroadcastQueue(send, min_interval=.1, initial_delay=.01)
        try:
            for _ in range(10):
                queue.request()
            await asyncio.wait_for(first.wait(), 1)
            self.assertEqual(len(sent), 1)
            for _ in range(10):
                queue.request()
            await asyncio.wait_for(second.wait(), 1)
            self.assertEqual(len(sent), 2)
            self.assertGreaterEqual(sent[1] - sent[0], .095)
        finally:
            await queue.close()

    async def test_request_does_not_wait_for_socket_delivery(self):
        started = asyncio.Event()
        release = asyncio.Event()
        delivered = asyncio.Event()
        calls = 0

        async def send():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            delivered.set()

        queue = BroadcastQueue(send, min_interval=.01, initial_delay=.01)
        try:
            queue.request()
            await asyncio.wait_for(started.wait(), 1)
            queue.request()
            self.assertTrue(queue.pending)
            self.assertFalse(delivered.is_set())
            release.set()
            await asyncio.wait_for(delivered.wait(), 1)
        finally:
            await queue.close()
