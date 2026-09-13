# Custom VoiceProtocol audit

Inspected installed distributions in isolated temporary venvs on 2026-09-14;
versions verified with `importlib.metadata.version`. No upstream main/master
sources or native VoiceClient implementation were used as evidence.

| Distribution | State callback (`state.py`) | Server callback (`state.py`) | Registration (`abc.py`) |
| --- | --- | --- | --- |
| discord.py 2.7.1 | 1663–1674 | 1697–1703 | 2144–2152 |
| nextcord 3.2.0 | 2099–2114 | 2134–2147 | 1792–1800 |
| disnake 2.12.1 | 1824–1837 | 1855–1863 | 2086–2095 |
| py-cord 2.8.1 | 1887–1903 | 1928–1942 | 2079–2087 |

All four register the custom protocol by guild before calling `connect`.
State dispatch selects the bot's own user ID and guild; server dispatch selects
the protocol by guild. Each callback runs in its own `asyncio.create_task`,
wrapped by `logging_coroutine` (await and exception logging only).
There is no move correlation or callback serialization. nextcord's task set
retains tasks and discards completed tasks; it does not serialize them.
py-cord wraps payloads in `RawVoiceStateUpdateEvent` / `RawVoiceServerUpdateEvent`;
`raw_models.py:822–829` supplies mapping access to the original payload.

## Deterministic observation on afc1fb0

With two different move targets, hold each real Mafic state callback at its
outbound Lavalink update using two `asyncio.Event` barriers. Let both callbacks
start, then release only the first update. The second move returns while its
own update remains blocked; the first move times out. Two gateway requests
were issued. `Event.set()` wakes all current waiters; `clear()` does not retract
those wakeups. Both waiters inspect the shared, already overwritten channel.
This reproduces ambiguity without depending on network scheduling.

## Live probe

`python -m unittest tests.integration_discord_voice -v` requires explicit opt-in
and an operator check for competing bot instances; see its module docstring.
The probe checks Lavalink `/version == 4.2.2`, emits only selected safe fields,
and never sends Discord messages. It was run against pinned discord.py 2.7.1,
nextcord 3.2.0, disnake 2.12.1 and py-cord 2.8.1, each in an isolated venv.

Live observations:

- 9/9 sequential moves received `VOICE_STATE_UPDATE` first and
  `VOICE_SERVER_UPDATE` afterwards; the observed gap was 47-500 ms.
- The Discord session ID stayed unchanged. Endpoint and token changed for all
  nine server updates.
- The state callback sent the cached previous server state immediately. The
  server callback sent the new credentials separately. `move_to()` returned
  after the state callback's Lavalink request, often while Lavalink still
  reported `connected=False`.
- Voice websocket close codes 1000, 4006 and 4022 were observed. A close does
  not prove that the bounded readiness/settling check will pass.
- Before serialization, concurrent moves issued two gateway requests, returned
  early, and left Lavalink disconnected. A per-player `_move_lock` made the
  public calls FIFO-ordered and both returned successfully, but final readiness
  could still fail: concurrent `VOICE_STATE_UPDATE` and
  `VOICE_SERVER_UPDATE` callbacks issued overlapping Lavalink PATCHes, and a
  late 4006 close left `connected=False`.
- Adding a per-player `_voice_update_lock` around `_dispatch_player_update`
  preserved callback order for those PATCHes. The probe then passed on all four
  adapters; concurrent moves settled with the final channel and
  `connected=True`.
- `move_to()` explicitly does not wait for `VOICE_SERVER_UPDATE` or readiness.
  A queued move waits for the per-player move lock; disconnect remains
  independent and can make an interrupted move time out. No broader lifecycle
  lock was added.
