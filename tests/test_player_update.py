"""Tests for Lavalink player update serialization."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from mafic.node import Node
from mafic.player import Player
from mafic.track import Track

from .test_track import track_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mafic.__libraries import Client
    from mafic.typings import JSONValue, Player as PlayerPayload, UpdatePlayerPayload


class UpdateKwargs(TypedDict, total=False):
    """Arguments exercised through Node.update."""

    track: Track | str | None
    end_time: int | None
    user_data: Mapping[str, JSONValue]


def player_payload(correlation_id: str = "request-a") -> PlayerPayload:
    """Build a Lavalink 4 player response."""
    return {
        "guildId": "1",
        "track": track_payload(correlation_id),
        "volume": 100,
        "paused": False,
        "state": {
            "time": 1_000,
            "position": 250,
            "connected": True,
            "ping": 5,
        },
        "voice": {
            "token": "token",
            "endpoint": "endpoint",
            "sessionId": "discord-session",
            "channelId": "10",
        },
        "filters": {},
    }


def make_node(version: int) -> Node[Client]:
    """Build the minimal Node state needed by Node.update."""
    node: Node[Client] = object.__new__(Node)
    node._version = version
    node._session_id = "lavalink-session"
    node._label = "TEST"
    return node


class NodePlayerUpdateTests(IsolatedAsyncioTestCase):
    """Verify version-specific Update Player payloads."""

    async def update_payload(
        self, node: Node[Client], **kwargs: UpdateKwargs
    ) -> UpdatePlayerPayload:
        """Run an update and return its serialized JSON payload."""
        request = AsyncMock(return_value=player_payload())
        with patch.object(Node, "_Node__request", request):
            await node.update(guild_id=1, **kwargs)

        if request.await_args is None:
            self.fail("Node did not make an update request.")
        return request.await_args.args[2]

    async def test_v4_track_uses_nested_payload_and_track_user_data(self) -> None:
        """A Track uses canonical v4 nesting and keeps its user data."""
        track = Track.from_data_with_info(track_payload())

        payload = await self.update_payload(make_node(4), track=track)

        self.assertEqual(
            payload,
            {
                "track": {
                    "encoded": "encoded-track",
                    "userData": {"correlation_id": "request-a"},
                }
            },
        )

    async def test_v4_explicit_user_data_overrides_track_data(self) -> None:
        """Explicit user data replaces metadata already attached to a Track."""
        track = Track.from_data_with_info(track_payload())

        payload = await self.update_payload(
            make_node(4), track=track, user_data={"correlation_id": "override"}
        )

        self.assertEqual(
            payload,
            {
                "track": {
                    "encoded": "encoded-track",
                    "userData": {"correlation_id": "override"},
                }
            },
        )

    async def test_v4_identifier_accepts_user_data(self) -> None:
        """Identifiers can carry user data in the nested v4 track object."""
        payload = await self.update_payload(
            make_node(4), track="identifier", user_data={"request_id": "abc"}
        )

        self.assertEqual(
            payload,
            {
                "track": {
                    "identifier": "identifier",
                    "userData": {"request_id": "abc"},
                }
            },
        )

    async def test_v4_explicit_empty_user_data_is_sent(self) -> None:
        """An explicit empty object differs from an omitted identifier user data."""
        with_empty = await self.update_payload(
            make_node(4), track="identifier", user_data={}
        )
        omitted = await self.update_payload(make_node(4), track="identifier")

        self.assertEqual(
            with_empty, {"track": {"identifier": "identifier", "userData": {}}}
        )
        self.assertEqual(omitted, {"track": {"identifier": "identifier"}})

    async def test_v4_stop_uses_null_nested_encoded_track(self) -> None:
        """Stopping uses track.encoded=null on Lavalink v4."""
        payload = await self.update_payload(make_node(4), track=None)

        self.assertEqual(payload, {"track": {"encoded": None}})

    async def test_v3_payload_remains_legacy(self) -> None:
        """Lavalink v3 keeps its existing top-level encodedTrack payload."""
        track = Track.from_data_with_info(track_payload())
        track.user_data.clear()

        payload = await self.update_payload(make_node(3), track=track)

        self.assertEqual(payload, {"encodedTrack": "encoded-track"})

    async def test_v3_rejects_user_data(self) -> None:
        """Lavalink v3 reports unsupported metadata instead of dropping it."""
        with self.assertRaisesRegex(TypeError, "does not support track user data"):
            await make_node(3).update(
                guild_id=1, track="identifier", user_data={"request_id": "abc"}
            )

    async def test_end_time_distinguishes_omitted_from_null(self) -> None:
        """Omitted endTime leaves state unchanged while null resets it."""
        omitted = await self.update_payload(make_node(4))
        reset = await self.update_payload(make_node(4), end_time=None)

        self.assertNotIn("endTime", omitted)
        self.assertEqual(reset, {"endTime": None})


class PlayerPlayTests(IsolatedAsyncioTestCase):
    """Verify the public Player API reaches the v4 serializer."""

    async def test_play_sends_track_user_data(self) -> None:
        """Player.play preserves Track.user_data by default."""
        node = make_node(4)
        player: Player[Client] = object.__new__(Player)
        player._node = node
        player._connected = True
        player._guild_id = 1
        request = AsyncMock(return_value=player_payload())

        with patch.object(Node, "_Node__request", request):
            await player.play(Track.from_data_with_info(track_payload()))

        if request.await_args is None:
            self.fail("Player did not make an update request.")
        payload = request.await_args.args[2]
        self.assertEqual(
            payload,
            {
                "track": {
                    "encoded": "encoded-track",
                    "userData": {"correlation_id": "request-a"},
                }
            },
        )

    async def test_play_forwards_explicit_user_data(self) -> None:
        """Player.play forwards an explicit user data object."""
        node = make_node(4)
        player: Player[Client] = object.__new__(Player)
        player._node = node
        player._connected = True
        player._guild_id = 1
        request = AsyncMock(return_value=player_payload("explicit"))

        with patch.object(Node, "_Node__request", request):
            await player.play("identifier", user_data={"correlation_id": "explicit"})

        if request.await_args is None:
            self.fail("Player did not make an update request.")
        payload = request.await_args.args[2]
        self.assertEqual(
            payload,
            {
                "track": {
                    "identifier": "identifier",
                    "userData": {"correlation_id": "explicit"},
                }
            },
        )
