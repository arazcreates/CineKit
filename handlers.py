# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Application handlers and msgbus subscriptions.

frame_change_post does O(1) checks with early-out (see shots.on_frame_change)
— no heavy per-frame work. load_post clears any stale modal/overlay state,
migrates data from older CineKit versions and re-validates tagged data the
user may have deleted manually.
"""

import bpy
from bpy.app.handlers import persistent

from . import constants as K
from . import batch, focus, overlay, shots, utils

_msgbus_owner = object()
_warned_unlocked_render = False
# scene name -> camera object name at the last frame change
_last_camera = {}


def _unlocked_render(scene):
    """True while a render job runs and the interface is not locked.

    In that state a change to Blender data can collide with a redraw of the
    interface. Render > Lock Interface prevents that.
    """
    return (bpy.app.is_job_running('RENDER')
            and not scene.render.use_lock_interface)


def _follow_active_camera(scene):
    """Keep the exposure, the aperture and the lens blur on the active camera.

    A timeline marker can switch the camera during playback and renders,
    and keyframes can animate the physical settings. Property updates do
    not run for either, so the frame handler syncs here.
    """
    cam = scene.camera
    if cam is None or cam.type != 'CAMERA':
        return
    from . import rig
    if _last_camera.get(scene.name) != cam.name:
        _last_camera[scene.name] = cam.name
        rig.sync_active_camera(scene)
        return
    data = cam.data
    if data.cinekit.enabled and rig.settings_animated(data):
        rig.on_camera_settings_changed(data, scene)


@persistent
def _on_frame_change(scene, _depsgraph=None):
    global _warned_unlocked_render
    try:
        cam = scene.camera
        physical = (cam is not None and cam.type == 'CAMERA'
                    and cam.data.cinekit.enabled)
        if _unlocked_render(scene) and (scene.cinekit.shots or physical) \
                and not _warned_unlocked_render:
            _warned_unlocked_render = True
            print("CineKit: CineKit changes cameras, looks or camera "
                  "settings during this render. Turn on Render > Lock "
                  "Interface to prevent a crash.")
        _follow_active_camera(scene)
        if batch._active is None:
            # The batch render applies each shot itself. A shot switch here
            # could pick an overlapping shot in the middle of a render.
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
    panels and removal ops never error on partially missing CineKit data.

    The active look stays set when its compositor node is gone. Its camera
    and color layers are still on the scene, and Remove Look needs the
    look id to give them back. Apply Look builds the node again.
    """
    from .looks import engine
    for scene in bpy.data.scenes:
        if utils.is_linked(scene):
            continue
        prev = utils.fetch_state(scene, K.PREV_COMP)
        if prev is not None and engine._find_pipeline_node(scene) is None:
            utils.clear_state(scene, K.PREV_COMP)
    for obj in bpy.data.objects:
        if utils.is_linked(obj):
            continue
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


def _refresh_file_state():
    """Load the registries, migrate old data and check stored state."""
    from . import lenses, looks, migrate
    looks.reload_registry()
    lenses.reload_registry()
    try:
        from . import sensors
    except ImportError:      # CineKit 1.x has no sensor presets
        sensors = None
    if sensors is not None:
        sensors.reload_registry()
    try:
        migrate.migrate_file()
    except Exception as exc:  # never block the file from opening
        print(f"CineKit: could not migrate old data: {exc}")
    _validate_tags()


@persistent
def _on_load_post(_dummy=None):
    global _warned_unlocked_render
    _warned_unlocked_render = False
    _last_camera.clear()
    overlay.cleanup()
    batch.cancel_stale()
    focus.cancel_stale()
    shots.invalidate_cache()
    _refresh_file_state()
    _subscribe_msgbus()
    overlay.set_enabled(any(s.cinekit.overlay_enabled
                            for s in bpy.data.scenes))


def _deferred_refresh():
    """Run after the add-on is enabled in a file that is already open.

    register() cannot read bpy.data, so a timer runs this one time.
    """
    try:
        _refresh_file_state()
        overlay.set_enabled(any(s.cinekit.overlay_enabled
                                for s in bpy.data.scenes))
    except Exception as exc:
        print(f"CineKit: could not check the open file: {exc}")
    return None  # do not repeat


def register():
    if _on_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_on_frame_change)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    _subscribe_msgbus()
    if not bpy.app.timers.is_registered(_deferred_refresh):
        bpy.app.timers.register(_deferred_refresh, first_interval=0.0)


def unregister():
    if bpy.app.timers.is_registered(_deferred_refresh):
        bpy.app.timers.unregister(_deferred_refresh)
    if _on_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    bpy.msgbus.clear_by_owner(_msgbus_owner)
    overlay.cleanup()
