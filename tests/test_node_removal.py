"""Regression tests for owned node removal and player teardown observation."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from mafic.__libraries import Client, Guild, VoiceChannel
from mafic.player import Player
from mafic.pool import NodePool

if TYPE_CHECKING:
    from mafic.node import Node


class FakeNode:
    """Minimal node state used by pool removal tests."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._players: dict[int, Player[Client]] = {}
        self.connect = AsyncMock()
        self.quiesce = AsyncMock()
        self.close = AsyncMock()
        self.destroy = AsyncMock()

    @property
    def players(self) -> list[Player[Client]]:
        """Return the node's players in pool-removal order."""
        return list(self._players.values())

    def get_player(self, guild_id: int) -> Player[Client] | None:
        """Return the player currently registered for the guild."""
        return self._players.get(guild_id)

    def add_player(self, guild_id: int, player: Player[Client]) -> None:
        """Register one player for the guild."""
        self._players[guild_id] = player

    def remove_player(self, guild_id: int) -> None:
        """Forget one player without touching Discord voice state."""
        self._players.pop(guild_id, None)

    def discard_player(self, player: Player[Client]) -> bool:
        """Mirror Node.discard_player: remove only the exact registered player."""
        if self._players.get(player.guild.id) is not player:
            return False
        del self._players[player.guild.id]
        return True


