# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""CineKit — cinematography, lighting and camera-look toolkit.

Blender 4.2+ extension (see blender_manifest.toml). Targets Cycles and
EEVEE Next; engine-specific features degrade gracefully and say so in the
UI. See README.md for the feature/engine matrix.
"""

import bpy

from . import (batch, camera_ops, focus, handlers, lenses, lighting, looks,
               overlay, preferences, properties, shots, ui)
from .looks import ops as look_ops

_CLASS_MODULES = (
    preferences,
    camera_ops,
    shots,
    batch,
    focus,
    lighting,
    look_ops,
    ui,
)


def register():
    looks.reload_registry()
    lenses.reload_registry()
    properties.register()
    for module in _CLASS_MODULES:
        for cls in module.CLASSES:
            bpy.utils.register_class(cls)
    handlers.register()


def unregister():
    handlers.unregister()
    overlay.cleanup()
    for module in reversed(_CLASS_MODULES):
        for cls in reversed(module.CLASSES):
            bpy.utils.unregister_class(cls)
    properties.unregister()
