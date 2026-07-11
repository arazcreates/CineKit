# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Look engine: builds and maintains the CineKit compositor pipeline.

Design
------
ONE group node ("CineKit Pipeline", per scene) is inserted in-line between
whatever fed the compositor output and the output itself. The original
link is stored (scene custom prop) and restored exactly on removal. Inside
the pipeline tree: GroupInput -> white-balance gain -> [look group] ->
GroupOutput. The look group is a separate datablock named CK_Look_<Name>,
tagged with the look id.

Fast path: applying a look whose id matches the current look group only
writes group-node input socket values (no rebuild, no hitch). Rebuild only
happens on look-type change.

Blender version compatibility
-----------------------------
4.2-4.x: scene.node_tree + Composite node; legacy compositor nodes
(CompositorNodeMixRGB/Math/Texture...).
5.x: scene.compositing_node_group + Group Output node; unified nodes
(ShaderNodeMix/Math/TexNoise...), node settings as input sockets.
Every node the builders create goes through small compat helpers that try
the legacy type first and fall back to the 5.x equivalent.

Compositor artifacts run identically on Cycles and EEVEE. Viewport preview
of the compositor layer requires the Viewport Compositor; camera-layer
parts (sensor crop, deep DOF) are visible in any viewport.
"""

import bpy

from .. import constants as K
from .. import utils
from ..utils import CKError
from . import get as get_look
from . import schema

_WHITE = (1.0, 1.0, 1.0, 1.0)


# ------------------------------------------------------------- node helpers
def _set_first(node, names, value):
    """Set the first matching property or input socket; ignore misses so we
    degrade gracefully across Blender versions."""
    for name in names:
        if hasattr(node, name):
            try:
                setattr(node, name, value)
                return True
            except (TypeError, ValueError):
                continue
        if name in node.inputs:
            try:
                node.inputs[name].default_value = value
                return True
            except (TypeError, ValueError):
                continue
    return False


def _find_node(tree, tag_value):
    for node in tree.nodes:
        if node.get(K.TAG) == tag_value:
            return node
    return None


def _try_new(tree, *node_types):
    """First node type the tree accepts (legacy first, 5.x fallback)."""
    for node_type in node_types:
        try:
            return tree.nodes.new(node_type)
        except RuntimeError:
            continue
    raise CKError(f"No usable node type among {node_types} in this "
                  "Blender version")


def _new_mix(tree, blend='MIX', clamp=False):
    """Mix node across eras: (node, fac_in, a_in, b_in, out)."""
    try:
        node = tree.nodes.new('CompositorNodeMixRGB')
        node.blend_type = blend
        node.use_clamp = clamp
        return node, node.inputs[0], node.inputs[1], node.inputs[2], \
            node.outputs[0]
    except RuntimeError:
        node = tree.nodes.new('ShaderNodeMix')
        node.data_type = 'RGBA'
        node.blend_type = blend
        node.clamp_result = clamp
        return node, node.inputs[0], node.inputs[6], node.inputs[7], \
            node.outputs[2]


# ------------------------------------------------------ 4.x / 5.x compat
# Blender 5.0 moved scene compositing to a node-group datablock
# (scene.compositing_node_group) terminated by a Group Output node;
# 4.x uses the embedded scene.node_tree terminated by a Composite node.
def _has_group_compositor(scene):
    return hasattr(scene, "compositing_node_group")


def _get_comp_tree(scene):
    """The scene's compositor tree, or None if compositing is off."""
    if _has_group_compositor(scene):
        return scene.compositing_node_group
    if not scene.use_nodes:
        return None
    return scene.node_tree


def _ensure_comp_tree(scene, prev):
    """Get-or-create the scene compositor tree; records creation in prev."""
    if _has_group_compositor(scene):
        tree = scene.compositing_node_group
        if tree is None:
            tree = bpy.data.node_groups.new("Compositing Nodes",
                                            'CompositorNodeTree')
            tree.interface.new_socket("Image", in_out='OUTPUT',
                                      socket_type='NodeSocketColor')
            gout = tree.nodes.new('NodeGroupOutput')
            gout.location = (600, 0)
            rl = tree.nodes.new('CompositorNodeRLayers')
            rl.location = (0, 0)
            tree.links.new(rl.outputs["Image"], gout.inputs[0])
            scene.compositing_node_group = tree
            prev["created_tree"] = True
        return tree
    if not scene.use_nodes:
        scene.use_nodes = True  # Blender auto-adds RLayers + Composite
        prev["created_tree"] = True
    return scene.node_tree


def _drop_created_comp_tree(scene):
    """Undo _ensure_comp_tree's creation (the use_nodes=False equivalent)."""
    if _has_group_compositor(scene):
        tree = scene.compositing_node_group
        scene.compositing_node_group = None
        if tree is not None and tree.users == 0:
            bpy.data.node_groups.remove(tree)
    else:
        scene.use_nodes = False


def _terminal_dest(tree, prev=None):
    """The final-output input socket of a compositor tree.

    4.x: the Composite node's Image input. 5.x: the Group Output node's
    first real input (interface socket created if missing). Creates the
    terminal node if absent, recording it in prev.
    """
    node = next((n for n in tree.nodes if n.type == 'COMPOSITE'), None)
    if node is not None:
        return node, node.inputs["Image"]
    gout = next((n for n in tree.nodes if n.type == 'GROUP_OUTPUT'), None)
    if gout is None:
        try:
            node = tree.nodes.new('CompositorNodeComposite')
            node.location = (600, 0)
            if prev is not None:
                prev["created_composite"] = True
            return node, node.inputs["Image"]
        except RuntimeError:  # 5.x: Composite node removed
            gout = tree.nodes.new('NodeGroupOutput')
            gout.location = (600, 0)
            if prev is not None:
                prev["created_composite"] = True
    dest = next((s for s in gout.inputs
                 if s.bl_idname != 'NodeSocketVirtual'), None)
    if dest is None:
        tree.interface.new_socket("Image", in_out='OUTPUT',
                                  socket_type='NodeSocketColor')
        dest = next((s for s in gout.inputs
                     if s.bl_idname != 'NodeSocketVirtual'), None)
    return gout, dest


