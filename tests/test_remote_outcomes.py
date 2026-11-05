"""Policy tests for remote effects whose outcome was never observed."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import aiohttp

from mafic.errors import HTTPBadRequest, HTTPNotFound
from mafic.node import Node

from .test_voice_ownership_poc import GUILD_ID, VoiceTestCase

if TYPE_CHECKING:
    from mafic.__libraries import Client
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
        """Cancelling the await cannot undo a request already sent."""
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

    async def test_missing_voice_endpoint_never_marks_a_request_unknown(self) -> None:
        """A rejected local payload cannot have affected Lavalink."""
        player = await self.connected()
        node: Node[Client] = Node(
            host="127.0.0.1",
            port=2333,
            label="local",
            password="test",  # noqa: S106
            client=cast("Client", self.client),
        )
        player._node = node
        player._server_state = {
            "guild_id": GUILD_ID,
            "endpoint": None,
            "token": "token",
        }
        request = AsyncMock()
        with (
            patch.object(Node, "_Node__request", request),
            self.assertRaises(ValueError),
        ):
            await player._dispatch_player_update()

        request.assert_not_called()
        self.assertEqual(player._coordinator.remote_unknown, 0)

    async def test_closing_node_does_not_mark_an_unsent_request_unknown(self) -> None:
        """A local shutdown guard runs before the HTTP request can start."""
        player = await self.connected()
        node: Node[Client] = Node(
            host="127.0.0.1",
            port=2333,
            label="closing",
            password="test",  # noqa: S106
            client=cast("Client", self.client),
        )
        node._closing = True
        player._node = node
        try:
            with self.assertRaisesRegex(RuntimeError, "Node is closing"):
                await player.update(volume=10)
            self.assertEqual(player._coordinator.remote_unknown, 0)
        finally:
            node._closing = False
            await node.close()

    async def test_closed_session_does_not_mark_an_unsent_request_unknown(self) -> None:
        """A closed HTTP session cannot have sent the requested update."""
        player = await self.connected()
        session = aiohttp.ClientSession()
        await session.close()
        node: Node[Client] = Node(
            host="127.0.0.1",
            port=2333,
            label="closed-session",
            password="test",  # noqa: S106
            client=cast("Client", self.client),
            session=session,
        )
        player._node = node
        with self.assertRaisesRegex(RuntimeError, "HTTP session is closed"):
            await player.update(volume=10)
        self.assertEqual(player._coordinator.remote_unknown, 0)

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
