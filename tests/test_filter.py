"""Tests for filter composition."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest import TestCase

from mafic.filter import (
    ChannelMix,
    Distortion,
    Filter,
    Karaoke,
    LowPass,
    Rotation,
    Timescale,
    Tremolo,
    Vibrato,
)

if TYPE_CHECKING:
    from typing import Literal

    MergeOperator = Literal["or", "and", "ior", "iand"]


def merge_filters(name: MergeOperator, left: Filter, right: Filter) -> Filter:
    """Exercise the requested public merge operator."""
    if name == "or":
        return left | right
    if name == "and":
        return left & right
    if name == "ior":
        left |= right
    else:
        left &= right
    return left


class FilterMergeTests(TestCase):
    """Composition preserves precedence, object identity and zero volume."""

    def test_merge_operators_choose_each_filter_and_keep_inputs(self) -> None:
        """All operators use the same precedence for conflicting fields."""
        cases: tuple[tuple[MergeOperator, bool, bool], ...] = (
            ("or", True, False),
            ("and", False, False),
            ("ior", True, True),
            ("iand", False, True),
        )
        for name, prefer_right, in_place in cases:
            with self.subTest(operator=name):
                left = Filter(
                    equalizer=[0.1],
                    karaoke=Karaoke(level=0.2),
                    timescale=Timescale(speed=1.1),
                    tremolo=Tremolo(depth=0.3),
                    rotation=Rotation(rotation_hz=0.4),
                    distortion=Distortion(sin_offset=0.5),
                    low_pass=LowPass(smoothing=0.6),
                    volume=0.0,
                    plugin_filters={"left": {"enabled": True}},
                )
                right = Filter(
                    equalizer=[0.9],
                    karaoke=Karaoke(level=0.8),
                    vibrato=Vibrato(depth=0.7),
                    channel_mix=ChannelMix(left_to_left=0.6),
                    volume=1.0,
                    plugin_filters={"right": {"enabled": True}},
                )
                left_payload, right_payload = left.payload, right.payload
                expected = {
                    "equalizer": right.equalizer if prefer_right else left.equalizer,
                    "karaoke": right.karaoke if prefer_right else left.karaoke,
                    "timescale": left.timescale,
                    "tremolo": left.tremolo,
                    "vibrato": right.vibrato,
                    "rotation": left.rotation,
                    "distortion": left.distortion,
                    "channel_mix": right.channel_mix,
                    "low_pass": left.low_pass,
                }
                selected_plugins = (
                    right.plugin_filters if prefer_right else left.plugin_filters
                )

                result = merge_filters(name, left, right)

                if in_place:
                    self.assertIs(result, left)
                else:
                    self.assertIsNot(result, left)
                for attribute, value in expected.items():
                    self.assertIs(getattr(result, attribute), value, attribute)
                self.assertEqual(result.volume, 1.0 if prefer_right else 0.0)
                self.assertEqual(result.plugin_filters, selected_plugins)
                if in_place:
                    self.assertIs(result.plugin_filters, selected_plugins)
                else:
                    self.assertIsNot(result.plugin_filters, selected_plugins)
                    self.assertEqual(left.payload, left_payload)
                self.assertEqual(right.payload, right_payload)

    def test_empty_preferred_values_fall_back_except_zero_volume(self) -> None:
        """An empty plugin mapping falls back, while a zero volume is retained."""
        for name in ("or", "ior"):
            with self.subTest(operator=name):
                left = Filter(volume=1.0, plugin_filters={"left": {"enabled": True}})
                right = Filter(volume=0.0)

                result = merge_filters(name, left, right)

                self.assertEqual(result.volume, 0.0)
                self.assertEqual(result.plugin_filters, {"left": {"enabled": True}})

    def test_merge_rejects_non_filters(self) -> None:
        """Every merge operator reports the same invalid operand error."""
        invalid = cast("Filter", 42)
        for name in ("or", "and", "ior", "iand"):
            with (
                self.subTest(operator=name),
                self.assertRaisesRegex(TypeError, "Expected Filter instance"),
            ):
                merge_filters(name, Filter(), invalid)

    def test_and_does_not_call_the_other_operands_or_override(self) -> None:
        """Subclass operator overrides do not change the other operand's merge."""

        class CustomFilter(Filter):
            def __or__(self, other: Filter) -> Filter:
                """Make an unexpected call to this override visible."""
                message = f"Custom __or__ called with {other!r}"
                raise AssertionError(message)

        result = Filter(volume=0.0) & CustomFilter(volume=1.0)

        self.assertEqual(result.volume, 0.0)
