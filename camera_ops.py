# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Physical-camera operators: one-shot auto exposure (assist), exposure
release, rig creation/removal operators."""

import math

import bpy

from . import constants as K
from . import optics, rig, utils
from .utils import CKError


class CK_OT_auto_expose(bpy.types.Operator):
    bl_idname = "cinekit.auto_expose"
    bl_label = "Auto Expose (Assist)"
    bl_description = ("One-shot assist: sample viewport luminance through "
                      "the active camera (low-res offscreen render) and set "
                      "ISO so the average lands on middle grey. Sets values "
                      "once — does not animate")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.area is not None and context.area.type == 'VIEW_3D'
                and context.scene.camera is not None
                and context.scene.camera.type == 'CAMERA'
                and context.scene.camera.data.cinekit.enabled)

    def execute(self, context):
        import gpu
        scene = context.scene
        cam_obj = scene.camera
        ck = cam_obj.data.cinekit
        size = 96
        try:
            offscreen = gpu.types.GPUOffScreen(size, size)
            depsgraph = context.evaluated_depsgraph_get()
            proj = cam_obj.calc_matrix_camera(depsgraph, x=size, y=size)
            offscreen.draw_view3d(
                scene, context.view_layer, context.space_data,
                context.region, cam_obj.matrix_world.inverted(), proj,
                do_color_management=False)
            with offscreen.bind():
                fb = gpu.state.active_framebuffer_get()
                buf = fb.read_color(0, 0, size, size, 4, 0, 'FLOAT')
            offscreen.free()
        except Exception as exc:
            self.report({'ERROR'}, f"Offscreen sampling failed: {exc}")
            return {'CANCELLED'}

        buf.dimensions = size * size * 4
        log_sum, count = 0.0, 0
        for i in range(0, size * size * 4, 4):
            lum = (0.2126 * buf[i] + 0.7152 * buf[i + 1]
                   + 0.0722 * buf[i + 2])
            log_sum += math.log(max(lum, 1e-6))
            count += 1
        l_avg = math.exp(log_sum / count)
        if l_avg <= 1e-6:
            self.report({'ERROR'}, "Viewport is black — nothing to meter "
                                   "(add lights or a world first)")
            return {'CANCELLED'}

        from . import preferences
        target = preferences.get_prefs().target_grey
        needed_exposure = math.log2(target / l_avg)
        bias = float(scene.get("cinekit_look_bias", 0.0))
        fps = scene.render.fps / scene.render.fps_base
        time_s = optics.shutter_time_s(ck.shutter_mode, ck.shutter_speed,
                                       ck.shutter_angle, fps)
        # scene_exposure = CAL - EV + comp + bias  =>  solve EV, then ISO.
        ev_target = (optics.EXPOSURE_CALIBRATION + ck.exposure_comp + bias
                     - needed_exposure)
        iso = 100.0 * (ck.aperture ** 2 / time_s) / (2.0 ** ev_target)
        iso_clamped = int(min(max(iso, 25), 204800))
        ck.iso = iso_clamped  # update callback re-syncs scene exposure
        residual = math.log2(iso / iso_clamped) if iso > 0 else 0.0
        if abs(residual) > 0.02:
            ck.exposure_comp = min(max(ck.exposure_comp + residual, -5), 5)
            self.report({'WARNING'},
                        f"ISO clamped to {iso_clamped}; {residual:+.1f} EV "
                        "moved to exposure compensation")
        else:
            self.report({'INFO'},
                        f"Metered {l_avg:.4f} avg → ISO {iso_clamped}")
        return {'FINISHED'}


class CK_OT_release_exposure(bpy.types.Operator):
    bl_idname = "cinekit.release_exposure"
    bl_label = "Release Exposure Control"
    bl_description = ("Restore the scene exposure and white balance that "
                      "were in place before CineKit's physical camera took "
                      "over")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return utils.fetch_state(context.scene, K.PREV_EXPOSURE) is not None

    def execute(self, context):
        rig.release_exposure_control(context.scene)
        return {'FINISHED'}


# ------------------------------------------------------------------- rigs
class _RigCreateBase(bpy.types.Operator):
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        cam = context.scene.camera
        return (cam is not None and cam.type == 'CAMERA'
                and not utils.is_linked(cam)
                and rig.rig_root_of(cam) is None)

    def _create(self, context, fn, *args):
        try:
            root = fn(context, context.scene.camera, *args)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Rig '{root.name}' created — see Rig "
                              "Settings in the CineKit panel")
        return {'FINISHED'}


class CK_OT_rig_dolly(_RigCreateBase):
    bl_idname = "cinekit.rig_dolly"
    bl_label = "Add Dolly Rig"
    bl_description = ("Put the active camera on a dolly path with a "
                      "0-1 position property, optional banking and target "
                      "tracking")

    def execute(self, context):
        return self._create(context, rig.create_dolly)


class CK_OT_rig_crane(_RigCreateBase):
    bl_idname = "cinekit.rig_crane"
    bl_label = "Add Crane Rig"
    bl_description = ("Put the active camera on a crane/jib: pan, boom, arm "
                      "length and tilt properties")

    def execute(self, context):
        return self._create(context, rig.create_crane)


class CK_OT_rig_handheld(_RigCreateBase):
    bl_idname = "cinekit.rig_handheld"
    bl_label = "Add Handheld Rig"
    bl_description = ("Procedural handheld noise (F-modifiers, no handlers) "
                      "with a keyframeable intensity master")

    profile: bpy.props.EnumProperty(
        name="Profile",
        items=[(pid, spec["label"], f"Noise profile: {spec['label']}")
               for pid, spec in rig.HANDHELD_PROFILES.items()])

    def execute(self, context):
        return self._create(context, rig.create_handheld, self.profile)


class CK_OT_rig_orbit(_RigCreateBase):
    bl_idname = "cinekit.rig_orbit"
    bl_label = "Add Orbit Rig"
    bl_description = ("Turntable orbit around the camera's focus point with "
                      "radius/height/speed properties")

    def execute(self, context):
        return self._create(context, rig.create_orbit)


class CK_OT_rig_remove(bpy.types.Operator):
    bl_idname = "cinekit.rig_remove"
    bl_label = "Remove Rig"
    bl_description = ("Delete the rig and restore the camera's pre-rig "
                      "transform and parent")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        cam = context.scene.camera
        return cam is not None and rig.rig_root_of(cam) is not None

    def execute(self, context):
        root = rig.rig_root_of(context.scene.camera)
        try:
            rig.remove_rig(context, root)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class CK_OT_orbit_one_rev(bpy.types.Operator):
    bl_idname = "cinekit.orbit_one_revolution"
    bl_label = "One Revolution Over Frame Range"
    bl_description = ("Set orbit speed so the camera completes exactly one "
                      "revolution across the scene frame range")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        cam = context.scene.camera
        root = rig.rig_root_of(cam) if cam else None
        return root is not None and root.get(K.TAG_RIG) == K.RIG_ORBIT

    def execute(self, context):
        root = rig.rig_root_of(context.scene.camera)
        try:
            rig.orbit_one_revolution(context.scene, root)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (
    CK_OT_auto_expose,
    CK_OT_release_exposure,
    CK_OT_rig_dolly,
    CK_OT_rig_crane,
    CK_OT_rig_handheld,
    CK_OT_rig_orbit,
    CK_OT_rig_remove,
    CK_OT_orbit_one_rev,
)
