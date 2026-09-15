# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Look operators: apply, remove, save-as-custom, reload registry."""

import os
import zlib

import bpy

from .. import utils
from ..utils import CKError
from . import enum_items, get, reload_registry, user_looks_dir
from . import engine, schema


class CK_OT_look_apply(bpy.types.Operator):
    bl_idname = "cinekit.look_apply"
    bl_label = "Apply Look"
    bl_description = ("Apply a look. A look changes the camera, the color "
                      "management and the compositor together. Applying the "
                      "same look again only updates its values")
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
    bl_description = ("Remove the active look. CineKit restores the camera, "
                      "the color management and the compositor connections "
                      "from before the look")
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
    bl_description = ("Save the active look with your current values as a "
                      "user preset. CineKit writes a JSON file to the user "
                      "folder of the extension")
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
            self.report({'ERROR'}, "CineKit cannot find the active look in "
                                   "the preset list.")
            return {'CANCELLED'}
        name = self.preset_name.strip()
        if not name:
            self.report({'ERROR'}, "Type a name for the preset.")
            return {'CANCELLED'}
        look_id = "custom_" + custom_slug(name)

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
            self.report({'ERROR'}, f"The preset is not valid: {exc}")
            return {'CANCELLED'}

        directory = user_looks_dir(create=True)
        if not directory:
            self.report({'ERROR'}, "CineKit cannot open the user folder of "
                                   "the extension.")
            return {'CANCELLED'}
        path = os.path.join(directory, f"{look_id}.json")
        try:
            utils.dump_json(path, data)
        except OSError as exc:
            self.report({'ERROR'}, f"CineKit could not write the preset: "
                                   f"{exc}")
            return {'CANCELLED'}
        reload_registry()
        self.report({'INFO'}, f"Saved custom look to {path}")
        return {'FINISHED'}


def custom_slug(name):
    """An ASCII id part for a custom look name.

    Blender enum ids and file names work best with ASCII. A name with no
    ASCII letters or digits (for example a Persian name) gets a short hash.
    """
    slug = "".join(c if c.isascii() and c.isalnum() else "_"
                   for c in name.lower()).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    if not slug:
        slug = f"{zlib.crc32(name.encode('utf-8')):08x}"
    return slug


class CK_OT_looks_reload(bpy.types.Operator):
    bl_idname = "cinekit.looks_reload"
    bl_label = "Reload Look Presets"
    bl_description = "Read the built-in and the user look presets again"
    bl_options = {'REGISTER'}

    def execute(self, context):
        reload_registry()
        from . import load_errors
        errors = load_errors()
        if errors:
            self.report({'WARNING'},
                        f"{len(errors)} preset(s) are not valid. Open the "
                        "system console to see the errors.")
        return {'FINISHED'}


CLASSES = (
    CK_OT_look_apply,
    CK_OT_look_remove,
    CK_OT_look_save_custom,
    CK_OT_looks_reload,
)