# ================================================================== pipeline
def _pipeline_tree(scene):
    """Per-scene pipeline group tree (WB gain + look slot)."""
    name = f"{K.PIPELINE_GROUP} ({scene.name})"
    for tree in bpy.data.node_groups:
        if (tree.get(K.TAG) == "pipeline"
                and tree.get("cinekit_scene") == scene.name):
            return tree
    tree = bpy.data.node_groups.new(name, 'CompositorNodeTree')
    tree[K.TAG] = "pipeline"
    tree["cinekit_scene"] = scene.name
    tree.interface.new_socket("Image", in_out='INPUT',
                              socket_type='NodeSocketColor')
    wb_in = tree.interface.new_socket("White Balance", in_out='INPUT',
                                      socket_type='NodeSocketColor')
    wb_in.default_value = _WHITE
    tree.interface.new_socket("Image", in_out='OUTPUT',
                              socket_type='NodeSocketColor')

    gin = tree.nodes.new('NodeGroupInput')
    gin.location = (-400, 0)
    wb, fac_in, a_in, b_in, wb_out = _new_mix(tree, blend='MULTIPLY')
    fac_in.default_value = 1.0
    wb.label = "CK White Balance"
    wb[K.TAG] = "wb"
    wb.location = (-150, 0)
    gout = tree.nodes.new('NodeGroupOutput')
    gout.location = (350, 0)
    tree.links.new(gin.outputs["Image"], a_in)
    tree.links.new(gin.outputs["White Balance"], b_in)
    tree.links.new(wb_out, gout.inputs["Image"])
    return tree


def _pipeline_wb_out(ptree):
    """Output socket of the WB node inside the pipeline tree."""
    wb = _find_node(ptree, "wb")
    if wb is None:
        return None
    if wb.bl_idname == 'ShaderNodeMix':
        return wb.outputs[2]
    return wb.outputs[0]


def _find_pipeline_node(scene):
    tree = _get_comp_tree(scene)
    if tree is None:
        return None
    node = _find_node(tree, "pipeline")
    if node is not None and node.type == 'GROUP':
        return node
    return None


def ensure_pipeline(scene):
    """Insert (or return) the pipeline group node, preserving user links."""
    if utils.is_linked(scene):
        raise CKError(f"Scene '{scene.name}' is linked — cannot edit its "
                      "compositor")
    node = _find_pipeline_node(scene)
    if node is not None:
        if node.node_tree is None:  # user cleared it; heal
            node.node_tree = _pipeline_tree(scene)
        return node

    prev = {"created_tree": False, "created_composite": False,
            "created_rlayers": False, "had_link": False,
            "from_node": "", "from_socket": 0}
    nt = _ensure_comp_tree(scene, prev)
    if nt is None:
        raise CKError("Scene has no compositor node tree")
    if utils.is_linked(nt):
        raise CKError("The scene's compositor node tree is linked — "
                      "make it local first")
    terminal, dest = _terminal_dest(nt, prev)
    if dest is None:
        raise CKError("Could not find the compositor output socket")

    source_socket = None
    if dest.is_linked:
        link = dest.links[0]
        prev["had_link"] = True
        prev["from_node"] = link.from_node.name
        prev["from_socket"] = list(link.from_node.outputs).index(
            link.from_socket)
        source_socket = link.from_socket
        nt.links.remove(link)
    else:
        rl = next((n for n in nt.nodes if n.type == 'R_LAYERS'), None)
        if rl is None:
            rl = nt.nodes.new('CompositorNodeRLayers')
            rl.location = (terminal.location.x - 600, 0)
            prev["created_rlayers"] = True
        source_socket = rl.outputs["Image"]

    node = nt.nodes.new('CompositorNodeGroup')
    node.node_tree = _pipeline_tree(scene)
    node.label = "CineKit Pipeline"
    node[K.TAG] = "pipeline"
    node.location = (terminal.location.x - 250, terminal.location.y)
    nt.links.new(source_socket, node.inputs["Image"])
    nt.links.new(node.outputs["Image"], dest)
    utils.store_state(scene, K.PREV_COMP, prev)
    return node


def _maybe_cleanup_pipeline(scene):
    """Remove the pipeline entirely when it does nothing (no look, WB off),
    restoring the exact prior compositor connections."""
    node = _find_pipeline_node(scene)
    if node is None:
        return
    ptree = node.node_tree
    if ptree is not None and _find_node(ptree, "look") is not None:
        return
    wb = tuple(node.inputs["White Balance"].default_value)[:3] \
        if "White Balance" in node.inputs else (1, 1, 1)
    if any(abs(c - 1.0) > 1e-4 for c in wb):
        return

    nt = _get_comp_tree(scene)
    prev = utils.fetch_state(scene, K.PREV_COMP) or {}
    terminal, dest = _terminal_dest(nt)
    nt.nodes.remove(node)
    if ptree is not None and ptree.users == 0:
        bpy.data.node_groups.remove(ptree)

    if prev.get("created_tree"):
        _drop_created_comp_tree(scene)
    elif dest is not None and prev.get("had_link"):
        from_node = nt.nodes.get(prev.get("from_node", ""))
        if from_node is not None:
            outs = list(from_node.outputs)
            idx = prev.get("from_socket", 0)
            if 0 <= idx < len(outs):
                nt.links.new(outs[idx], dest)
    elif dest is not None:
        # We created the source RLayers: leave a working chain rather than
        # a dead end.
        rl = next((n for n in nt.nodes if n.type == 'R_LAYERS'), None)
        if rl is not None and not dest.is_linked:
            nt.links.new(rl.outputs["Image"], dest)
    utils.clear_state(scene, K.PREV_COMP)


def set_white_balance(scene, gains):
    """gains: (r, g, b) multipliers or None to neutralise/remove."""
    if gains is None:
        node = _find_pipeline_node(scene)
        if node is not None and "White Balance" in node.inputs:
            node.inputs["White Balance"].default_value = _WHITE
            _maybe_cleanup_pipeline(scene)
        return
    node = ensure_pipeline(scene)
    node.inputs["White Balance"].default_value = (*gains, 1.0)


