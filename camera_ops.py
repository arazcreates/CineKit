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
            self.report({'ERROR'}, "The viewport is black. Add a light "
                                   "or a world, then meter again.")
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
                        f"ISO limit reached at {iso_clamped}. CineKit moved "
                        f"{residual:+.1f} EV to exposure compensation.")
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

    def invoke(self, context, event):
        # Advanced Mode: present a customization dialog before building the
        # rig (its properties also populate the bottom-left "Adjust Last
        # Operation" redo panel). Otherwise build in one shot as before.
        if context.scene.cinekit.rig_advanced_mode:
            return context.window_manager.invoke_props_dialog(self, width=340)
        return self.execute(context)

    def _opts(self, context):
        return {}   # subclasses map their properties to rig.create_* opts

    def _create(self, context, fn, *args):
        try:
            root = fn(context, context.scene.camera, *args,
                      opts=self._opts(context))
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Rig '{root.name}' is ready. Open Rig "
                              "Settings in the CineKit panel.")
        return {'FINISHED'}


class CK_OT_rig_dolly(_RigCreateBase):
    bl_idname = "cinekit.rig_dolly"
    bl_label = "Add Dolly Rig"
    bl_description = ("Put the active camera on a dolly path with a "
                      "0-1 position property, optional banking and target "
                      "tracking")

    path_length: bpy.props.FloatProperty(
        name="Path Length", description="Length of the straight dolly track",
        default=6.0, min=0.5, max=200.0, subtype='DISTANCE')
    carrier_pos: bpy.props.FloatProperty(
        name="Start Position", description="Where the camera starts along "
        "the path (0-1)", default=0.5, min=0.0, max=1.0)
    aim_distance: bpy.props.FloatProperty(
        name="Aim Distance", description="How far ahead the aim target sits",
        default=5.0, min=0.1, max=200.0, subtype='DISTANCE')
    track: bpy.props.FloatProperty(
        name="Track Aim", description="Lock the camera onto the aim target "
        "(0 = free, 1 = locked)", default=0.0, min=0.0, max=1.0)
    banking: bpy.props.BoolProperty(
        name="Banking", description="Bank the camera into path curves",
        default=False)
    path_source: bpy.props.EnumProperty(
        name="Path From",
        items=(('GENERATED', "Straight track (generated)",
                "Auto-build a straight dolly track"),
               ('CURVE', "Selected curve",
                "Use the shape of another selected curve object"),
               ('GREASE_PENCIL', "Grease pencil",
                "Use a stroke you drew with a selected Grease Pencil "
                "object")),
        default='GENERATED')

    def _find_source(self, context):
        if self.path_source == 'GREASE_PENCIL':
            obj = context.active_object
            if obj is not None and obj.type == 'GREASEPENCIL':
                return obj
            return next((o for o in context.selected_objects
                         if o.type == 'GREASEPENCIL'), None)
        return next((o for o in context.selected_objects
                     if o.type == 'CURVE' and not o.get(K.TAG_RIG_MEMBER)),
                    None)

    def _opts(self, context):
        opts = {"path_length": self.path_length,
                "carrier_pos": self.carrier_pos,
                "aim_distance": self.aim_distance, "track": self.track,
                "banking": self.banking, "path_source": self.path_source}
        if self.path_source != 'GENERATED':
            opts["path_object"] = self._find_source(context)
        return opts

    def execute(self, context):
        if self.path_source != 'GENERATED' and \
                self._find_source(context) is None:
            kind = ("Grease Pencil object"
                    if self.path_source == 'GREASE_PENCIL' else "curve")
            self.report({'ERROR'}, f"Path From uses '{self.path_source}'. "
                        f"Select a {kind} first. The scene camera "
                        "stays the render camera.")
            return {'CANCELLED'}
        return self._create(context, rig.create_dolly)