class NodeRemovalTests(IsolatedAsyncioTestCase):
    """Exercise pool removal against real Player instances and fake nodes."""

    def setUp(self) -> None:
        """Install a private pool registry so tests never share class state."""
        client = Mock(spec=Client)
        client.is_closed.return_value = False
        self.client = cast("Client", client)
        self.nodes: dict[str, Node[Client]] = {}
        self.enterContext(patch.object(NodePool, "_nodes", self.nodes))
        self.pool = NodePool(self.client)
        self.node = FakeNode("A")
        self.target = FakeNode("B")
        self.nodes["A"] = cast("Node[Client]", self.node)
        self.nodes["B"] = cast("Node[Client]", self.target)

    def make_player(self, guild_id: int, node: FakeNode) -> Player[Client]:
        """Attach a real player that can be disconnected or transferred."""
        guild = Mock(spec=Guild, id=guild_id)
        guild.change_voice_state = AsyncMock()
        channel = Mock(spec=VoiceChannel, id=10, guild=guild)
        player = Player(
            self.client,
            cast("VoiceChannel", channel),
            node=cast("Node[Client]", node),
        )
        player._session_id = "discord-session"
        player._server_state = {
            "guild_id": guild_id,
            "endpoint": "voice.example.com",
            "token": "token",
        }
        player._connected = True
        guild.voice_client = player
        node.add_player(guild_id, player)
        return player

    def remove(self, *, transfer_players: bool = False) -> asyncio.Task[None]:
        """Start one removal through the public pool boundary."""
        return asyncio.create_task(
            self.pool.remove_node(
                cast("Node[Client]", self.node),
                transfer_players=transfer_players,
            )
        )

    async def test_remove_node_continues_after_one_player_failure(self) -> None:
        """One failed player teardown must not skip the other player or close."""
        first = self.make_player(1, self.node)
        second = self.make_player(2, self.node)
        first_disconnect = AsyncMock(side_effect=RuntimeError("first failed"))
        second_disconnect = AsyncMock()
        first.disconnect = first_disconnect
        second.disconnect = second_disconnect

        await self.pool.remove_node(
            cast("Node[Client]", self.node), transfer_players=False
        )

        first_disconnect.assert_awaited_once_with(force=True)
        second_disconnect.assert_awaited_once_with(force=True)
        self.node.close.assert_awaited_once()
        self.assertNotIn("A", self.nodes)

    async def test_quiesce_completes_before_the_player_snapshot(self) -> None:
        """A player registered while the transport stops is still removed."""
        late: Player[Client] | None = None

        async def quiesce() -> None:
            nonlocal late
            late = self.make_player(2, self.node)
            late.disconnect = AsyncMock()

        self.node.quiesce = AsyncMock(side_effect=quiesce)

        await self.pool.remove_node(
            cast("Node[Client]", self.node), transfer_players=False
        )

        if late is None:
            self.fail("quiesce did not register the late player")
        disconnect = cast("Mock", late.disconnect)
        disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.node.get_player(2))

    async def test_failed_quiesce_closes_the_node_without_teardown(self) -> None:
        """An unstopped transport never proceeds to player teardown."""
        player = self.make_player(1, self.node)
        disconnect = AsyncMock()
        player.disconnect = disconnect
        self.node.quiesce = AsyncMock(side_effect=RuntimeError("quiesce failed"))

        with self.assertRaisesRegex(RuntimeError, "quiesce failed"):
            await self.pool.remove_node(
                cast("Node[Client]", self.node), transfer_players=False
            )

        disconnect.assert_not_awaited()
        self.node.close.assert_awaited_once()
        self.assertNotIn("A", self.nodes)

    async def test_remove_node_cancellation_finishes_started_disconnects(self) -> None:
        """Cancelling the caller does not interrupt the owned removal task."""
        first = self.make_player(1, self.node)
        second = self.make_player(2, self.node)
        started = 0
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def disconnect(*, force: bool) -> None:
            nonlocal started
            self.assertTrue(force)
            started += 1
            if started == 2:
                both_started.set()
            await release.wait()

        first_disconnect = AsyncMock(side_effect=disconnect)
        second_disconnect = AsyncMock(side_effect=disconnect)
        first.disconnect = first_disconnect
        second.disconnect = second_disconnect

        removal = self.remove()
        await asyncio.wait_for(both_started.wait(), 2)
        self.node.close.assert_not_awaited()
        removal.cancel()
        await asyncio.sleep(0)
        removal.cancel()
        await asyncio.sleep(0)
        self.assertFalse(removal.done())

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await removal

        self.node.close.assert_awaited_once()
        self.assertEqual(self.node.close.await_count, 1)
        self.assertEqual(
            self.pool._remove_tasks, {}  # pyright: ignore[reportPrivateUsage]
        )

    async def test_concurrent_remove_node_shares_one_teardown(self) -> None:
        """Concurrent removals of the same label share one owned operation."""
        player = self.make_player(1, self.node)
        started, release = asyncio.Event(), asyncio.Event()

        async def disconnect(*, force: bool) -> None:
            self.assertTrue(force)
            started.set()
            await release.wait()

        player_disconnect = AsyncMock(side_effect=disconnect)
        player.disconnect = player_disconnect

        first = self.remove()
        await asyncio.wait_for(started.wait(), 2)
        self.node.close.assert_not_awaited()
        second = self.remove()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        player_disconnect.assert_awaited_once_with(force=True)
        self.node.close.assert_awaited_once()

    async def test_successful_transfer_is_preserved(self) -> None:
        """A committed transfer survives while a failed one tears only its player."""
        transferred = self.make_player(1, self.node)
        failed = self.make_player(2, self.node)

        async def transfer(
            target: Node[Client], *, expected_source: Node[Client] | None = None
        ) -> bool:
            self.assertIs(expected_source, cast("Node[Client]", self.node))
            transferred._node = target
            cast("FakeNode", target).add_player(transferred.guild.id, transferred)
            self.node.remove_player(transferred.guild.id)
            return True

        failed_transfer = AsyncMock(side_effect=RuntimeError("target unavailable"))
        transferred_disconnect = AsyncMock()
        failed_disconnect = AsyncMock()
        transferred.transfer_to = AsyncMock(side_effect=transfer)
        failed.transfer_to = failed_transfer
        failed.disconnect = failed_disconnect
        transferred.disconnect = transferred_disconnect

        with patch.object(
            NodePool,
            "get_node",
            return_value=cast("Node[Client]", self.target),
        ):
            await self.pool.remove_node(
                cast("Node[Client]", self.node), transfer_players=True
            )
        self.assertIs(transferred.assigned_node, cast("Node[Client]", self.target))
        self.assertIs(self.target.players[0], transferred)
        transferred_disconnect.assert_not_awaited()
        failed_disconnect.assert_awaited_once_with(force=True)
        self.node.close.assert_awaited_once()

    async def test_replaced_voice_client_is_not_touched(self) -> None:
        """A player whose guild voice client was replaced is not disconnected."""
        player = self.make_player(1, self.node)
        replacement = Mock()
        cast("Any", player.guild).voice_client = replacement
        player_disconnect = AsyncMock()
        player.disconnect = player_disconnect
        change_voice_state = cast("AsyncMock", player.guild.change_voice_state)

        await self.pool.remove_node(
            cast("Node[Client]", self.node), transfer_players=False
        )

        player_disconnect.assert_not_awaited()
        change_voice_state.assert_not_awaited()
        self.assertIs(player.guild.voice_client, replacement)
        self.node.close.assert_awaited_once()

    async def test_started_disconnect_does_not_touch_replacement_voice_client(
        self,
    ) -> None:
        """A replacement installed while disconnect waits on its lock survives."""
        guild = Mock(spec=Guild, id=1)
        guild.change_voice_state = AsyncMock()
        channel = Mock(spec=VoiceChannel, id=10, guild=guild)
        channel._get_voice_client_key.return_value = (1, 0)
        cast("Any", self.client)._connection = Mock()
        old = Player(
            self.client,
            cast("VoiceChannel", channel),
            node=cast("Node[Client]", self.node),
        )
        new = Player(
            self.client,
            cast("VoiceChannel", channel),
            node=cast("Node[Client]", self.node),
        )
        for player in (old, new):
            player._session_id = "discord-session"
            player._server_state = {
                "guild_id": 1,
                "endpoint": "voice.example.com",
                "token": "token",
            }
            player._connected = True
        self.node.add_player(1, old)
        self.node.add_player(1, new)
        guild.voice_client = new

        await old._voice_update_lock.acquire()
        disconnect = asyncio.create_task(old.disconnect(force=True))
        await asyncio.sleep(0)
        try:
            guild.voice_client = new
            self.node.add_player(1, new)
        finally:
            old._voice_update_lock.release()

        with patch("mafic.player.VoiceProtocol.cleanup") as adapter_cleanup:
            await asyncio.wait_for(disconnect, 2)
        adapter_cleanup.assert_not_called()
        self.assertIs(self.node.get_player(1), new)
        self.node.destroy.assert_not_awaited()
        guild.change_voice_state.assert_not_awaited()
        self.assertIs(guild.voice_client, new)
        self.assertIsNone(old.assigned_node)
        self.assertFalse(old.connected)

    async def test_late_old_removal_does_not_remove_replacement(self) -> None:
        """A delayed removal must pop only the node instance it owns."""
        old = FakeNode("A")
        replacement = FakeNode("A")
        self.nodes["A"] = cast("Node[Client]", old)

        removal = asyncio.create_task(
            self.pool.remove_node(cast("Node[Client]", old), transfer_players=False)
        )
        # Register the replacement before the removal task gets its first turn.
        self.nodes["A"] = cast("Node[Client]", replacement)
        await removal

        self.assertIs(self.nodes["A"], replacement)
        old.close.assert_awaited_once()
        replacement.close.assert_not_awaited()

        await self.pool.remove_node(
            cast("Node[Client]", replacement), transfer_players=False
        )
        self.assertNotIn("A", self.nodes)
        replacement.close.assert_awaited_once()

    async def test_replacement_can_be_removed_during_old_removal(self) -> None:
        """A replacement node has its own removal, not the old node's task."""
        old_player = self.make_player(1, self.node)
        started, release = asyncio.Event(), asyncio.Event()

        async def disconnect(*, force: bool) -> None:
            self.assertTrue(force)
            started.set()
            await release.wait()

        old_player.disconnect = AsyncMock(side_effect=disconnect)
        old_removal = asyncio.create_task(
            self.pool.remove_node(
                cast("Node[Client]", self.node), transfer_players=False
            )
        )
        await asyncio.wait_for(started.wait(), 2)

        replacement = FakeNode("A")
        await self.pool.add_node(cast("Node[Client]", replacement))
        replacement_removal = asyncio.create_task(
            self.pool.remove_node(
                cast("Node[Client]", replacement), transfer_players=False
            )
        )
        try:
            await asyncio.sleep(0.1)
            replacement.close.assert_awaited_once()
        finally:
            release.set()
            await old_removal
            await asyncio.gather(replacement_removal, return_exceptions=True)

        self.node.close.assert_awaited_once()
        self.assertNotIn("A", self.nodes)

    async def test_disconnect_does_not_remove_replacement_player(self) -> None:
        """Local removal must match the exact player, not only the guild id."""
        old = self.make_player(1, self.node)
        replacement = self.make_player(1, self.node)
        self.node._players[1] = old

        async def disconnect(*, force: bool) -> None:
            self.assertTrue(force)
            self.node._players[1] = replacement

        old.disconnect = AsyncMock(side_effect=disconnect)
        await self.pool._disconnect_player(
            cast("Node[Client]", self.node),
            old,
        )

        self.assertIs(self.node.get_player(1), replacement)

    async def test_removal_forgets_exact_player_after_lost_assignment(self) -> None:
        """A stale local entry is removed even when the player lost its node."""
        player = self.make_player(1, self.node)
        player._node = None

        await self.pool._disconnect_player(
            cast("Node[Client]", self.node),
            player,
        )

        self.assertIsNone(self.node.get_player(1))

    async def test_transfer_skips_player_that_left_source(self) -> None:
        """Removal must not transfer a player that already changed ownership."""
        player = self.make_player(1, self.node)
        other = FakeNode("C")
        player._node = cast("Node[Client]", other)
        player.transfer_to = AsyncMock()
        player.disconnect = AsyncMock()

        with patch.object(
            NodePool,
            "get_node",
            return_value=cast("Node[Client]", self.target),
        ):
            await self.pool._transfer_players(
                cast("Node[Client]", self.node),
                (player,),
            )

        player.transfer_to.assert_not_awaited()
        player.disconnect.assert_not_awaited()

    async def test_transfer_waiting_on_lock_returns_false_after_owner_change(
        self,
    ) -> None:
        """Expected-source checking happens under the player ownership lock."""
        player = self.make_player(1, self.node)
        source = player.assigned_node
        other = FakeNode("C")

        await player._voice_update_lock.acquire()
        transfer = asyncio.create_task(
            player.transfer_to(
                cast("Node[Client]", self.target),
                expected_source=source,
            )
        )
        await asyncio.sleep(0)
        player._node = cast("Node[Client]", other)
        player._voice_update_lock.release()

        self.assertFalse(await transfer)
        self.assertIs(player.assigned_node, cast("Node[Client]", other))

    async def test_removal_does_not_wait_for_player_owned_by_another_node(
        self,
    ) -> None:
        """A started teardown on a different node is not this removal's work."""
        player = self.make_player(1, self.node)
        other = FakeNode("C")
        player._node = cast("Node[Client]", other)
        release = asyncio.Event()

        async def teardown() -> None:
            await release.wait()

        teardown_task = asyncio.create_task(teardown())
        player._disconnect_task = teardown_task
        removal = asyncio.create_task(
            self.pool._disconnect_player(
                cast("Node[Client]", self.node),
                player,
            )
        )
        try:
            await asyncio.sleep(0.1)
            self.assertTrue(removal.done())
        finally:
            release.set()
            await removal
            await teardown_task

        self.assertIsNone(self.node.get_player(1))

    async def test_removal_waits_for_source_owned_started_teardown(self) -> None:
        """A teardown still owned by the source node is awaited."""
        player = self.make_player(1, self.node)
        started, release = asyncio.Event(), asyncio.Event()

        async def teardown() -> None:
            started.set()
            await release.wait()

        teardown_task = asyncio.create_task(teardown())
        player._disconnect_task = teardown_task
        removal = asyncio.create_task(
            self.pool._disconnect_player(
                cast("Node[Client]", self.node),
                player,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        self.assertFalse(removal.done())

        release.set()
        await asyncio.wait_for(removal, 2)
        self.assertTrue(teardown_task.done())
        self.assertIsNone(self.node.get_player(1))


class PlayerTeardownObservationTests(IsolatedAsyncioTestCase):
    """Exercise the public wait-without-starting disconnect contract."""

    def setUp(self) -> None:
        """Create a real player without a lifecycle owner."""
        client = Mock(spec=Client)
        client.is_closed.return_value = False
        guild = Mock(spec=Guild, id=1)
        guild.change_voice_state = AsyncMock()
        channel = Mock(spec=VoiceChannel, id=10, guild=guild)
        self.player = Player(cast("Client", client), cast("VoiceChannel", channel))
        self.disconnect = AsyncMock()
        self.player.disconnect = self.disconnect

    async def test_disconnect_not_started_does_not_start_teardown(self) -> None:
        """Waiting before a disconnect must not create a teardown task."""
        self.assertFalse(self.player.disconnect_started)

        await self.player.wait_for_disconnect()

        self.disconnect.assert_not_awaited()

    async def test_wait_for_disconnect_awaits_started_teardown(self) -> None:
        """Waiting joins an existing teardown without starting another."""
        started, release = asyncio.Event(), asyncio.Event()

        async def teardown() -> None:
            started.set()
            await release.wait()

        task = asyncio.create_task(teardown())
        self.player._disconnect_task = task
        waiter = asyncio.create_task(self.player.wait_for_disconnect())
        await asyncio.wait_for(started.wait(), 2)
        self.assertFalse(waiter.done())

        release.set()
        await asyncio.wait_for(waiter, 2)

        self.disconnect.assert_not_awaited()
        self.assertTrue(self.player.disconnect_started)