# ============================================================== look apply
def apply_look(scene, look_id):
    """Apply a look to the scene: compositor + camera + color layers.
    Idempotent; fast in-place update when the look type is unchanged."""
    look = get_look(look_id)
    if look is None:
        raise CKError(f"Unknown look '{look_id}' — check the Looks panel "
                      "for load errors")
    pipeline = ensure_pipeline(scene)
    ptree = pipeline.node_tree
    look_node = _find_node(ptree, "look")
    same_type = (look_node is not None and look_node.node_tree is not None
                 and look_node.node_tree.get(K.TAG_LOOK) == look_id)

    if not same_type:
        if scene.cinekit.active_look and scene.cinekit.active_look != look_id:
            _restore_look_camera(scene)
            _restore_look_color(scene)
        if look_node is not None:
            old_tree = look_node.node_tree
            ptree.nodes.remove(look_node)
            if old_tree is not None and old_tree.users == 0:
                _remove_look_tree(old_tree)
        tree = _build_look_tree(look, scene)
        look_node = ptree.nodes.new('CompositorNodeGroup')
        look_node.node_tree = tree
        look_node.label = f"CK Look: {look['name']}"
        look_node[K.TAG] = "look"
        look_node.location = (120, 0)
        wb_out = _pipeline_wb_out(ptree)
        gout = next(n for n in ptree.nodes if n.type == 'GROUP_OUTPUT')
        # Re-route wb -> look -> out (drop wb -> out link).
        for link in list(ptree.links):
            if link.to_node == gout:
                ptree.links.remove(link)
        ptree.links.new(wb_out, look_node.inputs["Image"])
        ptree.links.new(look_node.outputs["Image"], gout.inputs["Image"])

    _apply_look_camera(scene, look)
    _apply_look_color(scene, look)
    if (same_type and scene.cinekit.active_look == look_id
            and len(scene.cinekit.look_params) == len(look["chain"])):
        # Re-apply of the unchanged look (e.g. shot switch): keep the
        # user's tweaked parameter values, just push them to the sockets.
        for param in scene.cinekit.look_params:
            push_param(scene, param)
        set_intensity(scene)
    else:
        _populate_params(scene, look, look_node)
    scene.cinekit.active_look = look_id


def remove_look(scene):
    """Remove the active look; restore camera, color and compositor state."""
    _restore_look_camera(scene)
    _restore_look_color(scene)
    node = _find_pipeline_node(scene)
    if node is not None and node.node_tree is not None:
        ptree = node.node_tree
        look_node = _find_node(ptree, "look")
        if look_node is not None:
            tree = look_node.node_tree
            ptree.nodes.remove(look_node)
            if tree is not None and tree.users == 0:
                _remove_look_tree(tree)
    scene.cinekit.active_look = ""
    scene.cinekit.look_params.clear()
    _maybe_cleanup_pipeline(scene)


def _remove_look_tree(tree):
    """Delete a look group tree and its private textures/images (hygiene)."""
    look_id = tree.get(K.TAG_LOOK, "")
    bpy.data.node_groups.remove(tree)
    prefix = f"CK_{look_id}_"
    for tex in [t for t in bpy.data.textures
                if t.name.startswith(prefix) and t.users == 0]:
        bpy.data.textures.remove(tex)
    for img in [i for i in bpy.data.images
                if i.name.startswith(prefix) and i.users == 0]:
        bpy.data.images.remove(img)


def set_intensity(scene):
    node = _find_pipeline_node(scene)
    if node is None or node.node_tree is None:
        return
    look_node = _find_node(node.node_tree, "look")
    if look_node is not None and "Intensity" in look_node.inputs:
        look_node.inputs["Intensity"].default_value = \
            scene.cinekit.look_intensity


def push_param(scene, param):
    """Write one scene param to its group-input socket. In place, no
    rebuild."""
    node = _find_pipeline_node(scene)
    if node is None or node.node_tree is None:
        return
    look_node = _find_node(node.node_tree, "look")
    if look_node is not None and param.name in look_node.inputs:
        look_node.inputs[param.name].default_value = param.value


def _populate_params(scene, look, look_node):
    ck = scene.cinekit
    with utils.suppress_updates():
        ck.look_params.clear()
        for entry in look["chain"]:
            socket = schema.ARTIFACTS[entry["type"]]["socket"]
            param = ck.look_params.add()
            param.name = socket
            param.label = socket
            param.target = "group"
            param.vmin, param.vmax = 0.0, 2.0
            param.value = entry["strength"]
            if socket in look_node.inputs:
                look_node.inputs[socket].default_value = entry["strength"]
        if "Intensity" in look_node.inputs:
            look_node.inputs["Intensity"].default_value = ck.look_intensity


# ------------------------------------------------------------- camera layer
def _apply_look_camera(scene, look):
    """The layer that makes a look more than a filter: real sensor size,
    aspect crop and forced deep DOF on the shot camera — visible in any
    viewport, not just the render."""
    cam_obj = scene.camera
    if cam_obj is None or cam_obj.type != 'CAMERA':
        return
    cam = cam_obj.data
    if utils.is_linked(cam):
        print(f"CineKit: camera '{cam.name}' is linked; look camera layer "
              "skipped")
        return
    spec = look["camera"]
    ck = cam.cinekit

    if utils.fetch_state(cam, K.PREV_LOOK_CAMERA) is None:
        utils.store_state(cam, K.PREV_LOOK_CAMERA, {
            "sensor_width": cam.sensor_width,
            "sensor_height": cam.sensor_height,
            "sensor_fit": cam.sensor_fit,
            "aperture": ck.aperture,
            "dof_fstop": cam.dof.aperture_fstop,
            "breath_scale": float(cam.get("cinekit_breath_scale", 1.0)),
        })
    if utils.fetch_state(scene, K.PREV_RENDER) is None:
        utils.store_state(scene, K.PREV_RENDER, {
            "res_x": scene.render.resolution_x,
            "res_y": scene.render.resolution_y,
        })

    if spec["sensor_width"]:
        cam.sensor_fit = 'HORIZONTAL'
        cam.sensor_width = spec["sensor_width"]
    ratio = schema.aspect_ratio(spec["aspect"])
    if ratio:
        cam.sensor_fit = 'HORIZONTAL'
        cam.sensor_height = cam.sensor_width / ratio
        res_x = scene.render.resolution_x
        scene.render.resolution_y = int(round(res_x / ratio / 2.0)) * 2

    if spec["min_fstop"]:
        if ck.enabled:
            if ck.aperture < spec["min_fstop"]:
                ck.aperture = spec["min_fstop"]  # routed through apply layer
        elif cam.dof.aperture_fstop < spec["min_fstop"]:
            cam.dof.aperture_fstop = spec["min_fstop"]

    cam["cinekit_breath_scale"] = spec["breathing_scale"]
    if ck.focus_breathing:
        from .. import rig
        try:
            rig.set_breathing(cam)
        except CKError:
            pass


