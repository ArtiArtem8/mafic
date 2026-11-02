"""Lifetime tests for the per-guild voice coordinator registry."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import gc
import weakref
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from mafic.__libraries import Client, VoiceChannel
from mafic.player import Player
from mafic.pool import NodePool

from .test_voice_ownership_poc import (
    CHANNEL_ID,
    GUILD_ID,
    FakeClient,
    FakeGuild,
    FakeNode,
)

if TYPE_CHECKING:
    from mafic.node import Node


class CoordinatorLifetimeTests(IsolatedAsyncioTestCase):
    """The registry must not outlive the client it belongs to."""

    def setUp(self) -> None:
        """Install a pool registry so Player.connect can resolve a node."""
        self.node = FakeNode()
        self.enterContext(patch.object(NodePool, "nodes", [self.node]))

    def make_player(self, client: FakeClient, guild: FakeGuild) -> Player[Client]:
        """Build a real Player bound to the fake guild and client."""
        channel = Mock(spec=VoiceChannel, id=CHANNEL_ID, guild=guild)
        channel._get_voice_client_key.return_value = (GUILD_ID, None)
        return Player(cast("Client", client), channel)

    def bind(self, player: Player[Client], node: FakeNode) -> None:
        """Attach the player to a node, as on_voice_server_update would."""
        node.add_player(GUILD_ID, player)
        player._node = cast("Node[Client]", node)

    async def connect(self, player: Player[Client]) -> None:
        """Complete a connect without real gateway traffic."""
        player._voice_state_update_event.set()
        player._voice_server_update_event.set()
        player._node_player_ready_event.set()
        await player.connect(timeout=2, reconnect=True)

    async def closed_client_refs(
        self,
    ) -> tuple[weakref.ReferenceType[object], weakref.ReferenceType[object]]:
        """Return weak references after a full connect/disconnect cycle."""
        client = FakeClient()
        guild = FakeGuild(client)
        player = self.make_player(client, guild)
        self.bind(player, self.node)
        await self.connect(player)
        await player.disconnect(force=True)
        coordinator = player._coordinator
        return weakref.ref(client), weakref.ref(coordinator)

    async def test_closed_client_is_collectable(self) -> None:
        """A closed client and its coordinator must be collectable."""
        client_ref, coordinator_ref = await self.closed_client_refs()
        gc.collect()

        self.assertIsNone(client_ref())
        self.assertIsNone(coordinator_ref())

    async def test_clients_never_share_a_coordinator(self) -> None:
        """Two clients must not serialize independent guilds together."""
        first = FakeClient()
        second = FakeClient()
        guild = FakeGuild(first)
        player = self.make_player(first, guild)

        from mafic._voice_ownership import coordinator_for

        self.assertIs(coordinator_for(first, GUILD_ID), player._coordinator)
        self.assertIsNot(
            coordinator_for(first, GUILD_ID), coordinator_for(second, GUILD_ID)
        )

    async def test_repeated_cycles_reuse_one_coordinator(self) -> None:
        """Reconnecting one guild keeps one coordinator and grows the epoch."""
        client = FakeClient()
        guild = FakeGuild(client)

        first = self.make_player(client, guild)
        await self.connect(first)
        await first.disconnect(force=True)
        coordinator = first._coordinator
        first_epoch = coordinator.epoch

        second = self.make_player(client, guild)
        await self.connect(second)

        self.assertIs(second._coordinator, coordinator)
        self.assertGreater(coordinator.epoch, first_epoch)
        self.assertTrue(coordinator.is_current(second, second._ownership_epoch))

    async def test_retired_player_is_not_retained(self) -> None:
        """A superseded player must not stay alive through the coordinator."""
        client = FakeClient()
        guild = FakeGuild(client)
        first = self.make_player(client, guild)
        first._take_ownership()
        coordinator = first._coordinator
        retired = weakref.ref(first)

        second = self.make_player(client, guild)
        second._take_ownership()
        self.assertFalse(coordinator.is_current(first, first._ownership_epoch))

        del first
        gc.collect()

        self.assertIsNone(retired())

    async def test_retired_player_is_released_without_replacement(self) -> None:
        """The owner reference is weak even while it is still the owner."""
        client = FakeClient()
        guild = FakeGuild(client)
        player = self.make_player(client, guild)
        player._take_ownership()
        retired = weakref.ref(player)

        del player
        gc.collect()

        self.assertIsNone(retired())

    async def test_active_teardown_keeps_the_coordinator_alive(self) -> None:
        """A running teardown must keep its lane and its epoch valid."""
        client = FakeClient()
        guild = FakeGuild(client)
        player = self.make_player(client, guild)
        self.bind(player, self.node)
        await self.connect(player)

        self.node.block_destroy = True
        teardown = asyncio.create_task(player.disconnect(force=True))
        await asyncio.wait_for(self.node.destroy_started.wait(), 2)

        coordinator_ref = weakref.ref(player._coordinator)
        del player
        gc.collect()
        self.assertIsNotNone(coordinator_ref())

        self.node.release_destroy.set()
        await asyncio.wait_for(teardown, 2)

        # The client is still alive, so the guild entry must survive with it.
        self.assertIsNotNone(coordinator_ref())
