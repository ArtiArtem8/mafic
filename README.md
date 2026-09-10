# Mafic

> [!NOTE]
> This is a maintained fork of [ooliver1/mafic](https://github.com/ooliver1/mafic),
> focused on compatibility with current Lavalink v4 releases.
>
> Supports Python 3.12–3.14 and is maintained against current stable
> discord.py, nextcord, disnake and py-cord (with its `voice` extra).
>
> Targets Lavalink 4.2.2, with complete v4 track metadata
> (`pluginInfo` / `userData`) round-tripping, the current Update Player track
> payload, and related protocol correctness fixes.
>
> Upstream changes are kept separate so fixes can still be contributed back
> as focused pull requests.

[![MIT License](https://custom-icon-badges.demolab.com/github/license/artiartem8/mafic?color=845ec2&logo=code-square)](https://github.com/artiartem8/mafic/blob/maintained/lavalink-v4/LICENSE "License File")
[![Lint Workflow Status](https://custom-icon-badges.demolab.com/github/actions/workflow/status/artiartem8/mafic/lint.yml?label=lint&logo=codescan-checkmark&color=ff738c)](https://github.com/artiartem8/mafic/actions/workflows/lint.yml "Lint Workflow")
[![Upstream PyPI - Status](https://img.shields.io/pypi/status/mafic?color=ff9075&label=upstream%20PyPI&logo=pypi&logoColor=white)](https://pypi.org/project/mafic "Upstream Mafic on PyPI")
[![Open Issues](https://custom-icon-badges.demolab.com/github/issues-raw/artiartem8/mafic?logo=issue-opened&color=ffb263)](https://github.com/artiartem8/mafic/issues "Open Issues")
[![Open PRs](https://custom-icon-badges.demolab.com/github/issues-pr-raw/artiartem8/mafic?logo=git-pull-request&color=ffd55f)](https://github.com/artiartem8/mafic/pulls "Open Pull Requests")
[![Upstream Documentation](https://img.shields.io/readthedocs/mafic?logo=read%20the%20docs&logoColor=white&color=f9f871)](https://mafic.readthedocs.io/en/latest/ "Upstream Mafic documentation")

A properly typehinted lavalink client for discord.py, nextcord, disnake and py-cord.

## Installation

Install this fork from the `maintained/lavalink-v4` branch:

```bash
pip install "mafic @ git+https://github.com/artiartem8/mafic.git@maintained/lavalink-v4"
```

> **Note**
> `pip install mafic` installs the
> [upstream PyPI release](https://pypi.org/project/mafic "Upstream Mafic on PyPI"),
> which does not contain this fork's Lavalink v4 fixes.

For production, pin an exact commit instead of the moving branch:

```bash
pip install "mafic @ git+https://github.com/artiartem8/mafic.git@<commit-sha>"
```

> **Note**
> Use `python -m`, `py -m`, `python3 -m` or similar if that is how you install packages.
> Generally windows uses `py -m pip` and linux uses `python3 -m pip`

## Documentation

[Upstream documentation](https://mafic.readthedocs.io/en/latest/) covers the shared
Mafic API; this fork adds the Lavalink v4 fixes described above.

## Features

- Fully customisable node balancing.
- Multi-node support.
- Filters.
- Full API coverage.
- Properly typehinted for Pyright strict.

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
