"""Tests for Lavalink track metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import TestCase

from mafic.track import Track

if TYPE_CHECKING:
    from mafic.typings import TrackWithInfo


def track_payload(correlation_id: str = "request-a") -> TrackWithInfo:
    """Build a complete Lavalink 4 track payload."""
    return {
        "encoded": "encoded-track",
        "info": {
            "identifier": "source-id",
            "isSeekable": True,
            "author": "artist",
            "length": 60_000,
            "isStream": False,
            "position": 0,
            "title": "title",
            "uri": "https://example.com/track",
            "artworkUrl": None,
            "isrc": None,
            "sourceName": "test",
        },
        "pluginInfo": {"album": "metadata"},
        "userData": {"correlation_id": correlation_id},
    }


class TrackMetadataTests(TestCase):
    """Ensure complete Lavalink 4 tracks survive parsing."""

    def test_parses_plugin_info_and_user_data(self) -> None:
        """Track parsing keeps plugin info and user data."""
        payload = track_payload()

        track = Track.from_data_with_info(payload)

        self.assertEqual(track.plugin_info, {"album": "metadata"})
        self.assertEqual(track.user_data, {"correlation_id": "request-a"})

    def test_copies_metadata_mappings(self) -> None:
        """Track parsing copies caller-owned top-level mappings."""
        payload = track_payload()

        track = Track.from_data_with_info(payload)
        plugin_info = payload.get("pluginInfo")
        user_data = payload.get("userData")
        if plugin_info is None or user_data is None:
            self.fail("The complete test payload must contain metadata.")
        plugin_info["album"] = "changed"
        user_data["correlation_id"] = "changed"

        self.assertEqual(track.plugin_info, {"album": "metadata"})
        self.assertEqual(track.user_data, {"correlation_id": "request-a"})

    def test_missing_metadata_uses_empty_mappings(self) -> None:
        """Old Lavalink 4 payloads may omit metadata fields."""
        payload = track_payload()
        payload.pop("pluginInfo", None)
        payload.pop("userData", None)

        track = Track.from_data_with_info(payload)

        self.assertEqual(track.plugin_info, {})
        self.assertEqual(track.user_data, {})

    def test_identical_source_tracks_keep_distinct_correlation_ids(self) -> None:
        """User data distinguishes otherwise identical playback attempts."""
        track_a = Track.from_data_with_info(track_payload("A"))
        track_b = Track.from_data_with_info(track_payload("B"))

        self.assertEqual(track_a.id, track_b.id)
        self.assertEqual(track_a.identifier, track_b.identifier)
        self.assertEqual(track_a.source, track_b.source)
        self.assertEqual(track_a.user_data["correlation_id"], "A")
        self.assertEqual(track_b.user_data["correlation_id"], "B")
