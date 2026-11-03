"""Tests for per-client NodePool scope and registry lifetime."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import gc
import weakref
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from mafic.errors import NodePoolAlreadyBound
from mafic.pool import NodePool, pool_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mafic.__libraries import Client
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

    async def test_pool_can_be_recreated_after_close(self) -> None:
        """A closed pool releases its client for a new one."""
        client = _make_client()
        first = self._pool(client)
        await first.close()

        second = self._pool(client)

        self.assertIs(pool_for(client), second)

    async def test_add_node_rejects_a_foreign_client(self) -> None:
        """A node created for another client cannot join this pool."""
        first_client, second_client = _make_client(), _make_client()
        pool = self._pool(first_client)
        foreign = _make_node(second_client, "MAIN")

        with self.assertRaises(ValueError):
            await pool.add_node(cast("Node[Client]", foreign))

        foreign.connect.assert_not_awaited()
        self.assertEqual(pool.nodes, [])

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
