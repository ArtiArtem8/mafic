"""Ordering tests for MOVE against LEAVE, DELETE and replacement."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from mafic.__libraries import VoiceChannel
from mafic.errors import PlayerNotConnected

from .test_voice_ownership_poc import MOVE_CHANNEL_ID, VoiceTestCase

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mafic.__libraries import Client
    from mafic.player import Player


class MoveOrderingTests(VoiceTestCase):
    """JOIN, MOVE and LEAVE share one gateway opcode and one lane."""

    def target(self) -> Mock:
        """Build the voice channel a move is sent to."""
        return Mock(spec=VoiceChannel, id=MOVE_CHANNEL_ID, guild=self.guild)

    async def connected(self) -> Player[Client]:
        """Return a registered, claimed and node-bound owner."""
        player = await self.bound_player()
        player._connected = True
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
