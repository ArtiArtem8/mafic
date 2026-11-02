"""Boundary tests for remote mutations of one (session, guild) slot."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
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

if TYPE_CHECKING:
    from mafic.__libraries import VoiceServerUpdatePayload
    from mafic.node import Node


class RemoteMutationBoundaryTests(IsolatedAsyncioTestCase):
    """Every remote effect for a guild must pass through one ordered boundary."""

    def setUp(self) -> None:
        """Install private fakes so tests never share class state."""
        self.client = FakeClient()
        self.guild = FakeGuild(self.client)
        self.node = FakeNode()
        self.enterContext(patch.object(NodePool, "nodes", [self.node]))

    def make_player(self) -> Player[Client]:
        """Build a real Player whose guild is the fake cache owner."""
        channel = Mock(spec=VoiceChannel, id=CHANNEL_ID, guild=self.guild)
        channel._get_voice_client_key.return_value = (GUILD_ID, None)
        return Player(cast("Client", self.client), channel)

    async def connected(self) -> Player[Client]:
        """Return a registered, claimed and node-bound owner."""
        player = self.make_player()
        key, _ = player.channel._get_voice_client_key()
        self.client._connection._add_voice_client(key, player)
        player._voice_state_update_event.set()
        player._voice_server_update_event.set()
        player._node_player_ready_event.set()
        await player.connect(timeout=2, reconnect=True)
        # No fake gateway delivers a player state update, so mark the player
        # connected explicitly; otherwise the early guard would mask the very
        # checks these tests exercise.
        player._connected = True
        self.node.add_player(GUILD_ID, player)
        player._node = cast("Node[Client]", self.node)
        return player

    async def test_update_after_teardown_starts_is_refused(self) -> None:
        """A PATCH must not resurrect the player its own DELETE removed."""
        player = await self.connected()
        self.node.block_destroy = True

        teardown = asyncio.create_task(player.disconnect(force=True))
        await asyncio.wait_for(self.node.destroy_started.wait(), 2)

        update = asyncio.create_task(player.update(volume=40))
        self.node.release_destroy.set()

        with self.assertRaises(PlayerNotConnected):
            await asyncio.wait_for(update, 2)
        await asyncio.wait_for(teardown, 2)

        self.assertEqual(self.node.update_calls, 0)
        self.assertEqual(self.node.events, ["delete"])

    async def test_update_before_teardown_is_ordered_first(self) -> None:
        """A completed PATCH runs before the DELETE, never after it."""
        player = await self.connected()

        await player.update(volume=40)
        await player.disconnect(force=True)

        self.assertEqual(self.node.events, ["patch", "delete"])

    async def test_superseded_player_cannot_patch(self) -> None:
        """A retired generation must not write the current session slot."""
        player = await self.connected()
        replacement = self.make_player()
        replacement._take_ownership()

        with self.assertRaises(PlayerNotConnected):
            await player.update(volume=10)

        self.assertEqual(self.node.update_calls, 0)

    async def test_superseded_player_cannot_dispatch_voice_update(self) -> None:
        """The epoch check must precede the voice PATCH, not follow it."""
        player = await self.connected()
        replacement = self.make_player()
        replacement._take_ownership()

        await player._dispatch_player_update()

        self.assertEqual(self.node.voice_update_calls, 0)

    async def test_reentrant_lane_acquisition_fails_loudly(self) -> None:
        """Re-entering the non-reentrant lane must raise, not deadlock."""
        player = self.make_player()

        async def reenter() -> None:
            async with player._coordinator.hold():
                await asyncio.sleep(0)

        async with player._coordinator.hold():
            with self.assertRaises(RuntimeError):
                await reenter()

    async def test_transfer_requires_the_lane(self) -> None:
        """A transfer started outside the lane fails instead of racing."""
        player = await self.connected()

        with self.assertRaises(RuntimeError):
            await player._transfer_to_unlocked(cast("Node[Client]", self.node))

    async def test_teardown_requires_the_lane(self) -> None:
        """Teardown started outside the lane fails instead of racing."""
        player = await self.connected()

        with self.assertRaises(RuntimeError):
            await player._teardown_voice()

    async def test_voice_server_update_waits_for_the_lane(self) -> None:
        """A callback cannot overtake a teardown that holds the lane."""
        player = await self.connected()
        self.node.block_destroy = True
        teardown = asyncio.create_task(player.disconnect(force=True))
        await asyncio.wait_for(self.node.destroy_started.wait(), 2)

        callback = asyncio.create_task(
            player.on_voice_server_update(
                cast(
                    "VoiceServerUpdatePayload",
                    {
                        "guild_id": GUILD_ID,
                        "endpoint": "voice.example.com",
                        "token": "token",
                    },
                )
            )
        )
        self.node.release_destroy.set()
        await asyncio.wait_for(teardown, 2)
        await asyncio.wait_for(callback, 2)

        # The callback is superseded by the time it gets the lane, so no PATCH.
        self.assertEqual(self.node.voice_update_calls, 0)
