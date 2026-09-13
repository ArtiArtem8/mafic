"""Opt-in, manual Discord gateway / Lavalink 4.2.2 lifecycle probe.

Run ``python -m unittest tests.integration_discord_voice -v`` with:
MAFIC_DISCORD_VOICE_INTEGRATION=1, MAFIC_TEST_VOICE_EXCLUSIVE=1,
MAFIC_TEST_GUILD_ID, MAFIC_TEST_VOICE_CHANNEL_IDS (three comma-separated IDs),
DISCORD_BOT_TOKEN, LAVALINK_INTEGRATION_URL, LAVALINK_INTEGRATION_PASSWORD.
The exclusive flag attests that the operator checked for other bot instances.
Credentials must come from local environment/configuration, never fixtures.

No messages are sent. A connected bot in the target guild aborts the probe.
There are nine sequential moves, one pair of concurrent moves, and one
move/disconnect overlap, followed by a fresh connection and cleanup. REST
sampling is every 100 ms; absence of a sample or close event cannot rule out
a shorter internal reconnect. Each sequential observation lasts at least one
second and at most five seconds after return; events are attributed to that
observation window, not to an invented Discord request identifier.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import logging
from importlib import import_module
from importlib.metadata import version
from json import loads
from os import environ, getenv
from time import monotonic
from typing import TYPE_CHECKING, cast
from unittest import IsolatedAsyncioTestCase, SkipTest, skipUnless
from unittest.mock import patch

if TYPE_CHECKING:
    from mafic.__libraries import GuildVoiceStatePayload, VoiceServerUpdatePayload
    from mafic.typings import IncomingMessage


class Trace:
    """Keep only explicitly selected, credential-free observations."""

    def __init__(self) -> None:
        self.started = monotonic()
        self.phase = "preflight"
        self.rows: list[dict[str, object]] = []
        self.requests = 0
        self.sent = asyncio.Event()

    def record(self, event: str, **fields: object) -> None:
        """Emit monotonic timestamps without serializing external payloads."""
        elapsed = monotonic() - self.started
        self.rows.append(dict(t=elapsed, phase=self.phase, event=event, **fields))
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        print(f"{elapsed:8.3f} {self.phase} {event} {detail}", flush=True)  # noqa: T201


async def run_probe() -> None:
    """Run the bounded experiment with real adapters, pool, node and callbacks."""
    if getenv("MAFIC_DISCORD_VOICE_INTEGRATION") != "1":
        msg = "manual opt-in only"
        raise SkipTest(msg)

    from aiohttp import ClientSession
    from yarl import URL

    from mafic.__libraries import Client, VoiceChannel, library
    from mafic.errors import HTTPNotFound
    from mafic.node import Node
    from mafic.player import Player
    from mafic.pool import NodePool

    if getenv("MAFIC_TEST_VOICE_EXCLUSIVE") != "1":
        msg = "Check for other bot instances, then set MAFIC_TEST_VOICE_EXCLUSIVE=1."
        raise RuntimeError(msg)
    pinned = {
        "discord.py": "2.7.1",
        "nextcord": "3.2.0",
        "disnake": "2.12.1",
        "py-cord": "2.8.1",
    }
    if library not in pinned or version(library) != pinned[library]:
        msg = "Use an isolated environment with an audited pinned adapter."
        raise RuntimeError(msg)
    guild_id = int(environ["MAFIC_TEST_GUILD_ID"])
    channel_ids = [
        int(value) for value in environ["MAFIC_TEST_VOICE_CHANNEL_IDS"].split(",")
    ]
    if len(set(channel_ids)) != 3:
        msg = "Exactly three distinct voice channel IDs are required."
        raise ValueError(msg)
    url = URL(environ["LAVALINK_INTEGRATION_URL"])
    password = environ["LAVALINK_INTEGRATION_PASSWORD"]
    token = environ["DISCORD_BOT_TOKEN"]
    trace = Trace()
    trace.record("ADAPTER", library=library, version=version(library))
    async with (
        ClientSession(headers={"Authorization": password}) as session,
        session.get(url / "version") as response,
    ):
        if response.status != 200 or await response.text() != "4.2.2":
            msg = "The probe requires /version == 4.2.2."
            raise RuntimeError(msg)
    trace.record("LAVALINK_VERSION", version="4.2.2")

    class ProbeClient(Client):
        async def on_error(self, event_method: str, *_: object, **__: object) -> None:
            # Adapter default exception logging may expose raw external payloads.
            trace.record("CLIENT_CALLBACK_ERROR", method=event_method)

        async def on_socket_raw_send(self, payload: str) -> None:
            data = loads(payload)
            if data.get("op") == 4 and int(data["d"]["guild_id"]) == guild_id:
                trace.requests += 1
                trace.record("OPCODE_4_SEND", channel=data["d"]["channel_id"])
                trace.sent.set()

        async def on_socket_response(self, message: dict[str, object]) -> None:
            if message.get("t") not in ("VOICE_STATE_UPDATE", "VOICE_SERVER_UPDATE"):
                return
            data = cast("dict[str, object]", message["d"])
            if str(data.get("guild_id")) != str(guild_id):
                return
            if message["t"] == "VOICE_STATE_UPDATE":
                if self.user is None or str(data.get("user_id")) != str(self.user.id):
                    return
                trace.record("GATEWAY_STATE", channel=data.get("channel_id"))
            else:
                trace.record("GATEWAY_SERVER")

    class ProbePlayer(Player[Client]):
        async def on_voice_state_update(self, data: GuildVoiceStatePayload) -> None:
            trace.record(
                "VOICE_STATE_UPDATE",
                channel=data["channel_id"],
                session_changed=data["session_id"] != self._session_id,
            )
            try:
                await super().on_voice_state_update(data)
            except Exception as exc:
                trace.record("STATE_CALLBACK_ERROR", exception=type(exc).__name__)
                raise

        async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
            previous = self._server_state
            trace.record(
                "VOICE_SERVER_UPDATE",
                endpoint_changed=previous is None
                or data["endpoint"] != previous["endpoint"],
                token_changed=previous is None or data["token"] != previous["token"],
            )
            try:
                await super().on_voice_server_update(data)
            except Exception as exc:
                trace.record("SERVER_CALLBACK_ERROR", exception=type(exc).__name__)
                raise

    # These wrappers always call the real implementation, with unchanged arguments.
    voice_update = Node[ProbeClient].voice_update
    handle_msg = Node[ProbeClient]._handle_msg
    previous_voice: tuple[str, str | None, str] | None = None

    async def traced_voice_update(
        node: Node[ProbeClient],
        guild_id: int,
        session_id: str,
        data: VoiceServerUpdatePayload,
        channel_id: int,
    ) -> None:
        nonlocal previous_voice
        current = (session_id, data["endpoint"], data["token"])
        changes = [
            "same" if previous_voice is not None and before == after else "changed"
            for before, after in zip(
                previous_voice or (None, None, None), current, strict=True
            )
        ]
        previous_voice = current
        trace.record(
            "LAVALINK_VOICE_UPDATE",
            channel=channel_id,
            session=changes[0],
            endpoint=changes[1],
            token=changes[2],
        )
        await voice_update(node, guild_id, session_id, data, channel_id)
        trace.record("LAVALINK_VOICE_UPDATE_RETURN", channel=channel_id)

    async def traced_message(node: Node[ProbeClient], data: IncomingMessage) -> None:
        if data["op"] == "event" and data["type"] == "WebSocketClosedEvent":
            trace.record("LAVALINK_WEBSOCKET_CLOSE", code=data["code"])
        elif data["op"] == "playerUpdate":
            trace.record(
                "LAVALINK_PLAYER_UPDATE",
                connected=data["state"]["connected"],
                ping=data["state"].get("ping", -1),
            )
        await handle_msg(node, data)

    adapter = import_module(
        "discord" if library in ("discord.py", "py-cord") else library
    )
    intents = adapter.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    client = ProbeClient(intents=intents, enable_debug_events=True)
    pool = NodePool(client)
    gateway: asyncio.Task[None] | None = None
    player: ProbePlayer | None = None
    node: Node[ProbeClient] | None = None
    sampler: asyncio.Task[None] | None = None
    moving: asyncio.Task[None] | None = None
    last_sample: tuple[str | None, bool, int] | None = None

    async def snapshot(source: str) -> tuple[str | None, bool]:
        nonlocal last_sample
        if node is None:
            msg = "Node is not initialized."
            raise RuntimeError(msg)
        try:
            state = await node.fetch_player(guild_id)
        except HTTPNotFound:
            trace.record("LAVALINK_STATE", source=source, exists=False)
            return None, False
        voice_channel = state["voice"].get("channelId")
        connected = state["state"]["connected"]
        current = (voice_channel, connected, state["state"]["ping"])
        if source != "poll" or current != last_sample:
            trace.record(
                "LAVALINK_STATE",
                source=source,
                exists=True,
                channel=voice_channel,
                connected=connected,
                ping=current[2],
            )
        last_sample = current
        return voice_channel, connected

    async def sample() -> None:
        while True:
            await snapshot("poll")
            await asyncio.sleep(0.1)

    async def move(target: VoiceChannel) -> None:
        if player is None:
            msg = "Player is not initialized."
            raise RuntimeError(msg)
        trace.record("MOVE_REQUEST", target=target.id)
        try:
            await player.move_to(target, timeout=10)
        except Exception as exc:
            trace.record("MOVE_ERROR", target=target.id, exception=type(exc).__name__)
            raise
        trace.record("MOVE_RETURN", target=target.id)
        await snapshot("move_return")

    async def connected(target: VoiceChannel) -> None:
        started = monotonic()
        async with asyncio.timeout(5):
            while True:
                channel, ready = await snapshot("readiness")
                if ready and channel == str(target.id):
                    trace.record(
                        "CONNECTED_OBSERVED",
                        target=target.id,
                        elapsed=monotonic() - started,
                    )
                    return
                await asyncio.sleep(0.1)

    # Suppress library logs for this manual probe: they may include voice tokens.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with (
            patch.object(Node, "voice_update", traced_voice_update),
            patch.object(Node, "_handle_msg", traced_message),
        ):
            await client.login(token)
            gateway = asyncio.create_task(client.connect(reconnect=False))
            async with asyncio.timeout(30):
                await client.wait_until_ready()
            guild = client.get_guild(guild_id)
            if guild is None:
                msg = "The bot cannot see the configured guild."
                raise RuntimeError(msg)
            if guild.me.voice is not None and guild.me.voice.channel is not None:
                msg = "Bot is already in voice; no existing connection was touched."
                raise RuntimeError(msg)
            channels: list[VoiceChannel] = []
            for channel_id in channel_ids:
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, VoiceChannel):
                    msg = "Configured channel is not a voice channel in this guild."
                    raise TypeError(msg)
                channels.append(channel)
            # Check for another controller before making any voice request.
            await asyncio.sleep(1)
            if guild.me.voice is not None and guild.me.voice.channel is not None:
                msg = "Another controller entered voice during preflight."
                raise RuntimeError(msg)
            node = await pool.create_node(
                host=url.host or "localhost",
                port=url.port or 2333,
                password=password,
                label="voice-probe",
                secure=url.scheme == "https",
            )
            trace.phase = "connect"
            player = await channels[0].connect(cls=ProbePlayer, timeout=15)
            await connected(channels[0])
            sampler = asyncio.create_task(sample())
            for index in range(9):
                trace.phase = f"sequential-{index + 1}"
                started = monotonic()
                target = channels[(index + 1) % 3]
                await move(target)
                await connected(target)
                await asyncio.sleep(max(0.5, 1 - (monotonic() - started)))
                rows = [row for row in trace.rows if row["phase"] == trace.phase]
                states = [row for row in rows if row["event"] == "VOICE_STATE_UPDATE"]
                servers = [row for row in rows if row["event"] == "VOICE_SERVER_UPDATE"]
                trace.record(
                    "MOVE_OBSERVATION",
                    states=len(states),
                    servers=len(servers),
                    event_order=[
                        row["event"] for row in rows if row in states or row in servers
                    ],
                    server_after_state=(
                        cast("float", servers[0]["t"]) - cast("float", states[0]["t"])
                        if states and servers
                        else None
                    ),
                )
                if sampler.done():
                    # Surface observer failures before more voice changes.
                    await sampler
            trace.phase = "concurrent"
            before_requests = trace.requests
            results = await asyncio.gather(
                asyncio.create_task(move(channels[1])),
                asyncio.create_task(move(channels[2])),
                return_exceptions=True,
            )
            trace.record(
                "CONCURRENT_RESULTS",
                requests=trace.requests - before_requests,
                results=[
                    type(result).__name__ if isinstance(result, BaseException) else "ok"
                    for result in results
                ],
                final_channel=cast("VoiceChannel", player.channel).id,
            )
            await asyncio.sleep(1)
            trace.phase = "reset-to-A"
            await move(channels[0])
            await connected(channels[0])
            await asyncio.sleep(1)
            trace.phase = "move-disconnect"
            trace.sent.clear()
            moving = asyncio.create_task(move(channels[1]))
            async with asyncio.timeout(5):
                await trace.sent.wait()
            results = await asyncio.gather(
                moving,
                player.disconnect(force=True),
                return_exceptions=True,
            )
            # Keep the gateway and node observer alive for late callbacks.
            await asyncio.sleep(1)
            trace.record(
                "DISCONNECT_RESULTS",
                results=[
                    type(result).__name__ if isinstance(result, BaseException) else "ok"
                    for result in results
                ],
                discord_channel=(
                    guild.me.voice.channel.id
                    if guild.me.voice and guild.me.voice.channel
                    else None
                ),
                mafic_channel=cast("VoiceChannel", player.channel).id,
                mafic_connected=player.connected,
            )
            await snapshot("disconnect_final")
            trace.phase = "fresh-connect"
            player = await channels[0].connect(cls=ProbePlayer, timeout=15)
            await connected(channels[0])
    finally:
        trace.phase = "cleanup"
        if moving is not None and not moving.done():
            moving.cancel()
            await asyncio.gather(moving, return_exceptions=True)
        if sampler is not None:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        try:
            if player is not None:
                await player.disconnect(force=True)
            elif client.voice_clients:
                # Also clean up a connect that timed out before returning a Player.
                for voice in client.voice_clients:
                    await voice.disconnect(force=True)
        finally:
            await pool.close()
            await client.close()
            if gateway is not None:
                gateway.cancel()
                await asyncio.gather(gateway, return_exceptions=True)
            logging.disable(previous_logging)
            trace.record("TOTAL_VOICE_REQUESTS", count=trace.requests)


@skipUnless(getenv("MAFIC_DISCORD_VOICE_INTEGRATION") == "1", "manual opt-in only")
class DiscordVoiceIntegrationTests(IsolatedAsyncioTestCase):
    """Never discovered by ordinary test_*.py or enabled by CI configuration."""

    async def test_voice_lifecycle(self) -> None:
        """Record the real sequential and concurrent lifecycle without mocks."""
        try:
            await run_probe()
        except Exception as exc:  # noqa: BLE001 - sanitize the probe boundary
            # Do not print arbitrary HTTP/gateway exception bodies or credentials.
            msg = f"Voice probe failed: {type(exc).__name__}"
            raise AssertionError(msg) from None