def _restore_look_camera(scene):
    for obj in scene.objects:
        if obj.type != 'CAMERA':
            continue
        cam = obj.data
        prev = utils.fetch_state(cam, K.PREV_LOOK_CAMERA)
        if prev is None:
            continue
        cam.sensor_width = prev["sensor_width"]
        cam.sensor_height = prev["sensor_height"]
        cam.sensor_fit = prev["sensor_fit"]
        cam.dof.aperture_fstop = prev["dof_fstop"]
        cam["cinekit_breath_scale"] = prev["breath_scale"]
        if cam.cinekit.enabled:
            cam.cinekit.aperture = prev["aperture"]
        utils.clear_state(cam, K.PREV_LOOK_CAMERA)
    prev_r = utils.fetch_state(scene, K.PREV_RENDER)
    if prev_r is not None:
        scene.render.resolution_x = prev_r["res_x"]
        scene.render.resolution_y = prev_r["res_y"]
        utils.clear_state(scene, K.PREV_RENDER)


# -------------------------------------------------------------- color layer
def _apply_look_color(scene, look):
    """Only touch the user's view transform when the look explicitly sets
    one; store the previous transform for exact restore."""
    spec = look["color"]
    vs = scene.view_settings
    if spec["view_transform"] or spec["look"]:
        if utils.fetch_state(scene, K.PREV_COLOR) is None:
            utils.store_state(scene, K.PREV_COLOR, {
                "view_transform": vs.view_transform,
                "look": vs.look,
            })
        if spec["view_transform"]:
            try:
                vs.view_transform = spec["view_transform"]
            except TypeError:
                print(f"CineKit: view transform "
                      f"'{spec['view_transform']}' not available in this "
                      "OCIO config; keeping current")
        if spec["look"]:
            try:
                vs.look = spec["look"]
            except TypeError:
                pass
    # Exposure bias rides on top of the physical-camera exposure.
    bias = spec["exposure_bias"]
    old_bias = float(scene.get("cinekit_look_bias", 0.0))
    if abs(bias - old_bias) > 1e-6:
        scene["cinekit_look_bias"] = bias
        cam = scene.camera
        if cam and cam.type == 'CAMERA' and cam.data.cinekit.enabled:
            from .. import rig
            rig.sync_active_camera(scene)
        else:
            vs.exposure += bias - old_bias


def _restore_look_color(scene):
    vs = scene.view_settings
    prev = utils.fetch_state(scene, K.PREV_COLOR)
    if prev is not None:
        try:
            vs.view_transform = prev["view_transform"]
            vs.look = prev["look"]
        except TypeError:
            pass
        utils.clear_state(scene, K.PREV_COLOR)
    old_bias = float(scene.get("cinekit_look_bias", 0.0))
    if old_bias:
        cam = scene.camera
        if cam and cam.type == 'CAMERA' and cam.data.cinekit.enabled:
            scene["cinekit_look_bias"] = 0.0
            from .. import rig
            rig.sync_active_camera(scene)
        else:
            vs.exposure -= old_bias
    if "cinekit_look_bias" in scene.keys():
        del scene["cinekit_look_bias"]


