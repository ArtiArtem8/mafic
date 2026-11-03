"""Deterministic proof that one guild keeps one authoritative voice owner.

Each scenario blocks one generation at a known await point with an
``asyncio.Event`` and then lets the replacement run. The barrier is what makes
the ordering a proof rather than a timing guess.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from asyncio import Event
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from mafic.__libraries import Client, VoiceChannel, VoiceProtocol
from mafic.player import Player
from mafic.pool import NodePool

from .test_player_update import player_payload

if TYPE_CHECKING:
    from mafic.node import Node

GUILD_ID = 1
CHANNEL_ID = 10


class FakeConnection:
    """The Discord voice-client cache, keyed by guild id as the library keys it."""

    def __init__(self) -> None:
        self.voice_clients: dict[int, object] = {}

    def _add_voice_client(self, key: int, voice: object) -> None:
        self.voice_clients[key] = voice

    def _remove_voice_client(self, key: int) -> None:
        self.voice_clients.pop(key, None)

    def _get_voice_client(self, key: int) -> object | None:
        return self.voice_clients.get(key)


class FakeClient:
    """The client surface Player reaches for."""

    def __init__(self) -> None:
        self._connection = FakeConnection()

    def is_closed(self) -> bool:
        """Report a live client; teardown must proceed."""
        return False

    def dispatch(self, _event: str, *args: object) -> None:
        """Accept a lifecycle event; tests assert on registries instead."""


class FakeGuild:
    """A guild whose voice_client mirrors the library cache, as Discord does."""

    def __init__(self, client: FakeClient) -> None:
        self.id = GUILD_ID
        self._connection = client._connection
        self.calls: list[str] = []
        self.leave_started = Event()
        self.release_leave = Event()
        self.block_leave = False
        self.join_started = Event()
        self.release_join = Event()
        self.block_join = False
        self.join_error: BaseException | None = None

    @property
    def voice_client(self) -> object | None:
        """Return the registered protocol, exactly as Guild.voice_client does."""
        return self._connection._get_voice_client(self.id)

    async def change_voice_state(
        self, *, channel: object = None, **_kwargs: object
    ) -> None:
        """Record a gateway request, with optional holds and join failures."""
        if channel is None:
            self.calls.append("leave")
            if self.block_leave:
                self.leave_started.set()
                await self.release_leave.wait()
            return
        self.calls.append("join")
        self.join_started.set()
        if self.block_join:
            await self.release_join.wait()
        if self.join_error is not None:
            raise self.join_error


class FakeNode:
    """A node double that honours the identity-checked registry contract."""

    def __init__(self) -> None:
        self.label = "A"
        self.version = 4
        self._players: dict[int, Player[Client]] = {}
        self.destroy_calls = 0
        self.update_calls = 0
        self.voice_update_calls = 0
        self.events: list[str] = []
        self.destroy_error: BaseException | None = None
        self.update_error: BaseException | None = None
        self.voice_update_error: BaseException | None = None
        self.destroy_started = Event()
        self.release_destroy = Event()
        self.block_destroy = False

    @property
    def players(self) -> list[Player[Client]]:
        """Return the node's players in registration order."""
        return list(self._players.values())

    def get_player(self, guild_id: int) -> Player[Client] | None:
        """Return the player registered for the guild, if any."""
        return self._players.get(guild_id)

    def add_player(self, guild_id: int, player: Player[Client]) -> None:
        """Register one player for the guild."""
        self._players[guild_id] = player

    def remove_player(self, guild_id: int) -> None:
        """Forget the guild's player without any identity check."""
        self._players.pop(guild_id, None)

    def discard_player(self, player: Player[Client]) -> bool:
        """Remove one player only while it is still the registered one."""
        if self._players.get(player.guild.id) is not player:
            return False
        del self._players[player.guild.id]
        return True

    async def update(self, **_kwargs: object) -> object:
        """Record a PATCH and answer with a well-formed player payload."""
        self.update_calls += 1
        self.events.append("patch")
        if self.update_error is not None:
            raise self.update_error
        return player_payload()

    async def voice_update(self, **_kwargs: object) -> None:
        """Record a voice PATCH without a transport."""
        self.voice_update_calls += 1
        self.events.append("voice_patch")
        if self.voice_update_error is not None:
            raise self.voice_update_error

    async def destroy(self, **_kwargs: object) -> None:
        """Stand in for DELETE /sessions/{id}/players/{guild}, with a barrier."""
        self.destroy_calls += 1
        self.events.append("delete")
        self.destroy_started.set()
        if self.destroy_error is not None:
            raise self.destroy_error
        if self.block_destroy:
            await self.release_destroy.wait()


