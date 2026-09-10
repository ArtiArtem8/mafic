"""Opt-in smoke test against a real Lavalink 4.2.2 process.

This drives Mafic's own REST serialization and Track parsing. It does not cover
Mafic's websocket or voice handling, which needs a Discord client.
"""

from __future__ import annotations

from os import getenv
from unittest import IsolatedAsyncioTestCase, skipUnless

from yarl import URL

INTEGRATION_URL = getenv("LAVALINK_INTEGRATION_URL")
INTEGRATION_PASSWORD = getenv("LAVALINK_INTEGRATION_PASSWORD")
ENCODED_TRACK = (
    "QAAAjQIAJVJpY2sgQXN0bGV5IC0gTmV2ZXIgR29ubmEgR2l2ZSBZb3UgVXAA"
    "DlJpY2tBc3RsZXlWRVZPAAAAAAADPCAAC2RRdzR3OVdnWGNRAAEAK2h0dHBz"
    "Oi8vd3d3LnlvdXR1YmUuY29tL3dhdGNoP3Y9ZFF3NHc5V2dYY1EAB3lvdXR1"
    "YmUAAAAAAAAAAA=="
)
GUILD_ID = 123456789


@skipUnless(
    INTEGRATION_URL and INTEGRATION_PASSWORD,
    "set LAVALINK_INTEGRATION_URL and LAVALINK_INTEGRATION_PASSWORD",
)
class MaficLavalinkIntegrationTests(IsolatedAsyncioTestCase):
    """Exercise Mafic against a real Lavalink 4 node."""

    async def asyncSetUp(self) -> None:
        """Build a Node that talks to the real node without a Discord client."""
        # Mafic refuses to import when several Discord libraries are installed,
        # so `MAFIC_IGNORE_LIBRARY_CHECK=1` may be needed to collect this module.
        from asyncio import Event

        from mafic.node import Node

        if INTEGRATION_URL is None or INTEGRATION_PASSWORD is None:
            self.fail("Integration environment was not configured.")

        node: Node[object] = object.__new__(Node)
        node._version = 4
        node._label = "integration"
        node._rest_uri = URL(INTEGRATION_URL) / "v4"
        node._session_id = None
        node._ws = None
        node._ws_task = None
        node._connect_task = None
        node._ready = Event()
        node._event_queue = Event()
        object.__setattr__(node, "_Node__password", INTEGRATION_PASSWORD)
        object.__setattr__(node, "_Node__session", None)
        self.node = node

        # A websocket connection creates the session that owns the player.
        from aiohttp import ClientSession

        self.session = ClientSession(
            headers={
                "Authorization": INTEGRATION_PASSWORD,
                "User-Id": str(GUILD_ID),
                "Client-Name": "Mafic/integration",
            }
        )
        self.websocket = await self.session.ws_connect(
            f"{INTEGRATION_URL}/v4/websocket"
        )
        ready = await self.websocket.receive_json(timeout=10)
        self.assertEqual(ready["op"], "ready")
        self.node._session_id = ready["sessionId"]

    async def asyncTearDown(self) -> None:
        """Destroy the test player and release the session."""
        try:
            await self.node.destroy(GUILD_ID)
        finally:
            await self.node.close()
            await self.websocket.close()
            await self.session.close()

    async def test_track_user_data_round_trip(self) -> None:
        """Mafic sends and parses canonical v4 track metadata."""
        async with self.session.get(f"{INTEGRATION_URL}/version") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), "4.2.2")

        # Decoding goes through Mafic's Track parsing.
        decoded = await self.node.decode_track(ENCODED_TRACK)
        self.assertIsInstance(decoded.plugin_info, dict)
        self.assertIsInstance(decoded.user_data, dict)

        player = await self.node.update(
            guild_id=GUILD_ID,
            track=decoded,
            user_data={"correlation_id": "integration"},
        )

        track = player["track"]
        if track is None:
            self.fail("Lavalink did not accept the updated track.")
        self.assertEqual(track["userData"]["correlation_id"], "integration")

        # A subsequent read goes through the same parsing path.
        fetched = await self.node.fetch_player(GUILD_ID)
        track = fetched["track"]
        if track is None:
            self.fail("Lavalink did not report the updated track.")
        self.assertEqual(track["userData"]["correlation_id"], "integration")

        stopped = await self.node.update(guild_id=GUILD_ID, track=None)
        self.assertIsNone(stopped["track"])
