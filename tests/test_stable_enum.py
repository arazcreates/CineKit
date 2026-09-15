# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Pure-Python tests for stable_enum.py — run with:
    python -m unittest discover tests    (from the extension root)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stable_enum  # noqa: E402


def _entries(ids):
    return [(stable_enum.NONE_ID, "None", "")] + [(i, i.title(), "")
                                                  for i in ids]


class TestNumbers(unittest.TestCase):
    def test_none_is_zero(self):
        self.assertEqual(stable_enum.number_for("NONE"), 0)

    def test_numbers_avoid_legacy_range(self):
        for ident in ("vhs", "super8", "a", "custom_my_look", "35mm_modern"):
            self.assertGreaterEqual(stable_enum.number_for(ident),
                                    stable_enum.LEGACY_LIMIT)

    def test_numbers_fit_signed_int(self):
        for ident in ("x" * n for n in range(1, 60)):
            self.assertLess(stable_enum.number_for(ident), 2 ** 31)

    def test_collision_moves_to_next_free_number(self):
        taken = {0}
        first = stable_enum.number_for("vhs", taken)
        taken_again = {0, first}
        second = stable_enum.number_for("vhs", taken_again)
        self.assertEqual(second, first + 1)


class TestNumberedItems(unittest.TestCase):
    def test_order_change_keeps_numbers(self):
        before = stable_enum.numbered_items(_entries(["vhs", "super8"]))
        after = stable_enum.numbered_items(
            _entries(["aaa_new", "super8", "vhs"]))
        num_before = {e[0]: e[3] for e in before}
        num_after = {e[0]: e[3] for e in after}
        self.assertEqual(num_before["vhs"], num_after["vhs"])
        self.assertEqual(num_before["super8"], num_after["super8"])

    def test_display_order_stays(self):
        items = stable_enum.numbered_items(_entries(["zeta", "alpha"]))
        self.assertEqual([e[0] for e in items], ["NONE", "zeta", "alpha"])

    def test_numbers_unique(self):
        ids = [f"look_{i}" for i in range(500)]
        numbers = [e[3] for e in stable_enum.numbered_items(_entries(ids))]
        self.assertEqual(len(numbers), len(set(numbers)))


class TestLegacy(unittest.TestCase):
    ORDER = ["NONE", "anamorphic", "vhs", "super8"]

    def test_position_maps_to_identifier(self):
        self.assertEqual(stable_enum.legacy_identifier(2, self.ORDER), "vhs")

    def test_zero_is_not_migrated(self):
        self.assertIsNone(stable_enum.legacy_identifier(0, self.ORDER))

    def test_new_numbers_are_not_migrated(self):
        number = stable_enum.number_for("vhs")
        self.assertIsNone(stable_enum.legacy_identifier(number, self.ORDER))

    def test_out_of_range_position(self):
        self.assertIsNone(stable_enum.legacy_identifier(9, self.ORDER))

    def test_non_integer_values(self):
        for value in (None, "vhs", 2.0, True):
            self.assertIsNone(stable_enum.legacy_identifier(value,
                                                            self.ORDER))


if __name__ == "__main__":
    unittest.main()
