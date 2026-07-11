# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""UI: 3D View N-panel (CineKit tab), camera data properties panel,
compositor N-panel. Panels only read/draw PropertyGroups and call
operators — never set low-level values."""

import math

import bpy

from . import constants as K
from . import lighting, rig, utils


def _linked_warning(layout, id_block, what):
    if utils.is_linked(id_block):
        col = layout.column()
        col.label(text=f"{what} is linked from a library", icon='LINKED')
        col.label(text="Make it local to edit CineKit settings")
        return True
    return False


def _draw_physical(layout, context, cam_data):
    """Shared by the N-panel and the camera data properties panel."""
    scene = context.scene
    ck = cam_data.cinekit
    if _linked_warning(layout, cam_data, "Camera"):
        return
    layout.prop(ck, "enabled", toggle=True, icon='CAMERA_DATA')
    if not ck.enabled:
        return
    col = layout.column(align=True)
    col.prop(ck, "lens_preset")
    col.separator()
    col.prop(ck, "iso")
    col.prop(ck, "aperture", text="Aperture f/")
    row = col.row(align=True)
    row.prop(ck, "shutter_mode", expand=True)
    if ck.shutter_mode == 'SPEED':
        col.prop(ck, "shutter_speed")
    else:
        col.prop(ck, "shutter_angle")
    col.prop(ck, "exposure_comp")
    try:
        ev = rig.compute_ev100(cam_data, scene)
        col.label(text=f"EV100  {ev:+.2f}", icon='LIGHT_SUN')
    except ValueError:
        pass
    is_exposure_cam = (scene.camera is not None
                       and scene.camera.data == cam_data)
    if not is_exposure_cam:
        col.label(text="Not the active camera — exposure follows the "
                       "active one", icon='INFO')
    col.prop(ck, "drive_motion_blur")

    box = layout.box()
    box.prop(ck, "wb_enabled", toggle=True, icon='COLOR')
    if ck.wb_enabled:
        box.prop(ck, "wb_temperature")
        box.prop(ck, "wb_tint")
        if not hasattr(scene.view_settings, "white_balance_temperature"):
            box.label(text="4.2: WB via compositor — enable the Viewport "
                           "Compositor to preview", icon='INFO')
    row = layout.row(align=True)
    row.operator("cinekit.auto_expose", icon='LIGHT_HEMI')
    row.operator("cinekit.release_exposure", text="", icon='LOOP_BACK')

    box = layout.box()
    box.prop(ck, "focus_breathing")
    if ck.focus_breathing:
        box.prop(ck, "breathing_amount", slider=True)


class VIEW3D_PT_cinekit(bpy.types.Panel):
    bl_label = "CineKit"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CineKit"

    def draw(self, context):
        ck = context.scene.cinekit
        row = self.layout.row(align=True)
        row.label(text=f"Look: {ck.active_look or '—'}", icon='SHADERFX')
        row.prop(ck, "overlay_enabled", text="", icon='OVERLAY')


class _ChildPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "CineKit"
    bl_parent_id = "VIEW3D_PT_cinekit"
    bl_options = {'DEFAULT_CLOSED'}


class VIEW3D_PT_ck_camera(_ChildPanel, bpy.types.Panel):
    bl_label = "Camera"

    def draw(self, context):
        cam_obj = context.scene.camera
        if cam_obj is None or cam_obj.type != 'CAMERA':
            self.layout.label(text="No active camera in the scene",
                              icon='ERROR')
            return
        self.layout.label(text=cam_obj.name, icon='OUTLINER_OB_CAMERA')
        _draw_physical(self.layout, context, cam_obj.data)


class CK_UL_shots(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False)
        cam = item.camera.name if item.camera else "—"
        row.label(text=cam, icon='CAMERA_DATA')
        row.label(text=f"{item.frame_start}-{item.frame_end}")
        if item.look != 'NONE':
            row.label(text="", icon='SHADERFX')


class VIEW3D_PT_ck_shots(_ChildPanel, bpy.types.Panel):
    bl_label = "Shots"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        ck = scene.cinekit
        row = layout.row()
        row.template_list("CK_UL_shots", "", ck, "shots", ck, "shot_index",
                          rows=3)
        col = row.column(align=True)
        col.operator("cinekit.shot_add", text="", icon='ADD')
        col.operator("cinekit.shot_remove", text="", icon='REMOVE')
        col.operator("cinekit.shot_duplicate", text="", icon='DUPLICATE')

        if 0 <= ck.shot_index < len(ck.shots):
            shot = ck.shots[ck.shot_index]
            box = layout.box()
            box.prop(shot, "camera")
            row = box.row(align=True)
            row.prop(shot, "frame_start")
            row.prop(shot, "frame_end")
            box.prop(shot, "look")
            box.prop(shot, "notes", icon='TEXT')
            row = box.row(align=True)
            row.operator("cinekit.shot_jump", icon='PLAY')
            row.operator("cinekit.shot_set_range", text="Set Range",
                         icon='PREVIEW_RANGE')
        layout.prop(ck, "auto_switch_shot_look")
        row = layout.row(align=True)
        row.operator("cinekit.shots_sync_markers", icon='MARKER_HLT')
        layout.operator("cinekit.batch_render", icon='RENDER_ANIMATION')


class VIEW3D_PT_ck_rigs(_ChildPanel, bpy.types.Panel):
    bl_label = "Rigs"

    def draw(self, context):
        layout = self.layout
        cam = context.scene.camera
        root = rig.rig_root_of(cam) if cam else None

        if root is None:
            col = layout.column(align=True)
            col.operator("cinekit.rig_dolly", icon='TRACKING')
            col.operator("cinekit.rig_crane", icon='CON_TRACKTO')
            col.operator_menu_enum("cinekit.rig_handheld", "profile",
                                   text="Add Handheld Rig",
                                   icon='ORIENTATION_GIMBAL')
            col.operator("cinekit.rig_orbit", icon='PHYSICS')
            return

        box = layout.box()
        rig_type = root.get(K.TAG_RIG, "?")
        box.label(text=f"Rig Settings — {rig_type.title()}",
                  icon='OUTLINER_OB_ARMATURE')
        rig_id = root.get("cinekit_rig_uid", "")
        if rig_type == K.RIG_DOLLY:
            box.prop(root, '["ck_position"]', text="Position", slider=True)
            box.prop(root, '["ck_track"]', text="Track Aim", slider=True)
            carrier = next(
                (o for o in bpy.data.objects
                 if o.get(K.TAG_RIG_MEMBER) == rig_id
                 and "Carrier" in o.name), None)
            if carrier:
                con = carrier.constraints.get("CK Follow Path")
                if con:
                    box.prop(con, "use_curve_follow", text="Banking")
        elif rig_type == K.RIG_CRANE:
            box.prop(root, '["ck_pan"]', text="Pan", slider=True)
            box.prop(root, '["ck_boom"]', text="Boom", slider=True)
            box.prop(root, '["ck_length"]', text="Arm Length", slider=True)
            box.prop(root, '["ck_tilt"]', text="Tilt", slider=True)
        elif rig_type == K.RIG_HANDHELD:
            profile = root.get("cinekit_handheld_profile", "")
            box.label(text=f"Profile: {profile.replace('_', ' ').title()}")
            box.prop(root, '["ck_intensity"]', text="Intensity", slider=True)
        elif rig_type == K.RIG_ORBIT:
            box.prop(root, '["ck_radius"]', text="Radius", slider=True)
            box.prop(root, '["ck_height"]', text="Height", slider=True)
            box.prop(root, '["ck_speed"]', text="Speed °/s", slider=True)
            box.prop(root, '["ck_offset"]', text="Start Angle", slider=True)
            box.operator("cinekit.orbit_one_revolution", icon='FILE_REFRESH')
        box.operator("cinekit.rig_remove", icon='X')


class VIEW3D_PT_ck_focus(_ChildPanel, bpy.types.Panel):
    bl_label = "Focus"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column(align=True)
        col.operator("cinekit.focus_pick", icon='EYEDROPPER')
        col.operator("cinekit.focus_add_target", icon='EMPTY_AXIS')
        col.operator("cinekit.rack_focus", icon='ANIM')
        col.operator("cinekit.focus_clear_targets", icon='TRASH')

        cam_obj = scene.camera
        if cam_obj is None or cam_obj.type != 'CAMERA':
            return
        cam = cam_obj.data
        if not cam.dof.use_dof or not cam.cinekit.enabled:
            layout.label(text="Enable Physical Camera + DOF for the "
                              "readout", icon='INFO')
            return
        try:
            h, near, far, total = rig.dof_readout(cam, scene)
        except ValueError:
            return
        box = layout.box()
        box.label(text="DOF Readout", icon='DRIVER_DISTANCE')
        col = box.column(align=True)
        col.label(text=f"Hyperfocal:  {h:.2f} m")
        col.label(text=f"Near limit:  {near:.2f} m")
        col.label(text="Far limit:   ∞" if math.isinf(far)
                  else f"Far limit:   {far:.2f} m")
        col.label(text="Total DOF:  ∞" if math.isinf(total)
                  else f"Total DOF:  {total:.2f} m")


class VIEW3D_PT_ck_lighting(_ChildPanel, bpy.types.Panel):
    bl_label = "Lighting"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column(align=True)
        col.operator_menu_enum("cinekit.light_setup_add", "setup",
                               text="Add Lighting Setup", icon='LIGHT')
        col.operator("cinekit.light_setup_remove", icon='X')

        lights = lighting.cinekit_lights(scene)
        if lights:
            box = layout.box()
            box.label(text="Lighting Mixer", icon='OPTIONS')
            current_setup = None
            for obj in lights:
                data = obj.data
                if data.cinekit.setup_id != current_setup:
                    current_setup = data.cinekit.setup_id
                    box.label(text=current_setup, icon='OUTLINER_COLLECTION')
                if _linked_warning(box, data, f"Light {obj.name}"):
                    continue
                row = box.row(align=True)
                row.label(text=data.cinekit.role, icon='LIGHT_DATA')
                row.prop(data, "energy", text="W")
                row.prop(data, "color", text="")
                if data.type == 'AREA':
                    row.prop(data, "size", text="Size")
                elif data.type == 'SPOT':
                    row.prop(data, "shadow_soft_size", text="Size")

        box = layout.box()
        box.label(text="Gobos", icon='TEXTURE')
        is_cycles = scene.render.engine == K.CYCLES
        if not is_cycles:
            box.label(text="EEVEE: gobo uses a shadow plane fallback",
                      icon='INFO')
        row = box.row(align=True)
        row.operator("cinekit.gobo_add", icon='ADD')
        row.operator("cinekit.gobo_remove", text="", icon='X')

        box = layout.box()
        box.label(text="Light Linking (Cycles only)", icon='LINKED')
        if not is_cycles:
            box.label(text="Switch to Cycles to use light linking",
                      icon='INFO')
            return
        obj = context.active_object
        if obj is None or obj.type != 'LIGHT':
            box.label(text="Select a light", icon='INFO')
            return
        ll = getattr(obj, "light_linking", None)
        if ll is None:
            box.label(text="Light linking unavailable in this build",
                      icon='ERROR')
            return
        if ll.receiver_collection is None:
            box.operator("cinekit.lightlink_new", icon='ADD')
            return
        box.label(text=ll.receiver_collection.name,
                  icon='OUTLINER_COLLECTION')
        for member in ll.receiver_collection.objects:
            row = box.row(align=True)
            row.label(text=member.name, icon='OBJECT_DATA')
            op = row.operator("cinekit.lightlink_remove_object", text="",
                              icon='X')
            op.object_name = member.name
        box.operator("cinekit.lightlink_add_selected", icon='ADD')


def _draw_look_params(layout, scene):
    ck = scene.cinekit
    layout.prop(ck, "look_intensity", slider=True)
    for param in ck.look_params:
        layout.prop(param, "value", text=param.label, slider=True)
    if ck.active_look == "security_cam":
        layout.prop(ck, "security_timestamp")


class VIEW3D_PT_ck_looks(_ChildPanel, bpy.types.Panel):
    bl_label = "Looks"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        ck = scene.cinekit
        layout.operator_menu_enum("cinekit.look_apply", "look",
                                  text=(f"Look: {ck.active_look}"
                                        if ck.active_look else
                                        "Apply Look…"),
                                  icon='SHADERFX')
        if ck.active_look:
            _draw_look_params(layout, scene)
            row = layout.row(align=True)
            row.operator("cinekit.look_save_custom", icon='FILE_TICK')
            row.operator("cinekit.look_remove", text="", icon='X')
            layout.label(text="Compositor layer: enable the Viewport "
                              "Compositor to preview", icon='INFO')
            layout.label(text="Camera layer (crop, DOF) is live in any "
                              "viewport", icon='CHECKMARK')
        layout.operator("cinekit.looks_reload", icon='FILE_REFRESH')


class DATA_PT_cinekit_camera(bpy.types.Panel):
    """Mirror of the physical camera section in camera data properties."""
    bl_label = "CineKit Physical Camera"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return getattr(context, "camera", None) is not None

    def draw(self, context):
        _draw_physical(self.layout, context, context.camera)


class NODE_PT_cinekit_look(bpy.types.Panel):
    """Compositor N-panel: the active look's parameters (same
    PropertyGroup, second location)."""
    bl_label = "CineKit Look"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "CineKit"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (space.tree_type == 'CompositorNodeTree'
                and context.scene is not None)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        ck = scene.cinekit
        if not ck.active_look:
            layout.label(text="No active look", icon='INFO')
            layout.operator_menu_enum("cinekit.look_apply", "look",
                                      text="Apply Look…")
            return
        layout.label(text=ck.active_look, icon='SHADERFX')
        _draw_look_params(layout, scene)
        layout.operator("cinekit.look_remove", icon='X')


CLASSES = (
    VIEW3D_PT_cinekit,
    VIEW3D_PT_ck_camera,
    CK_UL_shots,
    VIEW3D_PT_ck_shots,
    VIEW3D_PT_ck_rigs,
    VIEW3D_PT_ck_focus,
    VIEW3D_PT_ck_lighting,
    VIEW3D_PT_ck_looks,
    DATA_PT_cinekit_camera,
    NODE_PT_cinekit_look,
)
