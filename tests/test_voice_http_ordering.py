"""HTTP-level tests for ordering PATCH and DELETE on one player slot."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from mafic.__libraries import Client, VoiceChannel
from mafic.node import Node
from mafic.player import Player

from .test_player_update import player_payload
from .test_voice_ownership_poc import CHANNEL_ID, GUILD_ID, FakeClient, FakeGuild

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mafic.typings import Player as PlayerPayload

SESSION = "session-1"


class Fixture:
    """A controlled Lavalink REST surface for one player slot."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.delete_started = asyncio.Event()
        self.delete_completed = asyncio.Event()
        self.release_delete = asyncio.Event()
        self.patch_started = asyncio.Event()
        self.patch_overlapped_delete = False
        self.hold_delete = False
        self.delete_status = 204
        self.payload: PlayerPayload = player_payload()

    async def patch_player(self, _request: web.Request) -> web.Response:
        """Record a PATCH and note whether it overlapped a running DELETE."""
        self.order.append("PATCH")
        self.patch_started.set()
        if self.delete_started.is_set() and not self.delete_completed.is_set():
            self.patch_overlapped_delete = True
        return web.json_response(dict(self.payload))

    async def delete_player(self, _request: web.Request) -> web.Response:
        """Record a DELETE, optionally holding it open for the test."""
        self.order.append("DELETE")
        self.delete_started.set()
        if self.hold_delete:
            await self.release_delete.wait()
        self.delete_completed.set()
        return web.Response(status=self.delete_status)


class HttpOrderingTests(IsolatedAsyncioTestCase):
    """The lane must order real PATCH and DELETE requests on one slot."""

    async def asyncSetUp(self) -> None:
        """Start a disposable REST server and a node pointed at it."""
        self.fixture = Fixture()
        app = web.Application()
        app.router.add_patch(
            "/v4/sessions/{session}/players/{guild_id}", self.fixture.patch_player
        )
        app.router.add_delete(
            "/v4/sessions/{session}/players/{guild_id}", self.fixture.delete_player
        )
        self.server = TestServer(app)
        await self.server.start_server()

        self.client = FakeClient()
        self.guild = FakeGuild(self.client)
        self.node: Node[Client] = Node(
            host=self.server.host,
            port=cast("int", self.server.make_url("/").port),
            label="fixture",
            password="secret",  # noqa: S106
            client=cast("Client", self.client),
            timeout=1,
        )
        self.node._session_id = SESSION
        # Node.connect appends the version segment while checking /version;
        # these tests speak REST directly, so mirror that one step.
        self.node._rest_uri /= "v4"
        self.node._version = 4

    async def asyncTearDown(self) -> None:
        """Release any held request, then close node and server."""
        self.fixture.release_delete.set()
        await self.node.close()
        await self.server.close()

    def make_player(self) -> Player[Client]:
        """Build a claimed, connected player bound to the fixture node."""
        channel = Mock(spec=VoiceChannel, id=CHANNEL_ID, guild=self.guild)
        channel._get_voice_client_key.return_value = (GUILD_ID, None)
        player = Player(cast("Client", self.client), channel)
        player._node = self.node
        player._connected = True
        player._take_ownership()
        self.client._connection._add_voice_client(GUILD_ID, player)
        self.node.add_player(GUILD_ID, player)
        return player

    async def test_patch_and_delete_reach_the_slot_in_order(self) -> None:
        """A completed PATCH precedes the DELETE for the same slot."""
        player = self.make_player()

        await player.update(volume=40)
        await player.disconnect(force=True)

        self.assertEqual(self.fixture.order, ["PATCH", "DELETE"])

    async def test_patch_never_reaches_the_server_while_delete_runs(self) -> None:
        """The replacement PATCH must not overlap a DELETE on the same slot.

        The server itself decides whether the two requests overlapped, so the
        assertion does not depend on which coroutine was scheduled first.
        """
        player = self.make_player()
        self.fixture.hold_delete = True

        teardown = asyncio.create_task(player.disconnect(force=True))
        await asyncio.wait_for(self.fixture.delete_started.wait(), 2)

        replacement = self.make_player()
        patch = asyncio.create_task(replacement.update(volume=10))
        for _ in range(8):
            if self.fixture.patch_started.is_set():
                break
            await asyncio.sleep(0)

        self.assertFalse(self.fixture.patch_started.is_set())
        self.assertFalse(patch.done())
        self.assertEqual(self.fixture.order, ["DELETE"])

        self.fixture.release_delete.set()
        await asyncio.wait_for(teardown, 2)
        await asyncio.wait_for(patch, 2)

        self.assertFalse(self.fixture.patch_overlapped_delete)
        self.assertEqual(self.fixture.order, ["DELETE", "PATCH"])

    async def test_without_the_lane_the_requests_do_overlap(self) -> None:
        """Negative control: the lane, not timing, is what prevents overlap."""
        from contextlib import asynccontextmanager

        from mafic._voice_ownership import GuildVoiceCoordinator

        @asynccontextmanager
        async def no_lane(_self: object) -> AsyncGenerator[None, None]:
            yield

        def allow(_self: GuildVoiceCoordinator) -> None:
            """Accept every caller; this control removes the lane checks."""

        player = self.make_player()
        self.fixture.hold_delete = True

        with (
            patch.object(GuildVoiceCoordinator, "hold", no_lane),
            patch.object(GuildVoiceCoordinator, "require_lane", allow),
        ):
            teardown = asyncio.create_task(player.disconnect(force=True))
            await asyncio.wait_for(self.fixture.delete_started.wait(), 2)

            replacement = self.make_player()
            patch_task = asyncio.create_task(replacement.update(volume=10))
            await asyncio.wait_for(self.fixture.patch_started.wait(), 2)

            self.assertTrue(self.fixture.patch_overlapped_delete)
            self.fixture.release_delete.set()
            await asyncio.wait_for(patch_task, 2)
            await asyncio.wait_for(teardown, 2)

    async def test_timeout_status_is_recorded_as_unknown(self) -> None:
        """A 408 has no confirmed effect, so the outcome stays unresolved."""
        player = self.make_player()
        self.fixture.delete_status = 408

        await player.disconnect(force=True)

        self.assertEqual(player._coordinator.remote_unknown, 1)
        self.assertIn("leave", self.guild.calls)

    async def test_rejected_delete_is_not_recorded_as_unknown(self) -> None:
        """A 400 proves the server refused the DELETE."""
        player = self.make_player()
        self.fixture.delete_status = 400

        await player.disconnect(force=True)

        self.assertEqual(player._coordinator.remote_unknown, 0)
        self.assertIn("leave", self.guild.calls)

    async def test_absent_delete_is_not_recorded_as_unknown(self) -> None:
        """A 404 is a confirmed absence, not a lost outcome."""
        player = self.make_player()
        self.fixture.delete_status = 404

        await player.disconnect(force=True)

        self.assertEqual(player._coordinator.remote_unknown, 0)
