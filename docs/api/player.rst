.. currentmodule:: mafic

Player
======

A :class:`Player` is used to interact with a :class:`Node` via a voice client managed by
your Discord library.

Playback position
-----------------

``reported_position`` is the last accepted server position in milliseconds.
``position`` adds elapsed local monotonic time only while connected, unpaused
and holding a track. Server wall timestamps are never compared to the client's
wall clock.

Live websocket state takes precedence over REST snapshots for the current
launch, since a REST response can briefly contain the pre-seek audio position.
REST seeds restored players, transfer targets and new launches without an
intervening websocket update. Resuming resets the monotonic time anchor.

Playback identity
-----------------

Every new v4 launch through ``Player.play`` or ``Player.update`` reserves
``userData.__mafic_playback_id`` for a fresh UUID, including launches without
user metadata. Other fields are preserved and caller mappings are not mutated.
``noReplace`` follows the launch actually accepted by Lavalink. Transfers keep
the existing ID. Late events still reach subscribers but cannot clear a newer
launch; v4 state restored without an ID requires REST confirmation for ambiguous
ends. That check belongs to the player and is cancelled and joined on disconnect.
Lavalink v3 retains its legacy event contract without metadata correlation.

Node ownership and voice readiness
----------------------------------

``assigned_node`` returns the assigned node or ``None`` without selecting a
fallback. ``move_to(channel)`` moves an active player to a voice or stage channel
in the same guild. It confirms the Discord channel transition and voice-state
update; a later voice-server update may still be reconnecting Lavalink.

``transfer_to(node)`` preserves the current track, position, volume, pause state
and filters. Transfers between Lavalink 3.7 and 4.2 are supported, but a v3 target
cannot preserve track ``userData``.

An explicitly supplied node owns the first voice handshake in either callback
order. A successful transfer commits ownership; it does not guarantee transport
readiness. ``connected`` reports Lavalink's connection to the voice gateway, so
applications must wait for it while confirming that the same player and target
still own the session.

``disconnect()`` is terminal for that player. Reconnecting requires a new
``VoiceProtocol`` instance.

API reference
-------------

.. attributetable:: Player

.. autoclass:: Player
   :exclude-members: on_voice_server_update, on_voice_state_update, update_state, set_state

.. autoclass:: SearchType
   :undoc-members:
