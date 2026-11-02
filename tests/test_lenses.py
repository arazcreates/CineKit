# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Pure-Python tests for the lens preset registry — run with:
    python -m unittest discover tests    (from the extension root)
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import lenses  # noqa: E402


def entry(**overrides):
    data = {"name": "Test", "focal": 50, "max_aperture": 1.4}
    data.update(overrides)
    return data


class TestNormalize(unittest.TestCase):
    def test_defaults_keep_old_behavior(self):
        lens = lenses._normalize(entry())
        self.assertEqual(lens["breathing_model"], "STYLIZED")
        self.assertEqual(lens["blades"], 0)
        self.assertEqual(lens["transmission"], 1.0)
        self.assertEqual(lens["squeeze"], 1.0)
        self.assertEqual(lens["breathing_table"], [])

    def test_table_is_sorted(self):
        lens = lenses._normalize(entry(
            breathing_model="TABLE",
            breathing_table=[[1.0, 51.0], [0.3, 54.0], [100.0, 50.0]]))
        self.assertEqual([d for d, _f in lens["breathing_table"]],
                         [0.3, 1.0, 100.0])

    def test_table_model_needs_two_points(self):
        with self.assertRaises(ValueError):
            lenses._normalize(entry(breathing_model="TABLE",
                                    breathing_table=[[1.0, 51.0]]))

    def test_rejects_unknown_model(self):
        with self.assertRaises(ValueError):
            lenses._normalize(entry(breathing_model="MAGIC"))

    def test_rejects_out_of_range(self):
        for bad in ({"transmission": 1.5}, {"squeeze": 0.5},
                    {"blades": 40}, {"breathing": 3.0}):
            with self.assertRaises(ValueError):
                lenses._normalize(entry(**bad))


class TestShippedLenses(unittest.TestCase):
    def setUp(self):
        lenses.reload_registry()

    def test_every_shipped_lens_loads(self):
        path = os.path.join(ROOT, "data", "lenses.json")
        with open(path, encoding="utf-8") as fh:
            ids = [lens["id"] for lens in json.load(fh)["lenses"]]
        for lid in ids:
            self.assertIsNotNone(lenses.get(lid), lid)

    def test_new_presets_stay_out_of_the_legacy_order(self):
        legacy = lenses.legacy_ids()
        self.assertNotIn("anamorphic_50_2x", legacy)
        # CineKit 2.1.1 and older stored list positions: 16 lenses plus
        # NONE, sorted by focal length.
        self.assertEqual(len(legacy), 17)
        self.assertEqual(legacy[3], "prime_24")

    def test_cine_t_stops_match_their_names(self):
        for lid in ("cine_32", "cine_75"):
            lens = lenses.get(lid)
            tstop = lens["max_aperture"] / lens["transmission"] ** 0.5
            self.assertAlmostEqual(tstop, 1.5, delta=0.02)
        anamorphic = lenses.get("anamorphic_50_2x")
        self.assertAlmostEqual(anamorphic["max_aperture"]
                               / anamorphic["transmission"] ** 0.5, 2.3,
                               delta=0.02)


if __name__ == "__main__":
    unittest.main()
