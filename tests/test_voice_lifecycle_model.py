"""Tests for the implementation-independent voice ownership model."""

# SPDX-License-Identifier: MIT

from __future__ import annotations

from typing import ClassVar
from unittest import TestCase

from .voice_lifecycle_model import GuildRegistry, Op, interleavings


def rules(registry: GuildRegistry) -> list[str]:
    """Return the rule ids a replay reported."""
    return [violation.rule for violation in registry.consistent()]


class ModelRuleTests(TestCase):
    """Negative controls: each rule must fire on the trace it describes."""

    def test_fixed_protocol_lifecycle_is_clean(self) -> None:
        """One generation registers, claims, patches, deletes and leaves."""
        registry = GuildRegistry()
        registry.apply("register", "g1")
        registry.apply("claim", "g1")
        registry.apply("patch", "g1", session="s1")
        registry.apply("delete", "g1", session="s1")
        registry.apply("unregister", "g1")

        self.assertEqual(rules(registry), [])
        self.assertIsNone(registry.registry)
        self.assertEqual(registry.remote, {})

    def test_register_over_live_owner_is_reported(self) -> None:
        """A second Player may only exist if something overwrote the cache."""
        registry = GuildRegistry()
        registry.apply("register", "g1")
        registry.apply("claim", "g1")
        registry.apply("register", "g2")

        self.assertIn("R-DISPLACE", rules(registry))

    def test_stale_unregister_is_reported(self) -> None:
        """Base VoiceProtocol.cleanup pops the slot whoever holds it."""
        registry = GuildRegistry()
        registry.apply("register", "g1")
        registry.apply("claim", "g1")
        registry.apply("register", "g2")
        registry.apply("claim", "g2")
        registry.apply("unregister", "g1")

        self.assertIn("R-STALE-UNREGISTER", rules(registry))
        self.assertEqual(registry.registry, "g2")

    def test_delete_of_newer_remote_player_is_reported(self) -> None:
        """The old DELETE lands after the new generation created a player."""
        registry = GuildRegistry()
        registry.apply("register", "g1")
        registry.apply("claim", "g1")
        registry.apply("register", "g2")
        registry.apply("claim", "g2")
        registry.apply("patch", "g2", session="s1")
        # The stale generation still issues its destroy for the same session.
        registry.apply("delete", "g1", session="s1")

        found = rules(registry)
        self.assertIn("R-DELETE-NEWER", found)
        self.assertEqual(registry.remote.get("s1"), "g2")

    def test_stale_remote_effect_is_reported(self) -> None:
        """A superseded generation must not patch after a newer claim."""
        registry = GuildRegistry()
        registry.apply("claim", "g1")
        registry.apply("claim", "g2")
        registry.apply("patch", "g1", session="s1")

        self.assertIn("R-STALE-REMOTE", rules(registry))

    def test_unclaimed_remote_effect_is_reported(self) -> None:
        """Every remote effect must belong to a claimed generation."""
        registry = GuildRegistry()
        registry.apply("delete", "g1", session="s1")

        self.assertIn("R-UNGOVERNED", rules(registry))

    def test_registered_but_unclaimed_is_reported(self) -> None:
        """A cache entry without a generation is an unowned session."""
        registry = GuildRegistry()
        registry.apply("register", "g1")

        self.assertIn("R-UNCLAIMED", rules(registry))


class InterleavingTests(TestCase):
    """The checker must never let a bad end state pass unreported."""

    # One generation retires, the next takes over. Each actor keeps its own
    # program order, and every merge is a scheduler decision we cannot control.
    FIRST: ClassVar[list[tuple[Op, str, str | None]]] = [
        ("register", "g1", None),
        ("claim", "g1", None),
        ("patch", "g1", "s1"),
        ("delete", "g1", "s1"),
        ("unregister", "g1", None),
    ]
    SECOND: ClassVar[list[tuple[Op, str, str | None]]] = [
        ("register", "g2", None),
        ("claim", "g2", None),
        ("patch", "g2", "s1"),
    ]

    def test_every_interleaving_is_either_clean_or_reported(self) -> None:
        """No merge may end with g2 unregistered or g1 owning s1 silently."""
        seen = 0
        clean = 0

        for merge in interleavings(self.FIRST, self.SECOND):
            registry = GuildRegistry()
            for op, token, session in merge:
                registry.apply(op, token, session=session)

            seen += 1
            if registry.consistent():
                # A reported breach is an allowed, visible outcome.
                continue

            clean += 1
            self.assertEqual(registry.registry, "g2")
            self.assertNotIn("g1", registry.remote.values())
            self.assertEqual(registry.remote.get("s1"), "g2")

        self.assertGreater(seen, 0)
        self.assertGreater(clean, 0)

    def test_a_single_reordering_of_cleanup_breaks_the_end_state(self) -> None:
        """A stale teardown running one step later must be caught."""
        registry = GuildRegistry()
        registry.apply("register", "g1")
        registry.apply("claim", "g1")
        registry.apply("patch", "g1", session="s1")
        registry.apply("register", "g2")
        registry.apply("claim", "g2")
        registry.apply("patch", "g2", session="s1")
        registry.apply("delete", "g1", session="s1")
        registry.apply("unregister", "g1")

        found = rules(registry)
        self.assertIn("R-DELETE-NEWER", found)
        self.assertIn("R-STALE-UNREGISTER", found)
