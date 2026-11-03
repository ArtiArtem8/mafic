"""Policy tests for remote effects whose outcome was never observed."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from mafic.__libraries import Client, VoiceChannel
from mafic.errors import HTTPBadRequest, HTTPNotFound
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
    from mafic.node import Node


class RemoteOutcomeTests(IsolatedAsyncioTestCase):
    """An unobserved DELETE must never be reported as a confirmed removal."""

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
        self.node.add_player(GUILD_ID, player)
        player._node = cast("Node[Client]", self.node)
        player._connected = True
        player._session_id = "discord-session"
        player._server_state = {
            "guild_id": GUILD_ID,
            "endpoint": "voice.example.com",
            "token": "token",
        }
        return player

    async def test_unknown_delete_outcome_is_recorded(self) -> None:
        """A timed-out DELETE leaves the guild marked as unresolved."""
        player = await self.connected()
        self.node.destroy_error = asyncio.TimeoutError()

        await player.disconnect(force=True)

        self.assertEqual(player._coordinator.remote_unknown, 1)
        # Teardown still completes: the Discord side is ours to clean up.
        self.assertIsNone(self.guild.voice_client)
        self.assertIn("leave", self.guild.calls)

    async def test_confirmed_absent_delete_is_not_unknown(self) -> None:
        """A 404 proves the player was already gone, so the outcome is known."""
        player = await self.connected()
        self.node.destroy_error = HTTPNotFound("gone")

        await player.disconnect(force=True)

        self.assertEqual(player._coordinator.remote_unknown, 0)

    async def test_patch_timeout_is_recorded_as_unknown(self) -> None:
        """A PATCH without a response may still be applied later."""
        player = await self.connected()
        self.node.update_error = asyncio.TimeoutError()

        with self.assertRaises(asyncio.TimeoutError):
            await player.update(volume=10)

        self.assertEqual(player._coordinator.remote_unknown, 1)

    async def test_cancelled_patch_is_recorded_and_propagates(self) -> None:
        """Cancelling the await does not cancel a request already sent."""
        player = await self.connected()
        self.node.update_error = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await player.update(volume=10)

        self.assertEqual(player._coordinator.remote_unknown, 1)

    async def test_voice_patch_failure_is_recorded_as_unknown(self) -> None:
        """The voice update is a remote effect and is tracked like the others."""
        player = await self.connected()
        self.node.voice_update_error = asyncio.TimeoutError()

        with self.assertRaises(asyncio.TimeoutError):
            await player._dispatch_player_update()

        self.assertEqual(player._coordinator.remote_unknown, 1)

    async def test_rejected_patch_is_not_recorded_as_unknown(self) -> None:
        """A 400 proves the server refused the PATCH."""
        player = await self.connected()
        self.node.update_error = HTTPBadRequest("bad payload")

        with self.assertRaises(HTTPBadRequest):
            await player.update(volume=10)

        self.assertEqual(player._coordinator.remote_unknown, 0)

    async def test_unknown_outcome_is_visible_to_the_next_generation(self) -> None:
        """The unresolved state must outlive the generation that caused it."""
        player = await self.connected()
        self.node.destroy_error = asyncio.TimeoutError()
        await player.disconnect(force=True)

        replacement = self.make_player()
        key, _ = replacement.channel._get_voice_client_key()
        self.client._connection._add_voice_client(key, replacement)
        replacement._voice_state_update_event.set()
        replacement._voice_server_update_event.set()
        replacement._node_player_ready_event.set()
        await replacement.connect(timeout=2, reconnect=True)

        self.assertIs(replacement._coordinator, player._coordinator)
        self.assertEqual(replacement._coordinator.remote_unknown, 1)
