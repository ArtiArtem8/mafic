"""Regression tests for node transport loss, retries and shutdown."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from mafic.__libraries import Client
from mafic.errors import NodeAlreadyConnected
from mafic.node import Node
from mafic.player import Player
from mafic.pool import NodePool


class NodeLifecycleTests(IsolatedAsyncioTestCase):
    """Exercise the real client against a local Lavalink protocol fixture."""

    async def asyncSetUp(self) -> None:
        """Start a disposable HTTP/WebSocket server without Discord credentials."""
        self.accept_connections = True
        self.send_ready = True
        self.hold_players = False
        self.players_requested = asyncio.Event()
        self.release_players = asyncio.Event()
        self.attempts: asyncio.Queue[None] = asyncio.Queue()
        self.events: asyncio.Queue[str] = asyncio.Queue()
        self.retries: asyncio.Queue[None] = asyncio.Queue()
        self.retry_permits: asyncio.Queue[None] = asyncio.Queue()
        self.sockets: list[web.WebSocketResponse] = []
        self.sessions: set[str] = set()
        self.player_sessions: list[str] = []
        app = web.Application()
        app.router.add_get("/version", self.version)
        app.router.add_get("/v4/websocket", self.websocket)
        app.router.add_patch("/v4/sessions/{session}", self.configure)
        app.router.add_get("/v4/sessions/{session}/players", self.players)
        self.server = TestServer(app)
        await self.server.start_server()
        self.client = Mock(spec=Client)
        self.client.user = Mock(id=123)
        self.client.wait_until_ready = AsyncMock()

        def dispatch(event: str, _: Node[Client]) -> None:
            self.events.put_nowait(event)

        self.client.dispatch.side_effect = dispatch
        self.node = Node(
            host=self.server.host,
            port=cast("int", self.server.make_url("/").port),
            label="test",
            password="test-password",  # noqa: S106
            client=cast("Client", self.client),
            timeout=1,
        )
        self.sleep_patch = patch("mafic.node.sleep", side_effect=self.retry_sleep)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    async def asyncTearDown(self) -> None:
        """Ensure a failed test cannot leave reconnect tasks or sockets running."""
        self.release_players.set()
        await self.node.close()
        await self.server.close()

    async def version(self, _: web.Request) -> web.Response:
        """Return the version expected by this fork."""
        return web.Response(text="4.2.2")

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Create a fresh server session, or simulate a server still restarting."""
        if not self.accept_connections:
            self.attempts.put_nowait(None)
            raise web.HTTPServiceUnavailable
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        session_id = f"session-{len(self.sockets)}"
        self.sessions.add(session_id)
        self.attempts.put_nowait(None)
        if self.send_ready:
            await ws.send_json(
                {"op": "ready", "resumed": False, "sessionId": session_id}
            )
        async for _ in ws:
            pass
        return ws

    async def configure(self, request: web.Request) -> web.Response:
        """Accept resuming configuration only for a live session."""
        if request.match_info["session"] not in self.sessions:
            raise web.HTTPNotFound(text="Session not found")
        return web.json_response({"resuming": True, "timeout": 60})

    async def players(self, request: web.Request) -> web.Response:
        """Return no players after a simulated server restart."""
        session_id = request.match_info["session"]
        self.player_sessions.append(session_id)
        self.players_requested.set()
        if self.hold_players:
            await self.release_players.wait()
        if session_id not in self.sessions:
            raise web.HTTPNotFound(text="Session not found")
        return web.json_response([])

    async def pending_message(self) -> None:
        """Represent an old connection's message handler awaiting I/O."""
        await asyncio.Event().wait()

    async def retry_sleep(self, _: float) -> None:
        """Control retries with gates instead of timing-dependent sleeps."""
        self.retries.put_nowait(None)
        await self.retry_permits.get()

    async def next_event(self, expected: str) -> None:
        """Wait for exactly the next dispatched lifecycle event."""
        self.assertEqual(await asyncio.wait_for(self.events.get(), 2), expected)

    async def allow_retry(self) -> None:
        """Release one backoff wait."""
        await asyncio.wait_for(self.retries.get(), 2)
        self.retry_permits.put_nowait(None)

    async def test_restart_retries_until_server_returns(self) -> None:
        """A real CLOSE frame clears availability and retries past a failed attempt."""
        await self.node.connect()
        await self.next_event("node_ready")
        await self.attempts.get()
        self.sessions.clear()
        self.accept_connections = False
        await self.sockets[0].close(code=1001)
        await self.next_event("node_unavailable")
        self.assertFalse(self.node.available)
        self.assertFalse(self.node._event_queue.is_set())
        await self.allow_retry()
        await asyncio.wait_for(self.attempts.get(), 2)
        await asyncio.wait_for(self.retries.get(), 2)
        self.assertFalse(self.node.available)
        self.assertTrue(self.events.empty())
        self.accept_connections = True
        self.retry_permits.put_nowait(None)
        await self.next_event("node_ready")
        self.assertTrue(self.node.available)
        self.assertEqual(self.node.session_id, "session-2")
        await self.node.sync_players()
        self.assertEqual(self.player_sessions, ["session-1", "session-2", "session-2"])

    async def test_initial_failure_does_not_register_unready_node(self) -> None:
        """NodePool waits through failures and retains the requested player class."""
        self.accept_connections = False
        nodes: dict[str, Node[Client]] = {}
        with (
            patch.object(NodePool, "_nodes", nodes),
            patch.object(NodePool, "_client", None),
            patch.object(NodePool, "_default_strategies", [], create=True),
            patch.object(Node, "sync_players", new_callable=AsyncMock) as sync,
        ):
            pool = NodePool(cast("Client", self.client))
            task = asyncio.create_task(pool.add_node(self.node, player_cls=Player))
            try:
                await asyncio.wait_for(self.retries.get(), 2)
                self.assertFalse(task.done())
                self.assertFalse(self.node.available)
                self.assertIsNone(self.node._ws_task)
                self.assertEqual(pool.nodes, [])
                with self.assertRaises(NodeAlreadyConnected):
                    await self.node.connect()
                self.accept_connections = True
                self.retry_permits.put_nowait(None)
                await asyncio.wait_for(task, 2)
                sync.assert_awaited_once_with(player_cls=Player)
                self.assertIn(self.node, pool.nodes)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_close_during_initial_retry_joins_tasks(self) -> None:
        """Closing cancels public connect and joins its retry task."""
        self.accept_connections = False
        connect = asyncio.create_task(self.node.connect())
        await asyncio.wait_for(self.retries.get(), 2)
        await self.node.close()
        with self.assertRaises(asyncio.CancelledError):
            await connect
        self.assertIsNone(self.node._connect_task)
        self.assertIsNone(self.node._ws_task)
        self.assertFalse(self.node.available)
        self.assertTrue(self.events.empty())

    async def test_close_during_reconnect_backoff(self) -> None:
        """The bot can remove the old node while its listener is waiting to retry."""
        await self.node.connect()
        await self.next_event("node_ready")
        await self.sockets[0].close()
        await self.next_event("node_unavailable")
        await asyncio.wait_for(self.retries.get(), 2)
        listener = self.node._ws_task
        with self.assertRaises(NodeAlreadyConnected):
            await self.node.connect()
        await self.node.close()
        self.assertIsNotNone(listener)
        if listener is not None:
            self.assertTrue(listener.done())
        self.assertIsNone(self.node._connect_task)
        self.assertIsNone(self.node._ws)
        self.assertFalse(self.node.available)
        self.assertEqual(len(self.sockets), 1)

    async def test_close_after_failed_background_reconnect(self) -> None:
        """A reconnect worker remains cancellable after its first failed attempt."""
        await self.node.connect()
        await self.next_event("node_ready")
        await self.attempts.get()
        self.accept_connections = False
        await self.sockets[0].close()
        await self.next_event("node_unavailable")
        await self.allow_retry()
        await asyncio.wait_for(self.attempts.get(), 2)
        await asyncio.wait_for(self.retries.get(), 2)
        reconnect = self.node._connect_task
        await self.node.close()
        self.assertIsNotNone(reconnect)
        if reconnect is not None:
            self.assertTrue(reconnect.cancelled())
        self.assertIsNone(self.node._connect_task)
        self.assertFalse(self.node.available)
        self.assertTrue(self.events.empty())

    async def test_unavailable_handler_can_close_node_immediately(self) -> None:
        """Match a bot that disposes the old node on node_unavailable."""
        close_task: asyncio.Task[None] | None = None

        def dispatch(event: str, _: Node[Client]) -> None:
            nonlocal close_task
            self.events.put_nowait(event)
            if event == "node_unavailable":
                close_task = asyncio.create_task(self.node.close())

        self.client.dispatch.side_effect = dispatch
        await self.node.connect()
        await self.next_event("node_ready")
        await self.sockets[0].close()
        await self.next_event("node_unavailable")
        self.assertIsNotNone(close_task)
        if close_task is not None:
            await asyncio.wait_for(close_task, 2)
        self.assertIsNone(self.node._connect_task)
        self.assertIsNone(self.node._ws_task)
        self.assertFalse(self.node.available)
        self.assertEqual(len(self.sockets), 1)

    async def test_background_reconnect_retains_player_class(self) -> None:
        """Retries pass the caller's player class to session synchronization."""
        with patch.object(Node, "sync_players", new_callable=AsyncMock) as sync:
            await self.node.connect(player_cls=Player)
            await self.next_event("node_ready")
            await self.sockets[0].close()
            await self.next_event("node_unavailable")
            await self.allow_retry()
            await self.next_event("node_ready")
            self.assertEqual(sync.await_count, 2)
            for call in sync.await_args_list:
                self.assertEqual(call.kwargs, {"player_cls": Player})

    async def test_planned_close_does_not_dispatch_loss(self) -> None:
        """Local shutdown cancels receive before aiohttp emits CLOSING."""
        await self.node.connect()
        await self.next_event("node_ready")
        listener = self.node._ws_task
        await self.node.close()
        self.assertTrue(self.events.empty())
        self.assertTrue(self.retries.empty())
        self.assertFalse(self.node._msg_tasks)
        if listener is not None:
            self.assertTrue(listener.done())

    async def test_loss_before_ready_keeps_connect_pending(self) -> None:
        """A pre-ready disconnect retries without publishing a false ready event."""
        self.send_ready = False
        connect = asyncio.create_task(self.node.connect())
        try:
            await asyncio.wait_for(self.attempts.get(), 2)
            # The server has prepared the socket before yielding its handler.
            await self.sockets[0].close()
            await self.next_event("node_unavailable")
            await asyncio.wait_for(self.retries.get(), 2)
            self.assertFalse(connect.done())
            self.assertFalse(self.node.available)
            self.send_ready = True
            self.retry_permits.put_nowait(None)
            await asyncio.wait_for(connect, 2)
            await self.next_event("node_ready")
        finally:
            connect.cancel()
            await asyncio.gather(connect, return_exceptions=True)

    async def test_loss_during_sync_never_publishes_stale_ready(self) -> None:
        """A REST sync belonging to a lost socket cannot make the node available."""
        self.hold_players = True
        connect = asyncio.create_task(self.node.connect())
        try:
            await asyncio.wait_for(self.players_requested.wait(), 2)
            self.sessions.clear()
            await self.sockets[0].close()
            await self.next_event("node_unavailable")
            await asyncio.wait_for(self.retries.get(), 2)
            self.release_players.set()
            self.hold_players = False
            self.assertFalse(self.node.available)
            self.assertTrue(self.events.empty())
            self.retry_permits.put_nowait(None)
            await asyncio.wait_for(connect, 2)
            await self.next_event("node_ready")
            self.assertEqual(self.node.session_id, "session-2")
        finally:
            connect.cancel()
            await asyncio.gather(connect, return_exceptions=True)

    async def test_ready_timeout_cleans_up_listener(self) -> None:
        """An established socket without ready times out and leaves no live listener."""
        self.send_ready = False
        self.node._timeout = 0.05
        with self.assertRaises(TimeoutError):
            await self.node.connect()
        self.assertFalse(self.node.available)
        self.assertIsNone(self.node._ws)
        self.assertFalse(self.node._msg_tasks)
        if self.node._ws_task is not None:
            self.assertTrue(self.node._ws_task.done())
        self.assertTrue(self.events.empty())

    async def test_permanent_handshake_errors_fail_without_retry(self) -> None:
        """Authentication/routing failures reach callers instead of looping forever."""
        for status in (400, 401, 403, 404, 302):
            with self.subTest(status=status):
                error = aiohttp.WSServerHandshakeError(
                    Mock(spec=aiohttp.RequestInfo), (), status=status
                )
                with patch.object(
                    Node, "_connect_to_websocket", side_effect=error
                ) as handshake:
                    with self.assertRaises(aiohttp.WSServerHandshakeError) as raised:
                        await asyncio.wait_for(self.node.connect(), 1)
                    self.assertIs(raised.exception, error)
                    handshake.assert_awaited_once()
                    self.assertTrue(self.retries.empty())
                    self.assertFalse(self.node.available)
                    self.assertIsNone(self.node._ws_task)

    async def test_transient_handshake_errors_keep_retrying(self) -> None:
        """Rate limits, server failures and network timeouts retain automatic retry."""
        errors = (
            aiohttp.WSServerHandshakeError(
                Mock(spec=aiohttp.RequestInfo), (), status=429
            ),
            aiohttp.WSServerHandshakeError(
                Mock(spec=aiohttp.RequestInfo), (), status=503
            ),
            aiohttp.ClientConnectionError("connection refused"),
            aiohttp.ClientPayloadError("incomplete response"),
            TimeoutError(),
        )
        for error in errors:
            with (
                self.subTest(error=error),
                patch.object(
                    Node, "_connect_to_websocket", side_effect=error
                ) as handshake,
            ):
                connect = asyncio.create_task(self.node.connect())
                try:
                    await asyncio.wait_for(self.retries.get(), 2)
                    self.assertFalse(connect.done())
                    self.assertFalse(self.node.available)
                    self.assertIsNone(self.node._ws_task)
                    handshake.assert_awaited_once()
                finally:
                    await self.node.close()
                    await asyncio.gather(connect, return_exceptions=True)

    async def test_tls_and_programming_errors_are_not_retried(self) -> None:
        """Retry does not hide certificate failures or unexpected library errors."""
        errors = (
            aiohttp.ClientSSLError(Mock(), OSError("certificate rejected")),
            aiohttp.ServerFingerprintMismatch(b"expected", b"got", "localhost", 443),
            ValueError("invalid configuration"),
        )
        for error in errors:
            with (
                self.subTest(error=type(error).__name__),
                patch.object(Node, "_connect_to_websocket", side_effect=error),
            ):
                with self.assertRaises(type(error)) as raised:
                    await asyncio.wait_for(self.node.connect(), 1)
                self.assertIs(raised.exception, error)
                self.assertTrue(self.retries.empty())
                self.assertFalse(self.node.available)

    async def test_loss_during_slow_close_cannot_publish_ready(self) -> None:
        """CLOSING invalidates ready before transport close finishes."""
        loss = asyncio.Event()
        close_started = asyncio.Event()
        release_close = asyncio.Event()
        ws = Mock(spec=aiohttp.ClientWebSocketResponse, closed=False)
        ready_frame = Mock(type=aiohttp.WSMsgType.TEXT)
        ready_frame.json.return_value = {
            "op": "ready",
            "resumed": True,
            "sessionId": "closing-session",
        }
        frames = iter((ready_frame, Mock(type=aiohttp.WSMsgType.CLOSING)))

        async def receive() -> Mock:
            frame = next(frames)
            if frame.type is aiohttp.WSMsgType.CLOSING:
                await loss.wait()
            return frame

        async def close() -> None:
            close_started.set()
            await release_close.wait()
            ws.closed = True

        async def handshake(**_: object) -> None:
            self.node._ws = cast("aiohttp.ClientWebSocketResponse", ws)

        async def sync(**_: object) -> None:
            loss.set()

        ws.receive = AsyncMock(side_effect=receive)
        ws.close = AsyncMock(side_effect=close)
        with (
            patch.object(Node, "_connect_to_websocket", side_effect=handshake),
            patch.object(Node, "sync_players", side_effect=sync),
        ):
            connect = asyncio.create_task(self.node.connect())
            try:
                await asyncio.wait_for(close_started.wait(), 2)
                await self.next_event("node_unavailable")
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(connect), 0.05)
                self.assertFalse(self.node.available)
                self.assertTrue(self.events.empty())
                release_close.set()
                await asyncio.wait_for(self.retries.get(), 2)
            finally:
                release_close.set()
                await self.node.close()
                await asyncio.gather(connect, return_exceptions=True)

    async def test_close_joins_listener_already_in_disconnect(self) -> None:
        """Shutdown waits for listener cleanup before taking over socket closure."""
        cleanup_started = asyncio.Event()
        ws = Mock(spec=aiohttp.ClientWebSocketResponse, closed=False)
        ws.receive = AsyncMock(return_value=Mock(type=aiohttp.WSMsgType.CLOSING))
        listener: asyncio.Task[None] | None = None

        async def pending_message() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await asyncio.Event().wait()

        async def close_socket() -> None:
            if listener is None:
                self.fail("Listener was not started")
            self.assertTrue(listener.done())
            self.assertTrue(message.done())
            ws.closed = True

        ws.close = AsyncMock(side_effect=close_socket)
        self.node._ws = cast("aiohttp.ClientWebSocketResponse", ws)
        message = asyncio.create_task(pending_message())
        self.node._msg_tasks.add(message)
        message.add_done_callback(self.node._msg_tasks.discard)
        listener = asyncio.create_task(self.node._ws_listener())
        self.node._ws_task = listener
        await asyncio.wait_for(cleanup_started.wait(), 2)
        await asyncio.wait_for(self.node.close(), 2)
        ws.close.assert_awaited_once()
        self.assertTrue(listener.cancelled())
        self.assertIsNone(self.node._ws_task)
        self.assertIsNone(self.node._connect_task)

    async def test_terminal_frames_and_receive_errors(self) -> None:
        """Every transport termination follows recovery without JSON decoding."""
        failures: tuple[aiohttp.WSMsgType | Exception, ...] = (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
            OSError("receive failed"),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                ws = Mock(spec=aiohttp.ClientWebSocketResponse)
                ws.close = AsyncMock()
                frame = Mock(type=failure)
                ws.receive = AsyncMock(
                    side_effect=failure if isinstance(failure, Exception) else None,
                    return_value=frame,
                )
                self.node._ws = cast("aiohttp.ClientWebSocketResponse", ws)
                self.node._available = True
                self.node._ready.set()
                self.node._event_queue.set()
                stale_handler = asyncio.create_task(self.pending_message())
                self.node._msg_tasks.add(stale_handler)
                stale_handler.add_done_callback(self.node._msg_tasks.discard)
                listener = asyncio.create_task(self.node._ws_listener())
                self.node._ws_task = listener
                await self.next_event("node_unavailable")
                await asyncio.wait_for(self.retries.get(), 2)
                self.assertFalse(self.node.available)
                self.assertFalse(self.node._ready.is_set())
                self.assertFalse(self.node._event_queue.is_set())
                self.assertTrue(stale_handler.cancelled())
                frame.json.assert_not_called()
                await self.node.close()
                self.assertTrue(listener.done())
