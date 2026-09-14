"""Tests for Lavalink 4.2 protocol alignment."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import warnings
from asyncio import Event
from collections import OrderedDict
from importlib import import_module
from time import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from yarl import URL

from mafic.__libraries import Client, Guild, StageChannel, VoiceChannel, library
from mafic.errors import PlayerNotConnected
from mafic.filter import Filter, Timescale
from mafic.ip import FailingAddress
from mafic.node import Node
from mafic.player import Player
from mafic.pool import NodePool
from mafic.stats import CPUStats
from mafic.track import Track
from mafic.warnings import UnsupportedVersionWarning

from .test_player_update import make_node, player_payload
from .test_track import track_payload

if TYPE_CHECKING:
    from mafic.__libraries import (
        Connectable,
        GuildVoiceStatePayload,
        VoiceServerUpdatePayload,
    )
    from mafic.typings import Player as PlayerPayload


class PlayerStateTests(TestCase):
    """Verify player reconstruction from the stable REST object."""

    def test_unassigned_node_is_none_without_selecting_fallback(self) -> None:
        """Inspecting an unassigned player never consults the pool."""
        player: Player[Client] = cast("Player[Client]", object.__new__(Player))
        player._node = None

        with patch.object(NodePool, "get_random_node") as fallback:
            self.assertIsNone(player.assigned_node)

        fallback.assert_not_called()

    def test_assigned_node_returns_current_assignment(self) -> None:
        """Both node properties return the exact assigned node."""
        player: Player[Client] = cast("Player[Client]", object.__new__(Player))
        node = make_node(4)
        player._node = node

        with patch.object(NodePool, "get_random_node") as fallback:
            self.assertIs(player.assigned_node, node)
            self.assertIs(player.node, node)

        fallback.assert_not_called()
        with self.assertRaises(AttributeError):
            setattr(player, "assigned_node", node)  # noqa: B010

    def test_node_fallback_does_not_assign_node(self) -> None:
        """The existing node fallback leaves the player unassigned."""
        player: Player[Client] = cast("Player[Client]", object.__new__(Player))
        player._node = None
        player._guild_id = 1
        node = make_node(4)

        with patch.object(NodePool, "get_random_node", return_value=node) as fallback:
            self.assertIs(player.node, node)
            self.assertIsNone(player.assigned_node)

        fallback.assert_called_once_with()

    def test_set_state_reads_player_state_and_track_metadata(self) -> None:
        """Sync/resume reconstruction keeps state and complete track metadata."""
        player: Player[Client] = cast("Player[Client]", object.__new__(Player))
        player._guild_id = 1

        player.set_state(player_payload("resumed"))

        self.assertTrue(player.connected)
        self.assertEqual(player.ping, 5)
        self.assertEqual(player._position, 250)
        self.assertIsNotNone(player.current)
        if player.current is None:
            self.fail("Player did not restore its current track.")
        self.assertEqual(player.current.user_data["correlation_id"], "resumed")
        self.assertEqual(player.current.plugin_info, {"album": "metadata"})


class FilterProtocolTests(TestCase):
    """Verify stable plugin filter support."""

    def test_plugin_filters_round_trip(self) -> None:
        """Plugin filter JSON is serialized and reconstructed."""
        original = Filter(
            plugin_filters={"example-plugin": {"enabled": True, "amount": 0.5}}
        )

        restored = Filter.from_payload(original.payload)

        self.assertEqual(restored.plugin_filters, original.plugin_filters)
        self.assertEqual(
            original.payload,
            {"pluginFilters": {"example-plugin": {"enabled": True, "amount": 0.5}}},
        )

    def test_zero_volume_is_serialized(self) -> None:
        """A valid zero filter volume is not mistaken for omission."""
        self.assertEqual(Filter(volume=0.0).payload, {"volume": 0.0})

    def test_zero_volume_survives_every_merge_operator(self) -> None:
        """Zero is a valid volume, so a merge must not treat it as unset."""
        cases: list[tuple[str, Filter | None]] = [
            ("or", Filter(volume=1.0) | Filter(volume=0.0)),
            ("and", Filter(volume=0.0) & Filter(volume=1.0)),
        ]

        ior_left, ior_right = Filter(volume=1.0), Filter(volume=0.0)
        ior_left |= ior_right
        cases.append(("ior", ior_left))

        iand_left, iand_right = Filter(volume=0.0), Filter(volume=1.0)
        iand_left &= iand_right
        cases.append(("iand", iand_left))

        for name, result in cases:
            with self.subTest(operator=name):
                if result is None:
                    self.fail(f"{name} did not return a filter.")
                self.assertEqual(result.volume, 0.0)
                self.assertEqual(result.payload, {"volume": 0.0})


class MiscProtocolTests(TestCase):
    """Verify corrected stats and routeplanner payload fields."""

    def test_cpu_loads_are_floats(self) -> None:
        """Lavalink CPU load fractions retain their float values."""
        stats = CPUStats({"cores": 4, "systemLoad": 0.25, "lavalinkLoad": 0.125})

        self.assertEqual(stats.system_load, 0.25)
        self.assertEqual(stats.lavalink_load, 0.125)

    def test_routeplanner_failing_address_uses_stable_field(self) -> None:
        """Routeplanner reads failingAddress from Lavalink 4."""
        address = FailingAddress(
            {
                "failingAddress": "/1.0.0.0",
                "failingTimestamp": 1_700_000_000_000,
                "failingTime": "ignored display value",
            }
        )

        self.assertEqual(address.address, "/1.0.0.0")
        self.assertEqual(address.time.year, 2023)


class NodeProtocolTests(IsolatedAsyncioTestCase):
    """Verify stable Lavalink 4.2 node routes and voice payloads."""

    async def test_voice_update_contains_non_null_channel_id(self) -> None:
        """DAVE voice updates send all four required fields."""
        node = make_node(4)
        request = AsyncMock(return_value=player_payload())

        with patch.object(Node, "_Node__request", request):
            await node.voice_update(
                guild_id=1,
                session_id="discord-session",
                data={
                    "guild_id": 1,
                    "endpoint": "voice.example.com",
                    "token": "token",
                },
                channel_id=10,
            )

        if request.await_args is None:
            self.fail("Node did not send a voice update.")
        self.assertEqual(
            request.await_args.args[2],
            {
                "voice": {
                    "sessionId": "discord-session",
                    "endpoint": "voice.example.com",
                    "token": "token",
                    "channelId": "10",
                }
            },
        )

    async def test_v4_fetch_plugins_uses_info_route(self) -> None:
        """Lavalink v4 exposes plugins through /info rather than /plugins."""
        node = make_node(4)
        request = AsyncMock(
            return_value={"plugins": [{"name": "test", "version": "1"}]}
        )

        with patch.object(Node, "_Node__request", request):
            plugins = await node.fetch_plugins()

        self.assertEqual(
            [(plugin.name, plugin.version) for plugin in plugins], [("test", "1")]
        )
        request.assert_awaited_once_with("GET", "info")

    async def test_v3_fetch_plugins_uses_unprefixed_route(self) -> None:
        """Lavalink v3 exposes plugins at /plugins, not /v3/plugins."""
        node = make_node(3)
        node._rest_uri = URL("http://localhost:2333/v3")
        request = AsyncMock(return_value=[{"name": "test", "version": "1"}])

        with patch.object(Node, "_Node__request", request):
            plugins = await node.fetch_plugins()

        self.assertEqual(
            [(plugin.name, plugin.version) for plugin in plugins], [("test", "1")]
        )
        request.assert_awaited_once_with("GET", "../plugins")

    async def test_disabled_routeplanner_returns_none(self) -> None:
        """A 204 routeplanner response maps to None."""
        node = make_node(4)
        request = AsyncMock(return_value=None)

        with patch.object(Node, "_Node__request", request):
            status = await node.fetch_route_planner_status()

        self.assertIsNone(status)

    async def test_version_4_2_is_supported_without_warning(self) -> None:
        """The version gate recognizes the audited Lavalink minor release."""

        class FakeResponse:
            async def __aenter__(self) -> FakeResponse:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def text(self) -> str:
                return "4.2.2"

        class FakeSession:
            def get(self, *_: object, **__: object) -> FakeResponse:
                return FakeResponse()

        node = make_node(3)
        node._checked_version = False
        node._rest_uri = URL("http://localhost:2333")
        node._ws_uri = URL("ws://localhost:2333")
        object.__setattr__(node, "_Node__password", "")
        object.__setattr__(node, "_Node__session", FakeSession())

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            version = await node._check_version()

        self.assertEqual(version, 4)
        self.assertFalse(
            any(isinstance(item.message, UnsupportedVersionWarning) for item in caught)
        )


class VoiceStateTests(IsolatedAsyncioTestCase):
    """Verify Discord voice state changes reach Lavalink."""

    def setUp(self) -> None:
        """Build a connected player using the selected adapter's channel types."""
        self.guild = Mock(spec=Guild, id=1)
        self.channel = Mock(spec=VoiceChannel, id=10, guild=self.guild)
        self.target = Mock(spec=VoiceChannel, id=20, guild=self.guild)
        self.guild.get_channel.side_effect = {
            10: self.channel,
            20: self.target,
        }.get
        self.sent = Event()

        async def send(*, channel: VoiceChannel | StageChannel) -> None:
            self.assertEqual(channel.guild.id, self.guild.id)
            self.sent.set()

        self.guild.change_voice_state = AsyncMock(side_effect=send)
        self.player = Player(
            cast("Client", Mock(spec=Client)),
            cast("VoiceChannel", self.channel),
            node=make_node(4),
        )
        self.guild.voice_client = self.player
        self.player._connected = True
        self.player._session_id = "discord-session"
        self.player._server_state = {
            "guild_id": 1,
            "endpoint": "voice.example.com",
            "token": "token",
        }
        self.player._voice_state_update_event.set()
        self.player._voice_server_update_event.set()
        self.player._node_player_ready_event.set()

    async def voice_state_update(self, channel_id: int) -> None:
        """Deliver the adapter's payload through the normal Mafic callback."""
        payload: object = {
            "session_id": "discord-session",
            "guild_id": "1",
            "channel_id": str(channel_id),
        }
        if library == "py-cord":
            # Pycord 2.8 dispatches payload objects with mapping compatibility.
            raw_models = import_module("discord.raw_models")
            payload = raw_models.RawVoiceStateUpdateEvent(data=payload, state=Mock())
        await self.player.on_voice_state_update(cast("GuildVoiceStatePayload", payload))

    async def test_move_waits_for_callback_and_sends_new_channel_id(self) -> None:
        """A state-only fixture uses cached server data and an unchanged session."""
        # This fixture emits no server or ready event; live Discord may emit both.
        self.player._voice_server_update_event.clear()
        self.player._node_player_ready_event.clear()
        request = AsyncMock(return_value=player_payload())
        async with asyncio.timeout(1):
            with patch.object(Node, "_Node__request", request):
                move = asyncio.create_task(self.player.move_to(self.target))
                await self.sent.wait()
                self.assertFalse(move.done())
                self.assertIs(self.player.channel, self.channel)
                request.assert_not_awaited()
                await self.voice_state_update(20)
                await move

        self.guild.change_voice_state.assert_awaited_once_with(channel=self.target)
        self.assertIs(self.player.channel, self.target)
        request.assert_awaited_once_with(
            "PATCH",
            "sessions/lavalink-session/players/1",
            {
                "voice": {
                    "sessionId": "discord-session",
                    "endpoint": "voice.example.com",
                    "token": "token",
                    "channelId": "20",
                }
            },
        )

    async def test_move_to_current_channel_is_noop(self) -> None:
        """The current channel is recognized by ID without touching events."""
        await self.player.move_to(Mock(spec=VoiceChannel, id=10, guild=self.guild))

        self.guild.change_voice_state.assert_not_awaited()
        self.assertTrue(self.player._voice_state_update_event.is_set())
        self.assertTrue(self.player._voice_server_update_event.is_set())
        self.assertTrue(self.player._node_player_ready_event.is_set())

    async def test_move_waits_for_lavalink_update_to_finish(self) -> None:
        """Updating the channel alone does not complete a pending move."""
        updating, release = Event(), Event()

        async def voice_update(**_: object) -> None:
            updating.set()
            await release.wait()

        async with asyncio.timeout(1):
            with patch.object(Node, "voice_update", side_effect=voice_update):
                move = asyncio.create_task(self.player.move_to(self.target))
                await self.sent.wait()
                callback = asyncio.create_task(self.voice_state_update(20))
                await updating.wait()
                self.assertIs(self.player.channel, self.target)
                self.assertFalse(move.done())
                release.set()
                await callback
                await move

    async def test_state_update_serializes_before_server_update(self) -> None:
        """A queued server update cannot overlap a blocked state update."""
        first_entered, release_first, server_started = Event(), Event(), Event()
        updates: list[tuple[str, str | None, object]] = []

        async def voice_update(**kwargs: object) -> None:
            data = cast("VoiceServerUpdatePayload", kwargs["data"])
            updates.append((data["token"], data["endpoint"], kwargs["channel_id"]))
            if len(updates) == 1:
                first_entered.set()
                await release_first.wait()

        async def server_update() -> None:
            server_started.set()
            await self.player.on_voice_server_update(
                {
                    "guild_id": 1,
                    "endpoint": "new.example.com",
                    "token": "new-token",
                }
            )

        self.player.node._players = {}
        with (
            patch.object(Node, "voice_update", side_effect=voice_update),
            patch("mafic.player.NodePool.get_node", return_value=self.player.node),
        ):
            state = asyncio.create_task(self.voice_state_update(20))
            await first_entered.wait()
            server = asyncio.create_task(server_update())
            await server_started.wait()
            self.assertEqual(len(updates), 1)
            self.assertFalse(server.done())
            release_first.set()
            await asyncio.gather(state, server)

        self.assertEqual(
            updates,
            [
                ("token", "voice.example.com", 20),
                ("new-token", "new.example.com", 20),
            ],
        )

    async def test_move_accepts_stage_channel(self) -> None:
        """The shared voice and stage channel surface works with every adapter."""
        self.target = Mock(spec=StageChannel, id=20, guild=self.guild)
        self.guild.get_channel.return_value = self.target
        self.guild.get_channel.side_effect = None

        async def confirm(*, channel: StageChannel) -> None:
            self.assertIs(channel, self.target)
            await self.voice_state_update(20)

        self.guild.change_voice_state.side_effect = confirm
        with patch.object(Node, "voice_update", AsyncMock()):
            await self.player.move_to(self.target, timeout=1)
        self.assertIs(self.player.channel, self.target)

    async def test_move_rejects_invalid_targets(self) -> None:
        """Invalid types and other guilds fail before changing voice state."""
        for target in (None, object()):
            with self.subTest(target=target), self.assertRaises(TypeError):
                await self.player.move_to(cast("VoiceChannel", target))

        other = Mock(spec=VoiceChannel, id=20, guild=Mock(spec=Guild, id=2))
        with self.assertRaises(ValueError):
            await self.player.move_to(other)
        self.guild.change_voice_state.assert_not_awaited()

    async def test_move_requires_connected_player(self) -> None:
        """Moving a disconnected player cannot implicitly start a connection."""
        self.player._connected = False
        self.guild.voice_client = None
        with self.assertRaises(PlayerNotConnected):
            await self.player.move_to(self.target)
        self.guild.change_voice_state.assert_not_awaited()

    async def test_move_during_lavalink_reconnection(self) -> None:
        """Discord can still move while Lavalink reports a temporary disconnect."""
        self.player._connected = False

        async def confirm(*, channel: VoiceChannel) -> None:
            await self.voice_state_update(channel.id)

        self.guild.change_voice_state.side_effect = confirm
        with patch.object(Node, "voice_update", AsyncMock()):
            await self.player.move_to(self.target, timeout=1)
        self.assertIs(self.player.channel, self.target)
        self.assertFalse(self.player.connected)

    async def test_concurrent_moves_confirm_each_transition_in_order(self) -> None:
        """The queued move sends only after the first caller has its confirmation."""
        other = Mock(spec=VoiceChannel, id=30, guild=self.guild)
        self.guild.get_channel.side_effect = {20: self.target, 30: other}.get
        queued, first_returned, second_sent = Event(), Event(), Event()

        async def send(*, channel: VoiceChannel) -> None:
            if channel.id == 20:
                self.sent.set()
            else:
                self.assertTrue(first_returned.is_set())
                second_sent.set()

        async def first_move() -> None:
            await self.player.move_to(self.target)
            first_returned.set()

        async def second_move() -> None:
            queued.set()
            await self.player.move_to(other)

        self.guild.change_voice_state.side_effect = send
        async with asyncio.timeout(1):
            with patch.object(Node, "voice_update", AsyncMock()):
                first = asyncio.create_task(first_move())
                await self.sent.wait()
                second = asyncio.create_task(second_move())
                await queued.wait()
                self.guild.change_voice_state.assert_awaited_once()
                self.assertFalse(second.done())
                await self.voice_state_update(20)
                await first
                await second_sent.wait()
                self.assertFalse(second.done())
                await self.voice_state_update(30)
                await second
        self.assertIs(self.player.channel, other)

    async def test_queued_current_channel_is_rechecked(self) -> None:
        """Moving back to the original channel cannot become an early no-op."""
        queued, second_sent = Event(), Event()

        async def send(*, channel: VoiceChannel) -> None:
            (self.sent if channel.id == 20 else second_sent).set()

        async def return_to_original() -> None:
            queued.set()
            await self.player.move_to(self.channel)

        self.guild.change_voice_state.side_effect = send
        async with asyncio.timeout(1):
            with patch.object(Node, "voice_update", AsyncMock()):
                first = asyncio.create_task(self.player.move_to(self.target))
                await self.sent.wait()
                second = asyncio.create_task(return_to_original())
                await queued.wait()
                self.assertFalse(second.done())
                await self.voice_state_update(20)
                await first
                await second_sent.wait()
                await self.voice_state_update(10)
                await second
        self.assertIs(self.player.channel, self.channel)

    async def test_queued_timeout_and_cancellation_preserve_active_move(self) -> None:
        """Leaving the lock queue cannot clear the active move's acknowledgement."""
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.setUp()
                queued, release_request = Event(), Event()

                async def send(
                    *, channel: VoiceChannel, release: Event = release_request
                ) -> None:
                    self.sent.set()
                    await release.wait()
                    await self.voice_state_update(channel.id)

                async def queued_move(signal: Event = queued) -> None:
                    signal.set()
                    await self.player.move_to(self.channel, timeout=0.01)

                self.guild.change_voice_state.side_effect = send
                async with asyncio.timeout(1):
                    with patch.object(Node, "voice_update", AsyncMock()):
                        active = asyncio.create_task(self.player.move_to(self.target))
                        await self.sent.wait()
                        waiting = asyncio.create_task(queued_move())
                        await queued.wait()
                        await self.voice_state_update(20)
                        if cancel:
                            waiting.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await waiting
                        else:
                            with self.assertRaises(asyncio.TimeoutError):
                                await waiting
                        self.assertTrue(self.player._voice_state_update_event.is_set())
                        self.guild.change_voice_state.assert_awaited_once()
                        self.assertFalse(active.done())
                        release_request.set()
                        await active
                        await self.player.move_to(self.channel, timeout=1)
                self.assertIs(self.player.channel, self.channel)
                self.assertFalse(self.player._move_lock.locked())

    async def test_queued_move_rechecks_protocol_after_disconnect(self) -> None:
        """A queued caller cannot revive a protocol removed by disconnect."""
        queued = Event()

        async def queued_move() -> None:
            queued.set()
            await self.player.move_to(self.channel)

        async with asyncio.timeout(1):
            with patch.object(Node, "voice_update", AsyncMock()):
                first = asyncio.create_task(self.player.move_to(self.target))
                await self.sent.wait()
                second = asyncio.create_task(queued_move())
                await queued.wait()
                self.guild.voice_client = None
                await self.voice_state_update(20)
                await first
                with self.assertRaises(PlayerNotConnected):
                    await second
        self.guild.change_voice_state.assert_awaited_once()

    async def test_late_server_update_is_independent_of_move_return(self) -> None:
        """Move confirmation can precede a new token at the same endpoint."""
        server_started, release_server = Event(), Event()
        self.player.node._players = {}

        async def update(**kwargs: object) -> None:
            data = cast("VoiceServerUpdatePayload", kwargs["data"])
            if data["token"] == "new-token":  # noqa: S105 - synthetic fixture
                server_started.set()
                await release_server.wait()

        async with asyncio.timeout(1):
            with patch.object(Node, "voice_update", side_effect=update):
                move = asyncio.create_task(self.player.move_to(self.target))
                await self.sent.wait()
                self.player._connected = False
                await self.voice_state_update(20)
                await move
                self.assertFalse(self.player.connected)
                # Same endpoint, new token; no new state event is needed here.
                callback = asyncio.create_task(
                    self.player.on_voice_server_update(
                        {
                            "guild_id": 1,
                            "endpoint": "voice.example.com",
                            "token": "new-token",
                        }
                    )
                )
                await server_started.wait()
                self.assertTrue(move.done())
                self.assertFalse(callback.done())
                release_server.set()
                await callback
                state = self.player._server_state
                if state is None:
                    self.fail("The server callback did not retain the new credentials.")
                self.assertEqual(state["token"], "new-token")

    async def test_timeout_does_not_break_next_move(self) -> None:
        """A late update from a timed out move cannot complete another target."""
        with self.assertRaises(asyncio.TimeoutError):
            await self.player.move_to(self.target, timeout=0.01)
        self.assertIs(self.player.channel, self.channel)
        self.assertFalse(self.player._voice_state_update_event.is_set())
        self.assertTrue(self.player._voice_server_update_event.is_set())
        self.assertTrue(self.player._node_player_ready_event.is_set())

        other = Mock(spec=VoiceChannel, id=30, guild=self.guild)
        self.guild.get_channel.side_effect = {20: self.target, 30: other}.get
        self.sent.clear()
        async with asyncio.timeout(1):
            with patch.object(Node, "voice_update", AsyncMock()):
                move = asyncio.create_task(self.player.move_to(other))
                await self.sent.wait()
                await self.voice_state_update(20)
                await asyncio.sleep(0)
                self.assertFalse(move.done())
                await self.voice_state_update(30)
                await move
        self.assertIs(self.player.channel, other)

    async def test_failed_request_does_not_break_next_move(self) -> None:
        """A failed gateway request leaves no pending synchronization behind."""
        self.guild.change_voice_state.side_effect = RuntimeError("gateway unavailable")
        with self.assertRaisesRegex(RuntimeError, "gateway unavailable"):
            await self.player.move_to(self.target)
        self.assertFalse(self.player._voice_state_update_event.is_set())
        self.assertIs(self.player.channel, self.channel)

        async def confirm(*, channel: VoiceChannel) -> None:
            await self.voice_state_update(channel.id)

        self.guild.change_voice_state.side_effect = confirm
        with patch.object(Node, "voice_update", AsyncMock()):
            await self.player.move_to(self.target, timeout=1)
        self.assertIs(self.player.channel, self.target)

    async def test_cancelled_move_does_not_break_next_move(self) -> None:
        """Cancellation releases the waiter without consuming future updates."""
        async with asyncio.timeout(1):
            move = asyncio.create_task(self.player.move_to(self.target))
            await self.sent.wait()
            move.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await move
            self.assertFalse(self.player._voice_state_update_event.is_set())

            self.sent.clear()
            with patch.object(Node, "voice_update", AsyncMock()):
                retry = asyncio.create_task(self.player.move_to(self.target))
                await self.sent.wait()
                await self.voice_state_update(20)
                await retry
        self.assertIs(self.player.channel, self.target)

    async def test_channel_move_dispatches_update_with_unchanged_session(self) -> None:
        """A voice channel move updates DAVE's required channel ID."""

        class FakeVoiceChannel:
            def __init__(self, channel_id: int) -> None:
                self.id = channel_id

        class FakeGuild:
            def get_channel(self, _: int) -> FakeVoiceChannel:
                return FakeVoiceChannel(20)

        player: Player[Client] = cast("Player[Client]", object.__new__(Player))
        player._disconnecting = False
        player._session_id = "discord-session"
        player.channel = cast("Connectable", FakeVoiceChannel(10))
        player.guild = cast("Guild", FakeGuild())
        player._voice_state_update_event = Event()
        dispatch = AsyncMock()

        channel_patch = patch("mafic.player.VoiceChannel", FakeVoiceChannel)
        dispatch_patch = patch.object(Player, "_dispatch_player_update", dispatch)
        with channel_patch, dispatch_patch:
            await player.on_voice_state_update(
                cast(
                    "GuildVoiceStatePayload",
                    {"session_id": "discord-session", "channel_id": "20"},
                )
            )

        dispatch.assert_awaited_once_with()
        self.assertEqual(cast("FakeVoiceChannel", player.channel).id, 20)


