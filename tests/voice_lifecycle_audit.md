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
and never sends Discord messages. Live results and production design remain
pending until that preflight condition is established. The deterministic race
does not establish Discord event order, server-update frequency or voice readiness.