# ============================================================ group building
class _Builder:
    """Helper context for artifact builders: node creation with auto-layout,
    exposed strength sockets multiplied by the master Intensity, and
    era-agnostic node helpers (legacy 4.x nodes / unified 5.x nodes)."""

    def __init__(self, tree, look, scene):
        self.t = tree
        self.look = look
        self.scene = scene
        self.x = 0.0
        self.gin = tree.nodes.new('NodeGroupInput')
        self.gin.location = (-300, 0)
        self.gout = tree.nodes.new('NodeGroupOutput')
        sock = tree.interface.new_socket("Intensity", in_out='INPUT',
                                         socket_type='NodeSocketFloat')
        sock.default_value, sock.min_value, sock.max_value = 1.0, 0.0, 1.0

    # ------------------------------------------------------------ plumbing
    def _place(self, node):
        node.location = (self.x, -50.0 * (len(self.t.nodes) % 5))
        self.x += 170.0
        return node

    def n(self, node_type, **settings):
        node = self._place(self.t.nodes.new(node_type))
        for attr, value in settings.items():
            _set_first(node, [attr], value)
        return node

    def link(self, out_sock, in_sock):
        self.t.links.new(out_sock, in_sock)

    def _plug(self, value, in_sock):
        if hasattr(value, "is_linked"):
            self.link(value, in_sock)
        else:
            in_sock.default_value = value

    def expose(self, name, default, vmin=0.0, vmax=2.0):
        sock = self.t.interface.new_socket(
            name, in_out='INPUT', socket_type='NodeSocketFloat')
        sock.default_value = default
        sock.min_value, sock.max_value = vmin, vmax
        return self.gin.outputs[name]

    def fac(self, socket_name, default=1.0):
        """Exposed per-artifact strength * master Intensity."""
        strength = self.expose(socket_name, default)
        mul = self.math('MULTIPLY', strength,
                        self.gin.outputs["Intensity"])
        return mul

    # -------------------------------------------------- era-agnostic nodes
    def mix(self, fac_sock, orig, processed, blend='MIX', clamp=False):
        node, fac_in, a_in, b_in, out = _new_mix(self.t, blend, clamp)
        self._place(node)
        self._plug(fac_sock, fac_in)
        self._plug(orig, a_in)
        self._plug(processed, b_in)
        return out

    def math(self, op, a, b=None, clamp=False):
        node = _try_new(self.t, 'CompositorNodeMath', 'ShaderNodeMath')
        self._place(node)
        node.operation = op
        node.use_clamp = clamp
        for i, val in enumerate((a, b)):
            if val is not None:
                self._plug(val, node.inputs[i])
        return node.outputs[0]

    def cxyz(self, x=0.0, y=0.0, z=0.0):
        node = _try_new(self.t, 'CompositorNodeCombineXYZ',
                        'ShaderNodeCombineXYZ')
        self._place(node)
        for i, val in enumerate((x, y, z)):
            self._plug(val, node.inputs[i])
        return node.outputs[0]

    def rgb(self, color):
        node = self.n('CompositorNodeRGB')
        node.outputs[0].default_value = (*color, 1.0)
        return node.outputs[0]

    def blur(self, img, px, py=None, fast=True):
        py = px if py is None else py
        node = self.n('CompositorNodeBlur')
        self.link(img, node.inputs[0])
        if hasattr(node, "filter_type"):  # 4.x
            node.filter_type = 'FAST_GAUSS' if fast else 'GAUSS'
            node.size_x, node.size_y = int(px), int(py)
            if "Size" in node.inputs:
                node.inputs["Size"].default_value = 1.0
        else:  # 5.x: Size is a pixel vector input, Type a menu socket
            node.inputs["Size"].default_value = (px, py)
            _set_first(node, ["Type"],
                       'Fast Gaussian' if fast else 'Gaussian')
        return node.outputs[0]

    def scale_rel(self, img, fx, fy):
        node = self.n('CompositorNodeScale')
        if hasattr(node, "space"):  # 4.x
            node.space = 'RELATIVE'
        else:
            _set_first(node, ["Type"], 'Relative')
        node.inputs["X"].default_value = fx
        node.inputs["Y"].default_value = fy
        self.link(img, node.inputs[0])
        return node.outputs[0]

    def scale_render_size(self, img):
        node = self.n('CompositorNodeScale')
        if hasattr(node, "space"):  # 4.x
            node.space = 'RENDER_SIZE'
            node.frame_method = 'STRETCH'
        else:
            _set_first(node, ["Type"], 'Render Size')
            _set_first(node, ["Frame Type"], 'Stretch')
        self.link(img, node.inputs[0])
        return node.outputs[0]

    def mask(self, kind, x, y, w, h):
        node = self.n(kind)
        if "Position" in node.inputs:  # 5.x
            node.inputs["Position"].default_value = (x, y)
            node.inputs["Size"].default_value = (w, h)
        else:  # 4.x property spellings vary
            _set_first(node, ["x", "mask_x"], x)
            _set_first(node, ["y", "mask_y"], y)
            _set_first(node, ["width", "mask_width"], w)
            _set_first(node, ["height", "mask_height"], h)
        return node.outputs[0]

    def alpha_over(self, fac_sock, background, foreground):
        node = self.n('CompositorNodeAlphaOver')
        if "Background" in node.inputs:  # 5.x
            self._plug(background, node.inputs["Background"])
            self._plug(foreground, node.inputs["Foreground"])
            self._plug(fac_sock, node.inputs["Factor"])
        else:  # 4.x: Fac, bg, fg
            self._plug(fac_sock, node.inputs[0])
            self._plug(background, node.inputs[1])
            self._plug(foreground, node.inputs[2])
        return node.outputs[0]

    def displace(self, img, offset_x_sock, offset_y=0.0):
        vec = self.cxyz(offset_x_sock, offset_y, 0.0)
        node = self.n('CompositorNodeDisplace')
        self.link(img, node.inputs[0])
        self.link(vec, node.inputs[1])
        if "X Scale" in node.inputs:  # 4.x scales the vector; keep 1:1 px
            node.inputs["X Scale"].default_value = 1.0
            node.inputs["Y Scale"].default_value = 1.0
        return node.outputs[0]

    def driver(self, node, socket_index, expression, component=-1):
        path = f'nodes["{node.name}"].inputs[{socket_index}].default_value'
        utils.add_driver(self.t, path, expression, [], index=component)

    # -------------------------------------------------- animated textures
    # 4.x: legacy Texture node + bpy.data.textures (tagged, cleaned up).
    # 5.x: unified texture nodes fed by Image Coordinates; animation drives
    # the 4th noise dimension (W) with the built-in 'frame' variable.
    def _legacy_tex_node(self, kind, name_suffix, noise_size=0.25,
                         wood_bands=False):
        name = f"CK_{self.look['id']}_{name_suffix}"
        tex = bpy.data.textures.get(name)
        if tex is None or tex.type != kind:
            if tex is not None:
                bpy.data.textures.remove(tex)
            tex = bpy.data.textures.new(name, kind)
        tex[K.TAG] = self.look["id"]
        if kind == 'CLOUDS':
            tex.noise_scale = noise_size
        if kind == 'WOOD' and wood_bands:
            tex.wood_type = 'BANDS'
        node = self.n('CompositorNodeTexture')
        node.texture = tex
        return node

    def coords(self, img, aniso=None):
        """Normalized image coordinates (5.x), optionally stretched."""
        node = self.n('CompositorNodeImageCoordinates')
        self.link(img, node.inputs[0])
        out = node.outputs["Normalized"]
        if aniso is not None:
            vm = self.n('ShaderNodeVectorMath', operation='MULTIPLY')
            self.link(out, vm.inputs[0])
            vm.inputs[1].default_value = (aniso[0], aniso[1], 1.0)
            out = vm.outputs[0]
        return out

    def tex_white(self, name_suffix, img):
        """Per-frame white noise value in [0, 1]."""
        try:
            node = self._legacy_tex_node('NOISE', name_suffix)
            return node.outputs["Value"]  # legacy NOISE re-randomises
        except RuntimeError:
            node = self.n('ShaderNodeTexWhiteNoise')
            node.noise_dimensions = '4D'
            self.link(self.coords(img), node.inputs["Vector"])
            self.driver(node, 1, "frame * 991.7")  # W: new seed per frame
            return node.outputs["Value"]

    def tex_clouds(self, name_suffix, img, features, aniso=None, speed=0.0):
        """Fractal noise value; `features` ~ cells across the frame,
        `aniso` stretches (x, y), `speed` animates over frames."""
        try:
            node = self._legacy_tex_node('CLOUDS', name_suffix,
                                         noise_size=1.0 / max(features, 1e-3))
            if aniso is not None:
                node.inputs[1].default_value = (aniso[0], aniso[1], 1.0)
            if speed:
                self.driver(node, 0, f"frame * {speed:.5f}", component=1)
            return node.outputs["Value"]
        except (RuntimeError, CKError):
            node = self.n('ShaderNodeTexNoise')
            node.noise_dimensions = '4D'
            self.link(self.coords(img, aniso), node.inputs["Vector"])
            node.inputs["Scale"].default_value = float(features)
            node.inputs["Detail"].default_value = 2.0
            if speed:
                self.driver(node, 1, f"frame * {speed:.5f}")  # W input
            return node.outputs[0]  # Factor

    def tex_bands(self, name_suffix, img, lines, flip_expression=None):
        """Horizontal line pattern (interlace); phase can flip per frame."""
        try:
            node = self._legacy_tex_node('WOOD', name_suffix,
                                         wood_bands=True)
            node.inputs[1].default_value = (0.0, lines, 0.0)
            if flip_expression:
                utils.add_driver(
                    self.t,
                    f'nodes["{node.name}"].inputs[0].default_value',
                    flip_expression, [], index=1)
            return node.outputs["Value"]
        except (RuntimeError, CKError):
            node = self.n('ShaderNodeTexWave')
            node.wave_type = 'BANDS'
            node.bands_direction = 'Y'
            self.link(self.coords(img), node.inputs["Vector"])
            node.inputs["Scale"].default_value = float(lines)
            if flip_expression:
                phase = node.inputs["Phase Offset"]
                idx = list(node.inputs).index(phase)
                self.driver(node, idx, f"({flip_expression}) * 3.14159")
            return node.outputs[0]  # Color (grayscale)


