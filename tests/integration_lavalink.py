"""Opt-in smoke test against a real Lavalink 4.2.2 process."""

from __future__ import annotations

from os import getenv
from unittest import IsolatedAsyncioTestCase, skipUnless

from aiohttp import ClientSession

INTEGRATION_URL = getenv("LAVALINK_INTEGRATION_URL")
INTEGRATION_PASSWORD = getenv("LAVALINK_INTEGRATION_PASSWORD")
ENCODED_TRACK = (
    "QAAAjQIAJVJpY2sgQXN0bGV5IC0gTmV2ZXIgR29ubmEgR2l2ZSBZb3UgVXAA"
    "DlJpY2tBc3RsZXlWRVZPAAAAAAADPCAAC2RRdzR3OVdnWGNRAAEAK2h0dHBz"
    "Oi8vd3d3LnlvdXR1YmUuY29tL3dhdGNoP3Y9ZFF3NHc5V2dYY1EAB3lvdXR1"
    "YmUAAAAAAAAAAA=="
)


@skipUnless(
    INTEGRATION_URL and INTEGRATION_PASSWORD,
    "set LAVALINK_INTEGRATION_URL and LAVALINK_INTEGRATION_PASSWORD",
)
class LavalinkIntegrationTests(IsolatedAsyncioTestCase):
    """Exercise the stable Lavalink 4.2.2 REST and websocket contracts."""

    async def test_track_user_data_round_trip(self) -> None:
        """Update/get/stop a player with canonical nested track metadata."""
        if INTEGRATION_URL is None or INTEGRATION_PASSWORD is None:
            self.fail("Integration environment was not configured.")

        headers = {
            "Authorization": INTEGRATION_PASSWORD,
            "User-Id": "123456789",
            "Client-Name": "Mafic/integration",
        }
        # Parenthesized multi-context syntax is unavailable on Python 3.8.
        async with ClientSession(headers=headers) as session:  # noqa: SIM117
            async with session.ws_connect(
                f"{INTEGRATION_URL}/v4/websocket"
            ) as websocket:
                ready = await websocket.receive_json(timeout=10)
                self.assertEqual(ready["op"], "ready")
                session_id = ready["sessionId"]

                async with session.get(f"{INTEGRATION_URL}/version") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.text(), "4.2.2")

                async with session.get(
                    f"{INTEGRATION_URL}/v4/decodetrack",
                    params={"encodedTrack": ENCODED_TRACK},
                ) as response:
                    self.assertEqual(response.status, 200)
                    decoded = await response.json()
                    self.assertIn("pluginInfo", decoded)
                    self.assertIn("userData", decoded)

                player_url = (
                    f"{INTEGRATION_URL}/v4/sessions/{session_id}/players/123456789"
                )
                update = {
                    "track": {
                        "encoded": ENCODED_TRACK,
                        "userData": {"correlation_id": "integration"},
                    }
                }
                async with session.patch(player_url, json=update) as response:
                    self.assertEqual(response.status, 200)
                    player = await response.json()
                    self.assertEqual(
                        player["track"]["userData"]["correlation_id"],
                        "integration",
                    )

                async with session.get(player_url) as response:
                    self.assertEqual(response.status, 200)
                    player = await response.json()
                    self.assertEqual(
                        player["track"]["userData"]["correlation_id"],
                        "integration",
                    )

                async with session.patch(
                    player_url, json={"track": {"encoded": None}}
                ) as response:
                    self.assertEqual(response.status, 200)
                    player = await response.json()
                    self.assertIsNone(player["track"])

                async with session.delete(player_url) as response:
                    self.assertEqual(response.status, 204)
