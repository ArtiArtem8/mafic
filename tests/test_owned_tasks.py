"""Tests for cancellation and failure of owned cleanup tasks."""

from __future__ import annotations

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

from mafic.utils.tasks import await_owned


class OwnedTaskTests(IsolatedAsyncioTestCase):
    """A cancelled caller still observes failures from its owned task."""

    async def test_owned_task_failure_is_propagated(self) -> None:
        """Without caller cancellation, the original failure reaches the caller."""

        async def cleanup() -> None:
            msg = "synthetic cleanup failure"
            raise RuntimeError(msg)

        with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
            await await_owned(asyncio.create_task(cleanup()))

    async def test_owned_task_cancellation_is_propagated(self) -> None:
        """Cancelling the owned task itself is not mistaken for caller cancellation."""
        owned = asyncio.create_task(asyncio.Event().wait())
        owned.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await await_owned(owned)

    async def test_failure_after_cancellation_is_logged_once(self) -> None:
        """A handled cleanup failure must not also reach the loop handler."""
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        handler = Mock()
        loop.set_exception_handler(handler)
        self.addCleanup(loop.set_exception_handler, previous_handler)
        started, release = asyncio.Event(), asyncio.Event()

        async def cleanup() -> None:
            started.set()
            await release.wait()
            msg = "synthetic cleanup failure"
            raise RuntimeError(msg)

        owned = asyncio.create_task(cleanup())
        caller = asyncio.create_task(await_owned(owned))
        await asyncio.wait_for(started.wait(), 2)
        caller.cancel()
        await asyncio.sleep(0)
        release.set()

        with (
            self.assertLogs("mafic.utils.tasks", level="ERROR") as logs,
            self.assertRaises(asyncio.CancelledError),
        ):
            await caller
        await asyncio.sleep(0)

        self.assertTrue(owned.done())
        self.assertEqual(len(logs.records), 1)
        self.assertIn("synthetic cleanup failure", logs.output[0])
        handler.assert_not_called()
