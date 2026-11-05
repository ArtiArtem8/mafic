"""Tests for per-client NodePool scope and registry lifetime."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import gc
import importlib
import weakref
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

import mafic
from mafic.__libraries import Client, Guild, VoiceChannel
from mafic.errors import NodePoolAlreadyBound, NoNodesAvailable
from mafic.player import Player
from mafic.pool import NodePool, pool_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mafic.node import Node


def _select_all(
    nodes: Sequence[Node[Client]],
    _guild_id: int,
    _shard_count: int | None,
    _endpoint: str | None,
) -> list[Node[Client]]:
    """Keep every candidate so one node always wins."""
    return list(nodes)


def _make_client() -> Mock:
    """Return a weak-referenceable stand-in for a Discord client."""
    client = Mock()
    client.shard_count = 1
    return client


def _make_node(client: Mock, label: str) -> Mock:
    """Return a node that belongs to the given client."""
    node = Mock()
    node.client = client
    node.label = label
    node.available = True
    node.players = []
    node.close = AsyncMock()
    node.quiesce = AsyncMock()
    node.connect = AsyncMock()
    return node


class PoolScopeTests(IsolatedAsyncioTestCase):
    """Two clients must never share nodes, strategies or removal state."""

    def _pool(self, client: Mock) -> NodePool[Client]:
        return NodePool(cast("Client", client), [_select_all])

    async def _wait_without_deadlock(
        self, task: asyncio.Task[None], pool: NodePool[Client]
    ) -> None:
        done, _ = await asyncio.wait({task}, timeout=2)
        if task not in done:
            if pool._close_task is not None:
                pool._close_task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.fail("The pool operation waited for its own completion.")
        await task

    def test_each_client_gets_its_own_nodes(self) -> None:
        """Identical labels and guild ids still resolve within one client."""
        first_client, second_client = _make_client(), _make_client()
        first = self._pool(first_client)
        second = self._pool(second_client)
        first_node = _make_node(first_client, "MAIN")
        second_node = _make_node(second_client, "MAIN")
        first._nodes["MAIN"] = cast("Node[Client]", first_node)
        second._nodes["MAIN"] = cast("Node[Client]", second_node)

        self.assertEqual(first.nodes, [first_node])
        self.assertEqual(second.nodes, [second_node])
        self.assertIs(first.get_node(guild_id=1, endpoint=None), first_node)
        self.assertIs(second.get_node(guild_id=1, endpoint=None), second_node)
        self.assertIs(pool_for(first_client), first)
        self.assertIs(pool_for(second_client), second)

    async def test_closing_one_pool_leaves_the_other_usable(self) -> None:
        """Closing A closes only A's nodes and releases only A's client."""
        first_client, second_client = _make_client(), _make_client()
        first = self._pool(first_client)
        second = self._pool(second_client)
        first_node = _make_node(first_client, "MAIN")
        second_node = _make_node(second_client, "MAIN")
        first._nodes["MAIN"] = cast("Node[Client]", first_node)
        second._nodes["MAIN"] = cast("Node[Client]", second_node)

        await first.close()

        first_node.close.assert_awaited_once()
        second_node.close.assert_not_awaited()
        self.assertIsNone(pool_for(first_client))
        self.assertIs(pool_for(second_client), second)
        self.assertEqual(second.nodes, [second_node])

    def test_one_active_pool_per_client(self) -> None:
        """A second pool must not silently replace the active one."""
        client = _make_client()
        first = self._pool(client)

        with self.assertRaises(NodePoolAlreadyBound):
            self._pool(client)

        self.assertIs(pool_for(client), first)

    def test_pool_for_is_available_for_public_recovery(self) -> None:
        """Applications can recover the bound pool without private imports."""
        client = _make_client()
        pool = self._pool(client)

        self.assertIs(mafic.pool_for(client), pool)

    def test_client_binding_does_not_overwrite_an_existing_attribute(self) -> None:
        """An attribute collision must not overwrite user-owned state."""
        client = _make_client()
        value = object()
        client._mafic_node_pool = value

        with self.assertRaisesRegex(RuntimeError, "_mafic_node_pool"):
            self._pool(client)

        self.assertIs(client._mafic_node_pool, value)
        self.assertIsNone(pool_for(client))

    def test_empty_pool_reports_no_available_nodes(self) -> None:
        """Random selection and an empty strategy list use the same pool error."""
        client = _make_client()
        pool = NodePool(cast("Client", client), [])

        with self.assertRaises(NoNodesAvailable):
            pool.get_random_node()
        with self.assertRaises(NoNodesAvailable):
            pool.get_node(guild_id=1, endpoint=None)

    async def test_pool_can_be_recreated_after_close(self) -> None:
        """A closed pool releases its client for a new one."""
        client = _make_client()
        first = self._pool(client)
        await first.close()

        second = self._pool(client)

        self.assertIs(pool_for(client), second)

    async def test_client_keeps_its_pool_until_successful_close(self) -> None:
        """Losing the application's reference cannot orphan connected nodes."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))
        pool_ref = weakref.ref(pool)
        del pool
        gc.collect()

        recovered = pool_for(client)
        self.assertIsNotNone(recovered)
        self.assertIs(recovered, pool_ref())
        with self.assertRaises(NodePoolAlreadyBound):
            self._pool(client)
        if recovered is not None:
            await recovered.close()
        node.close.assert_awaited_once()
        self.assertIsNone(pool_for(client))

    async def test_failed_close_remains_recoverable_after_reference_is_dropped(
        self,
    ) -> None:
        """A failed close retains the old owner for explicit recovery."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))

        async def fail_close() -> None:
            msg = "close failed"
            raise RuntimeError(msg)

        node.close.side_effect = fail_close
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await pool.close()
        node.close.side_effect = None
        pool_ref = weakref.ref(pool)
        del pool
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()

        recovered = pool_for(client)
        self.assertIsNotNone(recovered)
        self.assertIs(recovered, pool_ref())
        with self.assertRaises(NodePoolAlreadyBound):
            self._pool(client)
        if recovered is not None:
            self.assertFalse(recovered.closing)
            self.assertIn(node, recovered.failed_nodes)
            self.assertIn(node, recovered.managed_nodes)
            await recovered.close()
        self.assertIsNone(pool_for(client))
        replacement = self._pool(client)
        await replacement.close()

    async def test_closed_pool_is_released_while_client_remains_alive(self) -> None:
        """The client's strong reference ends with a successful close."""
        client = _make_client()
        pool = self._pool(client)
        pool_ref = weakref.ref(pool)
        await pool.close()
        del pool
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()

        self.assertIsNone(pool_for(client))
        self.assertIsNone(pool_ref())

    async def test_real_discord_client_retains_and_releases_its_pool(self) -> None:
        """The binding works with the installed Discord adapter, not only mocks."""
        adapter = importlib.import_module(Client.__module__.partition(".")[0])
        intents_type = vars(adapter)["Intents"]
        client = Client(intents=intents_type.none())
        pool = NodePool(client)
        client_ref, pool_ref = weakref.ref(client), weakref.ref(pool)
        del pool
        gc.collect()

        recovered = mafic.pool_for(client)
        self.assertIsNotNone(recovered)
        self.assertIs(recovered, pool_ref())
        if recovered is not None:
            await recovered.close()
        await client.close()
        del recovered, client
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        self.assertIsNone(client_ref())
        self.assertIsNone(pool_ref())

    async def test_failed_pool_and_client_are_collectible_without_external_roots(
        self,
    ) -> None:
        """The weak registry must not globally retain an unreachable client."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))

        async def fail_close() -> None:
            msg = "close failed"
            raise RuntimeError(msg)

        node.close.side_effect = fail_close
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await pool.close()
        node.close.side_effect = None
        client_ref, pool_ref = weakref.ref(client), weakref.ref(pool)
        del node, pool, client
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()

        self.assertIsNone(client_ref())
        self.assertIsNone(pool_ref())

    async def test_closed_pool_can_rebind_and_reuse_a_node(self) -> None:
        """A pool may be reused while its client has no newer pool."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))
        await pool.close()

        self.assertIsNone(pool_for(client))
        self.assertEqual(pool.label_to_node, {})
        await pool.add_node(cast("Node[Client]", node))

        self.assertIs(pool_for(client), pool)
        self.assertIs(pool.get_node(guild_id=1, endpoint=None), node)
        self.assertEqual(node.connect.await_count, 2)
        await pool.close()

    async def test_player_connects_through_a_reused_pool(self) -> None:
        """Player.connect discovers the pool again after it is reused."""
        client = _make_client()
        client.is_closed.return_value = False
        client._connection = Mock()
        pool = self._pool(client)
        await pool.close()
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))

        guild = Mock(spec=Guild, id=1)
        guild.change_voice_state = AsyncMock()
        channel = Mock(spec=VoiceChannel, id=10, guild=guild)
        channel._get_voice_client_key.return_value = (1, None)
        player = Player(cast("Client", client), cast("VoiceChannel", channel))
        guild.voice_client = player
        player._voice_state_update_event.set()
        player._voice_server_update_event.set()
        player._node_player_ready_event.set()

        await player.connect(timeout=1, reconnect=True)

        guild.change_voice_state.assert_awaited_once_with(
            channel=channel, self_mute=False, self_deaf=False
        )
        player.cleanup()
        await pool.close()

    async def test_old_pool_cannot_touch_a_new_pool_for_the_same_client(self) -> None:
        """A later pool owns the client and nodes even if the old pool survives."""
        client = _make_client()
        old = self._pool(client)
        node = _make_node(client, "MAIN")
        await old.add_node(cast("Node[Client]", node))
        await old.close()
        new = self._pool(client)
        await new.add_node(cast("Node[Client]", node))

        with self.assertRaises(NodePoolAlreadyBound):
            await old.close()
        with self.assertRaises(NodePoolAlreadyBound):
            await old.add_node(cast("Node[Client]", node))
        with self.assertRaises(NodePoolAlreadyBound):
            await old.remove_node(cast("Node[Client]", node))

        node.close.assert_awaited_once()
        node.quiesce.assert_not_awaited()
        self.assertEqual(new.nodes, [node])
        await new.close()
        await old.add_node(cast("Node[Client]", node))
        self.assertIs(pool_for(client), old)
        await old.close()

    async def test_duplicate_label_does_not_replace_or_connect_node(self) -> None:
        """A duplicate label cannot orphan the node already in the pool."""
        client = _make_client()
        pool = self._pool(client)
        first = _make_node(client, "MAIN")
        second = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", first))

        with self.assertRaisesRegex(ValueError, "MAIN"):
            await pool.add_node(cast("Node[Client]", second))

        second.connect.assert_not_awaited()
        first.close.assert_not_awaited()
        self.assertEqual(pool.nodes, [first])
        await pool.close()
        first.close.assert_awaited_once()

    async def test_concurrent_additions_reserve_the_label_before_connecting(
        self,
    ) -> None:
        """A second node cannot race a pending connection for the same label."""
        client = _make_client()
        pool = self._pool(client)
        first = _make_node(client, "MAIN")
        second = _make_node(client, "MAIN")
        entered, release = asyncio.Event(), asyncio.Event()

        async def connect(*, player_cls: object = None) -> None:
            self.assertIsNone(player_cls)
            entered.set()
            await release.wait()

        first.connect.side_effect = connect
        adding = asyncio.create_task(pool.add_node(cast("Node[Client]", first)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with self.assertRaisesRegex(ValueError, "MAIN"):
                await pool.add_node(cast("Node[Client]", second))
            second.connect.assert_not_awaited()
            release.set()
            await asyncio.wait_for(adding, 2)
            self.assertEqual(pool.nodes, [first])
        finally:
            release.set()
            await asyncio.gather(adding, return_exceptions=True)
            await pool.close()

    async def test_close_waits_for_pending_addition(self) -> None:
        """A node added during shutdown is closed before the client is released."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        entered, release = asyncio.Event(), asyncio.Event()

        async def connect(*, player_cls: object = None) -> None:
            self.assertIsNone(player_cls)
            entered.set()
            await release.wait()

        node.connect.side_effect = connect
        adding = asyncio.create_task(pool.add_node(cast("Node[Client]", node)))
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            closing = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertIs(pool_for(client), pool)
            self.assertTrue(pool.closing)
            self.assertIn(node, pool.managed_nodes)
            with self.assertRaisesRegex(RuntimeError, "closing"):
                await pool.remove_node(cast("Node[Client]", node))
            with self.assertRaisesRegex(RuntimeError, "closing"):
                await pool.add_node(cast("Node[Client]", _make_node(client, "NEW")))
            with self.assertRaisesRegex(RuntimeError, "closing"):
                await pool.create_node(
                    host="127.0.0.1",
                    port=2333,
                    label="NEW",
                    password="test-password",  # noqa: S106
                )
            release.set()
            await asyncio.wait_for(adding, 2)
            await asyncio.wait_for(closing, 2)
            node.close.assert_awaited_once()
            self.assertIsNone(pool_for(client))
        finally:
            release.set()
            await asyncio.gather(adding, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            if pool_for(client) is pool:
                await pool.close()

    async def test_addition_does_not_replace_an_internally_registered_node(
        self,
    ) -> None:
        """An internal mapping change never overwrites a different node."""
        client = _make_client()
        pool = self._pool(client)
        candidate = _make_node(client, "MAIN")
        replacement = _make_node(client, "MAIN")
        entered, release = asyncio.Event(), asyncio.Event()

        async def connect(*, player_cls: object = None) -> None:
            self.assertIsNone(player_cls)
            entered.set()
            await release.wait()

        candidate.connect.side_effect = connect
        adding = asyncio.create_task(pool.add_node(cast("Node[Client]", candidate)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pool._nodes["MAIN"] = cast("Node[Client]", replacement)
            release.set()
            with self.assertRaisesRegex(ValueError, "MAIN"):
                await asyncio.wait_for(adding, 2)
            candidate.close.assert_awaited_once()
            replacement.close.assert_not_awaited()
            self.assertEqual(pool.nodes, [replacement])
        finally:
            release.set()
            await asyncio.gather(adding, return_exceptions=True)
            await pool.close()

    async def test_label_to_node_is_read_only_and_reflects_pool_changes(self) -> None:
        """The public view follows additions and removals without allowing writes."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        view = pool.label_to_node
        attempted_write = cast("dict[str, Node[Client]]", view)

        with self.assertRaises(TypeError):
            attempted_write["MAIN"] = cast("Node[Client]", node)
        await pool.add_node(cast("Node[Client]", node))
        self.assertIs(view["MAIN"], node)
        with self.assertRaises(TypeError):
            del attempted_write["MAIN"]
        await pool.remove_node(cast("Node[Client]", node), transfer_players=False)
        self.assertEqual(view, {})
        await pool.close()

    async def test_cancelled_addition_releases_its_label(self) -> None:
        """A cancelled connection must not reserve the label forever."""
        client = _make_client()
        pool = self._pool(client)
        first = _make_node(client, "MAIN")
        second = _make_node(client, "MAIN")
        entered = asyncio.Event()

        async def connect(*, player_cls: object = None) -> None:
            self.assertIsNone(player_cls)
            entered.set()
            await asyncio.Event().wait()

        first.connect.side_effect = connect
        adding = asyncio.create_task(pool.add_node(cast("Node[Client]", first)))
        await asyncio.wait_for(entered.wait(), 2)
        adding.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await adding

        await pool.add_node(cast("Node[Client]", second))
        self.assertEqual(pool.nodes, [second])
        await pool.close()

    async def test_close_refuses_node_changes_until_it_finishes(self) -> None:
        """A closing pool cannot start a new add or removal."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_close() -> None:
            entered.set()
            await release.wait()

        node.close.side_effect = slow_close
        closing = asyncio.create_task(pool.close())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with self.assertRaisesRegex(RuntimeError, "closing"):
                await pool.remove_node(cast("Node[Client]", node))
            with self.assertRaisesRegex(RuntimeError, "closing"):
                await pool.add_node(cast("Node[Client]", _make_node(client, "NEW")))
        finally:
            release.set()
            await asyncio.wait_for(closing, 2)

    async def test_close_waits_for_pending_removal(self) -> None:
        """A removed node remains owned until its teardown has completed."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))
        entered, release = asyncio.Event(), asyncio.Event()

        async def quiesce() -> None:
            entered.set()
            await release.wait()

        node.quiesce.side_effect = quiesce
        removing = asyncio.create_task(
            pool.remove_node(cast("Node[Client]", node), transfer_players=False)
        )
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            closing = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertIs(pool_for(client), pool)
            self.assertTrue(pool.closing)
            self.assertNotIn("MAIN", pool.label_to_node)
            self.assertIn(node, pool.managed_nodes)
            release.set()
            await asyncio.wait_for(removing, 2)
            await asyncio.wait_for(closing, 2)
            node.close.assert_awaited_once()
            self.assertIsNone(pool_for(client))
            self.assertFalse(pool.closing)
            self.assertEqual(pool.managed_nodes, ())
        finally:
            release.set()
            await asyncio.gather(removing, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            if pool_for(client) is pool:
                await pool.close()

    async def test_close_retries_a_node_whose_removal_failed_to_close(self) -> None:
        """A failed removal cannot free the client while its node stays open."""
        client = _make_client()
        pool = self._pool(client)
        retiring = _make_node(client, "retiring")
        other = _make_node(client, "other")
        await pool.add_node(cast("Node[Client]", retiring))
        await pool.add_node(cast("Node[Client]", other))
        entered, release = asyncio.Event(), asyncio.Event()

        async def quiesce() -> None:
            entered.set()
            await release.wait()

        retiring.quiesce.side_effect = quiesce
        retiring.close.side_effect = [
            RuntimeError("removal close failed"),
            RuntimeError("pool close failed"),
            None,
        ]
        removing = asyncio.create_task(
            pool.remove_node(cast("Node[Client]", retiring), transfer_players=False)
        )
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            closing = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            release.set()
            with self.assertRaisesRegex(RuntimeError, "removal close failed"):
                await removing
            with self.assertRaisesRegex(RuntimeError, "pool close failed"):
                await closing
            other.close.assert_awaited_once()
            self.assertIs(pool_for(client), pool)
            self.assertFalse(pool.closing)
            self.assertIn(retiring, pool.failed_nodes)
            self.assertIn(retiring, pool.managed_nodes)
            with self.assertRaises(NodePoolAlreadyBound):
                self._pool(client)

            await pool.close()
            self.assertEqual(retiring.close.await_count, 3)
            self.assertIsNone(pool_for(client))
        finally:
            release.set()
            await asyncio.gather(removing, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            if pool_for(client) is pool:
                await pool.close()

    async def test_close_from_add_callback_does_not_wait_for_itself(self) -> None:
        """An addition cannot directly wait for its own pool to close."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")

        async def connect(*, player_cls: object = None) -> None:
            self.assertIsNone(player_cls)
            with self.assertRaisesRegex(RuntimeError, "own operations"):
                await pool.close()

        node.connect.side_effect = connect
        adding = asyncio.create_task(pool.add_node(cast("Node[Client]", node)))
        await self._wait_without_deadlock(adding, pool)
        await pool.close()
        node.close.assert_awaited_once()

    async def test_close_from_removal_callback_does_not_wait_for_itself(self) -> None:
        """The owned removal task cannot directly wait for its own completion."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))

        async def quiesce() -> None:
            with self.assertRaisesRegex(RuntimeError, "own operations"):
                await pool.close()

        node.quiesce.side_effect = quiesce
        removing = asyncio.create_task(
            pool.remove_node(cast("Node[Client]", node), transfer_players=False)
        )
        await self._wait_without_deadlock(removing, pool)
        await pool.close()
        node.close.assert_awaited_once()

    async def test_node_close_callback_is_tracked_for_direct_reentry(self) -> None:
        """The same reentry guard covers the task closing each node."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        await pool.add_node(cast("Node[Client]", node))

        async def recursive_close() -> None:
            caller = asyncio.current_task()
            self.assertTrue(
                any(owner is caller for owner, _ in pool._pending_operations.values())
            )

        node.close.side_effect = recursive_close
        await pool.close()
        node.close.assert_awaited_once()
        self.assertIsNone(pool_for(client))

    async def test_child_can_close_after_the_originating_add_finishes(self) -> None:
        """A descendant can close the pool after the originating add finishes."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        release = asyncio.Event()
        closer: asyncio.Task[None] | None = None

        async def later_close() -> None:
            await release.wait()
            await pool.close()

        async def connect(*, player_cls: object = None) -> None:
            nonlocal closer
            self.assertIsNone(player_cls)
            closer = asyncio.create_task(later_close())

        node.connect.side_effect = connect
        await pool.add_node(cast("Node[Client]", node))
        self.assertIsNotNone(closer)
        release.set()
        if closer is not None:
            await asyncio.wait_for(closer, 2)
        node.close.assert_awaited_once()
        self.assertIsNone(pool_for(client))

    async def test_independent_child_can_close_during_addition(self) -> None:
        """An independently scheduled closer waits for its originating add."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        closer: asyncio.Task[None] | None = None

        async def connect(*, player_cls: object = None) -> None:
            nonlocal closer
            self.assertIsNone(player_cls)
            closer = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            self.assertFalse(closer.done())

        node.connect.side_effect = connect
        await pool.add_node(cast("Node[Client]", node))
        self.assertIsNotNone(closer)
        if closer is not None:
            await asyncio.wait_for(closer, 2)
        node.close.assert_awaited_once()
        self.assertIsNone(pool_for(client))

    async def test_close_waits_for_the_addition_not_its_callers_task(self) -> None:
        """The task that added a node may itself await the shared close."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        entered, release = asyncio.Event(), asyncio.Event()

        async def connect(*, player_cls: object = None) -> None:
            self.assertIsNone(player_cls)
            entered.set()
            await release.wait()

        async def add_then_close() -> None:
            await pool.add_node(cast("Node[Client]", node))
            await pool.close()

        node.connect.side_effect = connect
        caller = asyncio.create_task(add_then_close())
        other: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            other = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            self.assertFalse(other.done())
            release.set()
            await self._wait_without_deadlock(caller, pool)
            await asyncio.wait_for(other, 2)
            node.close.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(caller, return_exceptions=True)
            if other is not None:
                await asyncio.gather(other, return_exceptions=True)

    async def test_add_node_rejects_a_foreign_client(self) -> None:
        """A node created for another client cannot join this pool."""
        first_client, second_client = _make_client(), _make_client()
        pool = self._pool(first_client)
        foreign = _make_node(second_client, "MAIN")

        with self.assertRaises(ValueError):
            await pool.add_node(cast("Node[Client]", foreign))

        foreign.connect.assert_not_awaited()
        self.assertEqual(pool.nodes, [])

    async def test_remove_node_rejects_a_foreign_client(self) -> None:
        """Removing from one pool must not stop another client's node."""
        first_client, second_client = _make_client(), _make_client()
        first = self._pool(first_client)
        second = self._pool(second_client)
        foreign = _make_node(second_client, "MAIN")
        second._nodes["MAIN"] = cast("Node[Client]", foreign)

        with self.assertRaises(ValueError):
            await first.remove_node(cast("Node[Client]", foreign))

        foreign.quiesce.assert_not_awaited()
        foreign.close.assert_not_awaited()
        self.assertEqual(second.nodes, [foreign])

    async def test_cancelled_close_finishes_all_nodes_and_shares_work(self) -> None:
        """Repeated cancellation cannot strand nodes or another close caller."""
        client = _make_client()
        pool = self._pool(client)
        first = _make_node(client, "first")
        second = _make_node(client, "second")
        pool._nodes.update(
            first=cast("Node[Client]", first), second=cast("Node[Client]", second)
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_close() -> None:
            entered.set()
            await release.wait()

        first.close.side_effect = slow_close
        cancelled = asyncio.create_task(pool.close())
        await asyncio.wait_for(entered.wait(), 2)
        other = asyncio.create_task(pool.close())
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)

        self.assertFalse(cancelled.done())
        self.assertIs(pool_for(client), pool)
        with self.assertRaises(NodePoolAlreadyBound):
            self._pool(client)

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, 2)
        await asyncio.wait_for(other, 2)

        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        self.assertIsNone(pool_for(client))

    async def test_failed_close_attempts_all_nodes_and_can_be_retried(self) -> None:
        """A close error keeps the client bound while the other nodes stop."""
        client = _make_client()
        pool = self._pool(client)
        first = _make_node(client, "first")
        second = _make_node(client, "second")
        pool._nodes.update(
            first=cast("Node[Client]", first), second=cast("Node[Client]", second)
        )
        first.close.side_effect = [RuntimeError("close failed"), None]

        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await pool.close()

        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        self.assertIs(pool_for(client), pool)
        with self.assertRaises(NodePoolAlreadyBound):
            self._pool(client)

        await pool.close()

        self.assertEqual(first.close.await_count, 2)
        self.assertEqual(second.close.await_count, 2)
        self.assertIsNone(pool_for(client))

    async def test_cancelled_close_preserves_binding_when_node_fails(self) -> None:
        """Cancellation does not hide failure from another waiter or free the client."""
        client = _make_client()
        pool = self._pool(client)
        node = _make_node(client, "MAIN")
        pool._nodes["MAIN"] = cast("Node[Client]", node)
        entered, release = asyncio.Event(), asyncio.Event()

        async def fail_close() -> None:
            entered.set()
            await release.wait()
            msg = "close failed"
            raise RuntimeError(msg)

        node.close.side_effect = fail_close
        cancelled = asyncio.create_task(pool.close())
        await asyncio.wait_for(entered.wait(), 2)
        other = asyncio.create_task(pool.close())
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        release.set()

        with (
            self.assertLogs("mafic.utils.tasks", level="ERROR"),
            self.assertRaises(asyncio.CancelledError),
        ):
            await asyncio.wait_for(cancelled, 2)
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await asyncio.wait_for(other, 2)
        self.assertIs(pool_for(client), pool)

    def test_registry_does_not_keep_pools_or_clients_alive(self) -> None:
        """Neither the pool nor its client may outlive the caller's references."""
        client = _make_client()
        pool = self._pool(client)
        pool_ref = weakref.ref(pool)
        client_ref = weakref.ref(client)
        self.assertIs(pool_for(client), pool)

        del pool
        del client
        gc.collect()

        self.assertIsNone(pool_ref())
        self.assertIsNone(client_ref())
