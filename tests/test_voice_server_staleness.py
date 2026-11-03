"""Staleness tests for the voice-server callback and transfer commit."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from asyncio import Event
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock, patch

from .test_player_update import player_payload
from .test_voice_ownership_poc import GUILD_ID, FakeNode, VoiceTestCase

if TYPE_CHECKING:
    from mafic.__libraries import Client, VoiceServerUpdatePayload
    from mafic.node import Node
    from mafic.player import Player

ENDPOINT = "voice.example.com"


class VoiceServerStalenessTests(VoiceTestCase):
    """A synchronous claim must win against any awaiting callback."""

    def setUp(self) -> None:
        """Add a second target node to the shared isolated fixture."""
        super().setUp()
        self.target = FakeNode("B")

    def server_update(self, endpoint: str = ENDPOINT) -> VoiceServerUpdatePayload:
        """Build a voice server payload for the guild."""
        return cast(
            "VoiceServerUpdatePayload",
            {"guild_id": GUILD_ID, "endpoint": endpoint, "token": "token"},
        )

    async def connected(self) -> Player[Client]:
        """Return a registered, claimed and node-bound owner."""
        player = self.make_player()
        self.register(player)
        await self.connect(player)
        player._connected = True
        player._node = cast("Node[Client]", self.node)
        player._session_id = "discord-session"
        player._server_state = {
            "guild_id": GUILD_ID,
            "endpoint": ENDPOINT,
            "token": "token",
        }
        return player

    async def test_claim_while_waiting_for_the_voice_lock_blocks_registration(
        self,
    ) -> None:
        """A claim during the lock wait must stop the registry write."""
        player = await self.connected()
        self.node._players.clear()

        # Hold the player lock so the callback stops before its ownership write.
        await player._voice_update_lock.acquire()
        callback = asyncio.create_task(
            player.on_voice_server_update(self.server_update())
        )
        for _ in range(5):
            await asyncio.sleep(0)

        replacement = self.make_player()
        replacement._take_ownership()

        player._voice_update_lock.release()
        await asyncio.wait_for(callback, 2)

        self.assertEqual(self.node.players, [])
        self.assertEqual(self.node.voice_update_calls, 0)
        self.assertTrue(player._superseded())

    async def test_claim_during_transfer_preparation_blocks_the_commit(self) -> None:
        """A stale transfer must not commit, and its target must be cleaned up."""
        player = await self.connected()
        started, release = Event(), Event()

        async def fetch(_guild_id: int) -> object:
            started.set()
            await release.wait()
            return player_payload()

        self.node.fetch_player.side_effect = fetch

        pool = Mock(nodes=[self.target], get_node=Mock(return_value=self.target))

        def resolve(_client: object) -> Mock:
            return pool

        with patch("mafic.player.pool_for", resolve):
            callback = asyncio.create_task(
                player.on_voice_server_update(self.server_update("other.example.com"))
            )
            await asyncio.wait_for(started.wait(), 2)

            replacement = self.make_player()
            replacement._take_ownership()

            release.set()
            await asyncio.wait_for(callback, 2)

        # No commit: the old node keeps the player and the target has nobody.
        self.assertIs(player._node, self.node)
        self.assertEqual(self.target.players, [])
        self.assertEqual(self.node.players, [])
        # The prepared target was removed, and the old player was not destroyed.
        self.assertEqual(self.target.destroy_calls, 1)
        self.assertEqual(self.node.destroy_calls, 0)

    async def test_inline_callback_inside_the_lane_still_applies(self) -> None:
        """The inline branch must not re-enter the non-reentrant lane."""
        player = await self.connected()
        self.node._players.clear()

        async with player._coordinator.hold():
            await player.on_voice_server_update(self.server_update())

        self.assertEqual(len(self.node.players), 1)
        self.assertEqual(self.node.voice_update_calls, 1)
        self.assertTrue(player._voice_server_update_event.is_set())

    async def test_inline_callback_of_a_superseded_player_is_ignored(self) -> None:
        """The lane does not protect against a synchronous claim either."""
        player = await self.connected()
        self.node._players.clear()

        async with player._coordinator.hold():
            replacement = self.make_player()
            replacement._take_ownership()
            await player.on_voice_server_update(self.server_update())

        self.assertEqual(self.node.players, [])
        self.assertEqual(self.node.voice_update_calls, 0)
