# Mafic Fork Update

October 14, 2026

Changes in `fix/player-state-contracts`, compared with
upstream `ooliver1/mafic:master` at `a8c923f`. The major changes include:

- Updated Lavalink v4 payloads and types for Lavalink 4.2
- Added support for preserving track `pluginInfo` and `userData`
- Added plugin filter support
- Added `Player.assigned_node` to inspect node assignment without selecting a fallback
- Added `Player.move_to()` for voice-channel changes
- Added `Player.transfer_to()` to preserve playback state when moving between nodes

  - Supports transfers between Lavalink 3.7 and 4.2
  - Transfers to v3 cannot preserve `userData`
  - Voice transport readiness must be checked separately after transfer

- Added `Player.reported_position` for the latest server-reported position
- Fixed playback position drifting when the system clock changes
- Fixed playback position advancing while paused, disconnected or without a track
- Fixed REST snapshots overwriting live playback position after seeking
- Fixed delayed v4 track-end events clearing a newer playback of the same track

  - Each new launch reserves `userData.__mafic_playback_id` for a unique ID
  - Transfers preserve the ID; v3 retains its existing event behavior

- Fixed REST responses restoring playback that had already ended
- Fixed initial voice callbacks ignoring an explicitly assigned node
- Fixed race conditions during voice updates, node transfers and disconnects
- Fixed zero volume being lost during filter serialization and merging
- Fixed in-place filter merge operators returning `None`
- Fixed the plugin endpoint used with Lavalink v3
- Updated exception handling to preserve additional Lavalink error details
- Updated Python support to 3.12–3.14 and refreshed dependencies and development tools
- Added regression tests and opt-in Lavalink and Discord voice integration checks
- Updated player lifecycle documentation, including terminal disconnect behavior
