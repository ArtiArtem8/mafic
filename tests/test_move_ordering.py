"""Ordering tests for MOVE against LEAVE, DELETE and replacement."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
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
    MOVE_CHANNEL_ID,
    FakeClient,
    FakeGuild,
    FakeNode,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mafic.node import Node


class MoveOrderingTests(IsolatedAsyncioTestCase):
    """JOIN, MOVE and LEAVE share one gateway opcode and one lane."""

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

    def target(self) -> Mock:
        """Build the voice channel a move is sent to."""
        return Mock(spec=VoiceChannel, id=MOVE_CHANNEL_ID, guild=self.guild)

    async def connected(self) -> Player[Client]:
        """Return a registered, claimed and node-bound owner."""
        player = self.make_player()
        self.client._connection._add_voice_client(GUILD_ID, player)
        player._voice_state_update_event.set()
        player._voice_server_update_event.set()
        player._node_player_ready_event.set()
        await player.connect(timeout=2, reconnect=True)
        player._connected = True
        self.node.add_player(GUILD_ID, player)
        player._node = cast("Node[Client]", self.node)
        return player

    async def test_leave_and_delete_wait_for_an_in_flight_move(self) -> None:
        """A MOVE that owns the lane finishes before teardown touches the guild."""
        player = await self.connected()
        self.guild.block_move = True

        move = asyncio.create_task(player.move_to(self.target(), timeout=5))
        await asyncio.wait_for(self.guild.move_started.wait(), 2)

        leave = asyncio.create_task(player.disconnect(force=True))
        for _ in range(5):
            await asyncio.sleep(0)

        self.assertEqual(self.guild.gateway, ["join", "move"])
        self.assertEqual(self.node.destroy_calls, 0)

        self.guild.release_move.set()
        await asyncio.wait_for(leave, 2)

        self.assertFalse(self.guild.leave_overlapped_move)
        self.assertEqual(self.guild.gateway, ["join", "move", "leave"])
        self.assertEqual(self.node.destroy_calls, 1)

        move.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await move

    async def test_superseded_player_cannot_send_a_move(self) -> None:
        """The epoch is checked under the lane, immediately before the send."""
        player = await self.connected()
        replacement = self.make_player()
        # Claimed but not registered in the cache, so only the epoch can stop it.
        replacement._take_ownership()

        with self.assertRaises(PlayerNotConnected):
            await player.move_to(self.target(), timeout=1)

        self.assertNotIn("move", self.guild.gateway)

    async def test_disconnected_player_cannot_send_a_move(self) -> None:
        """A teardown that started first keeps the move off the gateway."""
        player = await self.connected()
        self.guild.block_leave = True

        leave = asyncio.create_task(player.disconnect(force=True))
        await asyncio.wait_for(self.guild.leave_started.wait(), 2)

        with self.assertRaises(PlayerNotConnected):
            await player.move_to(self.target(), timeout=1)

        self.assertNotIn("move", self.guild.gateway)

        self.guild.release_leave.set()
        await asyncio.wait_for(leave, 2)

    async def test_without_the_lane_a_move_overlaps_the_leave(self) -> None:
        """Negative control: the lane, not timing, keeps the two apart."""
        player = await self.connected()
        self.guild.block_move = True

        @asynccontextmanager
        async def no_lane(_self: object) -> AsyncGenerator[None, None]:
            yield

        def allow(_self: object) -> None:
            """Accept every lane assertion; this control removes them."""

        with (
            patch(
                "mafic._voice_ownership.GuildVoiceCoordinator.hold",
                no_lane,
            ),
            patch(
                "mafic._voice_ownership.GuildVoiceCoordinator.require_lane",
                allow,
            ),
        ):
            move = asyncio.create_task(player.move_to(self.target(), timeout=5))
            await asyncio.wait_for(self.guild.move_started.wait(), 2)

            leave = asyncio.create_task(player.disconnect(force=True))
            await asyncio.wait_for(leave, 2)

            self.assertTrue(self.guild.leave_overlapped_move)

            self.guild.release_move.set()
            move.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await move
