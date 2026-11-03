"""Regression tests for planned transport shutdown through Node.quiesce."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from mafic.errors import NodeQuiesced
from mafic.node import Node

if TYPE_CHECKING:
    from aiohttp import ClientWebSocketResponse

    from mafic.__libraries import Client


class _FakeWebSocket:
    """Stand in for a connected websocket that only receives."""

    def __init__(self) -> None:
        self.closed = False
        self.close_code = 1000
        self.receiving = asyncio.Event()

    async def receive(self) -> object:
        """Block like an idle websocket until the listener is cancelled."""
        self.receiving.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        """Mark the socket closed."""
        self.closed = True


class NodeQuiesceTests(IsolatedAsyncioTestCase):
    """Exercise planned shutdown against real tasks and REST sessions."""

    def _make_node(
        self, *, session: aiohttp.ClientSession | None = None
    ) -> Node[Client]:
        client = Mock()
        client.dispatch = Mock()
        return Node(
            host="127.0.0.1",
            port=2333,
            label="quiesce",
            password=f"test-{self.id()}",
            client=cast("Client", client),
            session=session,
        )

    def _attach_listener(
        self, node: Node[Client]
    ) -> tuple[_FakeWebSocket, asyncio.Task[None]]:
        ws = _FakeWebSocket()
        node._ws = cast("ClientWebSocketResponse", ws)
        listener = asyncio.create_task(node._ws_listener())
        node._ws_task = listener
        return ws, listener

    async def test_quiesce_cancels_listener_and_closes_socket(self) -> None:
        """A busy receive loop is joined and the socket is closed."""
        node = self._make_node()
        ws, listener = self._attach_listener(node)
        await asyncio.wait_for(ws.receiving.wait(), 1.0)

        await asyncio.wait_for(node.quiesce(), 1.0)

        self.assertTrue(listener.done())
        self.assertTrue(ws.closed)
        self.assertIsNone(node._ws)
        self.assertFalse(node.available)

    async def test_quiesce_publishes_no_loss_and_starts_no_reconnect(self) -> None:
        """A planned shutdown must not look like an accidental websocket loss."""
        node = self._make_node()
        ws, _listener = self._attach_listener(node)
        await asyncio.wait_for(ws.receiving.wait(), 1.0)

        with patch.object(Node, "_start_connect") as start_connect:
            await asyncio.wait_for(node.quiesce(), 1.0)

        start_connect.assert_not_called()
        dispatch = cast("Mock", node._client.dispatch)
        dispatch.assert_not_called()

    async def test_quiesce_cancels_a_reconnect_waiting_in_rest(self) -> None:
        """A reconnect inside an HTTP call is cancelled and joined."""
        received = asyncio.Event()

        async def hanging(_request: web.Request) -> web.Response:
            received.set()
            await asyncio.sleep(3600)
            return web.Response()

        app = web.Application()
        app.router.add_get("/v4/players", hanging)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        session = aiohttp.ClientSession()
        self.addAsyncCleanup(session.close)
        node = self._make_node(session=session)

        async def reconnect() -> None:
            async with session.get(server.make_url("/v4/players")) as response:
                await response.read()

        node._connect_task = asyncio.create_task(reconnect())
        await asyncio.wait_for(received.wait(), 1.0)

        await asyncio.wait_for(node.quiesce(), 1.0)

        self.assertTrue(node._connect_task.done())
        self.assertFalse(session.closed)

    async def test_quiesce_keeps_the_session_until_close(self) -> None:
        """Quiesce leaves REST usable; close still owns session teardown."""
        node = self._make_node()
        session = await node._create_session()
        cast("Any", node)._Node__session = session

        await node.quiesce()
        self.assertFalse(session.closed)

        await node.close()
        self.assertTrue(session.closed)

    async def test_quiesce_never_closes_a_caller_supplied_session(self) -> None:
        """A session Mafic does not own stays open through close."""
        session = aiohttp.ClientSession()
        self.addAsyncCleanup(session.close)
        node = self._make_node(session=session)

        await node.quiesce()
        await node.close()

        self.assertFalse(session.closed)

    async def test_quiesce_is_single_flight(self) -> None:
        """Concurrent callers share one transport shutdown."""
        node = self._make_node()
        ws, _listener = self._attach_listener(node)
        await asyncio.wait_for(ws.receiving.wait(), 1.0)
        close = AsyncMock(wraps=ws.close)
        cast("Any", ws).close = close

        await asyncio.gather(node.quiesce(), node.quiesce(), node.quiesce())

        close.assert_awaited_once()

    async def test_quiesce_survives_repeated_caller_cancellation(self) -> None:
        """Owned shutdown finishes even when its caller is cancelled twice."""
        node = self._make_node()
        ws, _listener = self._attach_listener(node)
        await asyncio.wait_for(ws.receiving.wait(), 1.0)
        entered, release = asyncio.Event(), asyncio.Event()

        shutdown_finished = False

        async def blocked_close() -> None:
            nonlocal shutdown_finished
            entered.set()
            await release.wait()
            ws.closed = True
            shutdown_finished = True

        cast("Any", ws).close = blocked_close
        quiescing = asyncio.create_task(node.quiesce())
        await asyncio.wait_for(entered.wait(), 1.0)
        quiescing.cancel()
        await asyncio.sleep(0)
        quiescing.cancel()
        await asyncio.sleep(0)
        release.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(quiescing, 1.0)
        self.assertTrue(shutdown_finished)

    async def test_quiesce_then_close_finishes_without_error(self) -> None:
        """Concurrent planned shutdown and terminal close stay consistent."""
        node = self._make_node()
        ws, _listener = self._attach_listener(node)
        await asyncio.wait_for(ws.receiving.wait(), 1.0)

        await asyncio.wait_for(asyncio.gather(node.quiesce(), node.close()), 2.0)

        self.assertTrue(ws.closed)
        self.assertIsNone(node._ws)
        self.assertFalse(node._quiesced)

    async def test_connect_is_refused_until_close_resets_quiesce(self) -> None:
        """A quiesced node needs a close cycle before it may connect again."""
        node = self._make_node()
        await node.quiesce()

        with self.assertRaises(NodeQuiesced):
            await node.connect()

        await node.close()
        self.assertFalse(node._quiesced)
        with patch.object(Node, "_start_connect", new=AsyncMock()):
            await node.connect()
