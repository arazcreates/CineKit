# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Pure-Python tests for optics.py — run with:
    python -m unittest discover tests    (from the extension root)
"""

import itertools
import math
import os
import re
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

    def test_grey_card_renders_middle_grey(self):
        # Blender renders a Lambert card with albedo a under irradiance E
        # (W/m²) as a * E / pi. At the metered EV the card must show 0.18.
        for irradiance in (0.5, 1.0, 3.0, 40.0):
            lux = irradiance * optics.LUMINOUS_EFFICACY
            ev = optics.ev100_for_illuminance(lux)
            exposure = optics.scene_exposure_for_ev100(ev)
            pixel = optics.MIDDLE_GREY * irradiance / math.pi
            self.assertAlmostEqual(pixel * 2.0 ** exposure,
                                   optics.MIDDLE_GREY, places=9)

    def test_default_camera_exposes_strength_one_sun(self):
        # f/2.8, 1/50 s, ISO 100 under a strength-1 sun: within 1.2 stops
        # of the ideal exposure.
        ev_camera = optics.ev100(2.8, 1.0 / 50.0, 100)
        ev_scene = optics.ev100_for_illuminance(optics.LUMINOUS_EFFICACY)
        self.assertLess(abs(ev_camera - ev_scene), 1.2)

    def test_illuminance_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            optics.ev100_for_illuminance(0.0)


class TestLensExposure(unittest.TestCase):
    def test_t_stop(self):
        self.assertAlmostEqual(optics.t_stop(2.0, 1.0), 2.0)
        self.assertAlmostEqual(optics.t_stop(1.4, 0.87), 1.4 / math.sqrt(0.87))

    def test_transmission_costs_light(self):
        base = optics.camera_ev100(2.8, 1 / 50, 100)
        dim = optics.camera_ev100(2.8, 1 / 50, 100, transmission=0.81)
        self.assertAlmostEqual(dim - base, -math.log2(0.81), places=9)

    def test_nd_adds_stops(self):
        base = optics.camera_ev100(2.8, 1 / 50, 100)
        self.assertAlmostEqual(
            optics.camera_ev100(2.8, 1 / 50, 100, nd_stops=3.0) - base, 3.0)

    def test_rejects_bad_transmission(self):
        with self.assertRaises(ValueError):
            optics.t_stop(2.0, 0.0)


class TestAperture(unittest.TestCase):
    def test_stops_closed(self):
        self.assertAlmostEqual(optics.stops_closed(2.8, 1.4), 2.0)
        self.assertEqual(optics.stops_closed(1.0, 1.4), 0.0)

    def test_round_wide_open(self):
        blades, _ = optics.aperture_shape(1.4, 1.4, 9, round_stops=1.0)
        self.assertEqual(blades, 0)
        blades, _ = optics.aperture_shape(2.8, 1.4, 9, round_stops=1.0)
        self.assertEqual(blades, 9)

    def test_rotation_per_stop(self):
        _, rotation = optics.aperture_shape(5.6, 1.4, 8, rotation_deg=10.0,
                                            rotation_per_stop_deg=4.0)
        self.assertAlmostEqual(rotation, 10.0 + 4.0 * 4.0)

    def test_fewer_than_three_blades_is_round(self):
        self.assertEqual(optics.aperture_shape(8.0, 1.4, 2)[0], 0)


class TestLensSoftness(unittest.TestCase):
    def test_airy_sigma(self):
        # sigma = 0.42 * 550 nm * N
        self.assertAlmostEqual(optics.diffraction_sigma_mm(8.0),
                               0.42 * 550e-6 * 8.0)

    def test_magnification_widens_diffraction(self):
        m = optics.magnification(100.0, 0.3)      # f / (u - f) = 0.5
        self.assertAlmostEqual(m, 0.5)
        self.assertAlmostEqual(optics.diffraction_sigma_mm(8.0, m),
                               1.5 * optics.diffraction_sigma_mm(8.0))

    def test_aberration_falls_with_cube(self):
        wide = optics.aberration_sigma_mm(1.4, 1.4, 8.0)
        two_stops = optics.aberration_sigma_mm(2.8, 1.4, 8.0)
        self.assertAlmostEqual(wide, 0.008)
        self.assertAlmostEqual(two_stops, 0.008 / 8.0)

    def test_sweet_spot(self):
        # Wide open aberrations dominate, stopped far down diffraction does.
        blur = [optics.lens_blur_sigma_mm(n, 1.4, 8.0, 50.0, 1000.0)
                for n in (1.4, 2.8, 5.6, 11.0, 22.0)]
        best = blur.index(min(blur))
        self.assertIn(best, (1, 2, 3))
        self.assertGreater(blur[0], blur[best])
        self.assertGreater(blur[4], blur[best])

    def test_mtf50_sigma(self):
        self.assertAlmostEqual(optics.mtf50_to_sigma_um(40.0), 187.39 / 40.0,
                               places=2)

    def test_gaussian_mtf50_consistency(self):
        sigma_mm = optics.mtf50_to_sigma_um(30.0) / 1000.0
        mtf = math.exp(-2.0 * math.pi ** 2 * sigma_mm ** 2 * 30.0 ** 2)
        self.assertAlmostEqual(mtf, 0.5, places=6)

    def test_blur_radius_px(self):
        # 4.5 um pixels: sigma 4.5 um is one pixel, radius 3 px.
        self.assertAlmostEqual(optics.blur_radius_px(0.0045, 17.28, 3840),
                               3.0, places=9)

    def test_close_focus_stops_at_one_to_one(self):
        # At or inside the focal length the magnification stays 1:1.
        near = optics.lens_blur_sigma_mm(8.0, 1.4, 0.0, 50.0, 0.01)
        self.assertAlmostEqual(near, optics.diffraction_sigma_mm(8.0, 1.0))
        self.assertAlmostEqual(optics.lens_blur_sigma_mm(8.0, 1.4, 0.0, 50.0,
                                                         0.1), near)

    def test_frame_width_by_sensor_fit(self):
        self.assertEqual(optics.frame_width_mm('AUTO', 36.0, 24.0, 16 / 9),
                         36.0)
        self.assertAlmostEqual(optics.frame_width_mm('AUTO', 36.0, 24.0, 0.5),
                               18.0)
        self.assertEqual(
            optics.frame_width_mm('HORIZONTAL', 36.0, 24.0, 0.5), 36.0)
        self.assertAlmostEqual(
            optics.frame_width_mm('VERTICAL', 36.0, 24.0, 2.0), 48.0)

    def test_radii_follow_squeeze_and_pixel_aspect(self):
        wide, tall = optics.lens_blur_radii_px(0.0045, 17.28, 3840,
                                               squeeze=2.0)
        self.assertAlmostEqual(wide, 6.0)
        self.assertAlmostEqual(tall, 3.0)
        wide, tall = optics.lens_blur_radii_px(0.0045, 17.28, 3840,
                                               pixel_aspect=2.0)
        self.assertAlmostEqual(wide, 3.0)
        self.assertAlmostEqual(tall, 6.0)
        self.assertEqual(optics.lens_blur_radii_px(1.0, 1.0, 10000),
                         (optics.MAX_BLUR_RADIUS_PX,) * 2)


class TestBlurNode(unittest.TestCase):
    # Radius: variance of one blurred pixel, rendered in Blender 5.2 with
    # the Gaussian type.
    MEASURED = {0.3: 0.0, 0.5: 0.0, 0.75: 0.0007, 1.0: 0.0217, 1.25: 0.1010,
                1.5: 0.2150, 2.0: 0.4416, 3.0: 0.9959, 4.5: 2.2435,
                6.0: 3.9513, 9.0: 8.8536}

    def test_variance_matches_blender(self):
        for radius, variance in self.MEASURED.items():
            self.assertAlmostEqual(optics.blur_node_variance(radius), variance,
                                   places=4, msg=radius)

    def test_node_radius_gives_the_variance(self):
        for sigma in (0.05, 0.2, 0.5, 1.0, 3.0, 20.0):
            radius = optics.blur_node_radius(sigma)
            self.assertAlmostEqual(optics.blur_node_variance(radius),
                                   sigma * sigma, places=6)
        self.assertEqual(optics.blur_node_radius(0.0), 0.0)

    def test_small_sigma_needs_a_larger_radius(self):
        self.assertGreater(optics.blur_node_radius(0.1), 3.0 * 0.1)
        self.assertAlmostEqual(optics.blur_node_radius(2.0), 6.0, delta=0.05)

    def test_curve_accuracy(self):
        # Linear steps between the keys, and past the last key up to the
        # radius limit.
        steps = 2000
        for step in range(steps + 1):
            radius = 0.03 * (optics.MAX_BLUR_RADIUS_PX / 0.03) ** (step / steps)
            node = optics.blur_curve_radius(radius)
            sigma = math.sqrt(optics.blur_node_variance(node))
            self.assertAlmostEqual(sigma / (radius / 3.0), 1.0, delta=0.008,
                                   msg=f"radius {radius:.4f}")

    def test_curve_starts_at_zero(self):
        self.assertEqual(optics.blur_curve()[0], (0.0, 0.0))
        self.assertEqual(len(optics.blur_curve()), len(optics.BLUR_CURVE_KEYS))

    def test_curve_keys_survive_blender(self):
        # FCurve.update() drops a key 0.01 or less after the previous key.
        keys = optics.blur_curve()
        for (x0, _y0), (x1, _y1) in zip(keys, keys[1:]):
            self.assertGreater(x1 - x0, 0.5)


class TestLensBlurDriver(unittest.TestCase):
    FITS = ('AUTO', 'HORIZONTAL', 'VERTICAL')    # driver values 0, 1, 2

    def test_fits_in_a_simple_expression(self):
        allowed = {"sqrt", "pow", "min", "max"}
        allowed |= {name for name, _ in optics.LENS_BLUR_VARIABLES}
        for horizontal in (True, False):
            expression = optics.lens_blur_driver(horizontal)
            self.assertLessEqual(len(expression),
                                 optics.DRIVER_EXPRESSION_LIMIT)
            self.assertNotIn("**", expression)
            self.assertLessEqual(set(re.findall(r"[A-Za-z_]\w*", expression)),
                                 allowed)

    def test_matches_the_python_math(self):
        namespace = {"__builtins__": {}, "sqrt": math.sqrt, "pow": math.pow,
                     "min": min, "max": max}
        drivers = [compile(optics.lens_blur_driver(horizontal), "driver",
                           "eval") for horizontal in (True, False)]
        renders = ((1920, 1080, 100, 1.0, 1.0), (1080, 1920, 50, 1.0, 1.0),
                   (1440, 1080, 100, 4.0, 3.0))
        cases = itertools.product((0.95, 1.4, 5.6, 22.0), (0.0, 12.0), (0, 1),
                                  (0.04, 0.3, 5.0), (0, 1, 2), renders,
                                  (1.0, 2.0))
        for n, s, d, u, t, (x, y, p, i, j), q in cases:
            values = {"n": n, "a": 1.4, "s": s, "d": d, "f": 50.0, "u": u,
                      "w": 36.0 * q, "h": 24.0, "t": t, "q": q, "x": x,
                      "y": y, "p": p, "i": i, "j": j}
            got = [eval(code, namespace, values) / optics.BLUR_DRIVER_SCALE
                   for code in drivers]
            sigma = optics.lens_blur_sigma_mm(n, 1.4, s, 50.0, u, bool(d))
            frame = optics.frame_width_mm(self.FITS[t], 36.0 * q, 24.0,
                                          x * i / (y * j))
            want = optics.lens_blur_radii_px(sigma, frame, x * p / 100.0, q,
                                             i / j)
            for got_value, want_value in zip(got, want):
                self.assertAlmostEqual(got_value, want_value, places=9,
                                       msg=str(values))


class TestBreathing(unittest.TestCase):
    def test_unit_focus_thin_lens(self):
        # A 50 mm lens at 0.3 m: v = f*u/(u-f) = 60 mm.
        self.assertAlmostEqual(optics.unit_focus_focal(50.0, 0.3), 60.0)

    def test_infinity_has_no_breathing(self):
        self.assertAlmostEqual(optics.unit_focus_focal(50.0, 1e9), 50.0)

    def test_amount_scales_and_can_widen(self):
        self.assertAlmostEqual(optics.unit_focus_focal(50.0, 0.3, 0.5), 55.0)
        self.assertLess(optics.unit_focus_focal(50.0, 0.3, -0.5), 50.0)

    def test_stylized_model_unchanged(self):
        self.assertAlmostEqual(optics.stylized_breathing_focal(50.0, 1.0, 0.1),
                               50.0 * (1.0 + 0.1 * 0.05))


class TestRollingShutter(unittest.TestCase):
    def test_cycles_values(self):
        shutter, duration = optics.rolling_shutter(0.02, 0.02, 24.0)
        self.assertAlmostEqual(shutter, 0.96)
        self.assertAlmostEqual(duration, 0.5)

    def test_global_shutter(self):
        shutter, duration = optics.rolling_shutter(1 / 48, 0.0, 24.0)
        self.assertAlmostEqual(shutter, 0.5)
        self.assertAlmostEqual(duration, 1.0)


class TestTransfer(unittest.TestCase):
    def test_endpoints(self):
        self.assertAlmostEqual(optics.srgb_to_linear(0.0), 0.0)
        self.assertAlmostEqual(optics.srgb_to_linear(1.0), 1.0)

    def test_video_levels(self):
        # 16/255 and 235/255 in display terms.
        self.assertAlmostEqual(optics.srgb_to_linear(16 / 255), 0.00518,
                               places=4)
        self.assertAlmostEqual(optics.srgb_to_linear(235 / 255), 0.83077,
                               places=4)

    def test_monotonic(self):
        values = [optics.srgb_to_linear(i / 100) for i in range(101)]
        self.assertEqual(values, sorted(values))


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
