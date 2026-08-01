# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Focus tools: click-to-focus picker (modal raycast), rack focus,
focus targets. Engine-agnostic. Breathing lives in rig.set_breathing."""

import bpy
from bpy_extras import view3d_utils

from . import constants as K
from . import utils
from .utils import CKError

# Track the running picker so load_post can clear stale modal state.
_picker_running = False


def cancel_stale():
    global _picker_running
    _picker_running = False


def focus_targets(scene):
    return [o for o in scene.objects if o.get(K.TAG_FOCUS)]


def _target_items(_self, context):
    items = [(o.name, o.name, "Focus target") for o in
             focus_targets(context.scene)]
    return items or [("", "No focus targets", "")]


def _new_focus_target(context, location):
    scene = context.scene
    existing = focus_targets(scene)
    name = f"CK_Focus_{chr(ord('A') + len(existing))}" \
        if len(existing) < 26 else f"CK_Focus_{len(existing)}"
    obj = utils.new_empty(scene, name, display='SPHERE', size=0.12)
    obj[K.TAG_FOCUS] = utils.new_id("focus_")
    obj.location = location
    return obj


class CK_OT_focus_pick(bpy.types.Operator):
    bl_idname = "cinekit.focus_pick"
    bl_label = "Focus Picker"
    bl_description = ("Click a surface to place the focus target and point "
                      "the active camera's DOF at it. Ctrl-click adds a new "
                      "target instead of moving the last one. ESC cancels")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (not _picker_running
                and context.area is not None
                and context.area.type == 'VIEW_3D'
                and context.scene.camera is not None
                and context.scene.camera.type == 'CAMERA'
                and not utils.is_linked(context.scene.camera.data))

    def invoke(self, context, event):
        global _picker_running
        _picker_running = True
        context.window.cursor_modal_set('EYEDROPPER')
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set(
            "Focus Picker: click a surface (Ctrl-click: new target) — "
            "ESC/RMB cancels")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return self._exit(context, cancelled=True)
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            hit = self._raycast(context, event)
            if hit is None:
                self.report({'WARNING'}, "No surface is under the cursor.")
                return {'RUNNING_MODAL'}
            try:
                self._set_focus(context, hit, new=event.ctrl)
            except CKError as exc:
                self.report({'ERROR'}, str(exc))
                return self._exit(context, cancelled=True)
            return self._exit(context, cancelled=False)
        return {'PASS_THROUGH'} if event.type in {
            'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} \
            else {'RUNNING_MODAL'}

    def _raycast(self, context, event):
        region = context.region
        rv3d = context.region_data
        if rv3d is None:
            return None
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        result, location, _n, _i, _obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction)
        return location if result else None

    def _set_focus(self, context, location, new=False):
        scene = context.scene
        cam = scene.camera.data
        utils.require_editable(cam, "Camera")
        targets = focus_targets(scene)
        if new or not targets:
            target = _new_focus_target(context, location)
        else:
            target = targets[-1]
            target.location = location
        cam.dof.use_dof = True
        cam.dof.focus_object = target
        dist = (scene.camera.matrix_world.translation - location).length
        self.report({'INFO'},
                    f"Focus on '{target.name}' at {dist:.2f} m")
        if cam.cinekit.focus_breathing:
            from . import rig
            rig.set_breathing(cam)  # switch driver to object distance

    def _exit(self, context, cancelled):
        global _picker_running
        _picker_running = False
        context.window.cursor_modal_restore()
        context.area.header_text_set(None)
        return {'CANCELLED'} if cancelled else {'FINISHED'}


class CK_OT_focus_add_target(bpy.types.Operator):
    bl_idname = "cinekit.focus_add_target"
    bl_label = "Add Focus Target"
    bl_description = "Add a focus target empty at the 3D cursor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _new_focus_target(context, context.scene.cursor.location.copy())
        return {'FINISHED'}


class CK_OT_rack_focus(bpy.types.Operator):
    bl_idname = "cinekit.rack_focus"
    bl_label = "Rack Focus"
    bl_description = ("Keyframe DOF distance from target A to target B over "
                      "N frames, starting at the current frame. Stores the "
                      "rack on the active shot")
    bl_options = {'REGISTER', 'UNDO'}

    target_a: bpy.props.EnumProperty(name="From", items=_target_items)
    target_b: bpy.props.EnumProperty(name="To", items=_target_items)
    frames: bpy.props.IntProperty(name="Frames", default=24, min=1, max=1000)
    ease: bpy.props.EnumProperty(
        name="Ease",
        items=(('LINEAR', "Linear", "Constant-speed pull"),
               ('SMOOTH', "Smooth", "Ease in and out"),
               ('SNAP', "Snap", "Hold, then jump (whip focus)")),
        default='SMOOTH')

    @classmethod
    def poll(cls, context):
        return (context.scene.camera is not None
                and context.scene.camera.type == 'CAMERA'
                and len(focus_targets(context.scene)) >= 2
                and not utils.is_linked(context.scene.camera.data))

    def invoke(self, context, event):
        scene = context.scene
        ck = scene.cinekit
        if 0 <= ck.shot_index < len(ck.shots):
            shot = ck.shots[ck.shot_index]
            if shot.rack_from and shot.rack_to:
                targets = {o.name for o in focus_targets(scene)}
                if shot.rack_from in targets and shot.rack_to in targets:
                    self.target_a = shot.rack_from
                    self.target_b = shot.rack_to
                    self.frames = shot.rack_frames
                    self.ease = shot.rack_ease
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        cam_obj = scene.camera
        cam = cam_obj.data
        obj_a = scene.objects.get(self.target_a)
        obj_b = scene.objects.get(self.target_b)
        if obj_a is None or obj_b is None:
            self.report({'ERROR'}, "Pick two focus targets first. Use "
                                   "Focus Picker or Add Focus Target.")
            return {'CANCELLED'}
        if obj_a == obj_b:
            self.report({'ERROR'}, "From and To use the same target. "
                                   "Pick two different targets.")
            return {'CANCELLED'}

        cam_loc = cam_obj.matrix_world.translation
        dist_a = (cam_loc - obj_a.matrix_world.translation).length
        dist_b = (cam_loc - obj_b.matrix_world.translation).length
        cam.dof.use_dof = True
        cam.dof.focus_object = None  # rack drives the distance directly

        f0 = scene.frame_current
        f1 = f0 + self.frames
        cam.dof.focus_distance = dist_a
        cam.dof.keyframe_insert("focus_distance", frame=f0)
        cam.dof.focus_distance = dist_b
        cam.dof.keyframe_insert("focus_distance", frame=f1)

        action = cam.animation_data.action if cam.animation_data else None
        if action:
            for fcu in utils.action_fcurves(action):
                if fcu.data_path.endswith("focus_distance"):
                    for kp in fcu.keyframe_points:
                        if abs(kp.co.x - f0) < 0.5 or abs(kp.co.x - f1) < 0.5:
                            kp.interpolation = {
                                'LINEAR': 'LINEAR',
                                'SMOOTH': 'BEZIER',
                                'SNAP': 'CONSTANT'}[self.ease]
                            kp.easing = 'EASE_IN_OUT'
                    fcu.update()

        ck = scene.cinekit
        if 0 <= ck.shot_index < len(ck.shots):
            shot = ck.shots[ck.shot_index]
            shot.rack_from = self.target_a
            shot.rack_to = self.target_b
            shot.rack_frames = self.frames
            shot.rack_ease = self.ease
        self.report({'INFO'},
                    f"Rack {dist_a:.2f} m → {dist_b:.2f} m over "
                    f"{self.frames} frames")
        return {'FINISHED'}


class CK_OT_focus_clear_targets(bpy.types.Operator):
    bl_idname = "cinekit.focus_clear_targets"
    bl_label = "Remove Focus Targets"
    bl_description = ("Delete all CineKit focus target empties in the scene "
                      "(cameras keep their focus distance)")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(focus_targets(context.scene)) > 0

    def execute(self, context):
        targets = focus_targets(context.scene)
        for obj in bpy.data.objects:
            if obj.type == 'CAMERA' and obj.data.dof.focus_object in targets:
                dist = (obj.matrix_world.translation -
                        obj.data.dof.focus_object.matrix_world.translation
                        ).length
                obj.data.dof.focus_object = None
                obj.data.dof.focus_distance = dist
        for target in targets:
            bpy.data.objects.remove(target, do_unlink=True)
        return {'FINISHED'}


CLASSES = (
    CK_OT_focus_pick,
    CK_OT_focus_add_target,
    CK_OT_rack_focus,
    CK_OT_focus_clear_targets,
)
