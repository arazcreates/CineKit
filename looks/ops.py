# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Look operators: apply, remove, save-as-custom, reload registry."""

import os

import bpy

from .. import utils
from ..utils import CKError
from . import enum_items, get, reload_registry, user_looks_dir
from . import engine, schema


class CK_OT_look_apply(bpy.types.Operator):
    bl_idname = "cinekit.look_apply"
    bl_label = "Apply Look"
    bl_description = ("Apply a look: camera optics + color management + "
                      "compositor artifacts together. Fast in-place update "
                      "when re-applying the same look")
    bl_options = {'REGISTER', 'UNDO'}

    look: bpy.props.EnumProperty(name="Look", items=enum_items)

    @classmethod
    def poll(cls, context):
        return not utils.is_linked(context.scene)

    def execute(self, context):
        scene = context.scene
        try:
            if self.look == 'NONE':
                engine.remove_look(scene)
            else:
                engine.apply_look(scene, self.look)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class CK_OT_look_remove(bpy.types.Operator):
    bl_idname = "cinekit.look_remove"
    bl_label = "Remove Look"
    bl_description = ("Remove the active look and restore camera, color "
                      "management and the exact prior compositor "
                      "connections")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.cinekit.active_look)

    def execute(self, context):
        try:
            engine.remove_look(context.scene)
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class CK_OT_look_save_custom(bpy.types.Operator):
    bl_idname = "cinekit.look_save_custom"
    bl_label = "Save as Custom Look"
    bl_description = ("Save the active look with your current parameter "
                      "values as a user preset (JSON in the extension's "
                      "user preferences directory)")
    bl_options = {'REGISTER'}

    preset_name: bpy.props.StringProperty(name="Name", default="My Look")

    @classmethod
    def poll(cls, context):
        return bool(context.scene.cinekit.active_look)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        base = get(scene.cinekit.active_look)
        if base is None:
            self.report({'ERROR'}, "Active look not found in the registry")
            return {'CANCELLED'}
        name = self.preset_name.strip()
        if not name:
            self.report({'ERROR'}, "Give the preset a name")
            return {'CANCELLED'}
        look_id = "custom_" + "".join(
            c if c.isalnum() else "_" for c in name.lower()).strip("_")

        data = {
            "id": look_id,
            "name": name,
            "description": f"Custom look based on {base['name']}",
            "rationale": base.get("rationale", ""),
            "camera": {k: v for k, v in base["camera"].items()
                       if v is not None},
            "color": {k: v for k, v in base["color"].items()
                      if v not in (None, 0.0)},
            "chain": [],
        }
        values = {p.name: p.value for p in scene.cinekit.look_params}
        for entry in base["chain"]:
            new_entry = dict(entry)
            socket = schema.ARTIFACTS[entry["type"]]["socket"]
            if socket in values:
                new_entry["strength"] = round(values[socket], 4)
            data["chain"].append(new_entry)
        try:
            schema.normalize_look(data)
        except schema.LookError as exc:
            self.report({'ERROR'}, f"Preset failed validation: {exc}")
            return {'CANCELLED'}

        directory = user_looks_dir(create=True)
        if not directory:
            self.report({'ERROR'}, "Extension user directory unavailable")
            return {'CANCELLED'}
        path = os.path.join(directory, f"{look_id}.json")
        utils.dump_json(path, data)
        reload_registry()
        self.report({'INFO'}, f"Saved custom look to {path}")
        return {'FINISHED'}


class CK_OT_looks_reload(bpy.types.Operator):
    bl_idname = "cinekit.looks_reload"
    bl_label = "Reload Look Presets"
    bl_description = "Re-scan built-in and user look preset JSON files"
    bl_options = {'REGISTER'}

    def execute(self, context):
        reload_registry()
        from . import load_errors
        errors = load_errors()
        if errors:
            self.report({'WARNING'},
                        f"{len(errors)} preset(s) failed validation — see "
                        "the system console")
        return {'FINISHED'}


CLASSES = (
    CK_OT_look_apply,
    CK_OT_look_remove,
    CK_OT_look_save_custom,
    CK_OT_looks_reload,
)
