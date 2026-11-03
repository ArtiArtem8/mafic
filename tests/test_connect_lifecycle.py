"""Exit-path tests for Player.connect after the ownership claim."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from mafic.__libraries import Client, VoiceChannel
from mafic.errors import PlayerNotConnected
from mafic.player import Player
from mafic.pool import NodePool

from .test_voice_ownership_poc import (
    CHANNEL_ID,
    GUILD_ID,
    FakeClient,
    FakeGuild,
    FakeNode,
)


class ConnectLifecycleTests(IsolatedAsyncioTestCase):
    """Every failure after the claim must release the guild and its tasks."""

    def setUp(self) -> None:
        """Install private fakes so tests never share class state."""
        self.client = FakeClient()
        self.guild = FakeGuild(self.client)
        self.node = FakeNode()
        self.enterContext(patch.object(NodePool, "nodes", [self.node]))

    def make_player(self) -> Player[Client]:
        """Build a real Player bound to the fake guild and client."""
        channel = Mock(spec=VoiceChannel, id=CHANNEL_ID, guild=self.guild)
        channel._get_voice_client_key.return_value = (GUILD_ID, None)
        return Player(cast("Client", self.client), channel)

    def register(self, player: Player[Client]) -> None:
        """Occupy the Discord cache slot as Connectable.connect would."""
        self.client._connection._add_voice_client(GUILD_ID, player)

    def assert_no_pending_tasks(self) -> None:
        """Fail if a connect left waiter or teardown tasks behind."""
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        self.assertEqual(pending, [])

    async def test_gateway_error_releases_the_slot_and_the_claim(self) -> None:
        """A failed join send must not leave the guild owned by a dead connect."""
        player = self.make_player()
        self.register(player)
        self.guild.join_error = RuntimeError("gateway down")

        with self.assertRaises(RuntimeError):
            await player.connect(timeout=1, reconnect=True)

        self.assertIsNone(self.guild.voice_client)
        self.assertIsNone(player._coordinator.owner)
        self.assertIn("leave", self.guild.calls)
        self.assert_no_pending_tasks()

    async def test_cancelled_connect_releases_the_slot_and_the_claim(self) -> None:
        """Cancellation after the claim still finishes the compensation."""
        player = self.make_player()
        self.register(player)
        self.guild.block_join = True

        connect = asyncio.create_task(player.connect(timeout=5, reconnect=True))
        await asyncio.wait_for(self.guild.join_started.wait(), 2)
        connect.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await connect

        self.assertIsNone(self.guild.voice_client)
        self.assertIsNone(player._coordinator.owner)
        self.assert_no_pending_tasks()

    async def test_timeout_cancels_every_waiter_task(self) -> None:
        """A readiness timeout must not orphan the event waiters."""
        player = self.make_player()
        self.register(player)

        with self.assertRaises(asyncio.TimeoutError):
            await player.connect(timeout=0.05, reconnect=True)

        self.assertIsNone(self.guild.voice_client)
        self.assertIsNone(player._coordinator.owner)
        self.assert_no_pending_tasks()

    async def test_failed_connect_never_touches_a_newer_owner(self) -> None:
        """A superseded connect must not leave voice for its replacement."""
        player = self.make_player()
        self.register(player)
        self.guild.block_join = True

        connect = asyncio.create_task(player.connect(timeout=5, reconnect=True))
        await asyncio.wait_for(self.guild.join_started.wait(), 2)

        replacement = self.make_player()
        self.register(replacement)
        replacement._take_ownership()

        self.guild.join_error = RuntimeError("gateway down")
        self.guild.release_join.set()

        with self.assertRaises(RuntimeError):
            await connect

        self.assertIs(self.guild.voice_client, replacement)
        self.assertIs(replacement._coordinator.owner, replacement)
        self.assertNotIn("leave", self.guild.calls)
        self.assert_no_pending_tasks()

    async def test_library_timeout_cleanup_does_not_repeat_the_teardown(
        self,
    ) -> None:
        """The library disconnect after our timeout shares the owned task."""
        player = self.make_player()
        self.register(player)

        with self.assertRaises(asyncio.TimeoutError):
            await player.connect(timeout=0.05, reconnect=True)

        task = player._disconnect_task
        self.assertIsNotNone(task)
        # Connectable.connect calls disconnect(force=True) on a timeout.
        await player.disconnect(force=True)
        self.assertIs(player._disconnect_task, task)
        self.assertEqual(self.guild.calls.count("leave"), 1)

    async def test_success_after_a_new_owner_is_rejected(self) -> None:
        """Readiness that completes too late must not report a live connect."""
        player = self.make_player()
        self.register(player)
        connect = asyncio.create_task(player.connect(timeout=5, reconnect=True))
        await asyncio.wait_for(self.guild.join_started.wait(), 2)

        replacement = self.make_player()
        self.register(replacement)
        replacement._take_ownership()

        player._voice_state_update_event.set()
        player._voice_server_update_event.set()
        player._node_player_ready_event.set()

        with self.assertRaises(PlayerNotConnected):
            await asyncio.wait_for(connect, 2)

        self.assertIs(self.guild.voice_client, replacement)
        self.assertIs(replacement._coordinator.owner, replacement)
        self.assertNotIn("leave", self.guild.gateway)
        self.assert_no_pending_tasks()

    async def test_cancellation_before_the_join_starts_no_teardown(self) -> None:
        """Nothing remote happened, so cleanup must not queue on the lane."""
        player = self.make_player()
        self.register(player)
        release = await self.hold_lane(player)

        connect = asyncio.create_task(player.connect(timeout=5, reconnect=True))
        for _ in range(5):
            await asyncio.sleep(0)
        connect.cancel()

        # The lane is still held: the cancelled caller must not wait for it.
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(connect, 2)

        self.assertFalse(player.disconnect_started)
        self.assertNotIn("leave", self.guild.gateway)
        self.assertIsNone(self.guild.voice_client)
        release.set()

    async def test_cancellation_after_the_join_finishes_the_teardown(self) -> None:
        """A join that may have reached Discord is not silently cancelled."""
        player = self.make_player()
        self.register(player)
        self.guild.block_join = True

        connect = asyncio.create_task(player.connect(timeout=5, reconnect=True))
        await asyncio.wait_for(self.guild.join_started.wait(), 2)
        connect.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(connect, 2)

        # The teardown is owned by the player, not left as a raw background task.
        self.assertTrue(player.disconnect_started)
        await asyncio.wait_for(player.wait_for_disconnect(), 2)
        self.assertIn("leave", self.guild.gateway)
        self.assertIsNone(self.guild.voice_client)
        self.assertIsNone(player._coordinator.owner)
        self.assert_no_pending_tasks()

    async def test_cancellation_after_readiness_does_not_leave_for_a_new_owner(
        self,
    ) -> None:
        """A superseded generation must never send a stale leave."""
        player = self.make_player()
        self.register(player)
        connect = asyncio.create_task(player.connect(timeout=5, reconnect=True))
        await asyncio.wait_for(self.guild.join_started.wait(), 2)

        replacement = self.make_player()
        self.register(replacement)
        replacement._take_ownership()

        connect.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(connect, 2)

        self.assertFalse(player.disconnect_started)
        self.assertNotIn("leave", self.guild.gateway)
        self.assertIs(self.guild.voice_client, replacement)

    async def hold_lane(self, player: Player[Client]) -> asyncio.Event:
        """Occupy the guild lane and return the event that releases it."""
        held, release = asyncio.Event(), asyncio.Event()

        async def holder() -> None:
            async with player._coordinator.hold():
                held.set()
                await release.wait()

        task = asyncio.create_task(holder())
        self.addCleanup(task.cancel)
        await asyncio.wait_for(held.wait(), 2)
        return release
