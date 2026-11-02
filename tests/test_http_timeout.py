"""Regression tests for node HTTP timeout configuration."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from mafic.__libraries import Client
from mafic.node import Node
from mafic.pool import NodePool


class HttpTimeoutTests(IsolatedAsyncioTestCase):
    """Verify explicit and default timeout semantics for internal sessions."""

    def make_node(self, timeout: aiohttp.ClientTimeout | None = None) -> Node[Client]:
        """Create a node without opening a connection."""
        client = cast("Client", Mock(spec=Client))
        return Node(
            host="localhost",
            port=2333,
            label="timeout",
            password=self.id(),
            client=client,
            http_timeout=timeout,
        )

    async def test_none_uses_aiohttp_session_defaults(self) -> None:
        """None leaves aiohttp's real defaults, including sock_connect=30."""
        node = self.make_node()
        self.assertIsNone(node._http_timeout)

        session = await node._create_session()
        try:
            self.assertEqual(session.timeout.total, 300)
            self.assertIsNone(session.timeout.connect)
            self.assertEqual(session.timeout.sock_connect, 30)
            self.assertIsNone(session.timeout.sock_read)
            self.assertEqual(session.timeout.ceil_threshold, 5)
        finally:
            await session.close()

    async def test_explicit_timeout_is_used_for_internal_session(self) -> None:
        """An explicit ClientTimeout is forwarded to the created session."""
        timeout = aiohttp.ClientTimeout(total=12, sock_connect=3, sock_read=7)
        node = self.make_node(timeout)
        self.assertIs(node._http_timeout, timeout)

        session = await node._create_session()
        try:
            self.assertEqual(session.timeout.total, 12)
            self.assertEqual(session.timeout.sock_connect, 3)
            self.assertEqual(session.timeout.sock_read, 7)
        finally:
            await session.close()

    async def test_total_none_explicitly_disables_the_total_deadline(self) -> None:
        """ClientTimeout(total=None) is the explicit opt-out, not None input."""
        timeout = aiohttp.ClientTimeout(total=None)
        node = self.make_node(timeout)
        self.assertIs(node._http_timeout, timeout)
        self.assertIsNone(timeout.total)

        session = await node._create_session()
        try:
            self.assertIsNone(session.timeout.total)
        finally:
            await session.close()

    async def test_create_node_forwards_http_timeout(self) -> None:
        """NodePool.create_node passes the timeout to the constructed node."""
        timeout = aiohttp.ClientTimeout(total=15)
        nodes: dict[str, Node[Client]] = {}
        with (
            patch.object(NodePool, "_nodes", nodes),
            patch.object(Node, "connect", new=AsyncMock()),
        ):
            pool = NodePool(cast("Client", Mock(spec=Client)))
            node = await pool.create_node(
                host="localhost",
                port=2333,
                label="forwarded",
                password=self.id(),
                http_timeout=timeout,
            )

        self.assertIs(node._http_timeout, timeout)
        self.assertIs(nodes["forwarded"], node)
