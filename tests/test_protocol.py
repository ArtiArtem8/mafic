"""Tests for Lavalink 4.2 protocol alignment."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import warnings
from asyncio import Event
from collections import OrderedDict
from time import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from yarl import URL

from mafic.filter import Filter
from mafic.ip import FailingAddress
from mafic.node import Node
from mafic.player import Player
from mafic.stats import CPUStats
from mafic.track import Track
from mafic.warnings import UnsupportedVersionWarning

from .test_player_update import make_node, player_payload
from .test_track import track_payload

if TYPE_CHECKING:
    from mafic.__libraries import (
        Client,
        Connectable,
        Guild,
        GuildVoiceStatePayload,
        VoiceServerUpdatePayload,
    )


class PlayerStateTests(TestCase):
    """Verify player reconstruction from the stable REST object."""

    def test_set_state_reads_player_state_and_track_metadata(self) -> None:
        """Sync/resume reconstruction keeps state and complete track metadata."""
        player: Player[Client] = object.__new__(Player)
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
        node._base_uri = URL("http://localhost:2333")
        node._rest_uri = node._base_uri / "v3"
        request = AsyncMock(return_value=[{"name": "test", "version": "1"}])

        with patch.object(Node, "_Node__request", request):
            plugins = await node.fetch_plugins()

        self.assertEqual(
            [(plugin.name, plugin.version) for plugin in plugins], [("test", "1")]
        )
        request.assert_awaited_once_with("GET", URL("http://localhost:2333/plugins"))

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

    async def test_channel_move_dispatches_update_with_unchanged_session(self) -> None:
        """A voice channel move updates DAVE's required channel ID."""

        class FakeVoiceChannel:
            def __init__(self, channel_id: int) -> None:
                self.id = channel_id

        class FakeGuild:
            def get_channel(self, _: int) -> FakeVoiceChannel:
                return FakeVoiceChannel(20)

        player: Player[Client] = object.__new__(Player)
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
        player: Player[Client] = object.__new__(Player)
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
