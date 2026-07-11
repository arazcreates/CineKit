# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Pure-Python tests for looks/schema.py, including validation of every
shipped look preset. Run with:
    python -m unittest discover tests    (from the extension root)
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from looks import schema  # noqa: E402


def minimal_look(**overrides):
    data = {"id": "test_look", "name": "Test Look"}
    data.update(overrides)
    return data


class TestValidation(unittest.TestCase):
    def test_minimal_valid(self):
        self.assertEqual(schema.validate_look(minimal_look()), [])

    def test_not_a_dict(self):
        self.assertTrue(schema.validate_look([1, 2]))

    def test_missing_id(self):
        errors = schema.validate_look({"name": "X"})
        self.assertTrue(any("'id'" in e for e in errors))

    def test_bad_id_characters(self):
        errors = schema.validate_look(minimal_look(id="bad id!"))
        self.assertTrue(any("may only contain" in e for e in errors))

    def test_unknown_artifact(self):
        errors = schema.validate_look(
            minimal_look(chain=[{"type": "sparkles"}]))
        self.assertTrue(any("unknown artifact" in e for e in errors))

    def test_duplicate_artifact(self):
        errors = schema.validate_look(minimal_look(
            chain=[{"type": "grain"}, {"type": "grain"}]))
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_setting_out_of_range(self):
        errors = schema.validate_look(minimal_look(
            chain=[{"type": "soft_res", "lines": 50000}]))
        self.assertTrue(any("outside" in e for e in errors))

    def test_unknown_setting(self):
        errors = schema.validate_look(minimal_look(
            chain=[{"type": "grain", "wat": 1}]))
        self.assertTrue(any("unknown setting" in e for e in errors))

    def test_bad_aspect(self):
        errors = schema.validate_look(
            minimal_look(camera={"aspect": "5:4"}))
        self.assertTrue(any("aspect" in e for e in errors))

    def test_unknown_camera_key(self):
        errors = schema.validate_look(
            minimal_look(camera={"focal": 50}))
        self.assertTrue(any("unknown key" in e for e in errors))

    def test_strength_range(self):
        errors = schema.validate_look(minimal_look(
            chain=[{"type": "grain", "strength": 9.0}]))
        self.assertTrue(any("strength" in e for e in errors))

    def test_boolean_is_not_a_number(self):
        errors = schema.validate_look(minimal_look(
            chain=[{"type": "grain", "amount": True}]))
        self.assertTrue(any("must be a number" in e for e in errors))


class TestNormalize(unittest.TestCase):
    def test_fills_defaults(self):
        look = schema.normalize_look(minimal_look(
            chain=[{"type": "grain"}]))
        entry = look["chain"][0]
        self.assertEqual(entry["strength"], 1.0)
        self.assertEqual(entry["size"],
                         schema.ARTIFACTS["grain"]["settings"]["size"][0])
        self.assertIsNone(look["camera"]["aspect"])
        self.assertEqual(look["color"]["exposure_bias"], 0.0)

    def test_raises_on_invalid(self):
        with self.assertRaises(schema.LookError):
            schema.normalize_look({"id": "x"})

    def test_keeps_explicit_values(self):
        look = schema.normalize_look(minimal_look(
            camera={"min_fstop": 8.0},
            chain=[{"type": "grain", "amount": 0.5, "strength": 0.7}]))
        self.assertEqual(look["camera"]["min_fstop"], 8.0)
        self.assertEqual(look["chain"][0]["amount"], 0.5)
        self.assertEqual(look["chain"][0]["strength"], 0.7)


class TestAspect(unittest.TestCase):
    def test_ratios(self):
        self.assertAlmostEqual(schema.aspect_ratio("4:3"), 4 / 3)
        self.assertAlmostEqual(schema.aspect_ratio("1.85:1"), 1.85)
        self.assertIsNone(schema.aspect_ratio(None))


class TestShippedPresets(unittest.TestCase):
    """Every JSON we ship must validate against the schema."""

    def _dir(self):
        return os.path.join(ROOT, "data", "looks")

    def test_six_looks_ship(self):
        files = [f for f in os.listdir(self._dir()) if f.endswith(".json")]
        self.assertEqual(len(files), 6)

    def test_all_presets_validate(self):
        for fname in os.listdir(self._dir()):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(self._dir(), fname),
                      encoding="utf-8") as fh:
                data = json.load(fh)
            errors = schema.validate_look(data)
            self.assertEqual(errors, [], f"{fname}: {errors}")
            look = schema.normalize_look(data)
            self.assertTrue(look["rationale"],
                            f"{fname} must document its rationale")

    def test_artifact_sockets_unique(self):
        sockets = [spec["socket"] for spec in schema.ARTIFACTS.values()]
        self.assertEqual(len(sockets), len(set(sockets)))


if __name__ == "__main__":
    unittest.main()
