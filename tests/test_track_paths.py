"""Tests for Track metadata across Lavalink REST paths."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from mafic.node import Node
from mafic.playlist import Playlist

from .test_player_update import make_node
from .test_track import track_payload

if TYPE_CHECKING:
    from mafic.__libraries import Client


class TrackRestPathTests(IsolatedAsyncioTestCase):
    """Ensure every supported v4 REST decoding path keeps metadata."""

    def setUp(self) -> None:
        """Build a v4 node."""
        self.node: Node[Client] = make_node(4)

    async def test_track_load_preserves_metadata(self) -> None:
        """A single track load keeps pluginInfo and userData."""
        request = AsyncMock(
            return_value={"loadType": "track", "data": track_payload("load")}
        )

        with patch.object(Node, "_Node__request", request):
            tracks = await self.node.fetch_tracks("track", search_type="")

        if not isinstance(tracks, list):
            self.fail("Track load did not return a list.")
        self.assertEqual(tracks[0].user_data["correlation_id"], "load")
        self.assertEqual(tracks[0].plugin_info, {"album": "metadata"})

    async def test_search_load_preserves_metadata(self) -> None:
        """Search results keep complete Track objects."""
        request = AsyncMock(
            return_value={"loadType": "search", "data": [track_payload("search")]}
        )

        with patch.object(Node, "_Node__request", request):
            tracks = await self.node.fetch_tracks("query", search_type="test")

        if not isinstance(tracks, list):
            self.fail("Search did not return a list.")
        self.assertEqual(tracks[0].user_data["correlation_id"], "search")

    async def test_playlist_keeps_playlist_and_track_plugin_info(self) -> None:
        """Playlist and member track plugin metadata remain separate."""
        request = AsyncMock(
            return_value={
                "loadType": "playlist",
                "data": {
                    "info": {"name": "playlist", "selectedTrack": 0},
                    "pluginInfo": {"playlist": "metadata"},
                    "tracks": [track_payload("playlist-track")],
                },
            }
        )

        with patch.object(Node, "_Node__request", request):
            result = await self.node.fetch_tracks("playlist", search_type="")

        if not isinstance(result, Playlist):
            self.fail("Playlist load did not return a Playlist.")
        self.assertEqual(result.plugin_info, {"playlist": "metadata"})
        self.assertEqual(result.tracks[0].user_data["correlation_id"], "playlist-track")
        self.assertEqual(result.tracks[0].plugin_info, {"album": "metadata"})

    async def test_decode_track_preserves_metadata(self) -> None:
        """Single track decoding parses the complete v4 Track object."""
        request = AsyncMock(return_value=track_payload("decode"))

        with patch.object(Node, "_Node__request", request):
            track = await self.node.decode_track("encoded-track")

        self.assertEqual(track.user_data["correlation_id"], "decode")
        self.assertEqual(track.plugin_info, {"album": "metadata"})

    async def test_decode_tracks_preserves_metadata(self) -> None:
        """Bulk track decoding parses complete v4 Track objects."""
        request = AsyncMock(
            return_value=[track_payload("decode-a"), track_payload("decode-b")]
        )

        with patch.object(Node, "_Node__request", request):
            tracks = await self.node.decode_tracks(["encoded-a", "encoded-b"])

        self.assertEqual(
            [track.user_data["correlation_id"] for track in tracks],
            ["decode-a", "decode-b"],
        )
