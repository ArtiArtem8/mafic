.. currentmodule:: mafic

Player
======

A :class:`Player` is used to interact with a :class:`Node` via a voice client managed by
your Discord library.

``reported_position`` is the last server position in milliseconds. ``position``
adds elapsed local monotonic time only while connected, unpaused and holding a
track. REST updates and websocket state establish a new receipt-time baseline;
server wall timestamps are never compared to the client's wall clock.

Every new v4 launch through ``Player.play`` or ``Player.update`` reserves
``userData.__mafic_playback_id`` for a fresh UUID, including launches without
user metadata. Other fields are preserved and caller mappings are not mutated.
``noReplace`` follows the launch actually accepted by Lavalink. Transfers keep
the existing ID. Late events still reach subscribers but cannot clear a newer
launch; v4 state restored without an ID requires REST confirmation for ambiguous
ends. That check belongs to the player and is cancelled and joined on disconnect.
Lavalink v3 retains its legacy event contract without metadata correlation.

An explicitly supplied node owns the first voice handshake in either callback
order. A successful transfer commits ownership; it does not guarantee transport
readiness. ``connected`` reports Lavalink's connection to the voice gateway, so
applications must wait for it while confirming that the same player and target
still own the session.

.. attributetable:: Player

.. autoclass:: Player
   :exclude-members: on_voice_server_update, on_voice_state_update, update_state, set_state

.. autoclass:: SearchType
   :undoc-members:
