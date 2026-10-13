# Mafic Fork Updates

Changes in `maintained/lavalink-v4` since upstream
[Mafic 2.11.0](https://github.com/ooliver1/mafic/commit/a8c923f51f525b1ec553d2bfdb90871d95f8b641).
Dates follow Git history. Related code, test and documentation commits are grouped
into one entry, with links to the original changes.

## October 14, 2026

[Changes](https://github.com/ArtiArtem8/mafic/compare/87224fb...96dfa48)

- Fixed REST snapshots overwriting live playback position after seeking
- Fixed later pause, resume and volume responses restoring the pre-seek position
- Fixed live voice connection state being overwritten by REST snapshots
- Kept REST state authoritative when restoring players or transferring to a new node
- Added regression tests for websocket/REST ordering, resume and transfer state
- Added this fork changelog and simplified the README to show the main fork changes
- Updated Player API documentation for position, playback identity and voice readiness

## October 13, 2026

[Changes](https://github.com/ArtiArtem8/mafic/compare/62b7113...87224fb)

- Added readonly `Player.reported_position` for the last accepted server position
- Fixed playback position drifting when the system clock changes
- Fixed playback position advancing while paused, disconnected or without a track
- Fixed position and its time baseline after play, pause, seek, resume and restoration
- Fixed a null track in REST responses leaving the old track active
- Added a unique `userData.__mafic_playback_id` to each new Lavalink v4 launch
  - Preserves caller metadata without modifying input dictionaries
  - Keeps the accepted server launch when `noReplace` prevents replacement
  - Preserves the launch ID during transfer
- Fixed delayed and duplicate track-end events clearing newer playback of the same track
  - Old events still reach listeners; `REPLACED` keeps the current track
  - Restored v4 players without an ID confirm ambiguous endings through REST
  - Disconnect cancels pending confirmation; v3 keeps its existing event behavior
- Fixed REST responses restoring playback that had already ended
- Fixed responses from an old node or playback operation updating a newer session
- Fixed initial voice callbacks ignoring an explicitly assigned node
- Removed obsolete playback bookkeeping
- Added regression tests for playback identity, position and both initial callback orders

## September 14, 2026

[Changes](https://github.com/ArtiArtem8/mafic/compare/dc118ca...62b7113)

- Added `Player.assigned_node` to inspect node ownership without selecting a fallback
- Added `Player.move_to()` for voice and stage channels in the same guild
  - Waits for the matching voice-state update
  - Serializes concurrent moves and rechecks queued requests
  - Keeps later moves usable after a timeout, cancellation or failed request
- Added `Player.transfer_to()` to preserve playback when moving between nodes
  - Restores track, position, pause state, volume and filters from the source node
  - Supports transfers between Lavalink 3.7 and 4.2; v3 cannot preserve `userData`
  - Prepares the target before changing ownership and cleans up failed preparation
  - Keeps the target after a committed transfer even if old-node cleanup fails
- Fixed races between voice-state updates, voice-server updates, moves and transfers
- Fixed node registration and voice credentials becoming inconsistent during migration
- Fixed target preparation events changing the player before transfer commits
- Fixed transfers completing after disconnect was requested
- Made disconnect terminal and shared its cleanup across concurrent callers
- Fixed cancellation interrupting voice teardown or committed transfer cleanup
- Fixed late voice callbacks recreating a disconnected player
- Added transfer, callback-ordering, cancellation and disconnect regression tests
- Added an opt-in Discord voice lifecycle probe and documented its live findings
- Documented the distinction between node ownership and voice transport readiness

## September 11, 2026

[Changes](https://github.com/ArtiArtem8/mafic/compare/80fc611...dc118ca)

- Updated supported Python versions to 3.12–3.14
- Updated runtime dependencies, formatters, linters and pre-commit hooks
- Added a Poetry lockfile and separate dependency groups for each Discord library
- Added the required `voice` extra for py-cord
- Added CI tests for discord.py, nextcord, disnake and py-cord in isolated environments
- Added import, unit-test and slots checks across all supported Python versions
- Updated Pyright checks and fixed the fork's type diagnostics
- Added CI integration testing against the exact Lavalink 4.2.2 release
- Updated CodeQL, dependency review, publishing and documentation build workflows
- Updated installation links to use the maintained fork and clarified the upstream PyPI package
- Documented development setup, compatibility checks and integration testing

## September 10, 2026

[Changes](https://github.com/ArtiArtem8/mafic/compare/a8c923f...80fc611)

- Added `Track.plugin_info` and `Track.user_data` for Lavalink v4 metadata
- Preserved metadata through track decoding, playlists, events and player updates
- Added `user_data` to player play/update operations and node updates
  - Copies caller mappings and preserves existing track metadata when omitted
  - Rejects unsupported metadata updates on Lavalink v3
- Updated v4 Update Player requests to use the nested `track` payload
- Added explicit `end_time=None` support to clear an existing end time
- Updated restored player state to read position, connectivity and ping from `state`
- Fixed voice updates not being sent when moving channels without a session ID change
- Fixed lowercase v4 `replaced` events clearing the current track
- Added plugin filter configuration, serialization, comparison and merge support
- Fixed zero volume being dropped during filter serialization and merging
- Fixed in-place filter merge operators returning `None`
- Fixed plugin lookup to use `/v4/info` for v4 and `/plugins` for v3
- Fixed disabled route planners failing on empty responses
- Fixed failing-address fields and converted their timestamps from milliseconds
- Updated CPU load types to preserve fractional values
- Preserved nullable exception messages and Lavalink cause stack traces
- Updated protocol types for JSON metadata, voice state, empty search results and version data
- Recognized Lavalink 4.2 without an unsupported-version warning
- Fixed runtime typing imports while retaining the then-supported Python 3.8 contract
- Added regression tests for metadata, payloads, filters, exceptions and protocol compatibility
- Added an opt-in Lavalink smoke test using real Mafic serialization and an exact server version
- Updated track, event, exception and compatibility documentation
