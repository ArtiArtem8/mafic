"""Ownership coordination for one guild voice session."""

# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary, ref

if TYPE_CHECKING:
    from asyncio import Lock, Task
    from collections.abc import AsyncGenerator

__all__ = ("GuildVoiceCoordinator", "coordinator_for")


class GuildVoiceCoordinator:
    """Single writer for one guild voice session on one client.

    Two kinds of mutation exist here, and they need different mechanisms.

    Local registries are the Discord voice-client cache and Node._players. A
    check and a write against them must happen in one synchronous block on the
    event loop, because that is the only thing that makes the pair atomic. This
    class does not wrap those writes; it supplies the epoch a caller compares
    against, and the callers keep their check and write adjacent.

    Remote effects are the Lavalink PATCH and DELETE that outlive the object
    that issued them. These need a real ordered lane, because a replacement
    generation is a different Player with a different lock, so no per-object
    lock can order it against its predecessor.

    The lane is not reentrant and must never be awaited while holding another
    lock that a Discord callback needs to acquire. A teardown that waited on a
    callback which itself needed the lane would deadlock. The lock order is
    therefore always lane-before-Player-lock, never the reverse.

    The owner is held through a weak reference. A strong one would make the
    registry value keep its own key alive: coordinator -> Player -> client,
    where the client is the weak key, so no entry would ever be collected.
    A live owner is always strongly reachable from the Discord voice-client
    cache or from Node._players, so the weak reference cannot drop an owner
    that still matters.
    """

    # __weakref__ lets callers observe when a coordinator is released without
    # keeping it alive; the registry itself never depends on that hook.
    __slots__ = (
        "__weakref__",
        "_epoch",
        "_holder",
        "_lane",
        "_owner",
        "_remote_unknown",
    )

    def __init__(self) -> None:
        self._lane: Lock = asyncio.Lock()
        self._holder: Task[object] | None = None
        self._owner: ref[object] | None = None
        self._epoch = 0
        self._remote_unknown = 0

    @property
    def owner(self) -> object | None:
        """Return the player currently authorised to speak for this guild."""
        if self._owner is None:
            return None
        return self._owner()

    @property
    def epoch(self) -> int:
        """The latest generation granted for this guild."""
        return self._epoch

    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[None, None]:
        """Serialize this guild's remote effects for the calling task.

        The lane is deliberately not reentrant. A task that already holds it
        would otherwise wait for itself, so re-entry fails loudly instead of
        deadlocking the guild forever.
        """
        task = asyncio.current_task()
        if self._holder is task:
            msg = "The guild lane is not reentrant."
            raise RuntimeError(msg)
        async with self._lane:
            self._holder = task
            try:
                yield
            finally:
                self._holder = None

    def holds_lane(self) -> bool:
        """Whether the calling task currently holds this guild's lane."""
        return self._holder is not None and self._holder is asyncio.current_task()

    @property
    def remote_unknown(self) -> int:
        """How many remote effects of this guild ended with an unknown result.

        Lavalink has no conditional write, so a lost DELETE response cannot be
        fenced: the server may still apply it after this counter moves. The
        value is a diagnostic and a reason to prefer another session, never
        proof that a slot is empty.
        """
        return self._remote_unknown

    def mark_remote_unknown(self) -> None:
        """Record that a remote effect may or may not have been applied."""
        self._remote_unknown += 1

    def require_lane(self) -> None:
        """Require the current task to hold this guild's lane.

        Internal helpers that require the lane call this before mutating remote
        state. Direct Node.update, Node.voice_update and Node.destroy calls are
        uncoordinated and remain the caller's responsibility.
        """
        if not self.holds_lane():
            msg = "This operation requires the guild lane."
            raise RuntimeError(msg)

    def claim(self, player: object) -> int:
        """Grant ownership to a player and return its new epoch.

        This is synchronous on purpose. It is the linearization point of a
        connect, so it must not be separated from the caller decision that
        follows by an await. A claim immediately demotes every older
        generation, which is what makes a hung teardown safe rather than
        merely slow.
        """
        self._epoch += 1
        self._owner = ref(player)
        return self._epoch

    def release(self, player: object) -> None:
        """Drop ownership only when the caller still holds it."""
        if self.owner is player:
            self._owner = None

    def is_current(self, player: object, epoch: int | None = None) -> bool:
        """Whether a player, optionally at a specific epoch, is authoritative.

        The epoch argument distinguishes one owner from itself before and after
        a re-claim, which identity alone cannot do.
        """
        if self.owner is not player:
            return False
        return epoch is None or self._epoch == epoch


_coordinators: WeakKeyDictionary[object, dict[int, GuildVoiceCoordinator]] = (
    WeakKeyDictionary()
)


def coordinator_for(client: object, guild_id: int) -> GuildVoiceCoordinator:
    """Return the coordinator for one guild on one client.

    Discord keys its voice-client cache by guild id per client. The coordinator
    shares that scope even if a pool is closed and replaced for the same client.
    """
    per_guild = _coordinators.get(client)
    if per_guild is None:
        per_guild = {}
        _coordinators[client] = per_guild
    coordinator = per_guild.get(guild_id)
    if coordinator is None:
        coordinator = per_guild[guild_id] = GuildVoiceCoordinator()
    return coordinator
