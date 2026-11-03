"""A Player is used to connect to a channel."""

# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import warnings
from asyncio import Event, Lock
from collections import OrderedDict
from copy import copy as shallow_copy
from functools import reduce
from logging import getLogger
from time import monotonic
from typing import TYPE_CHECKING, Generic, cast
from uuid import uuid4

from .__libraries import (
    MISSING,
    GuildChannel,
    StageChannel,
    VoiceChannel,
    VoiceProtocol,
)
from ._voice_ownership import coordinator_for
from .errors import (
    HTTPException,
    HTTPNotFound,
    NoNodesAvailable,
    PlayerNotConnected,
)
from .events import *
from .filter import Filter
from .pool import NodePool, pool_for
from .search_type import SearchType
from .track import Track
from .type_variables import ClientT
from .typings import TrackWithInfo
from .utils.tasks import await_owned

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .__libraries import (
        Connectable,
        Guild,
        GuildVoiceStatePayload,
        VoiceServerUpdatePayload,
    )
    from .node import Node
    from .playlist import Playlist
    from .typings import (
        EventPayload,
        JSONValue,
        Player as PlayerPayload,
        PlayerUpdateState,
    )


_log = getLogger(__name__)
__all__ = ("Player",)


_REJECTED_STATUSES = frozenset({400, 401, 403, 404, 405, 422})
"""Statuses that prove the server refused the request before applying it."""


