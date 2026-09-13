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
from time import time
from typing import TYPE_CHECKING, Generic, cast

from .__libraries import (
    MISSING,
    GuildChannel,
    StageChannel,
    VoiceChannel,
    VoiceProtocol,
)
from .errors import NoNodesAvailable, PlayerNotConnected
from .events import *
from .filter import Filter
from .pool import NodePool
from .search_type import SearchType
from .track import Track
from .type_variables import ClientT
from .typings import TrackWithInfo

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
        self._session_id: str | None = None
        self._server_state: VoiceServerUpdatePayload | None = None
        self._connected: bool = False
        self._position: int = 0
        self._last_update: int = 0
        self._ping = -1
        self._current: Track | None = None
        self._filters: OrderedDict[str, Filter] = OrderedDict()
        # Used to get the last track for TrackEndEvent.
        self._last_track: Track | None = None
        self._paused: bool = False

        self._voice_server_update_event: Event = Event()
        self._voice_state_update_event: Event = Event()
        self._node_player_ready_event: Event = Event()
        self._move_lock: Lock = Lock()
        self._voice_update_lock: Lock = Lock()
        self._disconnecting: bool = False

    def set_state(self, state: PlayerPayload) -> None:
        """Set the state of the player.

        .. note::

            This is used internally to set the state of the player.
            You should not need to use this.
        """
        self._session_id = state["voice"]["sessionId"]
        self._last_update = state["state"]["time"]
        self._position = state["state"]["position"]
        self._connected = state["state"]["connected"]
        self._ping = state["state"]["ping"]
        self._current = (
            Track.from_data_with_info(state["track"]) if state["track"] else None
        )
        self._filters = OrderedDict({"RESUME": Filter.from_payload(state["filters"])})
        self._paused = state["paused"]
        self._server_state = {
            "token": state["voice"]["token"],
            "endpoint": state["voice"]["endpoint"],
            "guild_id": self._guild_id,
        }

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
        """Whether the player is connected to a voice channel."""
        return self._connected

    @property
    def position(self) -> int:
        """The current position of the player in milliseconds."""
        pos = self._position

        if self._connected and self._current is not None:
            # Add the time since the last update to the position.
            # If the track total time is less than that, use that.
            pos = min(
                self._current.length, pos + int((time() * 1000) - self._last_update)
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
                "Unable to use best node, player not connected, finding random node.",
                extra={"guild": self._guild_id},
            )
            return NodePool[ClientT].get_random_node()

        return self._node

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

        self._last_update = state["time"]
        self._position = state.get("position", 0)
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
        async with self._voice_update_lock:
            await self._dispatch_player_update_unlocked()

    async def _dispatch_player_update_unlocked(self) -> None:
        """Dispatch using current state while holding `_voice_update_lock`."""
        if self._disconnecting:
            return

        node = self._node
        if node is None:
            _log.debug("Recieved voice update before node was found.")
            return

        session_id = self._session_id
        if session_id is None:
            _log.debug("Receieved player update before session ID was set.")
            return

        server_state = self._server_state
        if server_state is None:
            _log.debug("Receieved player update before server state was found")
            return

        channel = self.channel
        if not isinstance(channel, (VoiceChannel, StageChannel)):
            _log.debug("Received player update before channel was set.")
            return

        await node.voice_update(
            guild_id=self._guild_id,
            session_id=session_id,
            data=server_state,
            channel_id=int(channel.id),
        )

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
            track = (
                self._current
                if self.node.version == 3
                else Track.from_data_with_info(cast(TrackWithInfo, data.get("track")))
            )

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
            track = (
                self._last_track
                if self.node.version == 3
                else Track.from_data_with_info(cast(TrackWithInfo, data.get("track")))
            )

            if track is None:
                _log.error(
                    "Received track end event but no track was playing, discarding."
                )
                return

            event = TrackEndEvent(player=self, track=track, payload=data)
            self.client.dispatch("track_end", event)
            _log.debug("Received track end event: %s", event)

            if data["reason"] not in ("REPLACED", "replaced"):
                self._current = None
        elif data["type"] == "TrackExceptionEvent":
            track = (
                self._current
                if self.node.version == 3
                else Track.from_data_with_info(cast(TrackWithInfo, data.get("track")))
            )

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
            track = (
                self._current
                if self.node.version == 3
                else Track.from_data_with_info(cast(TrackWithInfo, data.get("track")))
            )

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
        if self._disconnecting:
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
            # This can happen and is on disconnect.
            # Not sure why this is typed as always Snowflake.
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

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        data:
            The voice server update payload.
        """
        async with self._voice_update_lock:
            if self._disconnecting:
                return

            current_node = self._node
            if (
                current_node is None
                or self._server_state is None
                or self._server_state["endpoint"] != data["endpoint"]
            ):
                _log.debug(
                    "Getting best node for player", extra={"guild": self._guild_id}
                )
                selected_node = NodePool[ClientT].get_node(
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
            self._endpoint = data["endpoint"]
            self._server_state = data

            if current_node is None:
                self._node = selected_node
                self._node.add_player(self._guild_id, self)
                await self._dispatch_player_update_unlocked()
            elif selected_node is not current_node:
                await self._transfer_to_unlocked(selected_node)
            else:
                current_node.add_player(self._guild_id, self)
                await self._dispatch_player_update_unlocked()

        self._voice_server_update_event.set()

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

        # I really don't like how pyright rules are so incredibly long,
        # This has 2 type issues.
        if not NodePool.nodes:  # pyright: ignore   # noqa: PGH003
            self.cleanup()
            raise NoNodesAvailable

        _log.debug("Connecting to voice channel %s", self.channel.id)

        await self.channel.guild.change_voice_state(
            channel=self.channel, self_mute=self_mute, self_deaf=self_deaf
        )
        futures = [
            self._voice_state_update_event.wait(),
            self._voice_server_update_event.wait(),
            self._node_player_ready_event.wait(),
        ]

        ensured = [asyncio.ensure_future(fut) for fut in futures]
        _, pending = await asyncio.wait(
            ensured, timeout=timeout, return_when=asyncio.ALL_COMPLETED
        )

        if len(pending) != 0:
            await self.disconnect(force=True)
            raise asyncio.TimeoutError

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
                if self.guild.voice_client is not self:
                    raise PlayerNotConnected
                if (
                    isinstance(self.channel, (VoiceChannel, StageChannel))
                    and self.channel.id == channel.id
                ):
                    return

                self._voice_state_update_event.clear()
                try:
                    await self.guild.change_voice_state(channel=channel)
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
        """
        if not self._connected and not force:
            return

        # Handled by Discord
        if self.client.is_closed():
            return

        self._disconnecting = True
        _log.debug(
            "Disconnecting player and destroying client.",
            extra={"guild": self._guild_id},
        )
        async with self._voice_update_lock:
            if self._node is not None:
                self._node.remove_player(self.guild.id)
                try:
                    await self._node.destroy(guild_id=self.guild.id)
                finally:
                    self._node = None

            try:
                _log.debug(
                    "Disconnecting from voice channel.",
                    extra={"guild": self._guild_id},
                )
                await self.guild.change_voice_state(channel=None)
            finally:
                self.cleanup()
                self._connected = False

    def cleanup(self) -> None:
        """Clean up the player.

        This shouldn't be called directly. Instead, use :meth:`disconnect`.
        """
        self._current = None
        self._position = 0
        self._paused = False
        self._ping = 0
        self._channel = None
        self._connected = False

        return super().cleanup()

    async def transfer_to(self, node: Node[ClientT]) -> None:
        """Transfer the player to a new node.

        Parameters
        ----------
        node:
            The node to transfer to.

        Raises
        ------
        PlayerNotConnected
            If the player has no authoritative node or valid voice channel.
        RuntimeError
            If the player has no session/server state or the old node snapshot
            is incomplete.

        Notes
        -----
        The target node is prepared before ownership is committed. If target
        preparation fails, the old node remains authoritative and its Lavalink
        player is preserved. After a successful commit, failure to destroy the
        old Lavalink player is logged and does not roll back the transfer.
        """
        async with self._voice_update_lock:
            await self._transfer_to_unlocked(node)

    async def _transfer_to_unlocked(self, node: Node[ClientT]) -> None:
        """Transfer ownership while holding `_voice_update_lock`."""
        if self._disconnecting:
            raise PlayerNotConnected

        old_node = self._node
        if old_node is None:
            raise PlayerNotConnected

        if old_node == node:
            return

        session_id = self._session_id
        server_state = self._server_state
        if session_id is None or server_state is None:
            msg = "Cannot transfer player with session data."
            raise RuntimeError(msg)

        channel = self.channel
        if not isinstance(channel, (VoiceChannel, StageChannel)):
            raise PlayerNotConnected

        snapshot = await old_node.fetch_player(self.guild.id)
        required = ("track", "volume", "paused", "state", "filters")
        if any(key not in snapshot for key in required):
            msg = "Cannot transfer player without playback state."
            raise RuntimeError(msg)

        if old_node.version == 3:
            if snapshot["track"] is None:
                snapshot_track = None
            elif self._current is None:
                msg = "Cannot restore Lavalink v3 track without local metadata."
                raise RuntimeError(msg)
            else:
                snapshot_track = shallow_copy(self._current)
                snapshot_track.id = cast("str", snapshot["track"])
        else:
            snapshot_track = (
                Track.from_data_with_info(snapshot["track"])
                if snapshot["track"] is not None
                else None
            )
        snapshot_filter = Filter.from_payload(snapshot["filters"])
        local_filter = (
            reduce(lambda left, right: left | right, self._filters.values())
            if self._filters
            else Filter()
        )
        labels_match_snapshot = local_filter.payload == snapshot["filters"]

        try:
            await node.voice_update(
                guild_id=self._guild_id,
                session_id=session_id,
                data=server_state,
                channel_id=int(channel.id),
            )
            target_state = await node.update(
                guild_id=self._guild_id,
                track=snapshot_track,
                position=snapshot["state"]["position"],
                volume=snapshot["volume"],
                pause=snapshot["paused"],
                filter=snapshot_filter,
            )
        except Exception:
            try:
                await node.destroy(guild_id=self.guild.id)
            except Exception:  # noqa: BLE001 - best-effort partial cleanup
                _log.exception(
                    "Failed to clean up partial target node player.",
                    extra={"guild": self.guild.id},
                )
            raise

        if self._disconnecting:
            try:
                await node.destroy(guild_id=self.guild.id)
            except Exception:  # noqa: BLE001 - best-effort partial cleanup
                _log.exception(
                    "Failed to clean up partial target node player.",
                    extra={"guild": self.guild.id},
                )
            raise PlayerNotConnected

        # Commit ownership only after the target is fully prepared.
        node.add_player(self.guild.id, self)
        self._node = node
        old_node.remove_player(self.guild.id)

        # Sync playback state from the actual target response. Voice credentials
        # remain owned by on_voice_server_update; do not let a transient target
        # response replace the authoritative Discord server state.
        if node.version == 3:
            self._current = snapshot_track
        else:
            self._current = (
                Track.from_data_with_info(target_state["track"])
                if target_state["track"] is not None
                else None
            )
        self._last_update = target_state["state"]["time"]
        self._position = target_state["state"]["position"]
        self._connected = target_state["state"]["connected"]
        self._ping = target_state["state"]["ping"]
        self._volume = target_state["volume"]
        self._paused = target_state["paused"]
        if labels_match_snapshot:
            # Labels already describe the authoritative merged filter payload.
            pass
        else:
            self._filters = OrderedDict({"RESUME": snapshot_filter})

        # The migration is committed; old cleanup is best-effort.
        try:
            await old_node.destroy(guild_id=self.guild.id)
        except Exception:  # noqa: BLE001 - committed migration, cleanup only
            _log.exception(
                "Failed to destroy old node player after transfer.",
                extra={"guild": self.guild.id},
            )

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

            This is only supported by Lavalink v4.

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
        if track is not None and self.node.version == 3:
            if isinstance(track, str):
                message = (
                    "Lavalink version 3 does not support identifiers "
                    "due to unexpected behaviour with events."
                )
                raise TypeError(message)

            self._current = track

        data = await self._node.update(
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

        if data["track"]:
            self._current = Track.from_data_with_info(data["track"])

        self._volume = data["volume"]
        self._paused = data["paused"]

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

            This is only supported by Lavalink v4.

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

    async def _update_filters(self, *, fast_apply: bool) -> None:
        """Update the filters on the player.

        Parameters
        ----------
        fast_apply:
            Whether to seek to the current position after updating the filters.
        """
        await self.update(
            filter=(
                reduce(lambda left, right: left | right, self._filters.values())
                if self._filters
                else Filter()
            )
        )

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
