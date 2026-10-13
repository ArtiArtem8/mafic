"""Playback identity and monotonic position through real Player operations."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Literal
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from mafic.__libraries import MISSING, VoiceProtocol
from mafic.node import Node
from mafic.track import Track

from .test_player_update import make_player, player_payload
from .test_track import track_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mafic.typings import JSONValue, Player as PlayerPayload, TrackWithInfo
    from mafic.typings.incoming import TrackEndEvent


class PlayerContracts(IsolatedAsyncioTestCase):
    """Exercise REST/event races without replacing Player's state machine."""

    def setUp(self) -> None:
        """Create a real player and an authoritative fake REST server."""
        self.player = make_player()
        self.state = player_payload()
        self.player.set_state(deepcopy(self.state))
        self.dispatch = Mock()
        self.player.client.dispatch = self.dispatch

    async def accepted(
        self,
        *,
        user_data: Mapping[str, JSONValue] = MISSING,
        pause: bool | None = None,
        position: int | None = None,
        no_replace: bool = False,
        **_kwargs: object,
    ) -> PlayerPayload:
        """Echo accepted metadata and state as Lavalink would."""
        if user_data is not MISSING and not no_replace:
            self.state["track"] = track_payload()
            self.state["track"]["userData"] = dict(user_data)
        if pause is not None:
            self.state["paused"] = pause
        if position is not None:
            self.state["state"]["position"] = position
        return deepcopy(self.state)

    def end(
        self,
        track: TrackWithInfo,
        reason: Literal["finished", "stopped", "replaced"] = "finished",
    ) -> None:
        """Deliver a public event including its complete transport metadata."""
        payload: TrackEndEvent = {
            "op": "event",
            "type": "TrackEndEvent",
            "guildId": "1",
            "track": track,
            "reason": reason,
        }
        self.player.dispatch_event(payload)

    async def test_new_launches_keep_user_fields_and_ignore_old_ends(self) -> None:
        """Replaying one Track creates identities while stale events still dispatch."""
        track = Track.from_data_with_info(track_payload())
        original = deepcopy(track.user_data)
        with patch.object(Node, "update", side_effect=self.accepted):
            await self.player.play(track)
            first = deepcopy(self.state["track"])
            await self.player.play(track)
            second = deepcopy(self.state["track"])
        if first is None or second is None:
            self.fail("Expected accepted tracks")
        first_id = first.get("userData", {})["__mafic_playback_id"]
        second_id = second.get("userData", {})["__mafic_playback_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertIsInstance(first_id, str)
        self.assertIsInstance(second_id, str)
        self.assertEqual(UUID(str(second_id)).version, 4)
        self.assertEqual(track.user_data, original)
        current = self.player.current
        for reason in ("stopped", "finished", "replaced", "finished"):
            self.end(first, reason)
            self.assertIs(self.player.current, current)
        self.assertEqual(self.dispatch.call_count, 4)
        self.end(second)
        self.assertIsNone(self.player.current)
        self.end(second)
        self.assertIsNone(self.player.current)

    async def test_end_before_rest_response_cannot_resurrect_launch(self) -> None:
        """An already ended accepted launch remains ended after its response."""

        async def early_end(
            *, user_data: Mapping[str, JSONValue], **_kwargs: object
        ) -> PlayerPayload:
            response = await self.accepted(user_data=user_data)
            track = response["track"]
            if track is None:
                self.fail("Expected track")
            self.end(track)
            return response

        with patch.object(Node, "update", side_effect=early_end):
            await self.player.play("identifier")
        self.assertIsNone(self.player.current)

    async def test_no_replace_keeps_server_accepted_identity(self) -> None:
        """The server response wins when noReplace declines a new launch."""
        with patch.object(Node, "update", side_effect=self.accepted):
            await self.player.play("first")
            first = self.player.current
            await self.player.play("second", replace=False)
        self.assertIsNotNone(first)
        self.assertIsNotNone(self.player.current)
        if first is not None and self.player.current is not None:
            self.assertEqual(first.user_data, self.player.current.user_data)

    async def test_position_pause_seek_resume_uses_receipt_monotonic_time(self) -> None:
        """Server wall timestamps cannot advance paused or disconnected playback."""
        with patch("mafic.player.monotonic", return_value=10) as clock:
            self.player.set_state(self.state)
            clock.return_value = 12
            self.assertEqual(self.player.position, 2250)
            self.assertEqual(self.player.reported_position, 250)
            with patch.object(Node, "update", side_effect=self.accepted):
                await self.player.pause()
                clock.return_value = 1000
                self.assertEqual(self.player.position, 250)
                await self.player.seek(5000)
                self.assertEqual(self.player.position, 5000)
                await self.player.resume()
                clock.return_value = 1002
                self.assertEqual(self.player.position, 7000)
                self.state["state"].update(time=-100000, position=6000, connected=False)
                self.player.update_state(
                    {"time": -100000, "position": 6000, "connected": False, "ping": -1}
                )
                clock.return_value = 2000
                self.assertEqual(self.player.position, 6000)
                self.state["track"] = None
                self.state["state"]["connected"] = True
                self.player.set_state(self.state)
                clock.return_value = 3000
                self.assertEqual(self.player.position, 6000)

    async def test_null_update_clears_current(self) -> None:
        """REST stop responses clear current even without a websocket event."""
        self.state["track"] = None
        with patch.object(Node, "update", new=AsyncMock(return_value=self.state)):
            await self.player.stop()
        self.assertIsNone(self.player.current)

    async def test_restored_untagged_end_confirms_rest_and_respects_generation(
        self,
    ) -> None:
        """Ambiguous legacy launches need server confirmation guarded by generation."""
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(_guild_id: int) -> PlayerPayload:
            entered.set()
            await release.wait()
            response = deepcopy(self.state)
            response["track"] = None
            return response

        with patch.object(Node, "fetch_player", side_effect=fetch):
            self.end(track_payload())
            await entered.wait()
            with patch.object(Node, "update", side_effect=self.accepted):
                await self.player.play("new")
            current = self.player.current
            release.set()
            if self.player._end_confirmation is not None:
                await self.player._end_confirmation
            self.assertIs(self.player.current, current)

        self.player.set_state(player_payload())
        with patch.object(Node, "fetch_player", side_effect=fetch):
            self.end(track_payload())
            if self.player._end_confirmation is not None:
                await self.player._end_confirmation
        self.assertIsNone(self.player.current)

    async def test_disconnect_cancels_owned_confirmation(self) -> None:
        """Disconnect joins its ambiguous-end check before tearing down voice."""
        entered = asyncio.Event()

        async def fetch(_guild_id: int) -> PlayerPayload:
            entered.set()
            await asyncio.Event().wait()
            return self.state

        with (
            patch.object(Node, "fetch_player", side_effect=fetch),
            patch.object(Node, "destroy", new_callable=AsyncMock),
            patch.object(Node, "remove_player"),
            patch.object(VoiceProtocol, "cleanup"),
            patch.object(
                self.player.guild, "change_voice_state", new_callable=AsyncMock
            ),
        ):
            self.end(track_payload())
            await entered.wait()
            await self.player.disconnect(force=True)
        self.assertFalse(self.player.connected)
        self.assertIsNone(self.player.current)
        if self.player._end_confirmation is not None:
            self.assertTrue(self.player._end_confirmation.cancelled())
