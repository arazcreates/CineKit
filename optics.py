# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Photographic optics and exposure math.

Pure Python, no bpy — unit-testable outside Blender (tests/test_optics.py).
All distances in millimetres unless suffixed otherwise.
"""

import functools
import math

# Zeiss convention: circle of confusion = sensor diagonal / 1500.
COC_DIVISOR = 1500.0

# Blender's scene-referred middle grey (Filmic/AgX pivot).
MIDDLE_GREY = 0.18

# SI luminous efficacy. CineKit reads 1 W/m² of Blender irradiance as
# 683 lux. A sun lamp with a strength of 1.0 therefore gives 683 lux.
LUMINOUS_EFFICACY = 683.0

# Incident-light meter constant for a flat receptor (ISO 2720), in lux.
# At ISO 100: 2^EV100 = illuminance / 2.5.
INCIDENT_C = 250.0

# Calibration, measured in Blender 5.2 with Cycles and EEVEE:
#   A Lambert surface with albedo a under irradiance E renders a * E / pi.
#   The incident meter says  2^EV100 = 683 * E / 2.5.
#   Middle grey must render 0.18:  2^exposure * 0.18 * E / pi = 0.18.
#   So  exposure = log2(683 * pi / 2.5) - EV100.
# With this value, a grey card lit to any EV renders exactly 0.18 when the
# camera is set to that EV. Default camera settings (f/2.8, 1/50 s,
# ISO 100 = EV100 8.6) expose a scene lit by a strength-1 sun correctly.
EXPOSURE_CALIBRATION = math.log2(
    LUMINOUS_EFFICACY * math.pi / (INCIDENT_C / 100.0))  # ~9.745

# Diffraction uses the peak wavelength of photopic vision.
WAVELENGTH_MM = 550e-6
# Sigma of the Gaussian that fits the Airy intensity pattern, per lambda*N.
AIRY_SIGMA = 0.42
# A Gaussian MTF drops to 50% at MTF50 = MTF50_SIGMA / sigma.
MTF50_SIGMA_MM = math.sqrt(math.log(2.0) / (2.0 * math.pi ** 2))  # ~0.1874
# The Blender 5.2 Gaussian blur node uses radius = 3 * sigma (measured).
BLUR_RADIUS_PER_SIGMA = 3.0
# CineKit limits the lens blur radius, so a bad value cannot stall the
# compositor.
MAX_BLUR_RADIUS_PX = 100.0
# Keys of the blur radius curve, as radius 3 * sigma in pixels. They are
# closer together where the Blender kernel changes fast.
BLUR_CURVE_KEYS = (0.0, 0.03, 0.04, 0.05, 0.06, 0.08, 0.1, 0.125, 0.15,
                   0.175, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.75, 1.0, 1.25,
                   1.5, 1.75, 2.0, 4.0, 8.0, 16.0, 32.0)
# The lens blur driver gives the radius in hundredths of a pixel. Blender
# merges F-curve keys that are 0.01 or less apart on the x axis, and in
# pixels some keys are that close.
BLUR_DRIVER_SCALE = 100.0
# Blender keeps 255 characters of a driver expression.
DRIVER_EXPRESSION_LIMIT = 255


def srgb_to_linear(value):
    """Decode one sRGB display value (0-1) to scene-linear.

    Look presets give video levels in display terms (16/255, 235/255).
    The compositor works in scene-linear, so the engine converts them.
    """
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def ev100_for_illuminance(lux):
    """Incident-meter EV100 for an illuminance in lux."""
    if lux <= 0:
        raise ValueError("illuminance must be positive")
    return math.log2(lux * 100.0 / INCIDENT_C)


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


def t_stop(fstop, transmission=1.0):
    """T-stop of a lens: the f-stop corrected for the light the glass loses.

    T = N / sqrt(transmission). The exposure uses the T-stop. The depth of
    field uses the f-stop.
    """
    if fstop <= 0 or not 0.0 < transmission <= 1.0:
        raise ValueError("f-stop must be positive and transmission in (0, 1]")
    return fstop / math.sqrt(transmission)


def camera_ev100(fstop, time_s, iso, transmission=1.0, nd_stops=0.0):
    """EV100 of the scene that these camera settings expose correctly.

    It uses the T-stop, and an ND filter needs that many more stops of
    scene light.
    """
    return ev100(t_stop(fstop, transmission), time_s, iso) + nd_stops


def stops_closed(fstop, max_aperture):
    """Stops from wide open: s = 2 * log2(N / N_max), never below 0."""
    if fstop <= 0 or max_aperture <= 0:
        raise ValueError("f-stops must be positive")
    return max(0.0, 2.0 * math.log2(fstop / max_aperture))


def aperture_shape(fstop, max_aperture, blades, rotation_deg=0.0,
                   rotation_per_stop_deg=0.0, round_stops=0.0):
    """(blade count, rotation in degrees) of the aperture at an f-stop.

    The blade count is 0 (a round aperture) up to round_stops stops closed.
    The polygon turns by rotation_per_stop_deg for each stop closed.
    """
    stops = stops_closed(fstop, max_aperture)
    count = int(blades)
    if count < 3 or stops <= round_stops + 1e-6:
        count = 0
    return count, rotation_deg + rotation_per_stop_deg * stops


def magnification(focal_mm, focus_m):
    """Thin-lens magnification m = f / (u - f) at a focus distance."""
    f_m = focal_mm / 1000.0
    if focus_m <= f_m:
        raise ValueError("focus distance must be longer than the focal length")
    return f_m / (focus_m - f_m)


def diffraction_sigma_mm(fstop, mag=0.0):
    """Gaussian sigma of the Airy pattern on the sensor, in mm.

    The working f-number grows with magnification: N_eff = N * (1 + m).
    """
    return AIRY_SIGMA * WAVELENGTH_MM * fstop * (1.0 + mag)


def aberration_sigma_mm(fstop, max_aperture, softness_um):
    """Blur from lens aberrations, in mm on the sensor.

    softness_um is the sigma wide open. Transverse spherical aberration
    scales with the cube of the aperture diameter, so the blur falls with
    (N_max / N)^3 when you stop down.
    """
    if fstop <= 0 or max_aperture <= 0:
        raise ValueError("f-stops must be positive")
    ratio = min(max_aperture / fstop, 1.0)
    return softness_um / 1000.0 * ratio ** 3


def lens_blur_sigma_mm(fstop, max_aperture, softness_um, focal_mm, focus_m,
                       diffraction=True):
    """Total lens blur sigma in mm: diffraction and aberrations add in
    quadrature.

    The magnification stops at 1:1, the limit of a macro lens, so a focus
    distance at or inside the focal length gives a finite blur.
    """
    f_m = focal_mm / 1000.0
    mag = f_m / max(focus_m - f_m, f_m)
    sd = diffraction_sigma_mm(fstop, mag) if diffraction else 0.0
    sa = aberration_sigma_mm(max(fstop, 0.1), max_aperture, softness_um)
    return math.hypot(sd, sa)


def frame_width_mm(sensor_fit, sensor_width, sensor_height, aspect):
    """Width of the sensor area that the render frame shows.

    aspect is the frame width over the frame height, with the pixel aspect.
    Auto fit puts the sensor width on the longer side of the frame.
    """
    if sensor_fit == 'VERTICAL':
        return sensor_height * aspect
    if sensor_fit == 'HORIZONTAL':
        return sensor_width
    return sensor_width * min(aspect, 1.0)


def mtf50_to_sigma_um(mtf50_lp_mm):
    """Blur sigma in micrometers for a lens test result in line pairs per
    mm."""
    if mtf50_lp_mm <= 0:
        raise ValueError("MTF50 must be positive")
    return MTF50_SIGMA_MM / mtf50_lp_mm * 1000.0


def blur_radius_px(sigma_mm, sensor_width_mm, width_px):
    """Radius of the Blender Gaussian blur node for a sigma on the sensor."""
    if sensor_width_mm <= 0 or width_px <= 0:
        raise ValueError("sensor width and pixel width must be positive")
    return BLUR_RADIUS_PER_SIGMA * sigma_mm * width_px / sensor_width_mm


def blur_node_variance(radius):
    """Variance in square pixels of the Blender Gaussian blur node.

    The node samples exp(-9 k^2 / (2 r^2)) at whole pixels k, up to ceil(r)
    pixels from the center, and normalizes the samples. This matches
    measurements in Blender 5.2. Below a radius of about 2 pixels the
    samples miss most of the curve, so the blur is weaker than
    sigma = r / 3. At a radius of 0.5 or less the node does not blur.
    """
    if radius <= 0.0:
        return 0.0
    exponent = -9.0 / (2.0 * radius * radius)
    total, weighted = 1.0, 0.0
    for k in range(1, math.ceil(radius) + 1):
        weight = math.exp(exponent * k * k)
        total += 2.0 * weight
        weighted += 2.0 * k * k * weight
    return weighted / total


def blur_node_radius(sigma_px):
    """Blur node radius that blurs with the variance of sigma_px.

    CineKit matches the variance, the second moment of the blur. A blur
    below one pixel then softens fine detail by the correct amount.
    """
    if sigma_px <= 0.0:
        return 0.0
    target = sigma_px * sigma_px
    low, high = 0.0, BLUR_RADIUS_PER_SIGMA * sigma_px + 2.0
    while blur_node_variance(high) < target:
        high *= 2.0
    for _ in range(40):
        middle = 0.5 * (low + high)
        if blur_node_variance(middle) < target:
            low = middle
        else:
            high = middle
    return high


@functools.lru_cache(maxsize=1)
def blur_curve():
    """Keys (driver value, blur node radius in px) for the driver F-curve.

    The driver value is 3 * sigma in hundredths of a pixel
    (BLUR_DRIVER_SCALE). The curve is linear between the keys and continues
    the last step. From a sigma of 0.01 px up, the blur sigma is in 0.8 %
    of the target.
    """
    return tuple((x * BLUR_DRIVER_SCALE,
                  blur_node_radius(x / BLUR_RADIUS_PER_SIGMA))
                 for x in BLUR_CURVE_KEYS)


def blur_curve_radius(radius):
    """The blur node radius that the driver curve gives for 3 * sigma px."""
    value = radius * BLUR_DRIVER_SCALE
    keys = blur_curve()
    index = 1
    while index < len(keys) - 1 and value > keys[index][0]:
        index += 1
    (x0, y0), (x1, y1) = keys[index - 1], keys[index]
    return y0 + (y1 - y0) * (value - x0) / (x1 - x0)


def lens_blur_radii_px(sigma_mm, frame_mm, width_px, squeeze=1.0,
                       pixel_aspect=1.0):
    """(horizontal, vertical) radius 3 * sigma in pixels of a round blur on
    the sensor.

    frame_mm is the sensor width across the frame (frame_width_mm). With an
    anamorphic lens it holds the squeeze, and the desqueezed image makes
    the blur wider by the squeeze. pixel_aspect is pixel_aspect_x divided
    by pixel_aspect_y. blur_curve_radius gives the blur node radius.
    """
    radius = blur_radius_px(sigma_mm, frame_mm, width_px)
    return (min(radius * squeeze, MAX_BLUR_RADIUS_PX),
            min(radius * pixel_aspect, MAX_BLUR_RADIUS_PX))


# Driver variables of the lens blur: (name, what it reads).
LENS_BLUR_VARIABLES = (
    ("n", "f-stop of the depth of field"),
    ("a", "widest f-stop"),
    ("s", "softness wide open in micrometers"),
    ("d", "diffraction on (1) or off (0)"),
    ("f", "focal length in mm"),
    ("u", "focus distance in m"),
    ("w", "sensor width in mm"),
    ("h", "sensor height in mm"),
    ("t", "sensor fit: 0 Auto, 1 Horizontal, 2 Vertical"),
    ("q", "anamorphic squeeze"),
    ("x", "render width in pixels"),
    ("y", "render height in pixels"),
    ("p", "render percentage"),
    ("i", "pixel aspect x"),
    ("j", "pixel aspect y"),
)


def lens_blur_driver(horizontal):
    """Driver expression for one blur radius 3 * sigma, in hundredths of a
    render pixel.

    It is the same math as frame_width_mm, lens_blur_sigma_mm and
    lens_blur_radii_px, written as a simple expression: Blender runs it
    without Python and keeps only DRIVER_EXPRESSION_LIMIT characters. See
    LENS_BLUR_VARIABLES for the names. The keys of blur_curve on the driver
    F-curve turn the value into the blur node radius.
    """
    aspect = "x*i/(y*j)"
    # min(max(aspect, t), 1) is min(aspect, 1) for Auto and 1 for Horizontal.
    frame = f"((t<1.5)*w*min(max({aspect},t),1)+(t>1.5)*h*{aspect})"
    airy_um = f"{AIRY_SIGMA * WAVELENGTH_MM * 1000.0:.6f}"
    sigma_um = (f"sqrt(pow({airy_um}*d*n*(1+f/max(1000*u-f,f)),2)"
                f"+pow(s*pow(min(a/max(n,0.1),1),3),2))")
    # Radius per sigma, micrometers to mm, the render percentage and the
    # driver units.
    scale = f"{BLUR_RADIUS_PER_SIGMA * 1e-5 * BLUR_DRIVER_SCALE:.8f}"
    stretch = "q" if horizontal else "i/j"
    return (f"min({scale}*x*p*{stretch}*{sigma_um}/{frame},"
            f"{MAX_BLUR_RADIUS_PX * BLUR_DRIVER_SCALE:g})")


def unit_focus_focal(focal_mm, focus_m, amount=1.0):
    """Focal length that gives the field of view at a focus distance.

    A thin lens that focuses as a whole moves away from the sensor, and
    the image distance v = f * u / (u - f) sets the field of view. amount
    scales the effect: 1 is a lens that focuses as a whole, 0 is a fully
    compensated lens, and a negative value widens the view at close focus.
    """
    f_m = focal_mm / 1000.0
    u = max(focus_m, f_m * 1.05)
    return focal_mm * (1.0 + amount * f_m / (u - f_m))


def stylized_breathing_focal(focal_mm, focus_m, amount):
    """The breathing model of earlier CineKit versions."""
    return focal_mm * (1.0 + amount * 0.05 / max(focus_m, 0.25))


def rolling_shutter(exposure_s, readout_s, fps):
    """Cycles values for a rolling shutter.

    Returns (motion_blur_shutter in frames, rolling_shutter_duration). The
    first line starts at the shutter open time and the last line ends at
    the close time, so the full window is exposure + readout.
    """
    if exposure_s <= 0 or readout_s < 0 or fps <= 0:
        raise ValueError("exposure and fps must be positive, readout >= 0")
    window = exposure_s + readout_s
    return window * fps, exposure_s / window


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
