# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Application handlers and msgbus subscriptions.

frame_change_post does O(1) checks with early-out (see shots.on_frame_change)
— no heavy per-frame work. load_post clears any stale modal/overlay state
and re-validates tagged data the user may have deleted manually.
"""

import bpy
from bpy.app.handlers import persistent

from . import constants as K
from . import batch, focus, overlay, shots, utils

_msgbus_owner = object()


@persistent
def _on_frame_change(scene, _depsgraph=None):
    try:
        shots.on_frame_change(scene)
    except Exception as exc:  # a handler must never break playback
        print(f"CineKit: frame handler error: {exc}")


def _on_camera_switch():
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return
    from . import rig
    try:
        rig.sync_active_camera(scene)
    except Exception as exc:
        print(f"CineKit: camera-switch sync error: {exc}")


def _subscribe_msgbus():
    bpy.msgbus.clear_by_owner(_msgbus_owner)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Scene, "camera"),
        owner=_msgbus_owner, args=(), notify=_on_camera_switch)


def _validate_tags():
    """Drop stored prior-state that points at data the user deleted, so
    panels and removal ops never error on partially missing CineKit data."""
    from .looks import engine
    for scene in bpy.data.scenes:
        prev = utils.fetch_state(scene, K.PREV_COMP)
        if prev is not None:
            if engine._find_pipeline_node(scene) is None:
                utils.clear_state(scene, K.PREV_COMP)
                if scene.cinekit.active_look:
                    scene.cinekit.active_look = ""
                    scene.cinekit.look_params.clear()
    for obj in bpy.data.objects:
        prev = utils.fetch_state(obj, K.PREV_CAMERA_RIG)
        if prev is None:
            continue
        rig_id = prev.get("rig")
        root_exists = any(o.get("cinekit_rig_uid") == rig_id
                          for o in bpy.data.objects)
        if not root_exists:
            utils.clear_state(obj, K.PREV_CAMERA_RIG)
            if "cinekit_rig_member_of" in obj.keys():
                del obj["cinekit_rig_member_of"]


@persistent
def _on_load_post(_dummy=None):
    overlay.cleanup()
    batch.cancel_stale()
    focus.cancel_stale()
    shots.invalidate_cache()
    from . import lenses, looks
    looks.reload_registry()
    lenses.reload_registry()
    _validate_tags()
    _subscribe_msgbus()
    if any(s.cinekit.overlay_enabled for s in bpy.data.scenes):
        overlay.set_enabled(True)


def register():
    if _on_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_on_frame_change)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    _subscribe_msgbus()


def unregister():
    if _on_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    bpy.msgbus.clear_by_owner(_msgbus_owner)
    overlay.cleanup()