def _consume_teardown_result(task: asyncio.Task[None]) -> None:
    """Retrieve a detached teardown result so asyncio does not warn about it.

    A cancelled caller hands cleanup to the owned task and returns; this keeps
    the task observed without making it an unmanaged background job.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _log.error(
            "Detached voice teardown failed.",
            exc_info=(type(error), error, error.__traceback__),
        )


def _is_unknown_outcome(error: BaseException) -> bool:
    """Whether a failed remote effect may still have been applied.

    Only a refusal proves the effect did not happen. Everything else is
    undecided: 408 and 5xx may have run downstream, and a timeout, dropped
    transport or cancellation has no response at all, so the request may still
    reach Lavalink later.
    """
    if isinstance(error, HTTPException):
        return error.status not in _REJECTED_STATUSES
    return True


_PLAYBACK_ID_KEY = "__mafic_playback_id"


class Player(VoiceProtocol, Generic[ClientT]):
    """Represents a player for a guild.

    .. note::

        This class is not meant to be instantiated by the user.
        Use ``Connectable.connect()`` from your Discord library to connect.

    Parameters
    ----------
    client:
        The client that the player is associated with.
    channel:
        The voice channel to connect to.
    node:
        The node to use for the player. If not provided, the best node will be used.

    Attributes
    ----------
    client: :data:`~mafic.type_variables.ClientT`
        The client that the player is associated with.
    channel:
        The voice channel that the player is connected to.
        This is an ``abc.Connectable`` from your Discord library.
    guild:
        The guild that the player is associated with.
        This is a ``Guild`` from your Discord library.
    """

    # VoiceProtocol is not generic; preserve the concrete client subtype.
    client: ClientT

    def __init__(
        self,
        client: ClientT,
        channel: Connectable,
        *,
        node: Node[ClientT] | None = None,
    ) -> None:
        self.client = client  # pyright: ignore[reportIncompatibleVariableOverride]
        self.channel: Connectable = channel

        if not isinstance(self.channel, GuildChannel):
            msg = "Voice channel must be a GuildChannel."
            raise TypeError(msg)

        self.guild: Guild = self.channel.guild
        self.endpoint: str | None = None

        self._node = node

        self._guild_id: int = self.guild.id
        # One guild has one authoritative voice owner per client. The
        # coordinator mints the generation this instance is compared against.
        self._coordinator = coordinator_for(client, self._guild_id)
        self._ownership_epoch = 0
        # Set immediately before the gateway join is sent, so a cancellation
        # can tell "nothing remote happened" from "it may already have".
        self._join_sent = False
        self._session_id: str | None = None
        self._server_state: VoiceServerUpdatePayload | None = None
        self._connected: bool = False
        self._position: int = 0
        self._position_received_at = monotonic()
        self._state_updates = 0
        self._playback_generation = 0
        self._pending_updates = 0
        self._ended_playbacks: set[str] = set()
        self._end_confirmation: asyncio.Task[None] | None = None
        self._ping = -1
        self._current: Track | None = None
        self._filters: OrderedDict[str, Filter] = OrderedDict()
        # Used to get the last track for TrackEndEvent.
        self._last_track: Track | None = None
        self._paused: bool = False
        self._volume: int = 100

        self._voice_server_update_event: Event = Event()
        self._voice_state_update_event: Event = Event()
        self._node_player_ready_event: Event = Event()
        self._move_lock: Lock = Lock()
        self._voice_update_lock: Lock = Lock()
        # Terminal for this instance; reconnect creates a new Discord protocol.
        self._disconnect_requested: bool = False
        self._disconnect_task: asyncio.Task[None] | None = None

    def set_state(self, state: PlayerPayload) -> None:
        """Set the state of the player.

        .. note::

            This is used internally to set the state of the player.
            You should not need to use this.
        """
        self._session_id = state["voice"]["sessionId"]
        self._playback_generation += 1
        self.endpoint = state["voice"]["endpoint"]
        track = Track.from_data_with_info(state["track"]) if state["track"] else None
        self._state_updates = 0
        self._sync_playback_state(state, track)
        self._filters = OrderedDict({"RESUME": Filter.from_payload(state["filters"])})
        self._server_state = {
            "token": state["voice"]["token"],
            "endpoint": state["voice"]["endpoint"],
            "guild_id": self._guild_id,
        }

    def _sync_playback_state(
        self,
        state: PlayerPayload,
        track: Track | None,
        *,
        preserve_live_state: bool = False,
    ) -> None:
        """Apply REST playback state without replacing Discord voice credentials."""
        self._current = track
        if not preserve_live_state:
            self._position = state["state"]["position"]
            self._position_received_at = monotonic()
            self._connected = state["state"]["connected"]
            self._ping = state["state"]["ping"]
        elif self._paused and not state["paused"]:
            self._position_received_at = monotonic()
        self._volume = state["volume"]
        self._paused = state["paused"]

    def __repr__(self) -> str:
        """Return a string representation of the player."""
        attrs = (
            ("guild_id", self._guild_id),
            ("session_id", self._session_id),
            ("connected", self._connected),
            ("position", self._position),
            ("current", self._current),
        )
        resolved = " ".join(f"{attr}={value!r}" for attr, value in attrs)
        return f"<{type(self).__name__} {resolved}>"

    @property
    def connected(self) -> bool:
        """Whether Lavalink reports a connection to the Discord voice gateway."""
        return self._connected

    @property
    def reported_position(self) -> int:
        """The last accepted server position, in milliseconds.

        This read-only value does not extrapolate between server updates.
        For an active launch, websocket updates take precedence over REST
        snapshots, which can briefly report a pre-seek audio position.
        """
        return self._position

    @property
    def position(self) -> int:
        """The current position of the player in milliseconds."""
        pos = self._position

        if self._connected and self._current is not None and not self._paused:
            # Add the time since the last update to the position.
            # If the track total time is less than that, use that.
            pos = min(
                self._current.length,
                pos + max(0, int((monotonic() - self._position_received_at) * 1000)),
            )

        return pos

    @property
    def ping(self) -> int:
        """The current ping of the player in milliseconds."""
        return self._ping

    @property
    def node(self) -> Node[ClientT]:
        """The assigned node, or a usable node from the pool.

        If no node is assigned, this selects a random available node without
        assigning it to the player. Use :attr:`assigned_node` to inspect the
        current assignment without selecting a fallback.
        """
        if self._node is None:
            _log.warning(
                "Player has no assigned node and cannot select one.",
                extra={"guild": self._guild_id},
            )
            raise NoNodesAvailable

        return self._node

    @property
    def _pool(self) -> NodePool[ClientT] | None:
        """The node scope of this player's Discord client, if one exists."""
        return cast("NodePool[ClientT] | None", pool_for(self.client))

    @property
    def assigned_node(self) -> Node[ClientT] | None:
        """The node explicitly assigned to this player, if any.

        Unlike :attr:`node`, this only reports the current assignment. It returns
        ``None`` when unassigned and never selects or assigns a fallback node.
        """
        return self._node

    @property
    def current(self) -> Track | None:
        """The current track that is playing."""
        return self._current

    @property
    def paused(self) -> bool:
        """Whether the player is paused."""
        return self._paused

    def update_state(self, state: PlayerUpdateState) -> None:
        """Update the player state.

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        state:
            The state to update the player with.
        """
        if not self._node_player_ready_event.is_set():
            self._node_player_ready_event.set()

        self._position = state.get("position", 0)
        self._state_updates += 1
        self._position_received_at = monotonic()
        self._connected = state["connected"]
        self._ping = state.get("ping", -1)

    # If people are so in love with the VoiceClient interface
    def is_connected(self) -> bool:
        """Whether the player is connected to a voice channel.

        This is an alias for :attr:`connected`.
        """
        return self._connected

    async def _dispatch_player_update(self) -> None:
        """Dispatch a player update to the node."""
        if self._coordinator.holds_lane():
            # A gateway callback delivered inside a move already runs in the
            # lane. Re-acquiring it would deadlock, and no other remote effect
            # can run while the caller holds it, so the ordering stands.
            async with self._voice_update_lock:
                await self._dispatch_player_update_unlocked()
            return

        # Ordered against every other generation of this guild, so a create
        # cannot overtake a predecessor's still-running destroy.
        async with self._coordinator.hold(), self._voice_update_lock:
            await self._dispatch_player_update_unlocked()

    async def _dispatch_player_update_unlocked(self) -> None:
        """Dispatch using current state while holding `_voice_update_lock`."""
        # The epoch check must precede the PATCH: a superseded generation that
        # still holds a stale node pointer would otherwise recreate the player
        # a newer generation just installed in the same session slot.
        if self._disconnect_requested or self._superseded():
            return

        node = self._node
        if node is None:
            _log.debug("Received voice update before node was found.")
            return

        session_id = self._session_id
        if session_id is None:
            _log.debug("Received player update before session ID was set.")
            return

        server_state = self._server_state
        if server_state is None:
            _log.debug("Received player update before server state was found.")
            return

        channel = self.channel
        if not isinstance(channel, (VoiceChannel, StageChannel)):
            _log.debug("Received player update before channel was set.")
            return

        try:
            await node.voice_update(
                guild_id=self._guild_id,
                session_id=session_id,
                data=server_state,
                channel_id=int(channel.id),
            )
        except BaseException as error:
            self._note_remote_failure(error)
            raise

    def dispatch_event(self, data: EventPayload) -> None:
        """Dispatch an event to the player.

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        data:
            The event payload to dispatch.
        """
        if data["type"] == "WebSocketClosedEvent":
            event = WebSocketClosedEvent(payload=data, player=self)
            _log.debug("Received websocket closed event: %s", event)
            self.client.dispatch("websocket_closed", event)
        elif data["type"] == "TrackStartEvent":
            track = self._track_from_event(data)

            if track is None:
                _log.error(
                    "Received track start event but no track was playing, discarding."
                )
                return

            event = TrackStartEvent(player=self, track=track)
            self.client.dispatch("track_start", event)
            _log.debug("Received track start event: %s", event)
            self._last_track = track
        elif data["type"] == "TrackEndEvent":
            track = self._track_from_event(data)

            if track is None:
                _log.error(
                    "Received track end event but no track was playing, discarding."
                )
                return

            event = TrackEndEvent(player=self, track=track, payload=data)
            self.client.dispatch("track_end", event)
            _log.debug("Received track end event: %s", event)

            self._end_playback(
                track, replaced=data["reason"] in ("REPLACED", "replaced")
            )
        elif data["type"] == "TrackExceptionEvent":
            track = self._track_from_event(data)

            if track is None:
                _log.error(
                    "Received track exception event but no track was playing, "
                    "discarding."
                )
                return

            event = TrackExceptionEvent(player=self, track=track, payload=data)
            self.client.dispatch("track_exception", event)
            _log.debug("Received track exception event: %s", event)
        elif data["type"] == "TrackStuckEvent":
            track = self._track_from_event(data)

            if track is None:
                _log.error(
                    "Received track stuck event but no track was playing, discarding."
                )
                return

            event = TrackStuckEvent(player=self, track=track, payload=data)
            self.client.dispatch("track_stuck", event)
            _log.debug("Received track stuck event: %s", event)
        else:
            # Pyright expects this to never happen, so do I, I really hope.
            # Nobody expects the Spanish Inquisition, neither does pyright.

            event_type = cast(str, data["type"])
            _log.warning("Unknown incoming event type %s", event_type)

    def _track_from_event(self, data: EventPayload) -> Track | None:
        """Resolve an event's track from the v4 payload or v3 player state."""
        if self.node.version == 3:
            return (
                self._last_track if data["type"] == "TrackEndEvent" else self._current
            )
        return Track.from_data_with_info(cast(TrackWithInfo, data.get("track")))

    @staticmethod
    def _playback_id(track: Track | None) -> str | None:
        """Read the reserved v4 launch identity without relying on encoded audio."""
        value = track.user_data.get(_PLAYBACK_ID_KEY) if track else None
        return value if isinstance(value, str) else None

    def _end_playback(self, track: Track, *, replaced: bool) -> None:
        """Clear only the ended launch; confirm untagged restored state via REST."""
        ended = self._playback_id(track)
        if ended is not None and self._pending_updates:
            self._ended_playbacks.add(ended)
        if replaced:
            return
        if self.node.version == 3:
            self._current = None
            return
        current = self._playback_id(self._current)
        if current is not None:
            if current == ended:
                self._current = None
        elif (
            self._current is not None
            and not self._disconnect_requested
            and (self._end_confirmation is None or self._end_confirmation.done())
        ):
            self._end_confirmation = asyncio.create_task(
                self._confirm_end(self.node, self._playback_generation, self._current)
            )

    async def _confirm_end(
        self, node: Node[ClientT], generation: int, current: Track
    ) -> None:
        """Resolve ambiguous restored playback without overwriting a newer owner."""
        try:
            state = await node.fetch_player(self._guild_id)
            if (
                self._node is node
                and not self._disconnect_requested
                and self._playback_generation == generation
                and self._current is current
            ):
                self._sync_playback_state(
                    state,
                    self._track_from_snapshot(state, node.version),
                    preserve_live_state=self._state_updates > 0,
                )
        except Exception:  # noqa: BLE001 - owned background reconciliation boundary
            _log.warning(
                "Unable to confirm ambiguous track end for guild %s", self._guild_id
            )
            _log.debug("Track end confirmation traceback", exc_info=True)

    async def on_voice_state_update(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, data: GuildVoiceStatePayload
    ) -> None:
        """Dispatch a voice state update.

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        data:
            The voice state update payload.
        """
        if self._disconnect_requested:
            return

        before_session_id = self._session_id
        before_channel_id = (
            self.channel.id
            if isinstance(self.channel, (VoiceChannel, StageChannel))
            else None
        )
        self._session_id = data["session_id"]

        channel_id = data["channel_id"]

        if channel_id is None:  # pyright: ignore[reportUnnecessaryComparison]
            await self.disconnect(force=True)
            return

        channel = self.guild.get_channel(int(channel_id))
        if not isinstance(channel, (VoiceChannel, StageChannel)):
            msg = "Channel was not a connectable channel that was recognised."
            raise TypeError(msg)

        self.channel = channel

        if self._session_id != before_session_id or channel.id != before_channel_id:
            await self._dispatch_player_update()

        self._voice_state_update_event.set()

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        """Handle a voice server update.

        The guild lane orders this update against the remote effects of every
        other generation for the same guild.

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        data:
            The voice server update payload.
        """
        if self._coordinator.holds_lane():
            # Same inline-callback case as _dispatch_player_update.
            if self._superseded():
                return
            await self._apply_voice_server_update(data)
            self._voice_server_update_event.set()
            return

        async with self._coordinator.hold():
            if self._superseded():
                return
            await self._apply_voice_server_update(data)
        self._voice_server_update_event.set()

    async def _apply_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        """Apply a voice server update while the caller holds the guild lane."""
        async with self._voice_update_lock:
            # The lane does not protect against a synchronous claim: a newer
            # generation can appear while this callback waits for the lock, or
            # during the REST preparation below. This check sits immediately
            # before the registry write, with no await in between.
            if self._disconnect_requested or self._superseded():
                return

            current_node = self._node
            if current_node is None or (
                self._server_state is not None
                and self._server_state["endpoint"] != data["endpoint"]
            ):
                _log.debug(
                    "Getting best node for player", extra={"guild": self._guild_id}
                )
                pool = self._pool
                if pool is None:
                    raise NoNodesAvailable
                selected_node = pool.get_node(
                    guild_id=data["guild_id"], endpoint=data["endpoint"]
                )
                _log.debug(
                    "Got best node for player: %s",
                    selected_node.label,
                    extra={"guild": self._guild_id},
                )
            else:
                selected_node = current_node

            # Discord owns these credentials even if node migration later fails.
            self._guild_id = int(data["guild_id"])
            self.endpoint = data["endpoint"]
            self._server_state = data

            if current_node is not None and selected_node is not current_node:
                try:
                    await self._transfer_to_unlocked(selected_node)
                except PlayerNotConnected:
                    # A newer generation claimed while the target was being
                    # prepared; the partial target is already cleaned up.
                    return
            else:
                self._node = selected_node
                selected_node.add_player(self._guild_id, self)
                await self._dispatch_player_update_unlocked()

    async def connect(
        self,
        *,
        timeout: float,
        reconnect: bool,  # noqa: ARG002
        self_mute: bool = False,
        self_deaf: bool = False,
    ) -> None:
        """Connect to a voice channel.

        This is called by your Discord library when using ``Connectable.connect``.

        Parameters
        ----------
        timeout:
            The timeout for the connection.
        reconnect:
            Whether to reconnect to the voice channel if the connection is lost.
        self_mute:
            Whether to mute the bot on connect.
        self_deaf:
            Whether to deafen the bot on connect.
        """
        if not isinstance(self.channel, (VoiceChannel, StageChannel)):
            msg = "Voice channel must be a VoiceChannel or StageChannel."
            raise TypeError(msg)

        pool = self._pool
        if pool is None or not pool.nodes:
            self.cleanup()
            raise NoNodesAvailable

        _log.debug("Connecting to voice channel %s", self.channel.id)

        # The claim is this connect's linearization point. It demotes any
        # older generation immediately, so a teardown still in flight can no
        # longer act for this guild once the claim has happened.
        self._take_ownership()
        if self._coordinator.remote_unknown:
            # Lavalink offers no conditional write, so an earlier DELETE that
            # timed out may still be applied after this join and remove the
            # player we are about to create. Only a different session fences
            # that; record the risk rather than hiding it.
            _log.warning(
                "Connecting over an unresolved remote state; a late DELETE "
                "from a previous generation may remove this player.",
                extra={"guild": self._guild_id},
            )
        failure: BaseException | None = None
        try:
            async with asyncio.timeout(timeout):
                async with self._coordinator.hold():
                    if self._coordinator.is_current(self, self._ownership_epoch):
                        self._join_sent = True
                        await self.channel.guild.change_voice_state(
                            channel=self.channel,
                            self_mute=self_mute,
                            self_deaf=self_deaf,
                        )
                    else:
                        failure = PlayerNotConnected()

            if failure is None and not await self._wait_for_voice_events(timeout):
                failure = asyncio.TimeoutError()
        except BaseException as error:
            await self._compensate_failed_connect(error)
            raise

        # Readiness can finish after another generation claimed the guild.
        if failure is None and self._superseded():
            failure = PlayerNotConnected()
        if failure is not None:
            await self._compensate_failed_connect(failure)
            raise failure

    async def _wait_for_voice_events(self, timeout: float) -> bool:
        """Wait for Discord and Lavalink readiness, retiring every waiter."""
        waiters = [
            asyncio.create_task(event.wait())
            for event in (
                self._voice_state_update_event,
                self._voice_server_update_event,
                self._node_player_ready_event,
            )
        ]
        try:
            _, pending = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.ALL_COMPLETED
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

        return not pending

    async def _compensate_failed_connect(self, error: BaseException) -> None:
        """Undo a failed connect without disturbing a newer owner.

        ``Connectable.connect`` only compensates on :exc:`asyncio.TimeoutError`,
        so a gateway error or a cancellation would otherwise leave this instance
        holding the Discord voice slot and the guild generation.

        A cancellation is split by whether the gateway join was sent:

        * before it, nothing remote can exist and only local registrations are
          retired - a cancelled caller must not queue behind a busy lane;
        * after it, the join, and possibly a Lavalink player, may already
          exist. The teardown is then started in its owned single-flight task
          and not awaited here, so the caller still unwinds promptly. Any later
          :meth:`disconnect` or :meth:`wait_for_disconnect` joins that task,
          and its failure is retrieved rather than left unobserved.
        """
        if (
            isinstance(error, asyncio.CancelledError)
            and self._join_sent
            and not self._superseded()
        ):
            task = self._start_teardown(force=True)
            if task is not None:
                task.add_done_callback(_consume_teardown_result)
            return

        if (
            isinstance(error, asyncio.CancelledError)
            or self._ownership_epoch == 0
            or self._coordinator.owner is not self
        ):
            # Cancelled before the join, never claimed, or already superseded:
            # only local registrations belong to this instance.
            self._detach_local()
            return

        try:
            await self.disconnect(force=True)
        except Exception:  # noqa: BLE001 - preserve the original connect failure
            _log.exception(
                "Failed to clean up after an unsuccessful connect.",
                extra={"guild": self._guild_id},
            )

    async def move_to(
        self,
        channel: VoiceChannel | StageChannel,
        *,
        timeout: float = 30.0,
    ) -> None:
        """Move the player to another voice or stage channel in the same guild.

        Parameters
        ----------
        channel:
            The voice or stage channel to move to. Moving to the current channel
            does nothing. Use :meth:`disconnect` to leave voice.
        timeout:
            Maximum time in seconds to wait for earlier moves, request and
            confirm the channel change.

        Raises
        ------
        TypeError
            If the channel is not a voice or stage channel.
        ValueError
            If the channel belongs to another guild.
        PlayerNotConnected
            If this player is no longer the guild's active Discord voice protocol.
        asyncio.TimeoutError
            If the voice state callback does not confirm the target in time.
            Discord may still complete the move after this timeout.

        Notes
        -----
        Moves on this player are serialized in arrival order. Completion confirms
        the Discord channel transition and the voice state callback's Lavalink
        request. It does not guarantee a ready Lavalink voice connection: a later
        voice server update can replace the credentials and trigger reconnection,
        even after Lavalink briefly reported connected. Server updates are handled
        independently and are not required to complete every move. Lavalink
        voice updates are serialized per player.

        A temporary Lavalink disconnect does not prevent moving an active Discord
        voice protocol. Disconnect is independent of this queue; an interrupted
        move may time out, and queued moves recheck the protocol registration.
        """
        if self._disconnect_requested:
            raise PlayerNotConnected

        if not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
            channel, (VoiceChannel, StageChannel)
        ):
            msg = "Voice channel must be a VoiceChannel or StageChannel."
            raise TypeError(msg)
        if channel.guild.id != self.guild.id:
            msg = "Voice channel must belong to the player's guild."
            raise ValueError(msg)
        async with asyncio.timeout(timeout):
            async with self._move_lock:
                if self._disconnect_requested or self.guild.voice_client is not self:
                    raise PlayerNotConnected
                if (
                    isinstance(self.channel, (VoiceChannel, StageChannel))
                    and self.channel.id == channel.id
                ):
                    return

                # JOIN, MOVE and LEAVE share Discord gateway opcode 4, so the
                # send is ordered on the guild lane. The wait for its callback
                # stays outside: on_voice_state_update must remain free to run,
                # otherwise the move would wait for the update it blocks.
                async with self._coordinator.hold():
                    stale = self._disconnect_requested or self._superseded()
                    if not stale:
                        self._voice_state_update_event.clear()
                        await self.guild.change_voice_state(channel=channel)
                if stale:
                    # Checked under the lane, immediately before the send.
                    raise PlayerNotConnected

                try:
                    while True:
                        await self._voice_state_update_event.wait()
                        self._voice_state_update_event.clear()
                        if (
                            isinstance(self.channel, (VoiceChannel, StageChannel))
                            and self.channel.id == channel.id
                        ):
                            return
                finally:
                    self._voice_state_update_event.clear()

    async def disconnect(self, *, force: bool = False) -> None:
        """Disconnect from the voice channel.

        Parameters
        ----------
        force:
            Whether to force the disconnect even if not connected.

        Notes
        -----
        Concurrent calls share one teardown. Once disconnect starts, cancellation
        is propagated only after teardown completes.
        """
        task = self._start_teardown(force=force)
        if task is None:
            return

        await await_owned(task, extra={"guild": self._guild_id})

    def _start_teardown(self, *, force: bool = False) -> asyncio.Task[None] | None:
        """Create the single-flight teardown task if one should run.

        Returns the task that owns the teardown, whether it was created here or
        by an earlier caller. ``None`` means there is nothing to do.
        """
        task = self._disconnect_task
        if task is not None:
            return task
        if not force and self.guild.voice_client is not self:
            return None

        # Handled by Discord
        if self.client.is_closed():
            return None

        self._disconnect_requested = True
        self._connected = False
        task = self._disconnect_task = asyncio.create_task(self._disconnect_voice())
        return task

    @property
    def disconnect_started(self) -> bool:
        """Whether terminal voice teardown has started.

        This remains true after teardown finishes. It lets callers distinguish
        "not started" from "already running or complete" without exposing the
        private task.
        """
        return self._disconnect_task is not None

    async def wait_for_disconnect(self) -> None:
        """Wait for an already-started disconnect without starting another.

        If no teardown has started this returns immediately. If one is running,
        waits for it to settle and propagates its failure. A caller cancellation
        is propagated only after the owned teardown completes.
        """
        task = self._disconnect_task
        if task is not None:
            await await_owned(task, extra={"guild": self._guild_id})

    async def _disconnect_voice(self) -> None:
        """Finish terminal voice teardown once for all disconnect callers."""
        async with self._coordinator.hold():
            if self._superseded():
                # A newer generation owns this guild. Retire local state only;
                # its registries and remote player are not ours to touch.
                self._detach_local()
                return
            await self._teardown_voice()

    async def _teardown_voice(self) -> None:
        """Tear down the generation that still owns this guild.

        Requires the guild lane; :meth:`_disconnect_voice` is the only caller.
        """
        self._coordinator.require_lane()
        if self._end_confirmation is not None:
            self._end_confirmation.cancel()
            await asyncio.gather(self._end_confirmation, return_exceptions=True)
        _log.debug(
            "Disconnecting player and destroying client.",
            extra={"guild": self._guild_id},
        )
        async with self._voice_update_lock:
            # A claim can happen while this teardown waits for the voice lock.
            if self._superseded():
                self._detach_local()
                return
            node = self._node
            # Either registry may already belong to someone else: a foreign
            # protocol can occupy the Discord slot, and a restore may have
            # adopted this guild on the node. In both cases this generation
            # has nothing left to mutate, and _detach_local retires it through
            # the same identity-checked path used everywhere else.
            voice_client = self.guild.voice_client
            owns_voice = voice_client is self or voice_client is None
            remote_player = node.get_player(self.guild.id) if node is not None else None
            owns_remote = node is None or remote_player is self or remote_player is None
            if not (owns_voice and owns_remote):
                self._detach_local()
                return

            if node is not None:
                node.discard_player(self)
                try:
                    await node.destroy(guild_id=self.guild.id)
                except HTTPNotFound:
                    # Confirmed absent: the exact DELETE outcome is known.
                    _log.debug(
                        "Player was already destroyed on the node.",
                        extra={"guild": self.guild.id, "node": node.label},
                    )
                except asyncio.CancelledError as error:
                    # The DELETE was already sent; cancelling the await does
                    # not cancel the request.
                    self._note_remote_failure(error)
                    raise
                except Exception as error:  # noqa: BLE001 - leave must continue
                    # A timeout or lost connection leaves the server state
                    # unknown: the DELETE may still be applied later. Record
                    # it instead of pretending the player is gone.
                    self._note_remote_failure(error)
                finally:
                    self._node = None

            voice_client = self.guild.voice_client
            if voice_client is not self and voice_client is not None:
                self._cleanup_local_state()
                return

            # The destroy above had an await point; a newer generation may have
            # claimed the guild while this teardown was in flight.
            if self._superseded():
                self._cleanup_local_state()
                return

            try:
                _log.debug(
                    "Disconnecting from voice channel.",
                    extra={"guild": self._guild_id},
                )
                await self.guild.change_voice_state(channel=None)
            finally:
                self.cleanup()

    def _cleanup_local_state(self) -> None:
        """Clear player state without removing a replacement Discord adapter."""
        self._current = None
        self._position = 0
        self._position_received_at = monotonic()
        self._playback_generation += 1
        self._paused = False
        self._ping = 0
        self._connected = False

    def _note_remote_failure(self, error: BaseException) -> None:
        """Record a remote effect whose outcome the server did not confirm."""
        if not _is_unknown_outcome(error):
            return
        self._coordinator.mark_remote_unknown()
        _log.warning(
            "A remote player effect ended without a server response; its "
            "outcome is unknown and a late request may still apply.",
            extra={"guild": self._guild_id},
        )
        _log.debug("Remote effect failure traceback", exc_info=error)

    def _take_ownership(self) -> None:
        """Claim a new ownership generation for this instance."""
        self._ownership_epoch = self._coordinator.claim(self)

    def _superseded(self) -> bool:
        """Whether a newer generation has taken this guild from this player.

        A player that never claimed has no generation to defend, so it keeps
        the pre-coordinator behaviour instead of silently losing its teardown.
        """
        return self._ownership_epoch != 0 and not self._coordinator.is_current(
            self, self._ownership_epoch
        )

    def _detach_local(self) -> None:
        """Retire this generation without touching a newer owner's state."""
        node = self._node
        if node is not None:
            node.discard_player(self)
        self._node = None
        # A superseded player is normally no longer the cache occupant, but
        # the gate makes the exception harmless instead of a leak.
        self.cleanup()

    def cleanup(self) -> None:
        """Clean up the player.

        This shouldn't be called directly. Instead, use :meth:`disconnect`.

        The Discord cache slot is removed only while this instance still holds
        it. The check and the removal share one synchronous block, so no other
        coroutine can install a replacement between them. Base
        ``VoiceProtocol.cleanup`` removes by guild id unconditionally, which
        would otherwise let a stale player evict the current owner.
        """
        self._cleanup_local_state()
        # Releasing is identity-gated, so a superseded player cannot free the
        # guild for adoption on behalf of the generation that replaced it.
        self._coordinator.release(self)
        if self.guild.voice_client is self:
            super().cleanup()

    async def transfer_to(
        self,
        node: Node[ClientT],
        *,
        expected_source: Node[ClientT] | None = None,
    ) -> bool:
        """Transfer the player to a new node.

        Parameters
        ----------
        node:
            The node to transfer to.
        expected_source:
            If provided, transfer only while this node still owns the player.
            The check runs under the same lock that protects ownership changes.

        Returns
        -------
        :class:`bool`
            ``True`` when the transfer ran, ``False`` when ``expected_source``
            no longer owned the player.

        Raises
        ------
        PlayerNotConnected
            If disconnect has started, or the player has no authoritative node
            or valid voice channel.
        RuntimeError
            If Discord session/server state or the old node snapshot is
            incomplete, or Lavalink v3 track metadata is unavailable locally.

        Notes
        -----
        The target node is prepared before ownership is committed. If target
        preparation fails, the old node remains authoritative and its Lavalink
        player is preserved. After a successful commit, failure to destroy the
        old Lavalink player is logged and does not roll back the transfer.
        Cancellation during old-node cleanup waits for it to finish before
        propagating. A Lavalink v3 target cannot preserve track user data.
        Ownership transfer preserves v4 playback identity but does not promise
        voice readiness: wait for :attr:`connected` on the assigned target.
        """
        async with self._coordinator.hold(), self._voice_update_lock:
            if expected_source is not None and self._node is not expected_source:
                return False
            if self._superseded():
                raise PlayerNotConnected
            await self._transfer_to_unlocked(node)
            return True

    async def _transfer_to_unlocked(self, node: Node[ClientT]) -> None:
        """Transfer ownership while holding the guild lane and the voice lock.

        Both callers already hold the lane, so this method must never acquire
        it again: the lane is not reentrant.
        """
        self._coordinator.require_lane()
        old_node = self._node
        if self._disconnect_requested or old_node is None:
            raise PlayerNotConnected

        if old_node == node:
            return

        session_id = self._session_id
        server_state = self._server_state
        if session_id is None or server_state is None:
            msg = "Cannot transfer player without Discord session and server state."
            raise RuntimeError(msg)

        channel = self.channel
        if not isinstance(channel, (VoiceChannel, StageChannel)):
            raise PlayerNotConnected

        snapshot = await old_node.fetch_player(self.guild.id)
        required = ("track", "volume", "paused", "state", "filters")
        if any(key not in snapshot for key in required):
            msg = "Cannot transfer player without complete playback state."
            raise RuntimeError(msg)

        snapshot_track = self._track_from_snapshot(snapshot, old_node.version)
        prepared_track = self._track_for_target(snapshot_track, node.version)
        snapshot_filter = Filter.from_payload(snapshot["filters"])
        labels_match_snapshot = self._merged_filter().payload == snapshot["filters"]

        try:
            await node.voice_update(
                guild_id=self._guild_id,
                session_id=session_id,
                data=server_state,
                channel_id=int(channel.id),
            )
            target_state = await node.update(
                guild_id=self._guild_id,
                track=prepared_track,
                position=snapshot["state"]["position"],
                volume=snapshot["volume"],
                pause=snapshot["paused"],
                filter=snapshot_filter,
            )
        except (Exception, asyncio.CancelledError) as error:
            self._note_remote_failure(error)
            await self._destroy_partial_target(node)
            raise

        if self._disconnect_requested or self._superseded():
            # The preparation above awaited REST calls, and a claim is
            # synchronous, so the generation may have changed meanwhile.
            # Never commit a transfer for a player that no longer owns the
            # guild; the prepared target is cleaned up, not kept.
            await self._destroy_partial_target(node)
            raise PlayerNotConnected

        # Register only after preparation so target events cannot mutate the owner.
        node.add_player(self.guild.id, self)
        self._node = node
        self._playback_generation += 1
        old_node.remove_player(self.guild.id)

        target_track = (
            prepared_track
            if node.version == 3
            else self._track_from_snapshot(target_state, node.version)
        )
        self._state_updates = 0
        self._sync_playback_state(target_state, target_track)
        if not labels_match_snapshot:
            self._filters = OrderedDict({"RESUME": snapshot_filter})

        # The migration is committed; old cleanup is best-effort.
        try:
            await await_owned(
                asyncio.create_task(old_node.destroy(guild_id=self.guild.id)),
                extra={"guild": self.guild.id, "node": old_node.label},
            )
        except Exception as error:  # noqa: BLE001 - committed migration
            self._note_remote_failure(error)
            _log.exception(
                "Failed to destroy old node player after transfer.",
                extra={"guild": self.guild.id, "node": old_node.label},
            )

    async def _destroy_partial_target(self, node: Node[ClientT]) -> None:
        """Best-effort cleanup of an uncommitted target, preserving the owner."""
        try:
            await node.destroy(guild_id=self.guild.id)
        except asyncio.CancelledError as error:
            # The DELETE was already sent; cancelling the await does not
            # cancel the request, so the outcome is unknown, not absent.
            self._note_remote_failure(error)
            raise
        except Exception as error:  # noqa: BLE001 - preserve the original error
            # Same unknown-outcome policy as every other remote effect.
            self._note_remote_failure(error)
            _log.exception(
                "Failed to clean up partial target node player.",
                extra={"guild": self.guild.id, "node": node.label},
            )

    @staticmethod
    def _track_for_target(track: Track | None, target_version: int) -> Track | None:
        """Adapt a parsed snapshot track to the target without mutating the source."""
        if track is None or target_version != 3:
            return track
        adapted = shallow_copy(track)
        adapted.user_data = {}
        return adapted

    def _track_from_snapshot(self, state: PlayerPayload, version: int) -> Track | None:
        """Restore the encoded track, using local metadata only for Lavalink v3."""
        if state["track"] is None:
            return None
        if version != 3:
            return Track.from_data_with_info(state["track"])
        if self._current is None:
            msg = "Cannot restore Lavalink v3 track without local metadata."
            raise RuntimeError(msg)

        track = shallow_copy(self._current)
        track.id = cast("str", state["track"])
        # A v3 snapshot has no user data; cached local values are not authoritative.
        track.user_data = {}
        return track

    async def destroy(self) -> None:  # noqa: D102
        warnings.warn(
            "Player.destroy is deprecated and will be removed in 3.0.0, "
            "use .disconnect instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.disconnect()

    async def fetch_tracks(
        self, query: str, search_type: SearchType | str = SearchType.YOUTUBE
    ) -> list[Track] | Playlist | None:
        r"""Fetch tracks from the node.

        Parameters
        ----------
        query:
            The query to search for.
        search_type:
            The search type to use.

        Returns
        -------
        :class:`list`\[:class:`Track`]
            A list of tracks if the load type is ``TRACK_LOADED`` or ``SEARCH_RESULT``.
        :class:`Playlist`
            A playlist if the load type is ``PLAYLIST_LOADED``.
        None
            If the load type is ``NO_MATCHES``.

        Notes
        -----
        If a node was not selected due to not being connected, this will use a
        random node.
        """
        node = self.node

        raw_type: str
        if isinstance(search_type, SearchType):
            raw_type = search_type.value
        else:
            raw_type = search_type

        return await node.fetch_tracks(query, search_type=raw_type)

    async def update(
        self,
        *,
        track: Track | str | None = MISSING,
        position: int | None = None,
        end_time: int | None = MISSING,
        volume: int | None = None,
        pause: bool | None = None,
        filter: Filter | None = None,
        replace: bool = False,
        user_data: Mapping[str, JSONValue] = MISSING,
    ) -> None:
        """Update the player.

        Parameters
        ----------
        track:
            The track to play. This can be a :class:`Track` or an identifier.

            .. versionchanged:: 2.4
                The track can now be a :class:`str` to play an identifier.
        position:
            The position to start the track at.
        end_time:
            The time to end the track at. Pass ``None`` explicitly to reset a
            previously configured end time. Omitting it leaves the end time unchanged.
        volume:
            The volume to play the track at.
        pause:
            Whether to pause the track.
        filter:
            The filter to apply to the track.
        replace:
            Whether to replace the current track if one is playing.
        user_data:
            JSON-compatible data to associate with the track. When omitted for a
            :class:`Track`, its existing :attr:`Track.user_data` is preserved.

            This is only supported by Lavalink v4. Each new launch overwrites
            the reserved ``__mafic_playback_id`` field with a fresh UUID.
            Caller mappings are not mutated; transfers preserve the launch ID.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        """
        if self._node is None or not self._connected:
            raise PlayerNotConnected

        if self.node.version == 3:
            if user_data is not MISSING:
                message = "Lavalink version 3 does not support track user data."
                raise TypeError(message)

            if isinstance(track, Track) and track.user_data:
                message = "Lavalink version 3 cannot preserve Track.user_data."
                raise TypeError(message)

        # v4+ receives the full payload in events, so there is no need to
        # pre-emptyively update the player.
        if track is not MISSING and track is not None and self.node.version == 3:
            if isinstance(track, str):
                message = (
                    "Lavalink version 3 does not support identifiers "
                    "due to unexpected behaviour with events."
                )
                raise TypeError(message)

            self._current = track

        node = self._node
        if node.version != 3 and track is not MISSING and track is not None:
            if user_data is not MISSING:
                metadata = dict(user_data)
            elif isinstance(track, Track):
                metadata = dict(track.user_data)
            else:
                metadata = {}
            metadata[_PLAYBACK_ID_KEY] = str(uuid4())
            user_data = metadata

        self._playback_generation += 1
        generation = self._playback_generation
        self._pending_updates += 1
        state_updates = self._state_updates
        try:
            # The PATCH is a remote mutation of one (session, guild) slot, so
            # it shares the guild lane with teardown and transfer. Without
            # this, a play/pause/seek racing a disconnect could recreate the
            # player the teardown had just deleted.
            async with self._coordinator.hold():
                # Teardown may have started while this call waited for the
                # lane; a PATCH after that DELETE would resurrect the player.
                if self._disconnect_requested or self._superseded():
                    raise PlayerNotConnected
                try:
                    data = await node.update(
                        guild_id=self._guild_id,
                        track=track,
                        position=position,
                        end_time=end_time,
                        volume=volume,
                        pause=pause,
                        filter=filter,
                        no_replace=not replace,
                        user_data=user_data,
                    )
                except BaseException as error:
                    self._note_remote_failure(error)
                    raise
            if (
                self._node is not node
                or self._disconnect_requested
                or self._playback_generation != generation
            ):
                return
            accepted = self._track_from_snapshot(data, node.version)
            # An end event can arrive before this REST response. noReplace may
            # also return the previous launch rather than the requested one.
            if self._playback_id(accepted) in self._ended_playbacks:
                accepted = None
            new_launch = self._playback_id(accepted) != self._playback_id(self._current)
            if node.version == 3:
                new_launch = track is not MISSING and track is not None and replace
            # REST can report the old audio position even after a seek's websocket
            # update, including in subsequent pause/volume responses. Keep one
            # live-state authority; REST seeds restoration and new launches only.
            preserve_live_state = self._state_updates > 0 and (
                self._state_updates != state_updates or not new_launch
            )
            self._sync_playback_state(
                data, accepted, preserve_live_state=preserve_live_state
            )
        finally:
            self._pending_updates -= 1
            if not self._pending_updates:
                self._ended_playbacks.clear()

    async def play(
        self,
        track: Track | str,
        /,
        *,
        start_time: int | None = None,
        end_time: int | None = MISSING,
        volume: int | None = None,
        replace: bool = True,
        pause: bool | None = None,
        user_data: Mapping[str, JSONValue] = MISSING,
    ) -> None:
        """Play the given track.

        Parameters
        ----------
        track:
            The track to play. This can be a :class:`Track` or an identifier.

            .. versionchanged:: 2.4
                The track can now be a :class:`str` to play an identifier.
        start_time:
            The position to start the track at.
        end_time:
            The time to end the track at. Pass ``None`` explicitly to reset a
            previously configured end time.
        volume:
            The volume to play the track at.
        replace:
            Whether to replace the current track if one is playing.
        pause:
            Whether to pause the track.
        user_data:
            JSON-compatible data to associate with the track. When omitted, the
            :attr:`Track.user_data` from a :class:`Track` is used.

            This is only supported by Lavalink v4. Each new launch overwrites
            the reserved ``__mafic_playback_id`` field with a fresh UUID.
            Caller mappings are not mutated; transfers preserve the launch ID.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """
        return await self.update(
            track=track,
            position=start_time,
            end_time=end_time,
            volume=volume,
            replace=replace,
            pause=pause,
            user_data=user_data,
        )

    async def pause(self, pause: bool = True) -> None:  # noqa: FBT001, FBT002
        """Pause the current track.

        Parameters
        ----------
        pause:
            Whether to pause the track.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """
        return await self.update(pause=pause)

    async def resume(self) -> None:
        """Resume the current track.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`pause`, with ``pause`` set to
        ``False``.
        """
        return await self.pause(False)  # noqa: FBT003

    async def stop(self) -> None:
        """Stop the current track.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """
        await self.update(track=None, replace=True)

    def _merged_filter(self) -> Filter:
        """Merge labeled filters in their application order."""
        return (
            reduce(lambda left, right: left | right, self._filters.values())
            if self._filters
            else Filter()
        )

    async def _update_filters(self, *, fast_apply: bool) -> None:
        """Update the filters on the player.

        Parameters
        ----------
        fast_apply:
            Whether to seek to the current position after updating the filters.
        """
        await self.update(filter=self._merged_filter())

        if fast_apply:
            await self.seek(self.position)

    async def add_filter(
        self, filter: Filter, /, *, label: str, fast_apply: bool = False
    ) -> None:
        """Add a filter to the player.

        Parameters
        ----------
        filter:
            The filter to add.
        label:
            The label to use for the filter. This is used to remove the filter
            later.
        fast_apply:
            Whether to seek to the current position after updating the filters.
            This clears Lavalink's internal buffer, which may cause a small
            delay in the audio.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        """
        self._filters[label] = filter

        await self._update_filters(fast_apply=fast_apply)

    async def has_filter(self, label: str) -> bool:
        """Check if the player has a filter with the given label.

        .. versionadded:: 2.1

        Parameters
        ----------
        label:
            The label to check for.

        Returns
        -------
        :class:`bool`
            Whether the player has a filter with the given label.
        """
        return label in self._filters

    async def remove_filter(self, label: str, *, fast_apply: bool = False) -> None:
        """Remove a filter from the player.

        Parameters
        ----------
        label:
            The label of the filter to remove.
        fast_apply:
            Whether to seek to the current position after updating the filters.
            This clears Lavalink's internal buffer, which may cause a small
            delay in the audio.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        ValueError
            If the filter does not exist.
        """
        self._filters.pop(label)

        await self._update_filters(fast_apply=fast_apply)

    async def clear_filters(self, *, fast_apply: bool = False) -> None:
        """Remove all filters from the player.

        Parameters
        ----------
        fast_apply:
            Whether to seek to the current position after updating the filters.
            This clears Lavalink's internal buffer, which may cause a small
            delay in the audio.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        """
        self._filters.clear()

        await self._update_filters(fast_apply=fast_apply)

    async def set_volume(self, volume: int, /) -> None:
        """Set the volume of the player.

        Parameters
        ----------
        volume:
            The volume to set.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """
        await self.update(volume=volume)

    async def seek(self, position: int, /) -> None:
        """Seek to a position in the current track.

        Parameters
        ----------
        position:
            The position to seek to, in milliseconds.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """
        return await self.update(position=position)
