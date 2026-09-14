"""Tests for player transfer ownership and terminal voice lifecycle."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from asyncio import Event
from collections import OrderedDict
from time import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from mafic.__libraries import Client, Guild, StageChannel, VoiceChannel
from mafic.errors import PlayerNotConnected
from mafic.filter import Filter, Timescale
from mafic.node import Node
from mafic.player import Player
from mafic.track import Track

from .test_player_update import make_node, player_payload
from .test_track import track_payload

if TYPE_CHECKING:
    from mafic.__libraries import (
        Connectable,
        GuildVoiceStatePayload,
        VoiceServerUpdatePayload,
    )
    from mafic.typings import Player as PlayerPayload


def v3_player_payload() -> PlayerPayload:
    """Build the encoded-track REST response returned by Lavalink v3."""
    return cast(
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

    def make_owner(
        self, *, payload: PlayerPayload | None = None, version: int = 4
    ) -> SimpleNamespace:
        """Assign the fixture player to its authoritative old node."""
        node = self.make_node("A", payload=payload, version=version)
        node.add_player(1, self.player)
        self.player._node = cast("Node[Client]", node)
        return node

    async def test_transfer_prepares_target_before_commit(self) -> None:
        """Target voice/state is prepared before ownership moves from A to B."""
        server_state = player_payload("server")
        old_node = self.make_owner(payload=server_state)
        new_node = self.make_node("B", payload=server_state)

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
        self.make_owner(payload=server_state)
        new_node = self.make_node("B", payload=server_state)

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
        self.make_owner(payload=server_state)
        new_node = self.make_node("B", payload=server_state)
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
        target_state["state"]["connected"] = False
        target_state["voice"] = {
            "sessionId": "stale-session",
            "token": "stale-token",
            "endpoint": "stale.example.com",
            "channelId": "99",
        }
        discord_state = self.player._server_state
        endpoint = self.player.endpoint
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
        self.assertFalse(self.player.connected)
        self.assertIs(self.player._server_state, discord_state)
        self.assertEqual(self.player._session_id, "discord-session")
        self.assertEqual(self.player.endpoint, endpoint)

    async def test_disconnect_during_blocked_transfer_aborts_target(self) -> None:
        """Disconnect during target preparation prevents transfer commit."""
        old_node = self.make_owner()
        new_node = self.make_node("B")
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
        node = self.make_owner()

        with patch("mafic.player.VoiceProtocol.cleanup"):
            await self.player.disconnect(force=True)

        self.assertIsNone(self.player._node)
        self.assertIsNone(self.player.assigned_node)
        self.assertNotIn(1, node._players)
        node.destroy.assert_awaited_once_with(guild_id=1)
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        self.assertFalse(self.player.connected)
        self.assertIsNone(self.player.current)
        self.assertEqual(self.player.position, 0)

    async def assert_cancelled_transfer_preserves_owner(
        self, phase: str, *, cleanup_fails: bool
    ) -> None:
        """Cancel preparation and wait for partial cleanup before propagation."""
        self.setUp()
        old_node = self.make_owner()
        target = self.make_node("B")
        preparing, cleaning, release_cleanup = Event(), Event(), Event()

        async def prepare(**_: object) -> None:
            preparing.set()
            await Event().wait()

        async def destroy(*, guild_id: int) -> None:
            self.assertEqual(guild_id, 1)
            cleaning.set()
            await release_cleanup.wait()
            if cleanup_fails:
                msg = "partial cleanup failed"
                raise RuntimeError(msg)

        getattr(target, phase).side_effect = prepare
        target.destroy.side_effect = destroy
        async with asyncio.timeout(1):
            transfer = asyncio.create_task(
                self.player.transfer_to(cast("Node[Client]", target))
            )
            await preparing.wait()
            transfer.cancel()
            try:
                await cleaning.wait()
                self.assertFalse(transfer.done())
            finally:
                release_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await transfer

        self.assertIs(self.player.assigned_node, old_node)
        self.assertIs(old_node.get_player(1), self.player)
        old_node.destroy.assert_not_awaited()
        target.destroy.assert_awaited_once_with(guild_id=1)
        target.add_player.assert_not_called()
        self.assertIsNone(target.get_player(1))
        if phase == "voice_update":
            target.update.assert_not_awaited()

    async def test_transfer_cancelled_during_target_voice_update(self) -> None:
        """Cancellation of target voice preparation keeps A and cleans partial B."""
        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails):
                await self.assert_cancelled_transfer_preserves_owner(
                    "voice_update", cleanup_fails=cleanup_fails
                )

    async def test_transfer_cancelled_during_target_update(self) -> None:
        """Cancellation of playback restoration keeps A and cleans partial B."""
        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails):
                await self.assert_cancelled_transfer_preserves_owner(
                    "update", cleanup_fails=cleanup_fails
                )

    def assert_disconnected(self, node: SimpleNamespace, cleanup: Mock) -> None:
        """Assert terminal ownership, adapter cleanup and a single teardown."""
        self.assertIsNone(self.player.assigned_node)
        self.assertIsNone(node.get_player(1))
        self.assertTrue(self.player._disconnect_requested)
        self.assertFalse(self.player.connected)
        self.assertIsNone(self.player.current)
        self.assertEqual(self.player.position, 0)
        node.destroy.assert_awaited_once_with(guild_id=1)
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        cleanup.assert_called_once()

    async def test_disconnect_cancelled_before_voice_lock(self) -> None:
        """Terminal disconnect finishes when cancelled before acquiring the lock."""
        node = self.make_owner()
        self.guild.voice_client = self.player
        started = Event()

        async def disconnect() -> None:
            started.set()
            await self.player.disconnect()

        with patch(
            "mafic.player.VoiceProtocol.cleanup",
            side_effect=lambda: setattr(self.guild, "voice_client", None),
        ) as cleanup:
            async with asyncio.timeout(1):
                async with self.player._voice_update_lock:
                    task = asyncio.create_task(disconnect())
                    await started.wait()
                    self.assertTrue(self.player._disconnect_requested)
                    self.assertIs(self.player.assigned_node, node)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assert_disconnected(node, cleanup)
            self.assertIsNone(self.guild.voice_client)

    async def assert_cancelled_disconnect_finishes(
        self, phase: str, *, leave_fails: bool = False
    ) -> None:
        """Hold a teardown operation and cancel its caller before releasing it."""
        node = self.make_owner()
        self.guild.voice_client = self.player
        entered, release, completed = Event(), Event(), Event()

        async def block(**_: object) -> None:
            entered.set()
            await release.wait()
            completed.set()
            if leave_fails:
                msg = "Discord leave failed"
                raise RuntimeError(msg)

        operation = (
            node.destroy if phase == "destroy" else self.guild.change_voice_state
        )
        operation.side_effect = block
        with patch("mafic.player.VoiceProtocol.cleanup") as cleanup:
            async with asyncio.timeout(1):
                task = asyncio.create_task(self.player.disconnect())
                await entered.wait()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(completed.is_set())
            self.assert_disconnected(node, cleanup)

    async def test_disconnect_cancelled_during_node_destroy(self) -> None:
        """Cancellation during node destroy still leaves Discord and cleans up."""
        await self.assert_cancelled_disconnect_finishes("destroy")

    async def test_disconnect_cancelled_during_discord_leave(self) -> None:
        """Cancellation during Discord leave propagates only after cleanup."""
        await self.assert_cancelled_disconnect_finishes("leave")
        with self.assertLogs("mafic.player", level="ERROR") as logs:
            self.setUp()
            await self.assert_cancelled_disconnect_finishes("leave", leave_fails=True)
        self.assertIn("after caller cancellation", logs.output[0])
        self.assertIn("Discord leave failed", logs.output[0])

    async def assert_disconnect_shares_teardown(self, *, cancel_first: bool) -> None:
        """Exercise shared teardown with an optional cancelled caller."""
        self.setUp()
        node = self.make_owner()
        self.guild.voice_client = self.player
        destroying, release, second_started = Event(), Event(), Event()

        async def destroy(**_: object) -> None:
            destroying.set()
            await release.wait()

        async def second_disconnect() -> None:
            second_started.set()
            await self.player.disconnect(force=True)

        node.destroy.side_effect = destroy
        with patch("mafic.player.VoiceProtocol.cleanup") as cleanup:
            async with asyncio.timeout(1):
                first = asyncio.create_task(self.player.disconnect())
                await destroying.wait()
                second = asyncio.create_task(second_disconnect())
                await second_started.wait()
                if cancel_first:
                    first.cancel()
                release.set()
                results = await asyncio.gather(first, second, return_exceptions=True)
                if cancel_first:
                    self.assertIsInstance(results[0], asyncio.CancelledError)
                else:
                    self.assertIsNone(results[0])
                self.assertIsNone(results[1])
                await self.player.disconnect(force=True)
            self.assert_disconnected(node, cleanup)

    async def test_concurrent_disconnect_shares_teardown(self) -> None:
        """Concurrent and repeated callers perform one destroy, leave and cleanup."""
        for cancel_first in (False, True):
            with self.subTest(cancel_first=cancel_first):
                await self.assert_disconnect_shares_teardown(cancel_first=cancel_first)

    async def test_disconnect_channel_none_reentrancy(self) -> None:
        """channel=None completion must not recursively disconnect/cleanup."""
        node = self.make_owner()
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
        for completed in (False, True):
            with self.subTest(disconnect_completed=completed):
                self.setUp()
                node = self.make_owner()
                if completed:
                    with patch("mafic.player.VoiceProtocol.cleanup"):
                        await self.player.disconnect(force=True)
                else:
                    self.player._disconnect_requested = True
                owner = self.player.assigned_node
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
                self.assertIs(self.player.assigned_node, owner)
                if completed:
                    self.assertNotIn(1, node._players)
                    self.assertFalse(self.player.connected)
                else:
                    self.assertIs(node.get_player(1), self.player)
                self.assertEqual(cast("VoiceChannel", self.player.channel).id, 10)

    async def test_transfer_after_disconnect_fails_fast(self) -> None:
        """Public transfer must not touch target after disconnect started."""
        old_node = self.make_owner()
        new_node = self.make_node("B")
        self.player._disconnect_requested = True

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
        node = self.make_owner()
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
        node = self.make_owner()
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
        self.player._disconnect_requested = True
        self.guild.voice_client = self.player

        with self.assertRaises(PlayerNotConnected):
            await self.player.move_to(cast("VoiceChannel", self.channel))

        self.guild.change_voice_state.assert_not_awaited()

    async def assert_transfer_direction(
        self, source_version: int, target_version: int
    ) -> None:
        """Exercise source parsing and target serialization through real Node calls."""
        old_node = make_node(source_version)
        old_node._session_id = "source-session"
        old_node._players = {1: self.player}
        target_node = make_node(target_version)
        target_node._session_id = "target-session"
        target_node._players = {}
        self.player._node = old_node
        source_state = (
            v3_player_payload() if source_version == 3 else player_payload("server")
        )
        source_state["volume"] = 55
        source_state["state"]["position"] = 333
        target_state = (
            v3_player_payload() if target_version == 3 else player_payload("server")
        )
        target_state["volume"] = 73
        target_state["state"]["position"] = 777
        target_state["paused"] = True
        if target_version == 4 and source_version == 3:
            target_track = target_state["track"]
            if target_track is None:
                self.fail("Expected target fixture track.")
            target_track["userData"] = {}
        local_track = self.player.current
        if local_track is None:
            self.fail("Expected local fixture track.")
        local_track.id = "stale-local-track"
        calls: list[tuple[str, str, object, object]] = []

        async def request(
            method: str, path: str, json: object = None, params: object = None
        ) -> object:
            calls.append((method, path, json, params))
            if method == "GET":
                self.assertIn("source-session", path)
                return source_state
            if method == "DELETE":
                self.assertIn("source-session", path)
                self.assertIs(self.player.assigned_node, target_node)
                self.assertIsNone(old_node.get_player(1))
                return None
            if method == "PATCH":
                self.assertIn("target-session", path)
                self.assertIs(self.player.assigned_node, old_node)
                self.assertIsNone(target_node.get_player(1))
                return target_state
            raise AssertionError(method)

        with patch.object(Node, "_Node__request", AsyncMock(side_effect=request)):
            await self.player.transfer_to(target_node)

        self.assertEqual(
            [call[0] for call in calls], ["GET", "PATCH", "PATCH", "DELETE"]
        )
        payload = cast("dict[str, object]", calls[2][2])
        if target_version == 3:
            self.assertEqual(payload["encodedTrack"], "encoded-track")
            self.assertNotIn("track", payload)
            self.assertNotIn("userData", payload)
        else:
            expected_data = {"correlation_id": "server"} if source_version == 4 else {}
            self.assertEqual(
                payload["track"],
                {"encoded": "encoded-track", "userData": expected_data},
            )
            self.assertNotIn("encodedTrack", payload)
        self.assertEqual(payload["position"], 333)
        self.assertEqual(payload["volume"], 55)
        self.assertFalse(payload["paused"])
        self.assertEqual(payload["filters"], {})
        self.assertIs(self.player.assigned_node, target_node)
        self.assertIs(target_node.get_player(1), self.player)
        self.assertIsNone(old_node.get_player(1))
        current = self.player.current
        if current is None:
            self.fail("Transfer did not restore the track.")
        self.assertEqual(current.id, "encoded-track")
        self.assertEqual(
            current.user_data,
            (
                {"correlation_id": "server"}
                if source_version == target_version == 4
                else {}
            ),
        )
        self.assertEqual(self.player._position, 777)
        self.assertEqual(self.player._volume, 73)
        self.assertTrue(self.player.paused)
        self.assertEqual(local_track.id, "stale-local-track")
        self.assertEqual(local_track.user_data, {"correlation_id": "transfer"})
        if source_version == 4:
            source_track = source_state["track"]
            if source_track is None:
                self.fail("Source fixture lost its track.")
            self.assertEqual(source_track.get("userData"), {"correlation_id": "server"})

    async def test_v3_transfer_uses_real_node_update_without_user_data(self) -> None:
        """A v3 source restores to v3 without leaking cached local userData."""
        await self.assert_transfer_direction(3, 3)

    async def test_v3_to_v4_transfer_uses_real_node_update(self) -> None:
        """A v3 source sends its encoded track in the v4 track envelope."""
        await self.assert_transfer_direction(3, 4)

    async def test_v4_to_v3_transfer_uses_real_node_update(self) -> None:
        """A v4 source is copied without userData for the v3 target."""
        await self.assert_transfer_direction(4, 3)

    async def test_v4_to_v4_transfer_uses_real_node_update(self) -> None:
        """A v4 transfer keeps snapshot userData through real serialization."""
        await self.assert_transfer_direction(4, 4)

    async def test_transfer_restores_lavalink_v3_state(self) -> None:
        """Lavalink v3 encoded track state transfers without v4 parsing."""
        v3_state = v3_player_payload()
        self.make_owner(payload=v3_state, version=3)
        new_node = self.make_node("B", payload=v3_state, version=3)

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
        old_node = self.make_owner()
        new_node = self.make_node("B")
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
        old_node = self.make_owner()
        new_node = self.make_node("B")
        self.player._connected = False
        new_node.update.side_effect = RuntimeError("target restore failed")
        new_node.destroy.side_effect = RuntimeError("partial cleanup failed")

        with (
            self.assertLogs("mafic.player", level="ERROR") as logs,
            self.assertRaisesRegex(RuntimeError, "target restore failed"),
        ):
            await self.player.transfer_to(cast("Node[Client]", new_node))

        self.assertIn("partial target node player", logs.output[0])
        self.assertIn("partial cleanup failed", logs.output[0])
        self.assertIs(self.player._node, old_node)
        self.assertIs(old_node._players[1], self.player)
        self.assertNotIn(1, new_node._players)
        self.assertFalse(self.player.connected)
        old_node.destroy.assert_not_awaited()
        new_node.destroy.assert_awaited_once_with(guild_id=1)

    async def test_old_cleanup_failure_keeps_committed_target(self) -> None:
        """Old-node cleanup is best-effort after ownership has committed."""
        old_node = self.make_owner()
        new_node = self.make_node("B")
        old_node.destroy.side_effect = RuntimeError("old cleanup failed")

        await self.player.transfer_to(cast("Node[Client]", new_node))

        self.assertIs(self.player._node, new_node)
        self.assertNotIn(1, old_node._players)
        self.assertIs(new_node._players[1], self.player)
        new_node.update.assert_awaited_once()
        old_node.destroy.assert_awaited_once_with(guild_id=1)

    async def test_cancelled_committed_cleanup_finishes_before_propagation(
        self,
    ) -> None:
        """Cancellation waits for old destroy and never rolls back committed B."""
        old_node = self.make_owner()
        target = self.make_node("B")
        destroying, release, destroyed = Event(), Event(), Event()

        async def destroy(*, guild_id: int) -> None:
            self.assertEqual(guild_id, 1)
            destroying.set()
            await release.wait()
            destroyed.set()

        old_node.destroy.side_effect = destroy
        async with asyncio.timeout(1):
            transfer = asyncio.create_task(
                self.player.transfer_to(cast("Node[Client]", target))
            )
            await destroying.wait()
            self.assertIs(self.player.assigned_node, target)
            self.assertIs(target.get_player(1), self.player)
            self.assertIsNone(old_node.get_player(1))
            transfer.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await transfer

        self.assertTrue(destroyed.is_set())
        self.assertIs(self.player.assigned_node, target)
        self.assertIs(target.get_player(1), self.player)
        self.assertIsNone(old_node.get_player(1))
        self.assertFalse(self.player._disconnect_requested)
        self.assertFalse(self.player._voice_update_lock.locked())
        old_node.destroy.assert_awaited_once_with(guild_id=1)
        target.destroy.assert_not_awaited()

    async def test_invalid_prerequisites_do_not_mutate_ownership(self) -> None:
        """Missing or invalid voice state leaves the old owner untouched."""
        cases = ("session", "server", "channel", "snapshot")
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                old_node = self.make_owner()
                new_node = self.make_node("B")
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
        node = self.make_owner()
        data: VoiceServerUpdatePayload = {
            "guild_id": 1,
            "endpoint": "new.example.com",
            "token": "new-token",
        }

        with patch("mafic.player.NodePool.get_node", return_value=node):
            await self.player.on_voice_server_update(data)

        self.assertIs(self.player._node, node)
        self.assertIs(node._players[1], self.player)
        self.assertEqual(self.player.endpoint, "new.example.com")
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
        a_node = self.make_owner()
        b_node = self.make_node("B")
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
        self.assertEqual(self.player.endpoint, "new.example.com")
        a_node.destroy.assert_not_awaited()
        b_node.destroy.assert_awaited_once_with(guild_id=1)

    async def test_transfer_rechecks_current_node_after_concurrent_migration(
        self,
    ) -> None:
        """A queued public transfer fetches from the actual authoritative node."""
        a_node = self.make_owner()
        b_node = self.make_node("B")
        c_node = self.make_node("C")

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
