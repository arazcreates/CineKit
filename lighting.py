# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Lighting workflow: JSON light setups, gobo library, light-linking helper.

Engine notes (also shown in the UI):
- Light setups and the mixer: Cycles and EEVEE.
- Gobos with light node trees: Cycles only. For EEVEE, CineKit puts a
  textured shadow plane in front of the light.
- Light linking: Cycles and EEVEE.
"""

import math
import os

import bpy
from mathutils import Vector

from . import constants as K
from . import utils
from .utils import CKError

_setups_cache = None
# Blender keeps only a pointer to the strings of dynamic enum items, so the
# module keeps each list alive.
_setup_enum = []
_gobo_enum = []
_existing_enum = []

GOBO_TAG = "cinekit_gobo"          # on gobo images: the gobo file name
GOBO_STATE = "cinekit_gobo_prev"   # on light data: node state before a gobo


def get_setups():
    global _setups_cache
    if _setups_cache is None:
        _setups_cache = utils.load_json(
            utils.data_path("light_setups.json"))["setups"]
    return _setups_cache


def setup_items(_self=None, _context=None):
    _setup_enum.clear()
    for sid, spec in get_setups().items():
        _setup_enum.append((sid, spec["name"], spec["description"]))
    return _setup_enum


def gobo_items(_self=None, _context=None):
    _gobo_enum.clear()
    gobo_dir = utils.data_path("gobos")
    if os.path.isdir(gobo_dir):
        for fname in sorted(os.listdir(gobo_dir)):
            if fname.lower().endswith(".png"):
                name = os.path.splitext(fname)[0]
                label = name.replace("_", " ").title()
                _gobo_enum.append((fname, label, f"Gobo texture {fname}"))
    if not _gobo_enum:
        _gobo_enum.append(("", "No gobos found", ""))
    return _gobo_enum


def _bbox_center_radius(obj):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    center = sum(corners, Vector()) / 8.0
    radius = max((c - center).length for c in corners)
    if radius < 0.05:  # empties / tiny objects
        center = obj.matrix_world.translation.copy()
        radius = 1.0
    return center, radius


def _light_position(center, radius, spec):
    az = math.radians(spec["azimuth"])
    el = math.radians(spec["elevation"])
    d = spec["distance"] * radius
    direction = Vector((math.sin(az) * math.cos(el),
                        -math.cos(az) * math.cos(el),
                        math.sin(el)))
    return center + direction * d, d


def setup_collections(scene):
    ck = utils.find_ck_collection(scene)
    if ck is None:
        return []
    return [c for c in ck.children if c.get(K.TAG_LIGHT_SETUP)]


def _scene_setup_collection(scene, uid):
    """The setup collection with this uid in the scene, or None."""
    return next((c for c in setup_collections(scene)
                 if c.get(K.TAG_LIGHT_SETUP) == uid), None)


class CK_OT_light_setup_add(bpy.types.Operator):
    bl_idname = "cinekit.light_setup_add"
    bl_label = "Add Lighting Setup"
    bl_description = ("Add a light setup that points at the active object "
                      "and fits its size. Run it again to update the setup "
                      "instead of adding a second one")
    bl_options = {'REGISTER', 'UNDO'}

    setup: bpy.props.EnumProperty(name="Setup", items=setup_items)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        scene = context.scene
        target = context.active_object
        spec = get_setups().get(self.setup)
        if spec is None:
            self.report({'ERROR'}, f"There is no light setup named "
                                   f"'{self.setup}'.")
            return {'CANCELLED'}
        center, radius = _bbox_center_radius(target)
        uid = f"{self.setup}:{target.name}"

        coll = _scene_setup_collection(scene, uid)
        if coll is None:
            coll = bpy.data.collections.new(
                f"CK {spec['name']} ({target.name})")
            coll[K.TAG_LIGHT_SETUP] = uid
            utils.ck_collection(scene).children.link(coll)
        existing = {o.get("cinekit_setup_slot"): o for o in coll.objects
                    if o.get("cinekit_setup_slot") is not None}

        for i, lspec in enumerate(spec["lights"]):
            obj = existing.get(i)
            if obj is None or obj.type != 'LIGHT':
                data = bpy.data.lights.new(
                    f"CK_{self.setup}_{lspec['role'].lower()}_{i}",
                    lspec["type"])
                obj = bpy.data.objects.new(data.name, data)
                obj[K.TAG] = uid
                obj["cinekit_setup_slot"] = i
                coll.objects.link(obj)
            data = obj.data
            pos, dist = _light_position(center, radius, lspec)
            obj.location = pos
            look_dir = center - pos
            obj.rotation_euler = look_dir.to_track_quat('-Z', 'Y').to_euler()
            # Inverse-square: keep subject illuminance constant vs the
            # nominal 1 m-radius reference the presets were designed for.
            data.energy = lspec["power"] * radius * radius
            data.color = lspec["color"]
            if data.type == 'AREA':
                data.size = lspec["size"] * radius
            elif data.type == 'SPOT':
                data.spot_size = math.radians(50.0)
                data.spot_blend = 0.3
                data.shadow_soft_size = lspec["size"] * radius * 0.15
            data.cinekit.is_cinekit = True
            data.cinekit.role = lspec["role"]
            data.cinekit.setup_id = uid
        self.report({'INFO'},
                    f"'{spec['name']}' aimed at {target.name} "
                    f"(radius {radius:.2f} m)")
        return {'FINISHED'}


def _existing_setup_items(_self, context):
    items = [(c.get(K.TAG_LIGHT_SETUP), c.name, "")
             for c in setup_collections(context.scene)]
    _existing_enum[:] = items or [("NONE", "No CineKit setups", "")]
    return _existing_enum


def _remove_gobo_planes(light_obj):
    """Delete the gobo shadow planes of a light. Return True if any."""
    removed = False
    for child in [c for c in light_obj.children if c.get(K.TAG) == "gobo"]:
        mesh = child.data
        bpy.data.objects.remove(child, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        removed = True
    return removed


def _purge_unused_gobo_data():
    """Delete gobo materials and images that nothing uses any more."""
    for mat in [m for m in bpy.data.materials
                if m.get(K.TAG) == "gobo" and m.users == 0]:
        bpy.data.materials.remove(mat)
    for img in [i for i in bpy.data.images
                if i.get(GOBO_TAG) and i.users == 0]:
        bpy.data.images.remove(img)


class CK_OT_light_setup_remove(bpy.types.Operator):
    bl_idname = "cinekit.light_setup_remove"
    bl_label = "Remove Lighting Setup"
    bl_description = ("Delete a CineKit lighting setup. CineKit deletes "
                      "only the lights and panels of that setup")
    bl_options = {'REGISTER', 'UNDO'}

    setup_uid: bpy.props.EnumProperty(name="Setup",
                                      items=_existing_setup_items)

    @classmethod
    def poll(cls, context):
        return bool(setup_collections(context.scene))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        # Dynamic enum: capture the value BEFORE deleting the collection,
        # or re-reading it resolves against the now-changed items list.
        uid = self.setup_uid
        coll = _scene_setup_collection(context.scene, uid)
        if coll is None:
            self.report({'ERROR'}, "The setup collection no longer "
                                   "exists.")
            return {'CANCELLED'}
        for obj in list(coll.objects):
            _remove_gobo_planes(obj)
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                if data.id_type == 'LIGHT':
                    bpy.data.lights.remove(data)
                elif data.id_type == 'MESH':
                    bpy.data.meshes.remove(data)
        bpy.data.collections.remove(coll)
        _purge_unused_gobo_data()
        return {'FINISHED'}


def cinekit_lights(scene):
    """All CineKit lights in the scene, for the Lighting Mixer."""
    out = []
    for obj in scene.objects:
        if obj.type == 'LIGHT' and obj.data.cinekit.is_cinekit:
            out.append(obj)
    return sorted(out, key=lambda o: (o.data.cinekit.setup_id,
                                      o.data.cinekit.role))


# ------------------------------------------------------------------- gobos
class CK_OT_gobo_add(bpy.types.Operator):
    bl_idname = "cinekit.gobo_add"
    bl_label = "Add Gobo"
    bl_description = ("Add a gobo pattern to the active spot or area light. "
                      "Cycles uses the light nodes. EEVEE uses a textured "
                      "shadow plane in front of the light. The plane can "
                      "show in reflections")
    bl_options = {'REGISTER', 'UNDO'}

    gobo: bpy.props.EnumProperty(name="Gobo", items=gobo_items)
    method: bpy.props.EnumProperty(
        name="Method",
        items=(('AUTO', "Auto (match engine)", "Light nodes in Cycles, "
                                               "shadow plane in EEVEE"),
               ('NODES', "Light Nodes (Cycles)", "Texture in the node tree "
                                                 "of the light"),
               ('PLANE', "Shadow Plane (both)", "Textured plane in front of "
                                                "the light, as its child")),
        default='AUTO')
    scale: bpy.props.FloatProperty(name="Pattern Scale", default=1.0,
                                   min=0.1, max=10.0)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.type == 'LIGHT'
                and obj.data.type in {'SPOT', 'AREA'}
                and not utils.is_linked(obj.data))

    def execute(self, context):
        if not self.gobo:
            self.report({'ERROR'}, "The data/gobos folder has no gobo "
                                   "textures.")
            return {'CANCELLED'}
        obj = context.active_object
        method = self.method
        if method == 'AUTO':
            method = ('NODES' if context.scene.render.engine == K.CYCLES
                      else 'PLANE')
        try:
            image = _gobo_image(self.gobo)
            if method == 'NODES':
                self._apply_nodes(obj, image)
                self.report({'INFO'},
                            "CineKit added the gobo to the light "
                            "nodes. This works in Cycles only.")
            else:
                self._apply_plane(context, obj, image)
                self.report({'INFO'},
                            "CineKit added a gobo shadow plane. "
                            "This works in EEVEE and in Cycles.")
        except CKError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}

    def _apply_nodes(self, obj, image):
        light = obj.data
        state = utils.fetch_state(light, GOBO_STATE) or {}
        if _ensure_light_tree(light):
            state["use_nodes"] = False    # give back the old node state
        nt = light.node_tree
        if nt is None:
            raise CKError("The light has no node tree. Turn on its nodes, "
                          "then try again.")
        emission = next((n for n in nt.nodes if n.type == 'EMISSION'), None)
        if emission is None:
            raise CKError("The light node tree has no Emission node. "
                          "Reset the light nodes, then try again.")
        color_in = emission.inputs["Color"]
        if ("color_link" not in state and color_in.is_linked
                and not color_in.links[0].from_node.get(K.TAG)):
            # The user feeds the light color from a node. The gobo goes on
            # top of that color, and Remove Gobo connects it back.
            link = color_in.links[0]
            state["color_link"] = [
                link.from_node.name,
                list(link.from_node.outputs).index(link.from_socket)]
        utils.store_state(light, GOBO_STATE, state)
        for node in [n for n in nt.nodes if n.get(K.TAG)]:
            nt.nodes.remove(node)  # idempotent re-apply

        def new(node_type, x, y, **props):
            node = nt.nodes.new(node_type)
            node.location = (x, y)
            node[K.TAG] = "gobo"
            for attr, val in props.items():
                setattr(node, attr, val)
            return node

        coord = new('ShaderNodeTexCoord', -900, 0)
        sep = new('ShaderNodeSeparateXYZ', -720, 0)
        nt.links.new(coord.outputs["Normal"], sep.inputs[0])
        # Perspective projection along the light axis: u = x/-z, v = y/-z.
        neg_z = new('ShaderNodeMath', -560, -80, operation='MULTIPLY')
        neg_z.inputs[1].default_value = -1.0
        nt.links.new(sep.outputs[2], neg_z.inputs[0])
        s = 0.5 / max(self.scale, 0.01)
        chans = []
        for i in range(2):
            div = new('ShaderNodeMath', -400, -i * 160, operation='DIVIDE')
            nt.links.new(sep.outputs[i], div.inputs[0])
            nt.links.new(neg_z.outputs[0], div.inputs[1])
            mul = new('ShaderNodeMath', -260, -i * 160,
                      operation='MULTIPLY')
            mul.inputs[1].default_value = s
            nt.links.new(div.outputs[0], mul.inputs[0])
            add = new('ShaderNodeMath', -120, -i * 160, operation='ADD')
            add.inputs[1].default_value = 0.5
            nt.links.new(mul.outputs[0], add.inputs[0])
            chans.append(add)
        comb = new('ShaderNodeCombineXYZ', 20, -60)
        nt.links.new(chans[0].outputs[0], comb.inputs[0])
        nt.links.new(chans[1].outputs[0], comb.inputs[1])
        tex = new('ShaderNodeTexImage', 160, 0, image=image,
                  extension='CLIP')
        nt.links.new(comb.outputs[0], tex.inputs[0])
        mix = new('ShaderNodeMix', 420, 0)
        mix.data_type = 'RGBA'
        mix.blend_type = 'MULTIPLY'
        mix.inputs["Factor"].default_value = 1.0
        mix.inputs[6].default_value = tuple(color_in.default_value)  # A
        source = _link_source(nt, state.get("color_link"))
        if source is not None:
            nt.links.new(source, mix.inputs[6])
        nt.links.new(tex.outputs["Color"], mix.inputs[7])  # B
        nt.links.new(mix.outputs[2], color_in)

    def _apply_plane(self, context, obj, image):
        light = obj.data
        existing = next((c for c in obj.children if c.get(K.TAG) == "gobo"),
                        None)
        if existing is not None:
            plane = existing
        else:
            mesh = bpy.data.meshes.new(f"CK_Gobo_{obj.name}")
            size = 0.5
            mesh.from_pydata(
                [(-size, -size, 0), (size, -size, 0),
                 (size, size, 0), (-size, size, 0)],
                [], [(0, 1, 2, 3)])
            uv = mesh.uv_layers.new()
            for loop_i, co in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
                uv.data[loop_i].uv = co
            plane = bpy.data.objects.new(mesh.name, mesh)
            plane[K.TAG] = "gobo"
            utils.link_to_ck(context.scene, plane)
            plane.parent = obj
        dist = 0.3
        if light.type == 'SPOT':
            half = math.tan(light.spot_size / 2.0) * dist * 1.15
        else:
            half = max(light.size, 0.5) * 0.75
        plane.location = (0.0, 0.0, -dist)
        plane.scale = (2 * half / max(self.scale, 0.01),) * 2 + (1.0,)

        mat = bpy.data.materials.get(f"CK_GoboMat_{self.gobo}")
        if mat is None or mat.get(K.TAG) != "gobo":
            mat = bpy.data.materials.new(f"CK_GoboMat_{self.gobo}")
            mat[K.TAG] = "gobo"
            nt = utils.ensure_node_tree(mat)
            nt.nodes.clear()
            out = nt.nodes.new('ShaderNodeOutputMaterial')
            out.location = (400, 0)
            mix = nt.nodes.new('ShaderNodeMixShader')
            mix.location = (200, 0)
            dark = nt.nodes.new('ShaderNodeBsdfDiffuse')
            dark.inputs["Color"].default_value = (0, 0, 0, 1)
            dark.location = (0, -120)
            clear = nt.nodes.new('ShaderNodeBsdfTransparent')
            clear.location = (0, 120)
            tex = nt.nodes.new('ShaderNodeTexImage')
            tex.image = image
            tex.location = (-250, 0)
            nt.links.new(tex.outputs["Color"], mix.inputs["Fac"])
            nt.links.new(dark.outputs[0], mix.inputs[1])   # black blocks
            nt.links.new(clear.outputs[0], mix.inputs[2])  # white passes
            nt.links.new(mix.outputs[0], out.inputs["Surface"])
            for attr, val in (("surface_render_method", 'DITHERED'),
                              ("blend_method", 'HASHED'),
                              ("shadow_method", 'HASHED')):
                if hasattr(mat, attr):
                    try:
                        setattr(mat, attr, val)
                    except TypeError:
                        pass
        plane.data.materials.clear()
        plane.data.materials.append(mat)
        if hasattr(plane, "visible_camera"):
            plane.visible_camera = False  # Cycles obeys this. EEVEE does not.


class CK_OT_gobo_remove(bpy.types.Operator):
    bl_idname = "cinekit.gobo_remove"
    bl_label = "Remove Gobo"
    bl_description = ("Remove the CineKit gobo nodes and planes from the "
                      "active light, and restore its node setup")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.type == 'LIGHT'
                and not utils.is_linked(obj.data))

    def execute(self, context):
        obj = context.active_object
        light = obj.data
        removed = False
        state = utils.fetch_state(light, GOBO_STATE)
        nt = light.node_tree
        if nt is not None:
            for node in [n for n in nt.nodes if n.get(K.TAG)]:
                nt.nodes.remove(node)
                removed = True
            emission = next((n for n in nt.nodes if n.type == 'EMISSION'),
                            None)
            source = _link_source(nt, (state or {}).get("color_link"))
            if emission is not None and source is not None:
                nt.links.new(source, emission.inputs["Color"])
        if state is not None:
            if state.get("use_nodes") is False:
                _disable_light_nodes(light)
            utils.clear_state(light, GOBO_STATE)
        removed = _remove_gobo_planes(obj) or removed
        _purge_unused_gobo_data()
        if not removed:
            self.report({'INFO'}, "This light has no CineKit gobo.")
        return {'FINISHED'}


def _gobo_image(fname):
    """Return the packed image of a gobo file. Load it one time only.

    CineKit tags its own image datablock, so it never changes an image that
    the user loaded from the same file.
    """
    image = next((i for i in bpy.data.images if i.get(GOBO_TAG) == fname),
                 None)
    if image is not None:
        return image
    path = os.path.join(utils.data_path("gobos"), fname)
    try:
        image = bpy.data.images.load(path, check_existing=False)
    except RuntimeError as exc:
        raise CKError(f"CineKit could not load the gobo '{fname}': "
                      f"{exc}") from exc
    image[GOBO_TAG] = fname
    image.colorspace_settings.name = 'Non-Color'
    image.pack()
    # The packed data is the only source. A relative path keeps the add-on
    # folder, and with it the user name, out of the .blend file.
    image.filepath_raw = f"//gobos/{fname}"
    return image


# Blender 5.x lights always use their node tree, and Light.use_nodes is
# deprecated. Older versions create the tree when use_nodes turns on.
_LIGHT_USE_NODES = bpy.types.Light.bl_rna.properties.get("use_nodes")
_LIGHT_NODES_OPTIONAL = (_LIGHT_USE_NODES is not None
                         and not getattr(_LIGHT_USE_NODES, "is_deprecated",
                                         False))


def _ensure_light_tree(light):
    """Make sure the light has a node tree. Return True if CineKit turned
    the nodes on."""
    if light.node_tree is not None and (
            not _LIGHT_NODES_OPTIONAL or light.use_nodes):
        return False
    if _LIGHT_USE_NODES is not None:
        light.use_nodes = True
        return True
    return False


def _disable_light_nodes(light):
    if _LIGHT_NODES_OPTIONAL:
        light.use_nodes = False


def _link_source(tree, spec):
    """The output socket that a stored [node name, index] names, or None."""
    if not spec:
        return None
    node = tree.nodes.get(spec[0])
    if node is None or node.get(K.TAG):
        return None
    outputs = list(node.outputs)
    return outputs[spec[1]] if 0 <= spec[1] < len(outputs) else None


# ----------------------------------------------------------- light linking
class CK_OT_lightlink_new(bpy.types.Operator):
    bl_idname = "cinekit.lightlink_new"
    bl_label = "New Light Linking Collection"
    bl_description = ("Add a receiver collection to the active light. Only "
                      "the objects in it get light from this light. Cycles "
                      "and EEVEE support light linking")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.type == 'LIGHT'
                and hasattr(obj, "light_linking")
                and not utils.is_linked(obj))

    def execute(self, context):
        obj = context.active_object
        if obj.light_linking.receiver_collection is not None:
            self.report({'INFO'},
                    "This light already has a receiver collection.")
            return {'CANCELLED'}
        coll = bpy.data.collections.new(f"CK LightLink {obj.name}")
        coll[K.TAG] = utils.new_id("ll_")
        obj.light_linking.receiver_collection = coll
        return {'FINISHED'}


class CK_OT_lightlink_add_selected(bpy.types.Operator):
    bl_idname = "cinekit.lightlink_add_selected"
    bl_label = "Add Selected Objects"
    bl_description = ("Add the selected objects to the receiver collection of "
                      "the active light. Only these objects get its light")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None and obj.type == 'LIGHT'
                and hasattr(obj, "light_linking")
                and obj.light_linking.receiver_collection is not None)

    def execute(self, context):
        coll = context.active_object.light_linking.receiver_collection
        added = 0
        for obj in context.selected_objects:
            if obj.type == 'LIGHT' or obj.name in coll.objects:
                continue
            coll.objects.link(obj)
            added += 1
        self.report({'INFO'}, f"CineKit added {added} object(s).")
        return {'FINISHED'}


class CK_OT_lightlink_remove_object(bpy.types.Operator):
    bl_idname = "cinekit.lightlink_remove_object"
    bl_label = "Remove"
    bl_description = "Remove this object from the receiver collection"
    bl_options = {'REGISTER', 'UNDO'}

    object_name: bpy.props.StringProperty()

    def execute(self, context):
        obj = context.active_object
        if obj is None or not hasattr(obj, "light_linking"):
            return {'CANCELLED'}
        coll = obj.light_linking.receiver_collection
        target = coll.objects.get(self.object_name) if coll else None
        if target is None:
            self.report({'ERROR'},
                        f"'{self.object_name}' is not in the collection.")
            return {'CANCELLED'}
        coll.objects.unlink(target)
        return {'FINISHED'}


CLASSES = (
    CK_OT_light_setup_add,
    CK_OT_light_setup_remove,
    CK_OT_gobo_add,
    CK_OT_gobo_remove,
    CK_OT_lightlink_new,
    CK_OT_lightlink_add_selected,
    CK_OT_lightlink_remove_object,
)