class VoiceOwnershipTests(IsolatedAsyncioTestCase):
    """One guild, two generations, every transition driven by a barrier."""

    def setUp(self) -> None:
        """Install a private client, guild and node for each test."""
        self.client = FakeClient()
        self.guild = FakeGuild(self.client)
        self.node = FakeNode()
        self.enterContext(patch.object(NodePool, "nodes", [self.node]))

    # --- fixtures ---------------------------------------------------------

    def make_player(self) -> Player[Client]:
        """Build a real Player whose guild is the fake cache owner."""
        channel = Mock(spec=VoiceChannel, id=CHANNEL_ID, guild=self.guild)
        channel._get_voice_client_key.return_value = (GUILD_ID, None)
        return Player(cast("Client", self.client), channel)

    def register(self, player: Player[Client]) -> None:
        """Model Connectable.connect, which registers before Player.connect."""
        key, _ = player.channel._get_voice_client_key()
        self.client._connection._add_voice_client(key, player)

    def bind(self, player: Player[Client]) -> None:
        """Bind a player to the node, as on_voice_server_update would."""
        self.node.add_player(GUILD_ID, player)
        player._node = cast("Node[Client]", self.node)

    async def connect(self, player: Player[Client]) -> None:
        """Complete a connect without waiting on real gateway events."""
        player._voice_state_update_event.set()
        player._voice_server_update_event.set()
        player._node_player_ready_event.set()
        await player.connect(timeout=2, reconnect=True)

    async def connected(self) -> Player[Client]:
        """Return a registered, connected and node-bound owner."""
        player = self.make_player()
        self.register(player)
        await self.connect(player)
        self.bind(player)
        return player

    async def hold_lane(self, player: Player[Client]) -> asyncio.Event:
        """Occupy the guild lane and return the event that releases it."""
        held = Event()
        release = Event()

        async def holder() -> None:
            async with player._coordinator.hold():
                held.set()
                await release.wait()

        task = asyncio.create_task(holder())
        self.addCleanup(task.cancel)
        await asyncio.wait_for(held.wait(), 2)
        return release

    # --- scenarios --------------------------------------------------------

    async def test_single_generation_joins_and_leaves(self) -> None:
        """The baseline: one generation owns the guild and retires cleanly."""
        player = await self.connected()
        self.assertIs(self.guild.voice_client, player)
        self.assertEqual(self.guild.calls, ["join"])

        await player.disconnect(force=True)

        self.assertEqual(self.guild.calls, ["join", "leave"])
        self.assertIsNone(self.guild.voice_client)
        self.assertEqual(self.node.players, [])
        self.assertEqual(self.node.destroy_calls, 1)

    async def test_stale_cleanup_cannot_evict_the_replacement(self) -> None:
        """A superseded Player must not remove the current owner from the cache.

        Base VoiceProtocol.cleanup pops the guild slot unconditionally, so the
        removal has to be gated by an identity check in the same synchronous
        block. The negative control below shows what the base method would do.
        """
        first = await self.connected()
        second = self.make_player()
        self.register(second)
        await self.connect(second)

        first.cleanup()
        self.assertIs(self.guild.voice_client, second)

        # Negative control: the library contract alone evicts the new owner.
        VoiceProtocol.cleanup(second)
        self.assertIsNone(self.guild.voice_client)

    async def test_replacement_during_lavalink_delete_keeps_the_new_owner(self) -> None:
        """A DELETE already in flight cannot destroy the next generation."""
        first = await self.connected()
        self.node.block_destroy = True

        teardown = asyncio.create_task(first.disconnect(force=True))
        await asyncio.wait_for(self.node.destroy_started.wait(), 2)

        second = self.make_player()
        self.register(second)
        second._take_ownership()
        self.assertTrue(second._coordinator.is_current(second))
        self.assertTrue(first._superseded())

        connect_second = asyncio.create_task(self.connect(second))
        self.assertEqual(self.guild.calls, ["join"])

        self.node.release_destroy.set()
        await asyncio.wait_for(teardown, 2)
        await asyncio.wait_for(connect_second, 2)

        self.assertEqual(self.node.destroy_calls, 1)
        self.assertEqual(self.guild.calls, ["join", "join"])
        self.assertIs(self.guild.voice_client, second)

    async def test_replacement_while_leaving_keeps_the_join_order(self) -> None:
        """A claim during change_voice_state(None) cannot invert the two sends."""
        first = await self.connected()
        self.guild.block_leave = True

        teardown = asyncio.create_task(first.disconnect(force=True))
        await asyncio.wait_for(self.guild.leave_started.wait(), 2)

        second = self.make_player()
        self.register(second)
        second._take_ownership()
        connect_second = asyncio.create_task(self.connect(second))

        self.guild.release_leave.set()
        await asyncio.wait_for(teardown, 2)
        await asyncio.wait_for(connect_second, 2)

        # Leave must be sent before the replacement join, never after it.
        self.assertEqual(self.guild.calls, ["join", "leave", "join"])
        self.assertIs(self.guild.voice_client, second)

    async def test_replacement_while_waiting_for_the_lane_skips_remote_work(
        self,
    ) -> None:
        """A teardown queued behind the lane does nothing once superseded."""
        first = await self.connected()
        second = self.make_player()
        self.register(second)
        await self.connect(second)
        self.node.add_player(GUILD_ID, second)

        release = await self.hold_lane(first)
        teardown = asyncio.create_task(first.disconnect(force=True))

        # The replacement claims while the queued teardown has not run yet.
        second._take_ownership()
        release.set()
        await asyncio.wait_for(teardown, 2)

        self.assertEqual(self.node.destroy_calls, 0)
        self.assertNotIn("leave", self.guild.calls)
        self.assertIs(self.guild.voice_client, second)
        self.assertEqual(self.node.players, [second])

    async def test_reconnect_after_cancelled_connect_keeps_one_owner(self) -> None:
        """A cancelled connect leaves a claim the replacement can take over."""
        first = self.make_player()
        self.register(first)
        release = await self.hold_lane(first)

        connect_first = asyncio.create_task(self.connect(first))
        while first._ownership_epoch == 0:
            await asyncio.sleep(0)

        connect_first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await connect_first
        self.assertNotIn("join", self.guild.calls)

        # Retrying is what a caller does after a cancelled connect; the
        # abandoned claim must not block or corrupt the retry.
        release.set()
        second = self.make_player()
        self.register(second)
        await self.connect(second)

        self.bind(first)
        await first.disconnect(force=True)

        self.assertIs(self.guild.voice_client, second)
        self.assertNotIn("leave", self.guild.calls)
        self.assertEqual(self.node.destroy_calls, 0)
