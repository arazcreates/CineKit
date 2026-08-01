# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Photographic optics and exposure math.

Pure Python, no bpy — unit-testable outside Blender (tests/test_optics.py).
All distances in millimetres unless suffixed otherwise.
"""

import math

# Zeiss convention: circle of confusion = sensor diagonal / 1500.
COC_DIVISOR = 1500.0

# Reflected-light meter constant (ISO 2720). Average scene luminance for a
# "correct" exposure at EV100 is L = K/100 * 2^EV100 with K = 12.5 cd/m².
METER_K = 12.5

# Blender's scene-referred middle grey (Filmic/AgX pivot).
MIDDLE_GREY = 0.18

# Scene exposure (in stops) that maps a metered scene to middle grey:
# 2^exposure * (K/100 * 2^EV) = 0.18  =>  exposure = log2(18/K) - EV.
EXPOSURE_CALIBRATION = math.log2(MIDDLE_GREY * 100.0 / METER_K)  # ~0.526


def coc_mm(sensor_width_mm, sensor_height_mm):
    """Circle of confusion for a sensor, Zeiss diagonal/1500 convention."""
    diag = math.hypot(sensor_width_mm, sensor_height_mm)
    return diag / COC_DIVISOR


def hyperfocal_mm(focal_mm, fstop, coc):
    """Hyperfocal distance. Focus here to make everything sharp from
    H/2 to infinity."""
    if focal_mm <= 0 or fstop <= 0 or coc <= 0:
        raise ValueError("focal, f-stop and CoC must be positive")
    return focal_mm * focal_mm / (fstop * coc) + focal_mm


def dof_limits_mm(focal_mm, fstop, focus_mm, coc):
    """(near, far) sharp limits. far is math.inf at/beyond hyperfocal.

    These are standard thin-lens DoF equations. Measure the focus
    distance from the lens.
    """
    if focus_mm <= focal_mm:
        # Focused inside the lens' own focal length: no real image.
        return (focus_mm, focus_mm)
    h = hyperfocal_mm(focal_mm, fstop, coc)
    near = h * focus_mm / (h + (focus_mm - focal_mm))
    if focus_mm >= h:
        return (near, math.inf)
    far = h * focus_mm / (h - (focus_mm - focal_mm))
    return (near, far)


def dof_total_mm(focal_mm, fstop, focus_mm, coc):
    """Total depth of field. Returns math.inf if the far limit is
    infinite."""
    near, far = dof_limits_mm(focal_mm, fstop, focus_mm, coc)
    return far - near


def shutter_time_s(mode, shutter_denom, shutter_angle_deg, fps):
    """Exposure time in seconds from either metering style.

    mode: 'SPEED' uses 1/shutter_denom, 'ANGLE' uses angle/360/fps.
    """
    if mode == 'ANGLE':
        if fps <= 0:
            raise ValueError("fps must be positive for shutter angle")
        return (shutter_angle_deg / 360.0) / fps
    if shutter_denom <= 0:
        raise ValueError("shutter denominator must be positive")
    return 1.0 / shutter_denom


def motion_blur_shutter(mode, shutter_denom, shutter_angle_deg, fps):
    """Blender's motion-blur shutter value (in frames): time * fps.

    180° == 0.5, matching film convention.
    """
    return shutter_time_s(mode, shutter_denom, shutter_angle_deg, fps) * fps


def ev100(fstop, time_s, iso):
    """Exposure value normalised to ISO 100: log2(N²/t) - log2(S/100)."""
    if fstop <= 0 or time_s <= 0 or iso <= 0:
        raise ValueError("f-stop, time and ISO must be positive")
    return math.log2(fstop * fstop / time_s) - math.log2(iso / 100.0)


def scene_exposure_for_ev100(ev, compensation=0.0):
    """Return the view_settings.exposure value for middle grey.

    The scene meters at `ev`.
    """
    return EXPOSURE_CALIBRATION - ev + compensation


def _planck_rgb(kelvin):
    """Approximate linear-RGB colour of a blackbody at `kelvin` (Tanner
    Helland fit, converted to linear, green-normalised). Used for WB gains."""
    t = min(max(kelvin, 1000.0), 40000.0) / 100.0
    if t <= 66.0:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * ((t - 60.0) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60.0) ** -0.0755148492)
    if t >= 66.0:
        b = 255.0
    elif t <= 19.0:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10.0) - 305.0447927307
    srgb = [min(max(c, 0.0), 255.0) / 255.0 for c in (r, g, b)]
    lin = [c ** 2.2 for c in srgb]
    g_ref = max(lin[1], 1e-4)
    return tuple(c / g_ref for c in lin)


def wb_gains(kelvin, tint=0.0):
    """Per-channel multipliers that neutralise a scene lit at `kelvin`.

    Photographic semantics: set WB temperature to the illuminant's
    temperature and whites come out white. Gains are the illuminant colour
    relative to D65, inverted, green-normalised. tint in [-1, 1]:
    positive shifts magenta (green channel down), negative shifts green.
    """
    ref = _planck_rgb(6500.0)
    ill = _planck_rgb(kelvin)
    gains = [ref[i] / max(ill[i], 1e-4) for i in range(3)]
    tint_scale = 1.0 - max(-0.99, min(0.99, tint)) * 0.25
    gains[1] *= tint_scale
    g = max(gains[1], 1e-4)
    return tuple(c / g for c in gains)
