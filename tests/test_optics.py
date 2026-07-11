# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Pure-Python tests for optics.py — run with:
    python -m unittest discover tests    (from the extension root)
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import optics  # noqa: E402


class TestCoC(unittest.TestCase):
    def test_full_frame(self):
        # 36x24 sensor: diagonal 43.27mm / 1500 ≈ 0.0288mm
        self.assertAlmostEqual(optics.coc_mm(36.0, 24.0), 0.02884, places=4)

    def test_smaller_sensor_smaller_coc(self):
        self.assertLess(optics.coc_mm(5.76, 4.32), optics.coc_mm(36, 24))


class TestHyperfocal(unittest.TestCase):
    def test_textbook_value(self):
        # 50mm f/2 with c=0.03: H = 2500/0.06 + 50 = 41716.67mm
        h = optics.hyperfocal_mm(50.0, 2.0, 0.03)
        self.assertAlmostEqual(h, 41716.6667, places=2)

    def test_stopping_down_shortens_hyperfocal(self):
        h2 = optics.hyperfocal_mm(50.0, 2.0, 0.03)
        h16 = optics.hyperfocal_mm(50.0, 16.0, 0.03)
        self.assertLess(h16, h2)

    def test_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            optics.hyperfocal_mm(0.0, 2.0, 0.03)


class TestDofLimits(unittest.TestCase):
    def test_far_infinite_at_hyperfocal(self):
        h = optics.hyperfocal_mm(50.0, 8.0, 0.03)
        near, far = optics.dof_limits_mm(50.0, 8.0, h, 0.03)
        self.assertTrue(math.isinf(far))
        # Near limit at hyperfocal ≈ H/2.
        self.assertAlmostEqual(near, (h + 50.0) / 2.0, delta=h * 0.01)

    def test_near_before_focus_before_far(self):
        near, far = optics.dof_limits_mm(85.0, 1.4, 2000.0, 0.03)
        self.assertLess(near, 2000.0)
        self.assertGreater(far, 2000.0)

    def test_portrait_dof_is_thin(self):
        total = optics.dof_total_mm(85.0, 1.4, 2000.0, 0.03)
        self.assertLess(total, 120.0)  # ~7cm on full frame

    def test_focus_inside_focal_length_degenerate(self):
        near, far = optics.dof_limits_mm(50.0, 2.0, 40.0, 0.03)
        self.assertEqual((near, far), (40.0, 40.0))


class TestExposure(unittest.TestCase):
    def test_ev_zero_reference(self):
        # f/1, 1s, ISO100 => EV100 = 0
        self.assertAlmostEqual(optics.ev100(1.0, 1.0, 100), 0.0)

    def test_sunny_16(self):
        # f/16, 1/100s, ISO 100 ≈ EV 14.6
        ev = optics.ev100(16.0, 1.0 / 100.0, 100)
        self.assertAlmostEqual(ev, math.log2(256 * 100), places=5)

    def test_iso_compensates(self):
        base = optics.ev100(2.8, 1.0 / 50.0, 100)
        pushed = optics.ev100(2.8, 1.0 / 50.0, 400)
        self.assertAlmostEqual(base - pushed, 2.0)

    def test_scene_exposure_direction(self):
        # Brighter metering (higher EV) needs LESS scene exposure.
        self.assertLess(optics.scene_exposure_for_ev100(15.0),
                        optics.scene_exposure_for_ev100(10.0))

    def test_compensation_adds_stops(self):
        self.assertAlmostEqual(
            optics.scene_exposure_for_ev100(10.0, 1.5)
            - optics.scene_exposure_for_ev100(10.0), 1.5)


class TestShutter(unittest.TestCase):
    def test_speed_mode(self):
        self.assertAlmostEqual(
            optics.shutter_time_s('SPEED', 50, 180.0, 24.0), 1.0 / 50.0)

    def test_angle_180_at_24fps_is_1_48(self):
        self.assertAlmostEqual(
            optics.shutter_time_s('ANGLE', 50, 180.0, 24.0), 1.0 / 48.0)

    def test_motion_blur_shutter_film_convention(self):
        # 180° == 0.5 frames regardless of fps.
        for fps in (24.0, 30.0, 60.0):
            self.assertAlmostEqual(
                optics.motion_blur_shutter('ANGLE', 50, 180.0, fps), 0.5)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            optics.shutter_time_s('SPEED', 0, 180.0, 24.0)


class TestWhiteBalance(unittest.TestCase):
    def test_daylight_is_neutral(self):
        gains = optics.wb_gains(6500)
        for g in gains:
            self.assertAlmostEqual(g, 1.0, delta=0.02)

    def test_tungsten_scene_boosts_blue(self):
        # Neutralising a 3200K scene: blue up, red down.
        r, g, b = optics.wb_gains(3200)
        self.assertGreater(b, 1.0)
        self.assertLess(r, b)
        self.assertAlmostEqual(g, 1.0, delta=1e-6)  # green-normalised

    def test_shade_scene_boosts_red(self):
        r, _g, b = optics.wb_gains(9000)
        self.assertGreater(r, b)

    def test_tint_shifts_magenta(self):
        neutral = optics.wb_gains(6500, 0.0)
        magenta = optics.wb_gains(6500, 1.0)
        # Positive tint: green gain drops relative to R/B (all normalised
        # to green, so R and B rise).
        self.assertGreater(magenta[0], neutral[0])
        self.assertGreater(magenta[2], neutral[2])


if __name__ == "__main__":
    unittest.main()
