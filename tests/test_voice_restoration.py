"""Restoration tests for sync_players and the Discord voice-client slot."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from mafic.__libraries import Client, VoiceChannel
from mafic.node import Node
from mafic.player import Player
from mafic.pool import NodePool

from .test_player_update import player_payload
from .test_voice_ownership_poc import CHANNEL_ID, GUILD_ID, FakeClient, FakeGuild

if TYPE_CHECKING:
    from mafic.typings import Player as PlayerPayload


class RestoreGuild(FakeGuild):
    """Guild double that also carries the bot member's voice state."""

    def __init__(self, client: FakeClient) -> None:
        super().__init__(client)
        self.me: object = SimpleNamespace(voice=SimpleNamespace(channel=None))


class RestoreClient(FakeClient):
    """A client that can resolve the guild being restored."""

    def __init__(self, guild: RestoreGuild) -> None:
        super().__init__()
        self._guild = guild

    def get_guild(self, _guild_id: int) -> RestoreGuild:
        """Return the fake guild for the restored player."""
        return self._guild


class RestorationTests(IsolatedAsyncioTestCase):
    """Restoring players must never install a second live owner."""

    def setUp(self) -> None:
        """Build a real node over a fake client, guild and channel."""
        self.node = cast("Node[Client]", object.__new__(Node))
        self.node._version = 4
        self.node._session_id = "lavalink-session"
        self.node._label = "TEST"
        self.node._players = {}

        self.remote: list[PlayerPayload] = [player_payload()]
        self.request = AsyncMock(side_effect=self._request)
        self.enterContext(patch.object(Node, "_Node__request", self.request))
        self.enterContext(patch.object(NodePool, "nodes", [self.node]))

        self.channel: Mock = Mock(spec=VoiceChannel, id=CHANNEL_ID)
        self.channel._get_voice_client_key.return_value = (GUILD_ID, None)
        self.guild = RestoreGuild(FakeClient())
        self.channel.guild = self.guild
        self.guild.me = SimpleNamespace(voice=SimpleNamespace(channel=self.channel))
        self.client = RestoreClient(self.guild)
        # Rebind the guild to the client that owns the cache under test.
        self.guild._connection = self.client._connection
        self.node._client = cast("Client", self.client)

    async def _request(
        self, method: str, path: str, *_args: object, **_kw: object
    ) -> object:
        """Answer the player listing used by sync_players."""
        self.assertEqual(method, "GET")
        self.assertIn("/players", path)
        return self.remote

    def make_player(self) -> Player[Client]:
        """Build a player for the guild under test."""
        return Player(cast("Client", self.client), self.channel)

    def register(self, player: Player[Client]) -> None:
        """Occupy the Discord cache slot as Connectable.connect would."""
        self.client._connection._add_voice_client(GUILD_ID, player)

    async def test_restore_skips_a_guild_that_is_still_connecting(self) -> None:
        """A claimed player without a node entry still owns the guild."""
        live = self.make_player()
        self.register(live)
        live._take_ownership()

        await self.node.sync_players()

        self.assertEqual(self.node.players, [])
        self.assertIs(self.guild.voice_client, live)
        self.assertIs(live._coordinator.owner, live)

    async def test_restore_skips_a_claimed_owner_without_a_cache_slot(self) -> None:
        """Ownership, not the cache slot, is what forbids a second generation."""
        live = self.make_player()
        live._take_ownership()

        await self.node.sync_players()

        self.assertEqual(self.node.players, [])
        self.assertIsNone(self.guild.voice_client)
        self.assertIs(live._coordinator.owner, live)

    async def test_restore_does_not_displace_owner_claimed_during_guild_fetch(
        self,
    ) -> None:
        """A claim during guild lookup still prevents restoration."""
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch_guild(_guild_id: int) -> RestoreGuild:
            started.set()
            await release.wait()
            return self.guild

        with (
            patch.object(self.client, "get_guild", return_value=None),
            patch.object(self.client, "fetch_guild", new=fetch_guild, create=True),
        ):
            restore = asyncio.create_task(self.node.sync_players())
            await asyncio.wait_for(started.wait(), 2)

            live = self.make_player()
            live._take_ownership()
            release.set()
            await asyncio.wait_for(restore, 2)

        self.assertIs(live._coordinator.owner, live)
        self.assertIsNone(self.guild.voice_client)
        self.assertIsNone(self.node.get_player(GUILD_ID))

    async def test_restore_adopts_when_no_local_owner_exists(self) -> None:
        """An unowned remote player is adopted and claimed."""
        await self.node.sync_players()

        adopted = self.node.get_player(GUILD_ID)
        if adopted is None:
            self.fail("Restoration did not adopt the remote player.")
        self.assertIs(self.guild.voice_client, adopted)
        self.assertIs(adopted._coordinator.owner, adopted)

    async def test_restore_never_overwrites_an_unclaimed_slot(self) -> None:
        """A protocol mafic did not create must not be evicted."""
        foreign = Mock(spec=VoiceChannel)
        self.client._connection._add_voice_client(GUILD_ID, foreign)

        await self.node.sync_players()

        self.assertIs(self.guild.voice_client, foreign)
        self.assertEqual(self.node.players, [])

    async def test_disconnect_releases_the_guild_for_restoration(self) -> None:
        """A completed teardown frees the guild instead of blocking adoption."""
        player = self.make_player()
        self.register(player)
        player._take_ownership()
        self.node.add_player(GUILD_ID, player)
        player._node = self.node
        player._connected = True

        await player.disconnect(force=True)
        self.assertIsNone(player._coordinator.owner)
        self.assertIsNone(self.guild.voice_client)

        await self.node.sync_players()

        self.assertIsNotNone(self.node.get_player(GUILD_ID))

    async def test_restore_removes_a_player_absent_remotely(self) -> None:
        """A local player with no remote counterpart is torn down."""
        self.remote = []
        stale = self.make_player()
        self.register(stale)
        stale._take_ownership()
        self.node.add_player(GUILD_ID, stale)
        stale._node = self.node
        stale._connected = True

        await self.node.sync_players()

        self.assertEqual(self.node.players, [])
        self.assertIsNone(stale._coordinator.owner)
