.. currentmodule:: mafic

Tracks and Playlists
====================

Tracks and playlists are returned from :meth:`Player.fetch_tracks` and
:meth:`Node.decode_track`.

.. attributetable:: Track

.. autoclass:: Track
   :exclude-members: from_data, from_data_with_info

.. attributetable:: Playlist

.. autoclass:: Playlist

Track Metadata
--------------

Lavalink v4 tracks expose plugin metadata through :attr:`Track.plugin_info` and
JSON-compatible playback metadata through :attr:`Track.user_data`. Mafic preserves
both fields when loading or decoding tracks, rebuilding players, transferring a
player to another node, and dispatching track events.

Pass ``user_data`` when starting a track to attach correlation data to that playback::

   await player.play(
       track,
       user_data={"request_id": "abc"},
   )

When a :class:`Track` is passed without ``user_data``, Mafic sends the track's existing
:attr:`Track.user_data`. Passing ``user_data={}`` explicitly clears it. Identifiers can
also carry metadata::

   await player.play(
       "https://example.com/audio.mp3",
       user_data={"request_id": "abc"},
   )

Lavalink v4 track events carry the track that the event is about, including the
:attr:`Track.user_data` that was provided when it was played, so an event can be matched
to one playback attempt::

   @bot.listen()
   async def on_track_end(event: mafic.TrackEndEvent) -> None:
       request_id = event.track.user_data.get("request_id")

Explicit ``user_data`` is a Lavalink v4 feature. Mafic raises :class:`TypeError` rather
than silently discarding it when connected to Lavalink v3.
