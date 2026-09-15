# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Migrate data that older CineKit versions saved in a .blend file.

Older CineKit versions stored enum selections (shot look, lens preset,
sensor preset) as list positions. The current version stores stable
numbers. This module converts the old positions one time. A file that
already uses stable numbers passes through without a change.

The same versions added the exposure bias of a look straight to the scene
exposure when the camera was not physical, and stored no record of it.
This module stores that record, so Remove Look takes the bias away one
time only.
"""

import bpy

from . import constants as K
from . import lenses, looks, stable_enum, utils

try:
    from . import sensors
except ImportError:      # CineKit 1.x has no sensor presets
    sensors = None


def _convert(owner, prop, legacy_order):
    ident = stable_enum.legacy_identifier(owner.get(prop), legacy_order)
    if ident is None:
        return 0
    try:
        setattr(owner, prop, ident)
    except TypeError:     # the preset no longer exists on this machine
        return 0
    return 1


def _record_legacy_bias(scene):
    bias = float(scene.get(K.LOOK_BIAS, 0.0))
    if abs(bias) < 1e-9 or utils.fetch_state(scene, K.PREV_EXPOSURE):
        return 0
    utils.store_state(scene, K.PREV_EXPOSURE, {
        "exposure": scene.view_settings.exposure - bias,
        "motion_blur_shutter": scene.render.motion_blur_shutter,
        "shutter_driven": False, "mode": "offset", "bias": bias})
    return 1


def migrate_file():
    """Convert old data in the open file. Return the count."""
    look_order = looks.legacy_ids()
    lens_order = lenses.legacy_ids()
    sensor_order = sensors.legacy_ids() if sensors else None
    converted = 0
    with utils.suppress_updates():     # a preset change must not re-apply
        for scene in bpy.data.scenes:
            if utils.is_linked(scene):
                continue
            for shot in scene.cinekit.shots:
                converted += _convert(shot, "look", look_order)
            converted += _record_legacy_bias(scene)
        for cam in bpy.data.cameras:
            if utils.is_linked(cam):
                continue
            converted += _convert(cam.cinekit, "lens_preset", lens_order)
            if sensor_order is not None:
                converted += _convert(cam.cinekit, "sensor_preset",
                                      sensor_order)
    if converted:
        print(f"CineKit: converted {converted} item(s) saved by an older "
              "CineKit version to the current format.")
    return converted
