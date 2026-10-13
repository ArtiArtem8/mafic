# Mafic

A typed Lavalink client for discord.py, nextcord, disnake and py-cord.

[![MIT License](https://custom-icon-badges.demolab.com/github/license/artiartem8/mafic?color=845ec2&logo=code-square)](https://github.com/artiartem8/mafic/blob/maintained/lavalink-v4/LICENSE "License File")
[![Lint Workflow Status](https://custom-icon-badges.demolab.com/github/actions/workflow/status/artiartem8/mafic/lint.yml?label=lint&logo=codescan-checkmark&color=ff738c)](https://github.com/artiartem8/mafic/actions/workflows/lint.yml "Lint Workflow")
[![Upstream PyPI - Status](https://img.shields.io/pypi/status/mafic?color=ff9075&label=upstream%20PyPI&logo=pypi&logoColor=white)](https://pypi.org/project/mafic "Upstream Mafic on PyPI")
[![Open Issues](https://custom-icon-badges.demolab.com/github/issues-raw/artiartem8/mafic?logo=issue-opened&color=ffb263)](https://github.com/artiartem8/mafic/issues "Open Issues")
[![Open PRs](https://custom-icon-badges.demolab.com/github/issues-pr-raw/artiartem8/mafic?logo=git-pull-request&color=ffd55f)](https://github.com/artiartem8/mafic/pulls "Open Pull Requests")
[![Upstream Documentation](https://img.shields.io/readthedocs/mafic?logo=read%20the%20docs&logoColor=white&color=f9f871)](https://mafic.readthedocs.io/en/latest/ "Upstream Mafic documentation")

**[Fork changelog](FORK_CHANGELOG.md)** · [Installation](#installation) ·
[Player lifecycle](#player-lifecycle) · [Usage](#usage) ·
[Upstream documentation](https://mafic.readthedocs.io/en/latest/)

## Maintained fork

This fork of [ooliver1/mafic](https://github.com/ooliver1/mafic) is maintained on
`maintained/lavalink-v4`. It supports Python 3.12–3.14 and targets Lavalink 4.2.2,
while retaining Lavalink 3.7 compatibility. Supported Discord libraries are
discord.py, nextcord, disnake and py-cord (with its `voice` extra).

The fork adds public node assignment, voice-channel movement and player transfer,
preserves v4 track metadata, and fixes playback identity and position tracking.
The **[fork changelog](FORK_CHANGELOG.md)** lists the changes from upstream,
including the October 14, 2026 player-state update.

## Installation

Install this fork from the `maintained/lavalink-v4` branch:

```bash
pip install "mafic @ git+https://github.com/artiartem8/mafic.git@maintained/lavalink-v4"
```

> **Note**
> `pip install mafic` installs the
> [upstream PyPI release](https://pypi.org/project/mafic "Upstream Mafic on PyPI"),
> which is the upstream release, not this maintained fork.

For production, pin an exact commit instead of the moving branch:

```bash
pip install "mafic @ git+https://github.com/artiartem8/mafic.git@<commit-sha>"
```

> **Note**
> Use `python -m`, `py -m`, `python3 -m` or similar if that is how you install packages.
> Generally windows uses `py -m pip` and linux uses `python3 -m pip`

## Documentation

[Upstream documentation](https://mafic.readthedocs.io/en/latest/) covers the shared
Mafic API. The fork-specific player lifecycle API and its Lavalink 4.2 behavior
are documented in this README and in the related docstrings.

## Features

- Fully customisable node balancing.
- Multi-node support with player transfer between nodes.
- Public voice-channel movement.
- Filters.
- Lavalink 4.2 track metadata (`pluginInfo` / `userData`) round-tripping.
- Lavalink 3.7 and 4.2 compatibility.
- Strict typing for Pyright, covering the supported discord.py, nextcord,
  disnake and py-cord adapters.

## Player lifecycle

### Node ownership and voice readiness

- `Player.assigned_node` returns the Lavalink node currently assigned to the
  player, or `None` when no node is assigned. Unlike `Player.node`, it never
  selects a fallback node.
- `Player.move_to(channel)` moves an active player to another voice or stage
  channel in the same guild through public API only.
- `Player.transfer_to(node)` transfers an active Lavalink player to another
  node. The transfer preserves the current track, position, volume, pause state
  and filters.
- Lavalink v4 `userData` is preserved when the target node supports it.
- Transfers between Lavalink 3.7 and 4.2 are supported. A transfer to a
  Lavalink v3 target cannot preserve `userData`.
- `disconnect()` is terminal for that `Player`; reconnecting creates a new
  `VoiceProtocol` instance.
- `move_to()` confirms the Discord channel transition and the corresponding
  voice-state update. It does not guarantee that a later `VOICE_SERVER_UPDATE`
  has already fully reconnected Lavalink.
- `connected` reports Lavalink's voice gateway connection. Assigning a transfer
  target does not guarantee readiness; wait for `connected` on that target.

### Playback position

`reported_position` is the last accepted server position in milliseconds.
`position` extrapolates from it using a local monotonic clock while connected,
playing and unpaused. Live websocket state takes precedence over REST snapshots
for the current launch, since a REST response can briefly contain the pre-seek
audio position. REST seeds restored players, transfer targets and new launches
that have not received a websocket update yet.

### Playback identity

Every v4 launch reserves `userData.__mafic_playback_id` for a new UUID. Caller
metadata is copied, transfers retain the ID, and `noReplace` keeps the launch
accepted by Lavalink. Late end events still reach listeners but cannot clear a
different launch. Untagged restored players need REST confirmation; v3 does not
provide this metadata identity guarantee.

## Usage

Go to the [Lavalink Repository](https://github.com/lavalink-devs/Lavalink#server-configuration)
to set up a Lavalink node.

Mafic 2.x retains Lavalink 3.7 compatibility. Its Lavalink v4 support is tested
against stable Lavalink 4.2.2.

```python
import os

import mafic
import nextcord
from nextcord.ext import commands


class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.pool = mafic.NodePool(self)
        self.loop.create_task(self.add_nodes())

    async def add_nodes(self):
        await self.pool.create_node(
            host="127.0.0.1",
            port=2333,
            label="MAIN",
            password="<password>",
        )


bot = MyBot(intents=nextcord.Intents(guilds=True, voice_states=True))


@bot.slash_command(dm_permission=False)
async def play(inter: nextcord.Interaction, query: str):
    if not inter.guild.voice_client:
        player = await inter.user.voice.channel.connect(cls=mafic.Player)
    else:
        player = inter.guild.voice_client

    tracks = await player.fetch_tracks(query)

    if not tracks:
        return await inter.send("No tracks found.")

    track = tracks[0]

    await player.play(track)

    await inter.send(f"Playing {track.title}.")


bot.run(...)
```
