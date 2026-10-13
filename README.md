# Mafic

A typed Lavalink client for discord.py, nextcord, disnake and py-cord.

[![MIT License](https://custom-icon-badges.demolab.com/github/license/artiartem8/mafic?color=845ec2&logo=code-square)](https://github.com/artiartem8/mafic/blob/maintained/lavalink-v4/LICENSE "License File")
[![Lint Workflow Status](https://custom-icon-badges.demolab.com/github/actions/workflow/status/artiartem8/mafic/lint.yml?label=lint&logo=codescan-checkmark&color=ff738c)](https://github.com/artiartem8/mafic/actions/workflows/lint.yml "Lint Workflow")
[![Upstream PyPI - Status](https://img.shields.io/pypi/status/mafic?color=ff9075&label=upstream%20PyPI&logo=pypi&logoColor=white)](https://pypi.org/project/mafic "Upstream Mafic on PyPI")
[![Open Issues](https://custom-icon-badges.demolab.com/github/issues-raw/artiartem8/mafic?logo=issue-opened&color=ffb263)](https://github.com/artiartem8/mafic/issues "Open Issues")
[![Open PRs](https://custom-icon-badges.demolab.com/github/issues-pr-raw/artiartem8/mafic?logo=git-pull-request&color=ffd55f)](https://github.com/artiartem8/mafic/pulls "Open Pull Requests")
[![Upstream Documentation](https://img.shields.io/readthedocs/mafic?logo=read%20the%20docs&logoColor=white&color=f9f871)](https://mafic.readthedocs.io/en/latest/ "Upstream Mafic documentation")

**[Fork changelog](FORK_CHANGELOG.md)** · [Installation](#installation) ·
[Player API](docs/api/player.rst) · [Usage](#usage) ·
[Upstream documentation](https://mafic.readthedocs.io/en/latest/)

## Maintained fork

This fork of [ooliver1/mafic](https://github.com/ooliver1/mafic) supports
Python 3.12–3.14 and Lavalink 4.2, with Lavalink 3.7 compatibility.

Main changes from upstream:

- Preserve Lavalink v4 track metadata and plugin filters.
- Move voice channels and transfer players between nodes through public APIs.
- Keep delayed track events from clearing newer playback.
- Track playback position with a monotonic clock, including pause, seek and resume.
- Test all four Discord libraries on Python 3.12–3.14.

See the **[full fork changelog](FORK_CHANGELOG.md)** for dated updates.

## Installation

Install this fork from the `maintained/lavalink-v4` branch:

```bash
pip install "mafic @ git+https://github.com/artiartem8/mafic.git@maintained/lavalink-v4"
```

> **Note**
> `pip install mafic` installs the
> [upstream PyPI release](https://pypi.org/project/mafic "Upstream Mafic on PyPI").
> Use the Git URL above to install this fork.

For production, pin an exact commit instead of the moving branch:

```bash
pip install "mafic @ git+https://github.com/artiartem8/mafic.git@<commit-sha>"
```

For py-cord, install its `voice` extra (`py-cord[voice]`).

## Documentation

[Upstream documentation](https://mafic.readthedocs.io/en/latest/) covers the shared
Mafic API. See the [fork's Player API](docs/api/player.rst) for voice readiness,
playback identity and position contracts, and [CONTRIBUTING.md](CONTRIBUTING.md)
for development and integration tests.

## Usage

Go to the [Lavalink Repository](https://github.com/lavalink-devs/Lavalink#server-configuration)
to set up a Lavalink node.

Mafic 2.x retains Lavalink 3.7 compatibility. Its Lavalink v4 support is tested
against stable Lavalink 4.2.2.

```python
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
