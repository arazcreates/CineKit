# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Shot management: shot list operators, marker sync, frame-change handler.

The shot list is the source of truth; markers (with marker.camera) are
rebuilt from it so playback switches cameras natively. The frame-change
handler is O(1) with early-out: it only rescans when the playhead leaves
the cached current shot's range or the active camera changed.
"""

import bpy

from . import constants as K
from . import rig, utils
from .utils import CKError

# scene name -> {"count": int, "current": int, "cam": str}
_cache = {}


def invalidate_cache(scene=None):
    if scene is None:
        _cache.clear()
    else:
        _cache.pop(scene.name, None)


def find_shot_index(scene, frame):
    """Index of the shot containing `frame` (last match wins), or -1."""
    found = -1
    for i, shot in enumerate(scene.cinekit.shots):
        if shot.frame_start <= frame <= shot.frame_end:
            found = i
    return found


def active_shot(scene):
    """The shot under the playhead, using the frame-change cache."""
    state = _cache.get(scene.name)
    if state is not None and 0 <= state["current"] < len(scene.cinekit.shots):
        shot = scene.cinekit.shots[state["current"]]
        if shot.frame_start <= scene.frame_current <= shot.frame_end:
            return shot
    idx = find_shot_index(scene, scene.frame_current)
    return scene.cinekit.shots[idx] if idx >= 0 else None


def on_frame_change(scene):
    """Called from the persistent frame_change_post handler. Cheap checks
    first; look application only on shot-boundary crossing."""
    ck = scene.cinekit
    if not ck.shots or not ck.auto_switch_shot_look:
        return
    state = _cache.get(scene.name)
    frame = scene.frame_current
    cam_name = scene.camera.name if scene.camera else ""

    if state is not None and state["count"] == len(ck.shots):
        cur = state["current"]
        if 0 <= cur < len(ck.shots):
            shot = ck.shots[cur]
            if shot.frame_start <= frame <= shot.frame_end:
                if state["cam"] != cam_name:  # marker switched the camera
                    state["cam"] = cam_name
                    rig.sync_active_camera(scene)
                return  # O(1) early-out: still inside the current shot

    idx = find_shot_index(scene, frame)
    _cache[scene.name] = {"count": len(ck.shots), "current": idx,
                          "cam": cam_name}
    if idx < 0:
        return
    shot = ck.shots[idx]
    _apply_shot(scene, shot, set_frame=False)


def _apply_shot(scene, shot, set_frame=True):
    from .looks import engine
    if shot.camera is not None and scene.camera != shot.camera:
        scene.camera = shot.camera
    if set_frame:
        scene.frame_current = shot.frame_start
    look_id = shot.look
    if look_id and look_id != 'NONE':
        engine.apply_look(scene, look_id)  # in-place fast path if unchanged
    elif scene.cinekit.active_look:
        engine.remove_look(scene)
    rig.sync_active_camera(scene)


def _unique_shot_name(scene, base):
    names = {s.name for s in scene.cinekit.shots}
    if base not in names:
        return base
    for i in range(2, 1000):
        cand = f"{base}.{i:03d}"
        if cand not in names:
            return cand
    return base


class CK_OT_shot_add(bpy.types.Operator):
    bl_idname = "cinekit.shot_add"
    bl_label = "New Shot from Active Camera"
    bl_description = ("Add a shot using the active camera, starting at the "
                      "current frame")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.scene.camera is not None
                and context.scene.camera.type == 'CAMERA')

    def execute(self, context):
        scene = context.scene
        ck = scene.cinekit
        shot = ck.shots.add()
        shot.name = _unique_shot_name(scene,
                                      f"SH{(len(ck.shots)) * 10:03d}")
        shot.camera = scene.camera
        shot.frame_start = scene.frame_current
        shot.frame_end = scene.frame_current + 99
        cam_look = scene.camera.data.cinekit.look
        from . import looks
        if cam_look and looks.get(cam_look):
            shot.look = cam_look
        ck.shot_index = len(ck.shots) - 1
        invalidate_cache(scene)
        return {'FINISHED'}


class CK_OT_shot_remove(bpy.types.Operator):
    bl_idname = "cinekit.shot_remove"
    bl_label = "Remove Shot"
    bl_description = "Remove the selected shot from the list"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ck = context.scene.cinekit
        return 0 <= ck.shot_index < len(ck.shots)

    def execute(self, context):
        ck = context.scene.cinekit
        ck.shots.remove(ck.shot_index)
        ck.shot_index = min(ck.shot_index, len(ck.shots) - 1)
        invalidate_cache(context.scene)
        return {'FINISHED'}


class CK_OT_shot_duplicate(bpy.types.Operator):
    bl_idname = "cinekit.shot_duplicate"
    bl_label = "Duplicate Shot"
    bl_description = "Duplicate the selected shot"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ck = context.scene.cinekit
        return 0 <= ck.shot_index < len(ck.shots)

    def execute(self, context):
        scene = context.scene
        ck = scene.cinekit
        src = ck.shots[ck.shot_index]
        dst = ck.shots.add()
        for key in ("camera", "frame_start", "frame_end", "look", "notes",
                    "rack_from", "rack_to", "rack_frames", "rack_ease"):
            setattr(dst, key, getattr(src, key))
        dst.name = _unique_shot_name(scene, src.name)
        ck.shot_index = len(ck.shots) - 1
        invalidate_cache(scene)
        return {'FINISHED'}


class CK_OT_shot_jump(bpy.types.Operator):
    bl_idname = "cinekit.shot_jump"
    bl_label = "Jump to Shot"
    bl_description = ("Set the active camera, current frame and look from "
                      "the selected shot")
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=-1)

    @classmethod
    def poll(cls, context):
        return len(context.scene.cinekit.shots) > 0

    def execute(self, context):
        scene = context.scene
        ck = scene.cinekit
        idx = self.index if self.index >= 0 else ck.shot_index
        if not 0 <= idx < len(ck.shots):
            self.report({'ERROR'}, "No shot selected")
            return {'CANCELLED'}
        ck.shot_index = idx
        try:
            _apply_shot(scene, ck.shots[idx], set_frame=True)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        invalidate_cache(scene)
        return {'FINISHED'}


class CK_OT_shot_set_range(bpy.types.Operator):
    bl_idname = "cinekit.shot_set_range"
    bl_label = "Set Scene Range to Shot"
    bl_description = "Set the scene frame range to the selected shot's range"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ck = context.scene.cinekit
        return 0 <= ck.shot_index < len(ck.shots)

    def execute(self, context):
        scene = context.scene
        shot = scene.cinekit.shots[scene.cinekit.shot_index]
        if shot.frame_end < shot.frame_start:
            self.report({'ERROR'},
                        f"Shot '{shot.name}' has end before start")
            return {'CANCELLED'}
        scene.frame_start = shot.frame_start
        scene.frame_end = shot.frame_end
        return {'FINISHED'}


class CK_OT_shots_sync_markers(bpy.types.Operator):
    bl_idname = "cinekit.shots_sync_markers"
    bl_label = "Sync Markers"
    bl_description = ("Rebuild CineKit timeline markers (with bound cameras) "
                      "from the shot list. Only markers named "
                      f"'{K.MARKER_PREFIX}…' are touched — user markers are "
                      "never removed")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.cinekit.shots) > 0

    def execute(self, context):
        scene = context.scene
        for marker in [m for m in scene.timeline_markers
                       if m.name.startswith(K.MARKER_PREFIX)]:
            scene.timeline_markers.remove(marker)
        skipped = 0
        for shot in scene.cinekit.shots:
            if shot.camera is None:
                skipped += 1
                continue
            marker = scene.timeline_markers.new(
                K.MARKER_PREFIX + shot.name, frame=shot.frame_start)
            marker.camera = shot.camera
        if skipped:
            self.report({'WARNING'},
                        f"{skipped} shot(s) skipped — no camera assigned")
        return {'FINISHED'}


CLASSES = (
    CK_OT_shot_add,
    CK_OT_shot_remove,
    CK_OT_shot_duplicate,
    CK_OT_shot_jump,
    CK_OT_shot_set_range,
    CK_OT_shots_sync_markers,
)
