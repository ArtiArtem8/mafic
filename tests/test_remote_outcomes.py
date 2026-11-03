"""Policy tests for remote effects whose outcome was never observed."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from mafic.errors import HTTPBadRequest, HTTPNotFound

from .test_voice_ownership_poc import GUILD_ID, VoiceTestCase

if TYPE_CHECKING:
    from mafic.__libraries import Client
    from mafic.node import Node
    from mafic.player import Player


class RemoteOutcomeTests(VoiceTestCase):
    """An unobserved DELETE must never be reported as a confirmed removal."""

    async def connected(self) -> Player[Client]:
        """Return a registered, claimed and node-bound owner."""
        player = await self.bound_player()
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

    async def test_cancelled_partial_target_cleanup_is_unknown_and_propagates(
        self,
    ) -> None:
        """A cancelled cleanup DELETE is unresolved, not a silent no-op."""
        player = await self.connected()
        self.node.destroy_error = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await player._destroy_partial_target(cast("Node[Client]", self.node))

        self.assertEqual(player._coordinator.remote_unknown, 1)

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