def _build_look_tree(look, scene):
    """Build (or rebuild) the CK_Look_<Name> group from look data."""
    name = f"{K.LOOK_GROUP_PREFIX}{look['name']}"
    tree = bpy.data.node_groups.get(name)
    if tree is not None:
        tree.nodes.clear()
        if tree.animation_data:
            tree.animation_data_clear()
        for item in list(tree.interface.items_tree):
            tree.interface.remove(item)
    else:
        tree = bpy.data.node_groups.new(name, 'CompositorNodeTree')
    tree[K.TAG_LOOK] = look["id"]
    tree.interface.new_socket("Image", in_out='INPUT',
                              socket_type='NodeSocketColor')
    tree.interface.new_socket("Image", in_out='OUTPUT',
                              socket_type='NodeSocketColor')

    b = _Builder(tree, look, scene)
    img = b.gin.outputs["Image"]
    for entry in look["chain"]:
        builder = _BUILDERS.get(entry["type"])
        if builder is None:
            continue
        img = builder(b, img, entry)
    b.gout.location = (b.x + 100, 0)
    b.link(img, b.gout.inputs["Image"])
    return tree


# ----------------------------------------------------------------- builders
def _bld_soft_res(b, img, s):
    res_y = max(b.scene.render.resolution_y, 1)
    f = min(max(s["lines"] / res_y, 0.05), 1.0)
    fx = min(f * 1.15, 1.0)
    down = b.scale_rel(img, fx, f)
    up = b.scale_rel(down, 1.0 / fx, 1.0 / f)
    return b.mix(b.fac("Softness", s["strength"]), img, up)


def _bld_chroma_bleed(b, img, s):
    blurred = b.blur(img, s["blur_px"], 1)
    shift = b.n('CompositorNodeTranslate')
    shift.inputs[1].default_value = float(s["offset_px"])
    b.link(blurred, shift.inputs[0])
    sep_sharp = b.n('CompositorNodeSeparateColor', mode='YCC')
    sep_soft = b.n('CompositorNodeSeparateColor', mode='YCC')
    b.link(img, sep_sharp.inputs[0])
    b.link(shift.outputs[0], sep_soft.inputs[0])
    comb = b.n('CompositorNodeCombineColor', mode='YCC')
    b.link(sep_sharp.outputs[0], comb.inputs[0])   # sharp luma
    b.link(sep_soft.outputs[1], comb.inputs[1])    # smeared chroma
    b.link(sep_soft.outputs[2], comb.inputs[2])
    b.link(sep_sharp.outputs[3], comb.inputs[3])
    return b.mix(b.fac("Chroma Bleed", s["strength"]), img, comb.outputs[0])


def _bld_luma_noise(b, img, s):
    noise = b.tex_white("luma_noise", img)
    centered = b.math('SUBTRACT', noise, 0.5)
    scaled = b.math('MULTIPLY', centered, s["amount"] * 2.0)
    return b.mix(b.fac("Noise", s["strength"]), img, scaled, blend='ADD')


def _bld_dropouts(b, img, s):
    streak = b.tex_clouds("dropout_streaks", img, features=12.5,
                          aniso=(0.03, 6.0), speed=0.61)
    gate = b.tex_white("dropout_gate", img)
    mask = b.math('GREATER_THAN', streak, 1.0 - s["density"] * 0.45)
    gated = b.math('MULTIPLY', mask, b.math('GREATER_THAN', gate, 0.6))
    fac = b.math('MULTIPLY', gated, b.fac("Dropouts", s["strength"]),
                 clamp=True)
    return b.mix(fac, img, b.rgb((0.9, 0.9, 0.9)))


def _bld_wobble(b, img, s):
    barrel = b.n('CompositorNodeLensdist')
    _set_first(barrel, ["Distort", "Distortion"], 0.015)
    b.link(img, barrel.inputs[0])
    noise = b.tex_clouds("wobble", img, features=3.3, aniso=(0.0, 9.0),
                         speed=0.83)
    centered = b.math('SUBTRACT', noise, 0.5)
    amount = b.math('MULTIPLY', s["pixels"] * 2.0,
                    b.fac("Wobble", s["strength"]))
    offset = b.math('MULTIPLY', centered, amount)
    return b.displace(barrel.outputs[0], offset)


def _bld_head_bar(b, img, s):
    h = s["height"]
    mask = b.mask('CompositorNodeBoxMask', 0.5, h / 2.0, 1.5, h)
    shift = b.n('CompositorNodeTranslate')
    b.link(img, shift.inputs[0])
    b.driver(shift, 1, "6.0 + (frame % 2) * 5.0")
    dark = b.mix(0.55, shift.outputs[0], b.rgb((0.08, 0.08, 0.09)),
                 blend='MULTIPLY')
    fac = b.math('MULTIPLY', mask, b.fac("Head Switch", s["strength"]),
                 clamp=True)
    return b.mix(fac, img, dark)


def _bld_sharpen(b, img, s):
    blurred = b.blur(img, s["radius_px"])
    detail = b.mix(1.0, img, blurred, blend='SUBTRACT')
    gain = b.math('MULTIPLY', b.fac("Sharpen", s["strength"]), s["amount"])
    scaled = b.mix(1.0, detail, gain, blend='MULTIPLY')
    return b.mix(1.0, img, scaled, blend='ADD')


def _bld_levels(b, img, s):
    span = max(s["white"] - s["black"], 0.01)
    squashed = b.mix(1.0, img, b.rgb((span,) * 3), blend='MULTIPLY')
    lifted = b.mix(1.0, squashed, b.rgb((s["black"],) * 3), blend='ADD',
                   clamp=True)
    return b.mix(b.fac("Levels", s["strength"]), img, lifted)


