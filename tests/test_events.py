"""Tests for Lavalink v4 track events."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import TestCase
from unittest.mock import Mock

from mafic.errors import TrackLoadException
from mafic.player import Player
from mafic.track import Track

from .test_player_update import make_node
from .test_track import track_payload

if TYPE_CHECKING:
    from mafic.__libraries import Client
    from mafic.events import (
        TrackEndEvent,
        TrackExceptionEvent,
        TrackStartEvent,
        TrackStuckEvent,
    )
    from mafic.typings import EventPayload, LavalinkException

    TrackEvent = (
        TrackStartEvent[Player[Client]]
        | TrackEndEvent[Player[Client]]
        | TrackExceptionEvent[Player[Client]]
        | TrackStuckEvent[Player[Client]]
    )


class TrackEventMetadataTests(TestCase):
    """Ensure every Lavalink v4 track event keeps metadata."""

    def setUp(self) -> None:
        """Build a minimal connected v4 player."""
        self.dispatch = Mock()
        self.player: Player[Client] = object.__new__(Player)
        self.player._node = make_node(4)
        self.player.client = cast("Client", SimpleNamespace(dispatch=self.dispatch))
        self.player._current = Track.from_data_with_info(track_payload("current"))
        self.player._last_track = self.player._current

    def dispatch_track_event(self, payload: EventPayload) -> TrackEvent:
        """Dispatch a payload and return the public event object."""
        self.player.dispatch_event(payload)
        if self.dispatch.call_args is None:
            self.fail("Player did not dispatch a track event.")
        return cast("TrackEvent", self.dispatch.call_args.args[1])

    def test_all_v4_track_events_preserve_user_data(self) -> None:
        """Start, end, exception and stuck events parse the full Track."""
        cases: list[EventPayload] = [
            cast(
                "EventPayload",
                {
                    "op": "event",
                    "type": "TrackStartEvent",
                    "guildId": "1",
                    "track": track_payload("start"),
                },
            ),
            cast(
                "EventPayload",
                {
                    "op": "event",
                    "type": "TrackEndEvent",
                    "guildId": "1",
                    "track": track_payload("end"),
                    "reason": "finished",
                },
            ),
            cast(
                "EventPayload",
                {
                    "op": "event",
                    "type": "TrackExceptionEvent",
                    "guildId": "1",
                    "track": track_payload("exception"),
                    "exception": {
                        "message": None,
                        "severity": "fault",
                        "cause": "failure",
                        "causeStackTrace": "trace",
                    },
                },
            ),
            cast(
                "EventPayload",
                {
                    "op": "event",
                    "type": "TrackStuckEvent",
                    "guildId": "1",
                    "track": track_payload("stuck"),
                    "thresholdMs": 10_000,
                },
            ),
        ]

        for payload, expected in zip(cases, ("start", "end", "exception", "stuck")):
            with self.subTest(event_type=payload["type"]):
                event = self.dispatch_track_event(payload)
                self.assertEqual(event.track.user_data["correlation_id"], expected)
                self.assertEqual(event.track.plugin_info, {"album": "metadata"})

    def test_identical_event_tracks_remain_distinguishable(self) -> None:
        """Event metadata identifies repeated playback of one source track."""
        events: list[TrackEvent] = []
        for correlation_id in ("A", "B"):
            payload = cast(
                "EventPayload",
                {
                    "op": "event",
                    "type": "TrackEndEvent",
                    "guildId": "1",
                    "track": track_payload(correlation_id),
                    "reason": "finished",
                },
            )
            events.append(self.dispatch_track_event(payload))

        self.assertEqual(events[0].track.id, events[1].track.id)
        self.assertEqual(events[0].track.identifier, events[1].track.identifier)
        self.assertEqual(events[0].track.user_data["correlation_id"], "A")
        self.assertEqual(events[1].track.user_data["correlation_id"], "B")

    def test_v4_replaced_event_keeps_current_track(self) -> None:
        """A replaced end event must not clear the newly current track."""
        current = self.player._current
        payload = cast(
            "EventPayload",
            {
                "op": "event",
                "type": "TrackEndEvent",
                "guildId": "1",
                "track": track_payload("old"),
                "reason": "replaced",
            },
        )

        self.dispatch_track_event(payload)

        self.assertIs(self.player.current, current)


class LavalinkExceptionTests(TestCase):
    """Verify the stable Lavalink 4.2 exception contract."""

    def test_nullable_message_and_stack_trace_are_preserved(self) -> None:
        """Load errors accept message=null and retain causeStackTrace."""
        payload: LavalinkException = {
            "message": None,
            "severity": "fault",
            "cause": "failure",
            "causeStackTrace": "full trace",
        }

        error = TrackLoadException.from_data(payload)

        self.assertIsNone(error.message)
        self.assertEqual(error.cause_stack_trace, "full trace")
