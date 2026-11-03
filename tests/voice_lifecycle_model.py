"""Reference model for guild voice ownership.

This module is deliberately independent of mafic: it models the two registries a
guild voice session lives in, the operations that mutate them, and the
invariants those operations must preserve. It exists so the rules can be checked
exhaustively on small traces without a running Discord client, and so concrete
tests can be stated in the same vocabulary.

The model distinguishes:

registry
    The Discord voice-client cache (ConnectionState._voice_clients), keyed by
    guild id and written by the Discord library itself, not by mafic.
remote
    The Lavalink player for a (session, guild) pair, identified by the token
    that created it. session is the Lavalink session id, so two nodes never
    share a slot.
epoch
    A monotonically increasing generation number for one guild. Every grant of
    ownership mints a new epoch, so a token can be tested for staleness with a
    value comparison instead of an identity guess.

Every operation is annotated with its linearization point: the single instant
at which the operation takes effect.
"""

# SPDX-License-Identifier: MIT

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Literal

__all__ = ("GuildRegistry", "Op", "Trace", "Violation", "interleavings")

Op = Literal["register", "claim", "patch", "delete", "unregister", "release"]


@dataclass(frozen=True)
class Violation:
    """One invariant breach found while replaying a trace."""

    rule: str
    detail: str

    def __str__(self) -> str:
        """Render the rule and the concrete operation that broke it."""
        return f"{self.rule}: {self.detail}"


class GuildRegistry:
    """Two registries plus a generation counter for one guild.

    Operations are total functions over an explicit operation name so a trace
    can be replayed and checked. A rejected operation is recorded as a
    violation rather than raising, because the checker must report every
    breach instead of stopping at the first one.
    """

    def __init__(self) -> None:
        """Start with an empty cache, no remote players and no owner."""
        self.registry: str | None = None
        # session -> token of the Lavalink player created there.
        self.remote: dict[str, str] = {}
        # token -> generation granted at claim time.
        self.epoch_of: dict[str, int] = {}
        # Latest generation granted for this guild.
        self.epoch: int = 0
        self.violations: list[Violation] = []

    def _fail(self, rule: str, detail: str) -> None:
        self.violations.append(Violation(rule, detail))

    # Linearization points:
    #   register   - the library writes the cache slot, before Player.connect
    #   claim      - the epoch counter advances; this is the ownership grant
    #   patch      - the (session, guild) slot takes the writer token
    #   delete     - the (session, guild) slot is cleared
    #   unregister - the cache slot is cleared, only if it still holds the caller
    #   release    - the guild is left without an owner

    def apply(self, op: Op, token: str, *, session: str | None = None) -> None:
        """Apply one operation and record any invariant it breaks."""
        if op == "register":
            if self.registry is not None and self.registry != token:
                # A second Player for one guild silently displaces the first.
                # Discord allows it only through direct cache writes, which is
                # exactly what Node.sync_players does.
                self._fail(
                    "R-DISPLACE", f"{token} registered over live {self.registry}"
                )
            self.registry = token
            return

        if op == "claim":
            self.epoch += 1
            self.epoch_of[token] = self.epoch
            return

        if op == "unregister":
            if self.registry != token:
                # The unconditional pop in VoiceProtocol.cleanup: a stale
                # Player removes the current owner from the Discord cache.
                self._fail(
                    "R-STALE-UNREGISTER",
                    f"{token} unregistered while {self.registry} is registered",
                )
                return
            self.registry = None
            return

        if op == "release":
            if self.epoch_of.get(token) == self.epoch:
                self.epoch_of.pop(token, None)
                if self.registry == token:
                    self.registry = None
            return

        if op not in ("patch", "delete"):
            msg = f"unknown operation {op}"
            raise ValueError(msg)
        if session is None:
            msg = f"{op} requires a session"
            raise ValueError(msg)
        self._apply_remote(op, token, session)

    def _apply_remote(self, op: Op, token: str, session: str) -> None:
        """Apply one Lavalink effect against a single session slot."""
        current = self.epoch_of.get(token)
        if current is None:
            self._fail("R-UNGOVERNED", f"{token} {op} without a claim")
        elif current != self.epoch:
            # A stale generation must not touch remote state that a newer
            # generation now owns.
            self._fail(
                "R-STALE-REMOTE",
                f"{token} {op} at epoch {current} while {self.epoch} is current",
            )

        if op == "patch":
            self.remote[session] = token
            return

        holder = self.remote.get(session)
        if holder is None:
            # Deleting an absent player is a no-op upstream: neither safe nor
            # unsafe on its own.
            return
        if holder != token:
            # The dangerous overlap: a DELETE issued for the old generation
            # destroys the player a newer generation created in the same
            # (session, guild) slot.
            self._fail("R-DELETE-NEWER", f"{token} deleted {holder} in {session}")
            return
        del self.remote[session]

    def consistent(self) -> list[Violation]:
        """Return every violation plus any end-state disagreement."""
        problems = list(self.violations)
        if self.registry is not None and self.registry not in self.epoch_of:
            problems.append(
                Violation(
                    "R-UNCLAIMED",
                    f"{self.registry} is registered but never claimed",
                )
            )
        return problems


class Trace:
    """A named sequence of operations replayed against one guild."""

    def __init__(self, name: str) -> None:
        """Start an empty, named trace."""
        self.name = name
        self.ops: list[tuple[Op, str, str | None]] = []

    def add(self, op: Op, token: str, session: str | None = None) -> Trace:
        """Append one operation and return this trace for chaining."""
        self.ops.append((op, token, session))
        return self

    def replay(self) -> GuildRegistry:
        """Replay every operation against one fresh guild registry."""
        registry = GuildRegistry()
        for op, token, session in self.ops:
            registry.apply(op, token, session=session)
        return registry


def interleavings(
    first: Iterable[tuple[Op, str, str | None]],
    second: Iterable[tuple[Op, str, str | None]],
) -> Iterator[list[tuple[Op, str, str | None]]]:
    """Yield every order-preserving merge of two operation sequences.

    Each source keeps its own internal order, which is what per-actor program
    order means: one actor cannot reorder its own operations, but two actors
    interleave freely. The merges cover the two supplied operation sequences;
    they do not model intermediate await points or verify the implementation.
    """
    left = list(first)
    right = list(second)

    def walk(
        i: int, j: int, acc: list[tuple[Op, str, str | None]]
    ) -> Iterator[list[tuple[Op, str, str | None]]]:
        if i == len(left) and j == len(right):
            yield list(acc)
            return
        if i < len(left):
            acc.append(left[i])
            yield from walk(i + 1, j, acc)
            acc.pop()
        if j < len(right):
            acc.append(right[j])
            yield from walk(i, j + 1, acc)
            acc.pop()

    yield from walk(0, 0, [])