def _bld_grain(b, img, s):
    chans = []
    for i, (suffix, size_mul) in enumerate(
            (("grain_r", 1.0), ("grain_g", 0.9), ("grain_b", 1.5))):
        features = 909.0 / max(s["size"] * size_mul, 0.1)
        noise = b.tex_clouds(suffix, img, features=features,
                             speed=0.371 + 0.11 * i)
        centered = b.math('MULTIPLY', b.math('SUBTRACT', noise, 0.5),
                          s["amount"] * (1.3 if i == 2 else 1.0))
        chans.append(b.math('ADD', centered, 0.5))
    comb = b.n('CompositorNodeCombineColor', mode='RGB')
    for i, ch in enumerate(chans):
        b.link(ch, comb.inputs[i])
    comb.inputs[3].default_value = 1.0
    return b.mix(b.fac("Grain", s["strength"]), img, comb.outputs[0],
                 blend='SOFT_LIGHT')


def _bld_weave(b, img, s):
    shift = b.n('CompositorNodeTranslate')
    b.link(img, shift.inputs[0])
    px = s["pixels"]
    b.driver(shift, 1,
             f"{px:.3f} * (sin(frame * 0.91) * 0.6 "
             "+ sin(frame * 2.33 + 1.7) * 0.4)")
    b.driver(shift, 2,
             f"{px:.3f} * (sin(frame * 1.27 + 0.5) * 0.5 "
             "+ sin(frame * 3.11) * 0.5)")
    return b.mix(b.fac("Gate Weave", s["strength"]), img, shift.outputs[0])


def _bld_halation(b, img, s):
    glare = b.n('CompositorNodeGlare')
    b.link(img, glare.inputs[0])
    if "Type" in glare.inputs:  # 5.x: everything is an input socket
        _set_first(glare, ["Type"], 'Fog Glow')
        _set_first(glare, ["Threshold"], s["threshold"])
        _set_first(glare, ["Size"], min(s["size"] / 9.0, 1.0))
        _set_first(glare, ["Strength"], 1.0)
        glow = glare.outputs["Glare"] if "Glare" in glare.outputs \
            else glare.outputs[0]
    else:  # 4.x properties
        glare.glare_type = 'FOG_GLOW'
        _set_first(glare, ["quality"], 'MEDIUM')
        _set_first(glare, ["mix"], 1.0)  # glow only
        _set_first(glare, ["threshold"], s["threshold"])
        _set_first(glare, ["size"], int(s["size"]))
        glow = glare.outputs[0]
    tinted = b.mix(1.0, glow, b.rgb((1.0, 0.38, 0.16)), blend='MULTIPLY')
    screened = b.mix(1.0, img, tinted, blend='SCREEN')
    return b.mix(b.fac("Halation", s["strength"]), img, screened)


def _bld_film_curve(b, img, s):
    c, w = s["contrast"], s["warmth"]
    node = b.n('CompositorNodeCurveRGB')
    combined = node.mapping.curves[3]
    combined.points[0].location = (0.0, 0.005)
    combined.points[1].location = (1.0, 0.985)
    combined.points.new(0.18, 0.18 - c * 0.35 * 0.18)
    combined.points.new(0.82, min(0.82 + c * 0.35 * 0.18, 1.0))
    if abs(w) > 1e-4:
        node.mapping.curves[0].points.new(0.5, 0.5 + w * 0.12)
        node.mapping.curves[2].points.new(0.5, 0.5 - w * 0.12)
    node.mapping.update()
    img_in = node.inputs.get("Image") or node.inputs[1]
    fac_in = (node.inputs.get("Factor") or node.inputs.get("Fac")
              or node.inputs[0])
    b.link(img, img_in)
    fac_in.default_value = 1.0
    return b.mix(b.fac("Tone", s["strength"]), img, node.outputs[0])


def _bld_vignette(b, img, s):
    mask = b.mask('CompositorNodeEllipseMask', 0.5, 0.5, 1.45, 1.15)
    res_y = max(b.scene.render.resolution_y, 64)
    soft = b.blur(mask, res_y * 0.25, res_y * 0.25, fast=True)
    darkened = b.mix(s["amount"], img,
                     b.mix(1.0, img, soft, blend='MULTIPLY'))
    return b.mix(b.fac("Vignette", s["strength"]), img, darkened)


def _bld_color_cast(b, img, s):
    cast = b.mix(1.0, img, b.rgb((s["r"], s["g"], s["b"])), blend='MULTIPLY')
    return b.mix(b.fac("Cast", s["strength"]), img, cast)


def _bld_dust(b, img, s):
    tex = b.tex_clouds("dust", img, features=166.0, speed=1.37)
    specks = b.math('GREATER_THAN', tex, 1.0 - s["density"] * 0.28)
    gate = b.tex_white("dust_gate", img)
    gated = b.math('MULTIPLY', specks, b.math('GREATER_THAN', gate, 0.72))
    fac = b.math('MULTIPLY', gated, b.fac("Dust", s["strength"]), clamp=True)
    return b.mix(fac, img, b.rgb((0.95, 0.93, 0.88)))


def _bld_desaturate(b, img, s):
    node = b.n('CompositorNodeHueSat')
    b.link(img, node.inputs["Image"])
    node.inputs["Saturation"].default_value = 1.0 - s["amount"]
    return b.mix(b.fac("Desaturate", s["strength"]), img, node.outputs[0])


def _bld_gamma(b, img, s):
    node = _try_new(b.t, 'CompositorNodeGamma', 'ShaderNodeGamma')
    b._place(node)
    b.link(img, node.inputs[0])
    node.inputs[1].default_value = s["value"]
    return b.mix(b.fac("Gamma", s["strength"]), img, node.outputs[0])


def _bld_interlace(b, img, s):
    res_y = max(b.scene.render.resolution_y, 1)
    band = b.tex_bands("interlace", img, lines=res_y / 2.0,
                       flip_expression="frame % 2")
    line = b.math('ADD', b.math('MULTIPLY', band, s["amount"]),
                  1.0 - s["amount"])
    lined = b.mix(1.0, img, line, blend='MULTIPLY')
    return b.mix(b.fac("Interlace", s["strength"]), img, lined)