class CK_OT_rig_crane(_RigCreateBase):
    bl_idname = "cinekit.rig_crane"
    bl_label = "Add Crane Rig"
    bl_description = ("Put the active camera on a crane/jib: pan, boom, arm "
                      "length and tilt properties")

    base_drop: bpy.props.FloatProperty(
        name="Base Drop", description="How far below the camera the crane "
        "base sits", default=1.5, min=0.0, max=20.0, subtype='DISTANCE')
    boom: bpy.props.FloatProperty(
        name="Boom Angle", default=45.0, min=-80.0, max=80.0)
    tilt: bpy.props.FloatProperty(
        name="Head Tilt", default=0.0, min=-90.0, max=90.0)

    def _opts(self, context):
        return {"base_drop": self.base_drop, "boom": self.boom,
                "tilt": self.tilt}

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
    intensity: bpy.props.FloatProperty(
        name="Intensity", description="Starting shake intensity",
        default=1.0, min=0.0, max=2.0)

    def _opts(self, context):
        return {"intensity": self.intensity}

    def execute(self, context):
        return self._create(context, rig.create_handheld, self.profile)


class CK_OT_rig_orbit(_RigCreateBase):
    bl_idname = "cinekit.rig_orbit"
    bl_label = "Add Orbit Rig"
    bl_description = ("Turntable orbit around the camera's focus point with "
                      "radius/height/speed properties")

    speed: bpy.props.FloatProperty(
        name="Speed °/s", default=30.0, min=-720.0, max=720.0)

    def _opts(self, context):
        return {"speed": self.speed}

    def execute(self, context):
        return self._create(context, rig.create_orbit)


class CK_OT_rig_select_point(bpy.types.Operator):
    bl_idname = "cinekit.rig_select_point"
    bl_label = "Select Rig Point"
    bl_description = ("Select and activate this rig point so you can move it "
                      "(press G) directly in the viewport")
    bl_options = {'REGISTER', 'UNDO'}

    object_name: bpy.props.StringProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None:
            self.report({'ERROR'}, "The rig point no longer exists. Remove "
                                   "the rig, then add it again.")
            return {'CANCELLED'}
        for other in list(context.selected_objects):
            other.select_set(False)
        obj.hide_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {'FINISHED'}


class _DollyActive:
    @classmethod
    def poll(cls, context):
        cam = context.scene.camera
        root = rig.rig_root_of(cam) if cam else None
        return root is not None and root.get(K.TAG_RIG) == K.RIG_DOLLY


class CK_OT_rig_path_from_gp(_DollyActive, bpy.types.Operator):
    bl_idname = "cinekit.rig_path_from_gp"
    bl_label = "Dolly Path from Grease Pencil"
    bl_description = ("Rebuild the active dolly's path from a stroke you "
                      "drew — select the Grease Pencil object first")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        gp = context.active_object
        if gp is None or gp.type != 'GREASEPENCIL':
            gp = next((o for o in context.selected_objects
                       if o.type == 'GREASEPENCIL'), None)
        if gp is None:
            self.report({'ERROR'}, "Select the Grease Pencil object "
                                   "that holds your drawn path.")
            return {'CANCELLED'}
        pts = rig.stroke_points_world(gp)
        if len(pts) < 2:
            self.report({'ERROR'}, "That Grease Pencil has no usable "
                                   "stroke. Draw a line with 2 or "
                                   "more points.")
            return {'CANCELLED'}
        try:
            rig.set_dolly_path(rig.rig_root_of(context.scene.camera), pts)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"Dolly path rebuilt from {len(pts)} drawn points")
        return {'FINISHED'}


class CK_OT_rig_path_from_curve(_DollyActive, bpy.types.Operator):
    bl_idname = "cinekit.rig_path_from_curve"
    bl_label = "Dolly Path from Selected Curve"
    bl_description = ("Rebuild the active dolly's path from another selected "
                      "curve object's shape")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        curve = next((o for o in context.selected_objects
                      if o.type == 'CURVE' and not o.get(K.TAG_RIG_MEMBER)),
                     None)
        if curve is None:
            self.report({'ERROR'}, "Select a curve object for the "
                                   "dolly path.")
            return {'CANCELLED'}
        pts = rig.curve_points_world(curve)
        if len(pts) < 2:
            self.report({'ERROR'}, "That curve has no usable points. "
                                   "Add points to the curve.")
            return {'CANCELLED'}
        try:
            rig.set_dolly_path(rig.rig_root_of(context.scene.camera), pts)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"Dolly path rebuilt from curve '{curve.name}'")
        return {'FINISHED'}


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
    CK_OT_rig_select_point,
    CK_OT_rig_path_from_gp,
    CK_OT_rig_path_from_curve,
    CK_OT_rig_remove,
    CK_OT_orbit_one_rev,
)
