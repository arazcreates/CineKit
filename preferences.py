# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Add-on preferences: auto-exposure target, user lens presets."""

import bpy

from . import lenses


class _PrefsFallback:
    target_grey = 0.18


def get_prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else _PrefsFallback()


class CK_OT_lens_add_user(bpy.types.Operator):
    bl_idname = "cinekit.lens_add_user"
    bl_label = "Add User Lens"
    bl_description = ("Save this lens to your user lens JSON "
                      "(extension preferences directory)")
    bl_options = {'REGISTER'}

    def execute(self, context):
        prefs = get_prefs()
        name = prefs.new_lens_name.strip()
        if not name:
            self.report({'ERROR'}, "Type a name for the lens.")
            return {'CANCELLED'}
        try:
            lenses.add_user_lens(name, prefs.new_lens_focal,
                                 prefs.new_lens_aperture,
                                 prefs.new_lens_breathing)
        except (RuntimeError, OSError) as exc:
            self.report({'ERROR'}, f"Could not save lens: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Lens '{name}' added")
        return {'FINISHED'}


class CineKitPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    target_grey: bpy.props.FloatProperty(
        name="Auto-Exposure Target",
        description="Scene-referred middle grey the auto-exposure assist "
                    "aims for (0.18 is standard)",
        default=0.18, min=0.02, max=0.8)
    new_lens_name: bpy.props.StringProperty(name="Name",
                                            default="My 40mm f/2")
    new_lens_focal: bpy.props.FloatProperty(name="Focal (mm)", default=40.0,
                                            min=4.0, max=1200.0)
    new_lens_aperture: bpy.props.FloatProperty(name="Max Aperture f/",
                                               default=2.0, min=0.5,
                                               max=32.0)
    new_lens_breathing: bpy.props.FloatProperty(name="Breathing", default=0.1,
                                                min=0.0, max=1.0)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "target_grey")
        box = layout.box()
        box.label(text="Add a lens preset", icon='CAMERA_DATA')
        row = box.row()
        row.prop(self, "new_lens_name")
        row = box.row(align=True)
        row.prop(self, "new_lens_focal")
        row.prop(self, "new_lens_aperture")
        row.prop(self, "new_lens_breathing")
        box.operator("cinekit.lens_add_user", icon='ADD')
        path = lenses.user_lenses_path()
        if path:
            box.label(text=f"User lenses file: {path}", icon='FILE_TEXT')


CLASSES = (CK_OT_lens_add_user, CineKitPreferences)