def _bld_timestamp(b, img, s):
    text = b.scene.cinekit.security_timestamp or "CAM 01"
    image = _timestamp_image(b.look["id"], text)
    node = b.n('CompositorNodeImage')
    node.image = image
    fit = b.scale_render_size(node.outputs[0])
    fac = b.fac("Timestamp", s["strength"])
    return b.alpha_over(fac, img, fit)


def _bld_blocking(b, img, s):
    block = max(int(s["block_px"]), 2)
    f = 1.0 / block
    down = b.scale_rel(img, f, f)
    sep = b.n('CompositorNodeSeparateColor', mode='RGB')
    b.link(down, sep.inputs[0])
    comb = b.n('CompositorNodeCombineColor', mode='RGB')
    levels = 18.0
    for i in range(3):
        q = b.math('DIVIDE',
                   b.math('FLOOR',
                          b.math('MULTIPLY', sep.outputs[i], levels)),
                   levels)
        b.link(q, comb.inputs[i])
    b.link(sep.outputs[3], comb.inputs[3])
    up = b.scale_rel(comb.outputs[0], block, block)
    return b.mix(b.fac("Blocking", s["strength"]), img, up)


def _bld_fringing(b, img, s):
    node = b.n('CompositorNodeFilter')
    if "Type" in node.inputs:  # 5.x: Image, Factor, Type(menu)
        _set_first(node, ["Type"], 'Sobel')
        b.link(img, node.inputs[0])
        node.inputs["Factor"].default_value = 1.0
    else:  # 4.x: Fac, Image + filter_type property
        node.filter_type = 'SOBEL'
        node.inputs[0].default_value = 1.0
        b.link(img, node.inputs[1])
    bw = b.n('CompositorNodeRGBToBW')
    b.link(node.outputs[0], bw.inputs[0])
    spread = b.blur(bw.outputs[0], s["width_px"])
    purple = b.mix(1.0, spread, b.rgb((0.45, 0.12, 0.65)), blend='MULTIPLY')
    fringed = b.mix(1.0, img, purple, blend='SCREEN')
    return b.mix(b.fac("Fringing", s["strength"]), img, fringed)


def _bld_shadow_noise(b, img, s):
    luma = b.n('CompositorNodeRGBToBW')
    b.link(img, luma.inputs[0])
    shadow_mask = b.math('SUBTRACT', 0.55, luma.outputs[0], clamp=True)
    sep = b.n('CompositorNodeSeparateColor', mode='YCC')
    b.link(img, sep.inputs[0])
    comb = b.n('CompositorNodeCombineColor', mode='YCC')
    b.link(sep.outputs[0], comb.inputs[0])
    for i, suffix in ((1, "shadow_cb"), (2, "shadow_cr")):
        noise_val = b.tex_clouds(suffix, img, features=250.0,
                                 speed=0.53 + 0.17 * i)
        noise = b.math('MULTIPLY', b.math('SUBTRACT', noise_val, 0.5),
                       s["amount"])
        masked = b.math('MULTIPLY', noise, shadow_mask)
        b.link(b.math('ADD', sep.outputs[i], masked), comb.inputs[i])
    b.link(sep.outputs[3], comb.inputs[3])
    return b.mix(b.fac("Shadow Noise", s["strength"]), img, comb.outputs[0])


def _bld_highlight_bloom(b, img, s):
    luma = b.n('CompositorNodeRGBToBW')
    b.link(img, luma.inputs[0])
    mask = b.math('GREATER_THAN', luma.outputs[0], s["threshold"])
    soft = b.blur(mask, 6)
    tint = b.mix(1.0, soft, b.rgb((1.0, 1.0 - s["magenta"], 1.0)),
                 blend='MULTIPLY')
    bloomed = b.mix(1.0, img, tint, blend='ADD')
    return b.mix(b.fac("Highlight Bloom", s["strength"]), img, bloomed)


_BUILDERS = {
    "soft_res": _bld_soft_res,
    "chroma_bleed": _bld_chroma_bleed,
    "luma_noise": _bld_luma_noise,
    "dropouts": _bld_dropouts,
    "wobble": _bld_wobble,
    "head_bar": _bld_head_bar,
    "sharpen": _bld_sharpen,
    "levels": _bld_levels,
    "grain": _bld_grain,
    "weave": _bld_weave,
    "halation": _bld_halation,
    "film_curve": _bld_film_curve,
    "vignette": _bld_vignette,
    "color_cast": _bld_color_cast,
    "dust": _bld_dust,
    "desaturate": _bld_desaturate,
    "gamma": _bld_gamma,
    "interlace": _bld_interlace,
    "timestamp": _bld_timestamp,
    "blocking": _bld_blocking,
    "fringing": _bld_fringing,
    "shadow_noise": _bld_shadow_noise,
    "highlight_bloom": _bld_highlight_bloom,
}


# ------------------------------------------------------- timestamp bitmap
# 5x7 bitmap font (subset), rows top->bottom, 5 bits each.
_FONT = {
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    ":": (0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x00),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C),
    "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
    "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    " ": (0, 0, 0, 0, 0, 0, 0),
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1E),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
}


def _timestamp_image(look_id, text):
    """Render text into a generated image datablock with a 5x7 bitmap font.
    No external files, so nothing absolute gets baked into the .blend."""
    name = f"CK_{look_id}_timestamp"
    width, height = 960, 540
    px_scale = 3
    img = bpy.data.images.get(name)
    if img is None or img.size[0] != width:
        if img is not None:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(name, width, height, alpha=True)
    img[K.TAG] = look_id

    buf = bytearray(width * height * 4)  # transparent black
    text = text.upper()[:40]
    origin_x, origin_y = 40, height - 40 - 7 * px_scale
    cx = origin_x
    for char in text:
        glyph = _FONT.get(char, _FONT[" "])
        for row in range(7):
            bits = glyph[row]
            for col in range(5):
                if not bits & (1 << (4 - col)):
                    continue
                for dy in range(px_scale):
                    y = origin_y + (6 - row) * px_scale + dy
                    base = (y * width + cx + col * px_scale) * 4
                    for dx in range(px_scale):
                        i = base + dx * 4
                        buf[i:i + 4] = b"\xff\xff\xff\xff"
        cx += 6 * px_scale
    img.pixels.foreach_set([v / 255.0 for v in buf])
    try:
        img.pack()
    except RuntimeError:
        pass  # stays a generated image; still saved with the .blend
    return img