class NodeTransferTests(IsolatedAsyncioTestCase):
    """Verify target-first node migration and its failure semantics."""

    def setUp(self) -> None:
        """Build an authoritative old-node player."""
        self.guild = Mock(spec=Guild, id=1)
        self.channel = Mock(spec=VoiceChannel, id=10, guild=self.guild)
        self.guild.get_channel.side_effect = {10: self.channel}.get
        self.guild.change_voice_state = AsyncMock()
        client = Mock(spec=Client)
        client.is_closed.return_value = False
        self.client = cast("Client", client)
        self.player = Player(self.client, cast("VoiceChannel", self.channel))
        self.player._guild_id = 1
        self.player._session_id = "discord-session"
        self.player._server_state = {
            "guild_id": 1,
            "endpoint": "voice.example.com",
            "token": "token",
        }
        self.player._connected = True
        self.player._current = Track.from_data_with_info(track_payload("transfer"))
        self.player._position = 250
        self.player._last_update = int(time() * 1_000)
        self.player._paused = False
        self.player._filters = OrderedDict()

    def make_node(
        self, label: str, *, payload: PlayerPayload | None = None, version: int = 4
    ) -> SimpleNamespace:
        """Build a tiny node with an observable player mapping."""
        state = payload if payload is not None else player_payload()
        players: dict[int, Player[Client]] = {}

        def add_player(guild_id: int, player: Player[Client]) -> None:
            players[guild_id] = player

        def remove_player(guild_id: int) -> None:
            players.pop(guild_id, None)

        def get_player(guild_id: int) -> Player[Client] | None:
            return players.get(guild_id)

        return SimpleNamespace(
            label=label,
            version=version,
            _players=players,
            add_player=Mock(side_effect=add_player),
            remove_player=Mock(side_effect=remove_player),
            get_player=get_player,
            voice_update=AsyncMock(),
            update=AsyncMock(return_value=state),
            destroy=AsyncMock(),
            fetch_player=AsyncMock(return_value=state),
        )

    async def test_transfer_prepares_target_before_commit(self) -> None:
        """Target voice/state is prepared before ownership moves from A to B."""
        server_state = player_payload("server")
        old_node = self.make_node("A", payload=server_state)
        new_node = self.make_node("B", payload=server_state)
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)

        await self.player.transfer_to(cast("Node[Client]", new_node))

        self.assertIs(self.player._node, new_node)
        self.assertNotIn(1, old_node._players)
        self.assertIs(new_node._players[1], self.player)
        old_node.voice_update.assert_not_awaited()
        new_node.voice_update.assert_awaited_once_with(
            guild_id=1,
            session_id="discord-session",
            data=self.player._server_state,
            channel_id=10,
        )
        new_node.update.assert_awaited_once()
        if new_node.update.await_args is None:
            self.fail("Transfer did not restore playback state on the target.")
        update_kwargs = new_node.update.await_args.kwargs
        transferred = cast("Track", update_kwargs["track"])
        self.assertEqual(transferred.user_data["correlation_id"], "server")
        self.assertEqual(update_kwargs["position"], server_state["state"]["position"])
        self.assertEqual(update_kwargs["volume"], server_state["volume"])
        self.assertIs(update_kwargs["pause"], server_state["paused"])
        restored_filter = cast("Filter", update_kwargs["filter"])
        self.assertEqual(restored_filter.payload, server_state["filters"])
        old_node.destroy.assert_awaited_once_with(guild_id=1)
        new_node.destroy.assert_not_awaited()

    async def test_transfer_restores_rest_snapshot_not_local_state(self) -> None:
        """Transfer restoration uses the old node snapshot, not local mirrors."""
        server_state = player_payload("server")
        server_state["state"]["position"] = 25_000
        server_state["volume"] = 37
        server_state["paused"] = True
        server_state["filters"] = Filter(volume=0.5).payload
        self.player._current = Track.from_data_with_info(track_payload("local"))
        self.player._position = 1_000
        self.player._last_update = int(time() * 1_000)
        self.player._paused = False
        self.player._filters = OrderedDict({"LOCAL": Filter(volume=0.25)})
        old_node = self.make_node("A", payload=server_state)
        new_node = self.make_node("B", payload=server_state)
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)

        await self.player.transfer_to(cast("Node[Client]", new_node))

        if new_node.update.await_args is None:
            self.fail("Transfer did not restore playback state on the target.")
        update_kwargs = new_node.update.await_args.kwargs
        restored_track = cast("Track", update_kwargs["track"])
        self.assertEqual(restored_track.user_data["correlation_id"], "server")
        self.assertEqual(update_kwargs["position"], 25_000)
        self.assertEqual(update_kwargs["volume"], 37)
        self.assertTrue(update_kwargs["pause"])
        restored_filter = cast("Filter", update_kwargs["filter"])
        self.assertEqual(restored_filter.payload, server_state["filters"])
        self.assertIsNotNone(self.player._current)
        self.assertEqual(self.player._current.user_data["correlation_id"], "server")
        self.assertEqual(self.player._position, 25_000)
        self.assertEqual(self.player._volume, 37)
        self.assertTrue(self.player._paused)
        self.assertFalse(await self.player.has_filter("LOCAL"))
        self.assertTrue(await self.player.has_filter("RESUME"))
        self.assertEqual(
            self.player._filters["RESUME"].payload, server_state["filters"]
        )

    async def test_transfer_preserves_filter_labels(self) -> None:
        """Transfer keeps local filter labels while using the merged snapshot."""
        local_filters = OrderedDict(
            {
                "bassboost": Filter(volume=0.25),
                "nightcore": Filter(timescale=Timescale(speed=1.25)),
            }
        )
        merged = Filter(volume=0.25) | Filter(timescale=Timescale(speed=1.25))
        server_state = player_payload("server")
        server_state["filters"] = merged.payload
        old_node = self.make_node("A", payload=server_state)
        new_node = self.make_node("B", payload=server_state)
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)
        self.player._filters = local_filters

        await self.player.transfer_to(cast("Node[Client]", new_node))

        self.assertTrue(await self.player.has_filter("bassboost"))
        self.assertTrue(await self.player.has_filter("nightcore"))
        self.assertEqual(list(self.player._filters), ["bassboost", "nightcore"])
        if new_node.update.await_args is None:
            self.fail("Transfer did not restore playback state on the target.")
        restored_filter = cast("Filter", new_node.update.await_args.kwargs["filter"])
        self.assertEqual(restored_filter.payload, server_state["filters"])

    async def test_preparation_events_do_not_touch_player_before_commit(self) -> None:
        """Target events before commit cannot mutate authoritative Player state."""
        old_node = make_node(4)
        old_node._players = {1: self.player}
        target_node = make_node(4)
        target_node._players = {}
        target_node._event_queue = Event()
        target_node._event_queue.set()
        self.player._node = old_node
        self.player._position = 100
        old_state = player_payload("old")
        old_state["state"]["position"] = 100
        target_state = player_payload("target")
        target_state["state"]["position"] = 500
        seen_positions: list[int] = []

        async def voice_update(**_: object) -> None:
            self.assertIsNone(target_node.get_player(1))
            await target_node._handle_msg(
                {
                    "op": "playerUpdate",
                    "guildId": "1",
                    "state": {
                        "time": 1_000,
                        "position": 15_000,
                        "connected": True,
                        "ping": 7,
                    },
                }
            )
            seen_positions.append(self.player._position)

        async def update(**_: object) -> PlayerPayload:
            return target_state

        with (
            patch.object(Node, "fetch_player", AsyncMock(return_value=old_state)),
            patch.object(Node, "voice_update", side_effect=voice_update),
            patch.object(Node, "update", side_effect=update),
            patch.object(Node, "destroy", AsyncMock()),
        ):
            await self.player.transfer_to(target_node)

        self.assertEqual(seen_positions, [100])
        self.assertIs(target_node.get_player(1), self.player)
        self.assertIsNone(old_node.get_player(1))
        self.assertEqual(self.player._position, 500)

    async def test_disconnect_during_blocked_transfer_aborts_target(self) -> None:
        """Disconnect during target preparation prevents transfer commit."""
        old_node = self.make_node("A")
        new_node = self.make_node("B")
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)
        target_preparing, release_target = Event(), Event()

        async def block_voice_update(**_: object) -> None:
            target_preparing.set()
            await release_target.wait()

        new_node.voice_update.side_effect = block_voice_update
        transfer = asyncio.create_task(
            self.player.transfer_to(cast("Node[Client]", new_node))
        )
        await target_preparing.wait()

        disconnect_started = Event()

        async def disconnect() -> None:
            disconnect_started.set()
            await self.player.disconnect(force=True)

        with patch.object(Player, "cleanup", Mock()):
            disconnect_task = asyncio.create_task(disconnect())
            await disconnect_started.wait()
            release_target.set()
            results = await asyncio.gather(
                transfer, disconnect_task, return_exceptions=True
            )

        self.assertIsInstance(results[0], PlayerNotConnected)
        self.assertNotIn(1, old_node._players)
        self.assertNotIn(1, new_node._players)
        self.assertIsNone(self.player._node)
        self.assertIsNone(self.player.assigned_node)
        self.assertFalse(self.player.connected)

    async def test_disconnect_clears_assignment(self) -> None:
        """Disconnect is terminal for the current Player instance."""
        node = self.make_node("A")
        node._players[1] = self.player
        self.player._node = cast("Node[Client]", node)

        with patch.object(Player, "cleanup", Mock()):
            await self.player.disconnect(force=True)

        self.assertIsNone(self.player._node)
        self.assertIsNone(self.player.assigned_node)
        self.assertNotIn(1, node._players)
        node.destroy.assert_awaited_once_with(guild_id=1)
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        self.assertFalse(self.player.connected)

    async def test_disconnect_channel_none_reentrancy(self) -> None:
        """channel=None completion must not recursively disconnect/cleanup."""
        node = self.make_node("A")
        node._players[1] = self.player
        self.player._node = cast("Node[Client]", node)
        payload = cast(
            "GuildVoiceStatePayload",
            {
                "session_id": "discord-session",
                "guild_id": "1",
                "channel_id": None,
            },
        )
        cleanup = Mock()

        async def change_voice_state(
            *, channel: VoiceChannel | StageChannel | None
        ) -> None:
            self.assertIsNone(channel)
            await self.player.on_voice_state_update(payload)

        self.guild.change_voice_state.side_effect = change_voice_state
        with patch.object(Player, "cleanup", cleanup):
            async with asyncio.timeout(1):
                await self.player.disconnect(force=True)

        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        node.destroy.assert_awaited_once_with(guild_id=1)
        cleanup.assert_called_once()
        self.assertIsNone(self.player._node)
        self.assertIsNone(self.player.assigned_node)
        self.assertFalse(self.player.connected)

    async def test_late_callbacks_after_disconnect_are_ignored(self) -> None:
        """Late server/state callbacks cannot resurrect a disconnecting Player."""
        node = self.make_node("A")
        node._players[1] = self.player
        self.player._node = cast("Node[Client]", node)
        self.player._disconnecting = True
        server_data: VoiceServerUpdatePayload = {
            "guild_id": 1,
            "endpoint": "late.example.com",
            "token": "late-token",
        }
        state_data = cast(
            "GuildVoiceStatePayload",
            {
                "session_id": "discord-session",
                "guild_id": "1",
                "channel_id": "20",
            },
        )

        with (
            patch("mafic.player.NodePool.get_node") as get_node,
            patch.object(Node, "voice_update", AsyncMock()) as voice_update,
        ):
            await self.player.on_voice_server_update(server_data)
            await self.player.on_voice_state_update(state_data)
            await self.player._dispatch_player_update()

        get_node.assert_not_called()
        voice_update.assert_not_awaited()
        self.assertIs(self.player._node, node)
        self.assertEqual(cast("VoiceChannel", self.player.channel).id, 10)

    async def test_transfer_after_disconnect_fails_fast(self) -> None:
        """Public transfer must not touch target after disconnect started."""
        old_node = self.make_node("A")
        new_node = self.make_node("B")
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)
        self.player._disconnecting = True

        with self.assertRaises(PlayerNotConnected):
            await self.player.transfer_to(cast("Node[Client]", new_node))

        new_node.voice_update.assert_not_awaited()
        new_node.update.assert_not_awaited()
        new_node.add_player.assert_not_called()
        self.assertIs(self.player._node, old_node)
        self.assertIs(old_node._players[1], self.player)

    async def test_disconnect_uses_adapter_registration_not_connected_flag(
        self,
    ) -> None:
        """Adapter registration, not Lavalink state, decides disconnect work."""
        node = self.make_node("A")
        node._players[1] = self.player
        self.player._node = cast("Node[Client]", node)
        self.player._connected = False
        self.guild.voice_client = self.player

        with patch.object(Player, "cleanup", Mock()):
            await self.player.disconnect()

        node.destroy.assert_awaited_once_with(guild_id=1)
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        self.assertIsNone(self.player.assigned_node)
        self.assertFalse(self.player.connected)

    async def test_disconnect_destroy_failure_still_leaves_and_cleans(self) -> None:
        """A failing node destroy must not skip Discord leave or cleanup."""
        node = self.make_node("A")
        node._players[1] = self.player
        self.player._node = cast("Node[Client]", node)
        self.guild.voice_client = self.player
        node.destroy.side_effect = RuntimeError("destroy failed")
        cleanup = Mock()

        with patch.object(Player, "cleanup", cleanup):
            async with asyncio.timeout(1):
                await self.player.disconnect(force=True)

        node.destroy.assert_awaited_once_with(guild_id=1)
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        cleanup.assert_called_once()
        self.assertIsNone(self.player.assigned_node)
        self.assertFalse(self.player.connected)

    async def test_move_to_after_disconnect_fails_fast(self) -> None:
        """move_to cannot send a gateway request after disconnect starts."""
        self.player._disconnecting = True
        self.guild.voice_client = self.player

        with self.assertRaises(PlayerNotConnected):
            await self.player.move_to(cast("VoiceChannel", self.channel))

        self.guild.change_voice_state.assert_not_awaited()

    async def test_v3_transfer_uses_real_node_update_without_user_data(
        self,
    ) -> None:
        """Lavalink 3.7.13 transfer uses encodedTrack and drops v4 userData."""
        old_node = make_node(3)
        old_node._players = {1: self.player}
        target_node = make_node(3)
        target_node._players = {}
        self.player._node = old_node
        v3_state = {
            "guildId": "1",
            "track": "encoded-track",
            "volume": 55,
            "paused": False,
            "state": {
                "time": 1_000,
                "position": 333,
                "connected": True,
                "ping": 5,
            },
            "filters": {},
        }
        calls: list[tuple[str, str, object, object]] = []

        async def request(
            method: str,
            path: str,
            json: object = None,
            params: object = None,
        ) -> object:
            calls.append((method, path, json, params))
            if method == "GET":
                return v3_state
            if method == "DELETE":
                return None
            if method == "PATCH":
                data = cast("dict[str, object]", json)
                if "voice" in data:
                    return None
                return v3_state
            raise AssertionError(method)

        with patch.object(Node, "_Node__request", AsyncMock(side_effect=request)):
            await self.player.transfer_to(target_node)

        self.assertIs(self.player._node, target_node)
        self.assertIs(target_node.get_player(1), self.player)
        self.assertIsNone(old_node.get_player(1))
        update_calls = [
            call
            for call in calls
            if call[0] == "PATCH"
            and isinstance(call[2], dict)
            and "encodedTrack" in call[2]
        ]
        self.assertEqual(len(update_calls), 1)
        payload = cast("dict[str, object]", update_calls[0][2])
        self.assertEqual(payload["encodedTrack"], "encoded-track")
        self.assertEqual(payload["position"], 333)
        self.assertEqual(payload["volume"], 55)
        self.assertFalse(payload["paused"])
        self.assertEqual(payload["filters"], {})
        self.assertNotIn("userData", payload)
        current = self.player._current
        if current is None:
            self.fail("Transfer did not restore the v3 track.")
        self.assertEqual(current.id, "encoded-track")
        self.assertEqual(current.user_data, {})

    async def test_transfer_restores_lavalink_v3_state(self) -> None:
        """Lavalink v3 encoded track state transfers without v4 parsing."""
        v3_state = cast(
            "PlayerPayload",
            {
                "guildId": "1",
                "track": "encoded-track",
                "volume": 55,
                "paused": False,
                "state": {
                    "time": 1_000,
                    "position": 333,
                    "connected": True,
                    "ping": 5,
                },
                "filters": {},
            },
        )
        old_node = self.make_node("A", payload=v3_state, version=3)
        new_node = self.make_node("B", payload=v3_state, version=3)
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)

        await self.player.transfer_to(cast("Node[Client]", new_node))

        if new_node.update.await_args is None:
            self.fail("Transfer did not restore playback state on the target.")
        update_kwargs = new_node.update.await_args.kwargs
        transferred = cast("Track", update_kwargs["track"])
        self.assertEqual(transferred.id, "encoded-track")
        self.assertEqual(update_kwargs["position"], 333)
        self.assertEqual(update_kwargs["volume"], 55)
        self.assertFalse(update_kwargs["pause"])
        restored_filter = cast("Filter", update_kwargs["filter"])
        self.assertEqual(restored_filter.payload, {})
        current = self.player._current
        if current is None:
            self.fail("Transfer did not restore the v3 track.")
        self.assertEqual(current.id, "encoded-track")
        self.assertEqual(self.player._position, 333)
        self.assertEqual(self.player._volume, 55)
        self.assertFalse(self.player._paused)

    async def test_actual_node_update_serializes_track_user_data(self) -> None:
        """The real Node.update payload carries Track.user_data for restore."""
        node = make_node(4)
        track = Track.from_data_with_info(track_payload("transfer"))
        request = AsyncMock(return_value=player_payload())

        with patch.object(Node, "_Node__request", request):
            await node.update(
                guild_id=1,
                track=track,
                position=250,
                volume=100,
                pause=False,
                filter=Filter(),
            )

        if request.await_args is None:
            self.fail("Node.update did not send a REST request.")
        payload = request.await_args.args[2]
        self.assertEqual(payload["track"]["userData"]["correlation_id"], "transfer")
        self.assertEqual(payload["position"], 250)

    async def test_target_voice_update_failure_preserves_old_owner(self) -> None:
        """A failed target voice PATCH cannot destroy or replace the old owner."""
        old_node = self.make_node("A")
        new_node = self.make_node("B")
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)
        new_node.voice_update.side_effect = RuntimeError("target voice failed")

        with self.assertRaisesRegex(RuntimeError, "target voice failed"):
            await self.player.transfer_to(cast("Node[Client]", new_node))

        self.assertIs(self.player._node, old_node)
        self.assertIs(old_node._players[1], self.player)
        self.assertNotIn(1, new_node._players)
        old_node.destroy.assert_not_awaited()
        new_node.destroy.assert_awaited_once_with(guild_id=1)
        new_node.update.assert_not_awaited()

    async def test_target_restore_failure_preserves_old_owner(self) -> None:
        """A failed target state restore cannot commit partial ownership."""
        old_node = self.make_node("A")
        new_node = self.make_node("B")
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)
        self.player._connected = False
        new_node.update.side_effect = RuntimeError("target restore failed")

        with self.assertRaisesRegex(RuntimeError, "target restore failed"):
            await self.player.transfer_to(cast("Node[Client]", new_node))

        self.assertIs(self.player._node, old_node)
        self.assertIs(old_node._players[1], self.player)
        self.assertNotIn(1, new_node._players)
        self.assertFalse(self.player.connected)
        old_node.destroy.assert_not_awaited()
        new_node.destroy.assert_awaited_once_with(guild_id=1)

    async def test_old_cleanup_failure_keeps_committed_target(self) -> None:
        """Old-node cleanup is best-effort after ownership has committed."""
        old_node = self.make_node("A")
        new_node = self.make_node("B")
        old_node._players[1] = self.player
        self.player._node = cast("Node[Client]", old_node)
        old_node.destroy.side_effect = RuntimeError("old cleanup failed")

        await self.player.transfer_to(cast("Node[Client]", new_node))

        self.assertIs(self.player._node, new_node)
        self.assertNotIn(1, old_node._players)
        self.assertIs(new_node._players[1], self.player)
        new_node.update.assert_awaited_once()
        old_node.destroy.assert_awaited_once_with(guild_id=1)

    async def test_invalid_prerequisites_do_not_mutate_ownership(self) -> None:
        """Missing or invalid voice state leaves the old owner untouched."""
        cases = ("session", "server", "channel", "snapshot")
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                old_node = self.make_node("A")
                new_node = self.make_node("B")
                old_node._players[1] = self.player
                self.player._node = cast("Node[Client]", old_node)
                if case == "session":
                    self.player._session_id = None
                elif case == "server":
                    self.player._server_state = None
                elif case == "channel":
                    self.player.channel = cast("Connectable", object())
                else:
                    old_node.fetch_player.return_value = {
                        key: value
                        for key, value in player_payload().items()
                        if key != "volume"
                    }

                if case == "channel":
                    with self.assertRaises(PlayerNotConnected):
                        await self.player.transfer_to(cast("Node[Client]", new_node))
                else:
                    with self.assertRaises(RuntimeError):
                        await self.player.transfer_to(cast("Node[Client]", new_node))

                self.assertIs(self.player._node, old_node)
                self.assertIs(old_node._players[1], self.player)
                self.assertNotIn(1, new_node._players)
                old_node.destroy.assert_not_awaited()
                new_node.voice_update.assert_not_awaited()
                new_node.update.assert_not_awaited()

    async def test_same_node_server_update_keeps_ownership(self) -> None:
        """Endpoint changes that select the current node only refresh credentials."""
        node = self.make_node("A")
        node._players[1] = self.player
        self.player._node = cast("Node[Client]", node)
        data: VoiceServerUpdatePayload = {
            "guild_id": 1,
            "endpoint": "new.example.com",
            "token": "new-token",
        }

        with patch("mafic.player.NodePool.get_node", return_value=node):
            await self.player.on_voice_server_update(data)

        self.assertIs(self.player._node, node)
        self.assertIs(node._players[1], self.player)
        node.voice_update.assert_awaited_once_with(
            guild_id=1,
            session_id="discord-session",
            data=data,
            channel_id=10,
        )
        node.update.assert_not_awaited()
        node.destroy.assert_not_awaited()

    async def test_server_migration_failure_keeps_new_credentials(self) -> None:
        """A failed automatic migration keeps Discord state but old ownership."""
        a_node = self.make_node("A")
        b_node = self.make_node("B")
        a_node._players[1] = self.player
        self.player._node = cast("Node[Client]", a_node)
        b_node.voice_update.side_effect = RuntimeError("server migration failed")
        server_data: VoiceServerUpdatePayload = {
            "guild_id": 1,
            "endpoint": "new.example.com",
            "token": "new-token",
        }

        with (
            patch("mafic.player.NodePool.get_node", return_value=b_node),
            self.assertRaisesRegex(RuntimeError, "server migration failed"),
        ):
            await self.player.on_voice_server_update(server_data)

        self.assertIs(self.player._node, a_node)
        self.assertIs(a_node._players[1], self.player)
        self.assertNotIn(1, b_node._players)
        self.assertEqual(self.player._server_state, server_data)
        a_node.destroy.assert_not_awaited()
        b_node.destroy.assert_awaited_once_with(guild_id=1)

    async def test_transfer_rechecks_current_node_after_concurrent_migration(
        self,
    ) -> None:
        """A queued public transfer fetches from the actual authoritative node."""
        a_node = self.make_node("A")
        b_node = self.make_node("B")
        c_node = self.make_node("C")
        a_node._players[1] = self.player
        self.player._node = cast("Node[Client]", a_node)

        server_voice_started, release_server = Event(), Event()

        async def block_target_voice(**_: object) -> None:
            server_voice_started.set()
            await release_server.wait()

        b_node.voice_update.side_effect = block_target_voice
        server_data: VoiceServerUpdatePayload = {
            "guild_id": 1,
            "endpoint": "new.example.com",
            "token": "new-token",
        }

        with patch("mafic.player.NodePool.get_node", return_value=b_node):
            server_task = asyncio.create_task(
                self.player.on_voice_server_update(server_data)
            )
            await server_voice_started.wait()

            public_started = Event()

            async def public_transfer() -> None:
                public_started.set()
                await self.player.transfer_to(cast("Node[Client]", c_node))

            public_task = asyncio.create_task(public_transfer())
            await public_started.wait()

            release_server.set()
            await asyncio.gather(server_task, public_task)

        self.assertIs(self.player._node, c_node)
        self.assertNotIn(1, a_node._players)
        self.assertNotIn(1, b_node._players)
        self.assertIs(c_node._players[1], self.player)
        self.assertEqual(a_node.fetch_player.await_count, 1)
        self.assertEqual(b_node.fetch_player.await_count, 1)
        self.assertEqual(c_node.fetch_player.await_count, 0)
        b_node.voice_update.assert_awaited_once_with(
            guild_id=1,
            session_id="discord-session",
            data=server_data,
            channel_id=10,
        )
        c_node.voice_update.assert_awaited_once_with(
            guild_id=1,
            session_id="discord-session",
            data=server_data,
            channel_id=10,
        )
        b_node.destroy.assert_awaited_once_with(guild_id=1)
        c_node.destroy.assert_not_awaited()
        self.assertEqual(self.player._server_state, server_data)
