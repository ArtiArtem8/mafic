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
from mafic.filter import Filter
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

    async def test_voice_updates_are_serialized(self) -> None:
        """State/server callbacks cannot issue overlapping voice updates."""
        first_entered, release_first = Event(), Event()
        sent_tokens: list[str] = []

        async def voice_update(**kwargs: object) -> None:
            data = cast("VoiceServerUpdatePayload", kwargs["data"])
            sent_tokens.append(data["token"])
            if len(sent_tokens) == 1:
                first_entered.set()
                await release_first.wait()

        async def first_server_update() -> None:
            await self.player.on_voice_server_update(
                {"guild_id": 1, "endpoint": "voice.example.com", "token": "first-token"}
            )

        async def second_server_update() -> None:
            await self.player.on_voice_server_update(
                {
                    "guild_id": 1,
                    "endpoint": "voice.example.com",
                    "token": "second-token",
                }
            )

        self.player.node._players = {}
        with patch.object(Node, "voice_update", side_effect=voice_update):
            first = asyncio.create_task(first_server_update())
            await first_entered.wait()
            second = asyncio.create_task(second_server_update())
            await asyncio.sleep(0)
            self.assertEqual(sent_tokens, ["first-token"])
            self.assertFalse(second.done())
            release_first.set()
            await asyncio.gather(first, second)

        self.assertEqual(sent_tokens, ["first-token", "second-token"])

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


class TransferMetadataTests(IsolatedAsyncioTestCase):
    """Verify node transfer preserves playback correlation metadata."""

    async def test_transfer_replays_current_track_with_user_data(self) -> None:
        """The target node receives the complete current Track object."""

        class FakeVoiceChannel:
            id = 10

        old_node = SimpleNamespace(
            fetch_player=AsyncMock(return_value=player_payload()),
            remove_player=Mock(),
            destroy=AsyncMock(),
        )
        new_node = SimpleNamespace(add_player=Mock(), voice_update=AsyncMock())
        player: Player[Client] = cast("Player[Client]", object.__new__(Player))
        player._node = cast("Node[Client]", old_node)
        player.guild = cast("Guild", SimpleNamespace(id=1))
        player.channel = cast("Connectable", FakeVoiceChannel())
        player._guild_id = 1
        player._session_id = "discord-session"
        player._server_state = cast(
            "VoiceServerUpdatePayload",
            {"guild_id": 1, "endpoint": "voice.example.com", "token": "token"},
        )
        player._current = Track.from_data_with_info(track_payload("transfer"))
        player._position = 250
        player._last_update = int(time() * 1_000)
        player._connected = True
        player._paused = False
        player._filters = OrderedDict()
        update = AsyncMock()

        channel_patch = patch("mafic.player.VoiceChannel", FakeVoiceChannel)
        update_patch = patch.object(Player, "update", update)
        with channel_patch, update_patch:
            await player.transfer_to(cast("Node[Client]", new_node))

        if update.await_args is None:
            self.fail("Transfer did not restore player state on the target node.")
        transferred = update.await_args.kwargs["track"]
        self.assertIsInstance(transferred, Track)
        self.assertEqual(transferred.user_data["correlation_id"], "transfer")
