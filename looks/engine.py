# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Look engine: builds and maintains the CineKit compositor pipeline.

Design
------
ONE group node ("CineKit Pipeline", per scene) sits in-line between
whatever fed the compositor output and the output itself. CineKit stores
the original link in a scene custom property and restores it exactly on
removal. Inside the pipeline tree: GroupInput -> white-balance gain ->
[look group] -> GroupOutput. The look group is a separate datablock named
CK_Look_<Name>. Each scene has its own pipeline tree and its own look
tree, so two scenes never share look settings.

Fast path: applying the look that is already active only writes the input
sockets of the group node. A rebuild happens when the look type changes
or when the aspect ratio of the render changes.

Light and resolution
--------------------
The compositor works in scene-linear light. Glow effects (halation,
bloom, streak flares, fringing) add light. A Screen blend inverts values
above 1.0, so it turned bright light sources black.

Pixel-size effects (blur radius, chroma offset, weave, sharpen) are
authored for a 1080-line image. A driver reads the render height and the
render percentage and scales them live, so a 50% preview matches the final
render. Soft resolution uses an absolute line count, and interlace uses
real pixel rows.

Blender version compatibility
-----------------------------
Blender 5.x uses scene.compositing_node_group with a Group Output node,
unified nodes (ShaderNodeMix, ShaderNodeMath, TexNoise) and node settings
as input sockets. CineKit targets 5.2 and creates these nodes first.
Blender 4.2-4.x used scene.node_tree with a Composite node and legacy
nodes (CompositorNodeMixRGB, Math, Texture). The fallbacks for that API
stay in the code.

Compositor effects give the same result in Cycles and in EEVEE. The
Viewport Compositor shows the compositor layer in the viewport. The
camera layer (sensor size, deep focus) shows in every viewport.
"""

import json
import math

import bpy

from .. import constants as K
from .. import optics, utils
from ..utils import CKError
from . import get as get_look
from . import schema

_WHITE = (1.0, 1.0, 1.0, 1.0)

# Blender 4.x has the legacy Texture node. Blender 5.x removed it. Probe the
# type once, so the engine never creates texture datablocks it cannot use.
_HAS_LEGACY_TEX = hasattr(bpy.types, "CompositorNodeTexture")

# Render height that the pixel sizes in the look presets were authored for.
_REFERENCE_LINES = 1080.0


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
    """Create the first node type that the tree accepts."""
    for node_type in node_types:
        try:
            return tree.nodes.new(node_type)
        except RuntimeError:
            continue
    raise CKError(f"No usable node type among {node_types} in this "
                  "Blender version")


def _new_mix(tree, blend='MIX', clamp=False):
    """Mix node for both APIs: (node, fac_in, a_in, b_in, out)."""
    try:
        node = tree.nodes.new('ShaderNodeMix')
    except RuntimeError:
        node = tree.nodes.new('CompositorNodeMixRGB')
        node.blend_type = blend
        node.use_clamp = clamp
        return node, node.inputs[0], node.inputs[1], node.inputs[2], \
            node.outputs[0]
    node.data_type = 'RGBA'
    node.blend_type = blend
    node.clamp_result = clamp
    return node, node.inputs[0], node.inputs[6], node.inputs[7], \
        node.outputs[2]


def _scene_uid(scene):
    """A stable id for the scene.

    The name of a scene can change, so CineKit does not use the name. A
    copy of a scene (Scene > New > Full Copy) gets the custom properties
    of the original. So when two scenes have the same id, the copy gets a
    new id. The copy is the scene whose name differs from the stored name.
    """
    uid = scene.get("cinekit_scene_uid")
    if uid:
        twin = next((s for s in bpy.data.scenes if s != scene
                     and s.get("cinekit_scene_uid") == uid), None)
        if twin is None or (
                scene.get("cinekit_scene_name") == scene.name
                and twin.get("cinekit_scene_name") != twin.name):
            if scene.get("cinekit_scene_name") != scene.name:
                scene["cinekit_scene_name"] = scene.name
            return uid
    uid = utils.new_id("scn_")
    scene["cinekit_scene_uid"] = uid
    scene["cinekit_scene_name"] = scene.name
    return uid


def _compositor_shared(scene):
    """True when another scene uses the same compositor node tree."""
    if not _has_group_compositor(scene):
        return False
    tree = scene.compositing_node_group
    return tree is not None and any(
        s != scene and s.compositing_node_group == tree
        for s in bpy.data.scenes)


def _render_aspect(scene):
    render = scene.render
    width = render.resolution_x * render.pixel_aspect_x
    height = max(render.resolution_y * render.pixel_aspect_y, 1e-6)
    return width / height


# ------------------------------------------------------ 4.x / 5.x compat
# Blender 5.0 moved scene compositing to a node-group datablock
# (scene.compositing_node_group). A Group Output node ends the tree.
# Blender 4.x uses scene.node_tree with a Composite node at the end.
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
    """Return the scene compositor tree, or create it. Record this in
    prev."""
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
    """Return the pipeline group tree of the scene (WB gain + look slot)."""
    uid = _scene_uid(scene)
    for tree in bpy.data.node_groups:
        if tree.get(K.TAG) != "pipeline":
            continue
        if tree.get("cinekit_scene_uid") == uid:
            return tree
        # A file from an older CineKit version tags the tree by scene name.
        if (not tree.get("cinekit_scene_uid")
                and tree.get("cinekit_scene") == scene.name):
            tree["cinekit_scene_uid"] = uid
            return tree
    tree = bpy.data.node_groups.new(f"{K.PIPELINE_GROUP} ({scene.name})",
                                    'CompositorNodeTree')
    tree[K.TAG] = "pipeline"
    tree["cinekit_scene_uid"] = uid
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


def _owned(tree, scene):
    """True when the pipeline tree belongs to the scene."""
    uid = _scene_uid(scene)
    owner = tree.get("cinekit_scene_uid")
    if owner:
        return owner == uid
    # Older CineKit versions tagged the tree with the scene name.
    if (tree.get("cinekit_scene", scene.name) == scene.name
            and not utils.is_linked(tree)):
        tree["cinekit_scene_uid"] = uid
        return True
    return False


def _own_pipeline(scene, node):
    """Give the scene its own pipeline tree, and keep the active look.

    A copied scene uses the pipeline tree of the original. Without its
    own tree, a look change in one scene also changes the other scene.
    """
    old = node.node_tree
    new = _pipeline_tree(scene)
    if old is not None and old != new:
        old_look = _find_node(old, "look")
        if (old_look is not None and old_look.node_tree is not None
                and _find_node(new, "look") is None):
            look_node = new.nodes.new('CompositorNodeGroup')
            look_node.node_tree = old_look.node_tree
            look_node.label = old_look.label
            look_node[K.TAG] = "look"
            look_node.location = old_look.location
            for sock in old_look.inputs:
                target = look_node.inputs.get(sock.name)
                if target is not None and hasattr(sock, "default_value"):
                    try:
                        target.default_value = sock.default_value
                    except (TypeError, ValueError):
                        pass
            _connect_look(new, look_node)
    node.node_tree = new


def _scene_pipeline(scene):
    """The pipeline node of the scene, with a tree that only this scene
    uses. None when the scene has no pipeline."""
    node = _find_pipeline_node(scene)
    if node is None or utils.is_linked(scene) or _compositor_shared(scene):
        return node
    if node.node_tree is None or not _owned(node.node_tree, scene):
        _own_pipeline(scene, node)
    return node


def ensure_pipeline(scene):
    """Insert (or return) the pipeline group node, preserving user links."""
    if utils.is_linked(scene):
        raise CKError(f"Scene '{scene.name}' comes from a linked library. "
                      "CineKit cannot edit its compositor.")
    node = _scene_pipeline(scene)
    if node is not None:
        if node.node_tree is None:  # the user cleared it: heal it
            node.node_tree = _pipeline_tree(scene)
        return node

    prev = {"created_tree": False, "created_composite": False,
            "created_rlayers": False, "had_link": False,
            "from_node": "", "from_socket": 0}
    nt = _ensure_comp_tree(scene, prev)
    if nt is None:
        raise CKError("The scene has no compositor node tree.")
    if utils.is_linked(nt):
        raise CKError("The compositor node tree of this scene comes from a "
                      "linked library. Make it local first.")
    terminal, dest = _terminal_dest(nt, prev)
    if dest is None:
        raise CKError("CineKit could not find the compositor output socket.")

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
def _needs_rebuild(tree, scene):
    """True when a look tree no longer matches the scene."""
    if tree is None:
        return True
    if (tree.get("cinekit_scene_uid") != _scene_uid(scene)
            and not _compositor_shared(scene)):
        return True
    built = json.loads(tree.get("cinekit_build", "{}") or "{}")
    return abs(built.get("aspect", 0.0) - _render_aspect(scene)) > 1e-3


def _connect_look(ptree, look_node):
    wb_out = _pipeline_wb_out(ptree)
    gout = next(n for n in ptree.nodes if n.type == 'GROUP_OUTPUT')
    for link in list(ptree.links):
        if link.to_node == gout:
            ptree.links.remove(link)
    ptree.links.new(wb_out, look_node.inputs["Image"])
    ptree.links.new(look_node.outputs["Image"], gout.inputs["Image"])


def _disconnect_look(ptree, look_node):
    """Remove the look node and link the WB output straight to the group
    output, so the pipeline still passes the image."""
    ptree.nodes.remove(look_node)
    gout = next((n for n in ptree.nodes if n.type == 'GROUP_OUTPUT'), None)
    wb_out = _pipeline_wb_out(ptree)
    if gout is not None and wb_out is not None \
            and not gout.inputs["Image"].is_linked:
        ptree.links.new(wb_out, gout.inputs["Image"])


def apply_look(scene, look_id):
    """Apply a look to the scene: camera, compositor and color layers.

    This is idempotent. It updates values in place when the look type
    does not change.
    """
    look = get_look(look_id)
    if look is None:
        raise CKError(f"There is no look named '{look_id}'. Open the Looks "
                      "panel and check the load errors.")
    pipeline = ensure_pipeline(scene)
    ptree = pipeline.node_tree
    look_node = _find_node(ptree, "look")
    current = look_node.node_tree if look_node is not None else None
    same_type = current is not None and current.get(K.TAG_LOOK) == look_id

    if not same_type and scene.cinekit.active_look \
            and scene.cinekit.active_look != look_id:
        _restore_look_camera(scene)
        _restore_look_color(scene)
    # The camera layer runs first: it sets the aspect ratio, and the build
    # reads that ratio.
    _apply_look_camera(scene, look)

    if not same_type or _needs_rebuild(current, scene):
        if look_node is not None:
            _disconnect_look(ptree, look_node)
            if current is not None and current.users == 0:
                _remove_look_tree(current)
        tree = _build_look_tree(look, scene)
        look_node = ptree.nodes.new('CompositorNodeGroup')
        look_node.node_tree = tree
        look_node.label = f"CK Look: {look['name']}"
        look_node[K.TAG] = "look"
        look_node.location = (120, 0)
        _connect_look(ptree, look_node)
    else:
        refresh_timestamp(scene)

    _apply_look_color(scene, look)
    if (scene.cinekit.active_look == look_id
            and len(scene.cinekit.look_params) == len(look["chain"])):
        # The same look again (a shot switch or a rebuild): keep the
        # parameter values of the user, and push them to the sockets.
        for param in scene.cinekit.look_params:
            push_param(scene, param)
        set_intensity(scene)
    else:
        _populate_params(scene, look, look_node)
    scene.cinekit.active_look = look_id


def remove_look(scene):
    """Remove the active look. Restore the camera, color and compositor
    state."""
    _restore_look_camera(scene)
    _restore_look_color(scene)
    node = _scene_pipeline(scene)
    if node is not None and node.node_tree is not None:
        ptree = node.node_tree
        look_node = _find_node(ptree, "look")
        if look_node is not None:
            tree = look_node.node_tree
            _disconnect_look(ptree, look_node)
            if tree is not None and tree.users == 0:
                _remove_look_tree(tree)
    scene.cinekit.active_look = ""
    scene.cinekit.look_params.clear()
    _maybe_cleanup_pipeline(scene)


def _remove_look_tree(tree):
    """Delete a look group tree and its private images and textures."""
    raw = tree.get("cinekit_private")
    look_id = tree.get(K.TAG_LOOK, "")
    bpy.data.node_groups.remove(tree)
    if raw is None:
        # A tree from an older CineKit version has no list. Its datablocks
        # have the name prefix CK_<look id>_.
        prefix = f"CK_{look_id}_"
        for pool in (bpy.data.textures, bpy.data.images):
            for item in [i for i in pool
                         if i.name.startswith(prefix) and i.users == 0]:
                pool.remove(item)
        return
    for kind, name in json.loads(raw or "[]"):
        pool = bpy.data.images if kind == "image" else bpy.data.textures
        item = pool.get(name)
        if item is not None and item.users == 0:
            pool.remove(item)


def set_intensity(scene):
    node = _scene_pipeline(scene)
    if node is None or node.node_tree is None:
        return
    look_node = _find_node(node.node_tree, "look")
    if look_node is not None and "Intensity" in look_node.inputs:
        look_node.inputs["Intensity"].default_value = \
            scene.cinekit.look_intensity


def push_param(scene, param):
    """Write one scene param to its group-input socket. In place, no
    rebuild."""
    node = _scene_pipeline(scene)
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
def _horizontal_sensor(cam, scene):
    """The sensor width that sets the horizontal field of view."""
    aspect = _render_aspect(scene)
    if cam.sensor_fit == 'VERTICAL':
        return cam.sensor_height * aspect
    if cam.sensor_fit == 'AUTO' and aspect < 1.0:
        return cam.sensor_width * aspect
    return cam.sensor_width


def _apply_look_camera(scene, look):
    """The layer that makes a look more than a filter.

    It sets the real sensor size, the aspect ratio and a depth-of-field
    floor on the shot camera. The result shows in every viewport.

    A smaller sensor behind the same lens narrows the view. So CineKit
    scales the focal length by the same factor, and the horizontal field
    of view stays the same. The shot keeps its framing, and the depth of
    field gets deeper, as it does on a real small-sensor camera.
    """
    cam_obj = scene.camera
    if cam_obj is None or cam_obj.type != 'CAMERA':
        return
    cam = cam_obj.data
    if utils.is_linked(cam):
        print(f"CineKit: camera '{cam.name}' comes from a linked "
              "library. CineKit skipped the camera layer of the look.")
        return
    from .. import rig
    spec = look["camera"]
    ck = cam.cinekit

    prev = utils.fetch_state(cam, K.PREV_LOOK_CAMERA)
    if prev is None:
        h_old = _horizontal_sensor(cam, scene)
        lens_scale = 1.0
        if spec["sensor_width"] and h_old > 1e-6:
            lens_scale = spec["sensor_width"] / h_old
        prev = {
            "sensor_width": cam.sensor_width,
            "sensor_height": cam.sensor_height,
            "sensor_fit": cam.sensor_fit,
            "dof_fstop": cam.dof.aperture_fstop,
            "breath_scale": float(cam.get("cinekit_breath_scale", 1.0)),
            "lens_scale": lens_scale,
        }
        utils.store_state(cam, K.PREV_LOOK_CAMERA, prev)
        if abs(lens_scale - 1.0) > 1e-9:
            rig.set_base_lens(cam, rig.base_lens(cam) * lens_scale)

    if spec["sensor_width"]:
        cam.sensor_fit = 'HORIZONTAL'
        cam.sensor_width = spec["sensor_width"]
    ratio = schema.aspect_ratio(spec["aspect"])
    if ratio:
        cam.sensor_fit = 'HORIZONTAL'
        cam.sensor_height = cam.sensor_width / ratio
        render = scene.render
        prev_r = utils.fetch_state(scene, K.PREV_RENDER)
        if prev_r is None:
            prev_r = {"res_x": render.resolution_x,
                      "res_y": render.resolution_y}
        res_y = int(round(render.resolution_x / ratio / 2.0)) * 2
        render.resolution_y = res_y
        prev_r["res_y_set"] = render.resolution_y
        utils.store_state(scene, K.PREV_RENDER, prev_r)

    if spec["min_fstop"]:
        cam[K.LOOK_MIN_FSTOP] = float(spec["min_fstop"])
        if ck.enabled:
            rig.apply_camera(cam, scene)   # DOF only, not the exposure
        elif cam.dof.aperture_fstop < spec["min_fstop"]:
            prev["dof_fstop"] = cam.dof.aperture_fstop
            prev["dof_raised_to"] = float(spec["min_fstop"])
            utils.store_state(cam, K.PREV_LOOK_CAMERA, prev)
            cam.dof.aperture_fstop = spec["min_fstop"]

    cam["cinekit_breath_scale"] = spec["breathing_scale"]
    if ck.focus_breathing:
        try:
            rig.set_breathing(cam)
        except CKError:
            pass


def _restore_camera_data(cam, scene):
    """Give one camera back the values it had before the look."""
    from .. import rig
    prev = utils.fetch_state(cam, K.PREV_LOOK_CAMERA)
    if prev is None or utils.is_linked(cam):
        return
    legacy = "lens_scale" not in prev      # saved by an older CineKit
    cam.sensor_width = prev["sensor_width"]
    cam.sensor_height = prev["sensor_height"]
    cam.sensor_fit = prev["sensor_fit"]
    cam["cinekit_breath_scale"] = prev["breath_scale"]
    scale = prev.get("lens_scale", 1.0)
    if abs(scale - 1.0) > 1e-9:
        # Divide instead of restoring an absolute value, so a focal length
        # change during the look stays.
        rig.set_base_lens(cam, rig.base_lens(cam) / scale)
    if K.LOOK_MIN_FSTOP in cam.keys():
        del cam[K.LOOK_MIN_FSTOP]
    ck = cam.cinekit
    if legacy and "aperture" in prev and ck.enabled:
        # The older versions raised the aperture itself.
        ck.aperture = prev["aperture"]
    if ck.enabled:
        rig.apply_camera(cam, scene)
    elif legacy:
        cam.dof.aperture_fstop = prev["dof_fstop"]
    elif ("dof_raised_to" in prev and abs(cam.dof.aperture_fstop
                                          - prev["dof_raised_to"]) < 1e-4):
        # Only give back the f-stop that the look raised. A value that the
        # user set during the look stays.
        cam.dof.aperture_fstop = prev["dof_fstop"]
    if ck.focus_breathing and not ck.enabled:
        try:
            rig.set_breathing(cam)
        except CKError:
            pass
    utils.clear_state(cam, K.PREV_LOOK_CAMERA)


def _restore_look_camera(scene, only=None):
    """Give back the camera and render values that the look changed.

    only: restore this camera data only, and keep the render size.
    """
    if only is not None:
        _restore_camera_data(only, scene)
        return
    for obj in scene.objects:
        if obj.type == 'CAMERA':
            _restore_camera_data(obj.data, scene)
    prev_r = utils.fetch_state(scene, K.PREV_RENDER)
    if prev_r is not None:
        render = scene.render
        if "res_y_set" not in prev_r:      # saved by an older version
            render.resolution_x = prev_r["res_x"]
            render.resolution_y = prev_r["res_y"]
        elif render.resolution_y == prev_r["res_y_set"]:
            # A height that the user changed during the look stays.
            render.resolution_y = prev_r["res_y"]
        utils.clear_state(scene, K.PREV_RENDER)


def edit_base_camera(scene, cam_data, change):
    """Run change() on the camera as if no look were active.

    A lens or sensor preset describes the camera without the look. So
    CineKit removes the camera layer of the look from this camera, runs
    the change, and then applies the camera layer again.
    """
    look_id = scene.cinekit.active_look if scene is not None else ""
    look = get_look(look_id) if look_id else None
    layered = utils.fetch_state(cam_data, K.PREV_LOOK_CAMERA) is not None
    if look is None or not layered or utils.is_linked(cam_data):
        change()
        return
    _restore_look_camera(scene, only=cam_data)
    change()
    if scene.camera is not None and scene.camera.data == cam_data:
        _apply_look_camera(scene, look)


# -------------------------------------------------------------- color layer
def _apply_look_color(scene, look):
    """Change the view transform only when the look sets one.

    Store the previous transform first, so Remove Look can restore it.
    The exposure bias goes through rig.sync_active_camera, so it works
    the same way with and without a physical camera.
    """
    from .. import rig
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
                      f"'{spec['view_transform']}' is not in this OCIO "
                      "config. CineKit kept the current transform.")
        if spec["look"]:
            try:
                vs.look = spec["look"]
            except TypeError:
                pass
    bias = float(spec["exposure_bias"])
    if abs(bias - rig.look_bias(scene)) > 1e-9:
        if abs(bias) > 1e-9:
            scene[K.LOOK_BIAS] = bias
        elif K.LOOK_BIAS in scene.keys():
            del scene[K.LOOK_BIAS]
        rig.sync_active_camera(scene)


def _restore_look_color(scene):
    from .. import rig
    vs = scene.view_settings
    prev = utils.fetch_state(scene, K.PREV_COLOR)
    if prev is not None:
        try:
            vs.view_transform = prev["view_transform"]
            vs.look = prev["look"]
        except TypeError:
            pass
        utils.clear_state(scene, K.PREV_COLOR)
    if K.LOOK_BIAS not in scene.keys():
        return
    del scene[K.LOOK_BIAS]
    rig.sync_active_camera(scene)


# ============================================================ group building
class _Builder:
    """Helper context for artifact builders.

    It creates nodes with auto-layout and exposes one strength socket per
    effect, multiplied by the master Intensity. It also hides the node
    differences between the Blender 4.x and 5.x compositors.
    """

    def __init__(self, tree, look, scene):
        self.t = tree
        self.look = look
        self.scene = scene
        self.x = 0.0
        self.modern = not _HAS_LEGACY_TEX
        self.private = []            # (kind, name) of datablocks to delete
        self.gin = tree.nodes.new('NodeGroupInput')
        self.gin.location = (-300, 0)
        self.gout = tree.nodes.new('NodeGroupOutput')
        sock = tree.interface.new_socket("Intensity", in_out='INPUT',
                                         socket_type='NodeSocketFloat')
        sock.default_value, sock.min_value, sock.max_value = 1.0, 0.0, 1.0
        self._pxs = None

    @property
    def pxs(self):
        """The pixel scale. The builder creates it at the first use."""
        if self._pxs is None:
            self._pxs = self._pixel_scale()
        return self._pxs

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
        return self.math('MULTIPLY', strength, self.gin.outputs["Intensity"])

    def driven_value(self, expression, label, variables=()):
        """A Value node whose output a driver sets."""
        node = _try_new(self.t, 'ShaderNodeValue', 'CompositorNodeValue')
        self._place(node)
        node.label = label
        utils.add_driver(self.t,
                         f'nodes["{node.name}"].outputs[0].default_value',
                         expression, variables)
        return node

    def _pixel_scale(self):
        """Scale factor for pixel sizes authored at 1080 lines.

        Blender 5.x: a live socket, set by a driver that reads the render
        height and percentage. Blender 4.x: a number taken at build time,
        because its legacy nodes keep sizes in properties, not sockets.
        """
        render = self.scene.render
        now = (render.resolution_y * render.resolution_percentage / 100.0
               / _REFERENCE_LINES)
        if not self.modern:
            return max(now, 0.01)
        node = self.driven_value(
            f"max(ck_res_y * ck_pct / {_REFERENCE_LINES * 100.0:.1f}, 0.01)",
            "CK Pixel Scale",
            [("ck_res_y", self.scene, "render.resolution_y", 'SCENE'),
             ("ck_pct", self.scene, "render.resolution_percentage",
              'SCENE')])
        node.outputs[0].default_value = max(now, 0.01)
        return node.outputs[0]

    def px(self, value):
        """A pixel size authored at 1080 lines, scaled to the render."""
        if not self.modern:
            return value * self.pxs
        return self.math('MULTIPLY', float(value), self.pxs)

    # -------------------------------------------------- era-agnostic nodes
    def mix(self, fac_sock, orig, processed, blend='MIX', clamp=False):
        node, fac_in, a_in, b_in, out = _new_mix(self.t, blend, clamp)
        self._place(node)
        self._plug(fac_sock, fac_in)
        self._plug(orig, a_in)
        self._plug(processed, b_in)
        return out

    def math(self, op, a, b=None, clamp=False):
        node = _try_new(self.t, 'ShaderNodeMath', 'CompositorNodeMath')
        self._place(node)
        node.operation = op
        node.use_clamp = clamp
        for i, val in enumerate((a, b)):
            if val is not None:
                self._plug(val, node.inputs[i])
        return node.outputs[0]

    def cxyz(self, x=0.0, y=0.0, z=0.0):
        node = _try_new(self.t, 'ShaderNodeCombineXYZ',
                        'CompositorNodeCombineXYZ')
        self._place(node)
        for i, val in enumerate((x, y, z)):
            self._plug(val, node.inputs[i])
        return node.outputs[0]

    def rgb(self, color):
        node = self.n('CompositorNodeRGB')
        node.outputs[0].default_value = (*color, 1.0)
        return node.outputs[0]

    def gamma(self, color, value):
        node = _try_new(self.t, 'ShaderNodeGamma', 'CompositorNodeGamma')
        self._place(node)
        self._plug(color, node.inputs[0])
        self._plug(value, node.inputs[1])
        return node.outputs[0]

    def blur(self, img, px, py=None, fast=True):
        """Blur with a radius authored at 1080 lines."""
        py = px if py is None else py
        node = self.n('CompositorNodeBlur')
        self.link(img, node.inputs[0])
        if hasattr(node, "filter_type"):  # 4.x
            node.filter_type = 'FAST_GAUSS' if fast else 'GAUSS'
            node.size_x = max(int(round(px * self.pxs)), 1)
            node.size_y = max(int(round(py * self.pxs)), 1)
            if "Size" in node.inputs:
                node.inputs["Size"].default_value = 1.0
        else:  # 5.x: Size is a pixel vector input, Type a menu socket
            self.link(self.cxyz(self.px(px), self.px(py)),
                      node.inputs["Size"])
            _set_first(node, ["Type"],
                       'Fast Gaussian' if fast else 'Gaussian')
        return node.outputs[0]

    def translate(self, img, x=0.0, y=0.0):
        """Move the image. The edge pixels repeat into the gap, so no black
        or green strip shows at the frame edge."""
        node = self.n('CompositorNodeTranslate')
        self.link(img, node.inputs[0])
        self._plug(x, node.inputs[1])
        self._plug(y, node.inputs[2])
        _set_first(node, ["Extension X"], 'Extend')
        _set_first(node, ["Extension Y"], 'Extend')
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
        _set_first(node, ["Extension X"], 'Extend')
        _set_first(node, ["Extension Y"], 'Extend')
        return node.outputs[0]

    def legacy_driver(self, node, socket_index, expression, component=-1):
        path = f'nodes["{node.name}"].inputs[{socket_index}].default_value'
        utils.add_driver(self.t, path, expression, [], index=component)

    # -------------------------------------------------- animated textures
    # 5.x: unified texture nodes fed by Image Coordinates. A driver moves
    # the 4th noise dimension (W) with 'frame'.
    # 4.x: the legacy Texture node with a texture datablock. The tree keeps
    # a list of these datablocks, so removal deletes them.
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
        self.private.append(("texture", tex.name))
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
        if not self.modern:
            node = self._legacy_tex_node('NOISE', name_suffix)
            return node.outputs["Value"]  # legacy NOISE re-randomises
        node = self.n('ShaderNodeTexWhiteNoise')
        node.noise_dimensions = '4D'
        self.link(self.coords(img), node.inputs["Vector"])
        self.legacy_driver(node, 1, "frame * 991.7")  # W: new seed per frame
        return node.outputs["Value"]

    def tex_clouds(self, name_suffix, img, features, aniso=None, speed=0.0):
        """Fractal noise value; `features` ~ cells across the frame,
        `aniso` stretches (x, y), `speed` animates over frames."""
        if not self.modern:
            node = self._legacy_tex_node('CLOUDS', name_suffix,
                                         noise_size=1.0 / max(features, 1e-3))
            if aniso is not None:
                node.inputs[1].default_value = (aniso[0], aniso[1], 1.0)
            if speed:
                self.legacy_driver(node, 0, f"frame * {speed:.5f}",
                                   component=1)
            return node.outputs["Value"]
        node = self.n('ShaderNodeTexNoise')
        node.noise_dimensions = '4D'
        self.link(self.coords(img, aniso), node.inputs["Vector"])
        node.inputs["Scale"].default_value = float(features)
        node.inputs["Detail"].default_value = 2.0
        if speed:
            self.legacy_driver(node, 1, f"frame * {speed:.5f}")  # W input
        return node.outputs[0]  # Factor


def _build_look_tree(look, scene):
    """Build (or rebuild) the CK_Look_<Name> group of one scene."""
    uid = _scene_uid(scene)
    tree = next((t for t in bpy.data.node_groups
                 if t.get(K.TAG_LOOK) == look["id"]
                 and t.get("cinekit_scene_uid") == uid), None)
    if tree is not None:
        if tree.animation_data:
            tree.animation_data_clear()
        tree.nodes.clear()
        for item in list(tree.interface.items_tree):
            tree.interface.remove(item)
    else:
        tree = bpy.data.node_groups.new(f"{K.LOOK_GROUP_PREFIX}{look['name']}",
                                        'CompositorNodeTree')
    tree[K.TAG_LOOK] = look["id"]
    tree["cinekit_scene_uid"] = uid
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
    tree["cinekit_private"] = json.dumps(b.private)
    tree["cinekit_build"] = json.dumps({"aspect": _render_aspect(scene)})
    return tree


# ----------------------------------------------------------------- builders
# Gaussian blur radius, in pixels per (render lines / format lines), that
# leaves 10% contrast at the line count of the format. That is the usual
# "limiting resolution" of a video format. Blender's Gaussian radius is
# three standard deviations.
_LINE_BLUR = 2.05


def _bld_soft_res(b, img, s):
    """Limit the detail to the line count of the format.

    Blender 5.x: a Gaussian blur. Its radius follows the render height, so
    a preview at 50% looks like the final render. A down and up pair of
    Scale nodes does not work here: the 5.x compositor merges the two
    scales into one transform and never samples the small image. Luma
    keeps a little more horizontal detail than vertical.
    """
    lines = float(s["lines"])
    if b.modern:
        radius = _LINE_BLUR * _REFERENCE_LINES / lines
        up = b.blur(img, radius / 1.15, radius, fast=False)
    else:
        res_y = max(b.scene.render.resolution_y, 1)
        f = min(max(lines / res_y, 0.05), 1.0)
        fx = min(f * 1.15, 1.0)
        up = b.scale_rel(b.scale_rel(img, fx, f), 1.0 / fx, 1.0 / f)
    return b.mix(b.fac("Softness", s["strength"]), img, up)


def _bld_chroma_bleed(b, img, s):
    blurred = b.blur(img, s["blur_px"], 1)
    shifted = b.translate(blurred, b.px(s["offset_px"]))
    sep_sharp = b.n('CompositorNodeSeparateColor', mode='YCC')
    sep_soft = b.n('CompositorNodeSeparateColor', mode='YCC')
    b.link(img, sep_sharp.inputs[0])
    b.link(shifted, sep_soft.inputs[0])
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
    fac = b.fac("Wobble", s["strength"])
    barrel = b.n('CompositorNodeLensdist')
    b.link(img, barrel.inputs[0])
    _set_first(barrel, ["Fit", "use_fit_image"], True)   # no black corners
    if b.modern and "Distortion" in barrel.inputs:
        # The strength also scales the barrel, so the look can fade out.
        b.link(b.math('MULTIPLY', 0.015, fac), barrel.inputs["Distortion"])
    else:
        _set_first(barrel, ["Distort", "Distortion"], 0.015)
    noise = b.tex_clouds("wobble", img, features=3.3, aniso=(0.0, 9.0),
                         speed=0.83)
    centered = b.math('SUBTRACT', noise, 0.5)
    amount = b.math('MULTIPLY', b.px(s["pixels"] * 2.0), fac)
    offset = b.math('MULTIPLY', centered, amount)
    return b.displace(barrel.outputs[0], offset)


# frame % 2 without the modulo operator. Blender evaluates this as a simple
# expression, so it runs when Python auto-run is off.
_FIELD = "(frame - 2.0 * floor(frame / 2.0))"


def _bld_head_bar(b, img, s):
    h = s["height"]
    mask = b.mask('CompositorNodeBoxMask', 0.5, h / 2.0, 1.5, h)
    if b.modern:
        jitter = b.driven_value(f"6.0 + {_FIELD} * 5.0",
                                "CK Head Switch Jitter")
        shifted = b.translate(img, b.math('MULTIPLY', jitter.outputs[0],
                                          b.pxs))
    else:
        node = b.n('CompositorNodeTranslate')
        b.link(img, node.inputs[0])
        b.legacy_driver(node, 1, f"({6.0 * b.pxs:.4f} + {_FIELD} * "
                                 f"{5.0 * b.pxs:.4f})")
        shifted = node.outputs[0]
    dark = b.mix(0.55, shifted, b.rgb((0.08, 0.08, 0.09)), blend='MULTIPLY')
    fac = b.math('MULTIPLY', mask, b.fac("Head Switch", s["strength"]),
                 clamp=True)
    return b.mix(fac, img, dark)


def _bld_sharpen(b, img, s):
    """Unsharp mask on a display copy of the image.

    A light can be many times brighter than white. Sharpening scene-linear
    values then puts black rings around every bright light. A video
    circuit sharpens the display signal, which stops at white. So the
    builder sharpens a clipped display copy (gamma 1/2.2) and adds only
    the change back to the linear image. Highlights above white stay.
    """
    gain = b.math('MULTIPLY', b.fac("Sharpen", s["strength"]), s["amount"])
    clipped = b.mix(0.0, img, img, clamp=True)
    display = b.gamma(clipped, 1.0 / 2.2)
    blurred = b.blur(display, s["radius_px"])
    detail = b.mix(1.0, display, blurred, blend='SUBTRACT')
    scaled = b.mix(1.0, detail, gain, blend='MULTIPLY')
    sharp = b.mix(1.0, display, scaled, blend='ADD', clamp=True)
    change = b.mix(1.0, b.gamma(sharp, 2.2), clipped, blend='SUBTRACT')
    return b.mix(1.0, img, change, blend='ADD')


def _bld_levels(b, img, s):
    """Video levels. The preset gives them in display terms (16/255,
    235/255). The compositor is scene-linear, so convert them first."""
    black = optics.srgb_to_linear(s["black"])
    white = optics.srgb_to_linear(s["white"])
    span = max(white - black, 0.001)
    squashed = b.mix(1.0, img, b.rgb((span,) * 3), blend='MULTIPLY')
    lifted = b.mix(1.0, squashed, b.rgb((black,) * 3), blend='ADD')
    if s["white"] < 0.999:
        # Clip the whites at the white level, like a video signal.
        lifted = b.mix(1.0, lifted, b.rgb((white,) * 3), blend='DARKEN')
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
    px = s["pixels"]
    wave_x = "sin(frame * 0.91) * 0.6 + sin(frame * 2.33 + 1.7) * 0.4"
    wave_y = "sin(frame * 1.27 + 0.5) * 0.5 + sin(frame * 3.11) * 0.5"
    if b.modern:
        wx = b.driven_value(wave_x, "CK Weave X")
        wy = b.driven_value(wave_y, "CK Weave Y")
        moved = b.translate(img, b.math('MULTIPLY', wx.outputs[0], b.px(px)),
                            b.math('MULTIPLY', wy.outputs[0], b.px(px)))
    else:
        node = b.n('CompositorNodeTranslate')
        b.link(img, node.inputs[0])
        scaled = px * b.pxs
        b.legacy_driver(node, 1, f"{scaled:.3f} * ({wave_x})")
        b.legacy_driver(node, 2, f"{scaled:.3f} * ({wave_y})")
        moved = node.outputs[0]
    return b.mix(b.fac("Gate Weave", s["strength"]), img, moved)


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
    glowing = b.mix(1.0, img, tinted, blend='ADD')   # light adds in linear
    return b.mix(b.fac("Halation", s["strength"]), img, glowing)


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
    node.mapping.extend = 'EXTRAPOLATED'   # keep highlights above 1.0
    node.mapping.update()
    img_in = node.inputs.get("Image") or node.inputs[1]
    fac_in = (node.inputs.get("Factor") or node.inputs.get("Fac")
              or node.inputs[0])
    b.link(img, img_in)
    fac_in.default_value = 1.0
    return b.mix(b.fac("Tone", s["strength"]), img, node.outputs[0])


def _bld_vignette(b, img, s):
    mask = b.mask('CompositorNodeEllipseMask', 0.5, 0.5, 1.45, 1.15)
    soft = b.blur(mask, 270.0, 270.0, fast=True)
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
    node = _try_new(b.t, 'ShaderNodeGamma', 'CompositorNodeGamma')
    b._place(node)
    b.link(img, node.inputs[0])
    node.inputs[1].default_value = s["value"]
    return b.mix(b.fac("Gamma", s["strength"]), img, node.outputs[0])


def _bld_interlace(b, img, s):
    """Alternate scan lines darken. The pattern swaps lines every frame.

    A scan line is one pixel row per 540 render lines, so the pattern has
    the same size in a 50% preview and in the final render.
    """
    if b.modern:
        coords = b.n('CompositorNodeImageCoordinates')
        b.link(img, coords.inputs[0])
        sep = _try_new(b.t, 'ShaderNodeSeparateXYZ',
                       'CompositorNodeSeparateXYZ')
        b._place(sep)
        b.link(coords.outputs["Pixel"], sep.inputs[0])
        line_px = b.math('MAXIMUM', b.math('ROUND', b.math('MULTIPLY',
                                                           b.pxs, 2.0)), 1.0)
        row = b.math('FLOOR', b.math('DIVIDE', sep.outputs[1], line_px))
        field = b.driven_value("frame", "CK Interlace Field")
        band = b.math('FLOORED_MODULO', b.math('ADD', row, field.outputs[0]),
                      2.0)
    else:
        node = b._legacy_tex_node('WOOD', "interlace", wood_bands=True)
        res_y = max(b.scene.render.resolution_y, 1)
        node.inputs[1].default_value = (0.0, res_y / 2.0, 0.0)
        utils.add_driver(b.t, f'nodes["{node.name}"].inputs[0].default_value',
                         f"{_FIELD} * {1.0 / res_y:.6f}", [], index=1)
        band = node.outputs["Value"]
    line = b.math('ADD', b.math('MULTIPLY', band, s["amount"]),
                  1.0 - s["amount"])
    lined = b.mix(1.0, img, line, blend='MULTIPLY')
    return b.mix(b.fac("Interlace", s["strength"]), img, lined)


def _bld_timestamp(b, img, s):
    text = b.scene.cinekit.security_timestamp or "CAM 01"
    image = _timestamp_image(b.look["id"], _scene_uid(b.scene), text,
                             _render_aspect(b.scene))
    b.private.append(("image", image.name))
    b.t["cinekit_timestamp_image"] = image.name
    b.t["cinekit_timestamp_text"] = text
    node = b.n('CompositorNodeImage')
    node.image = image
    fit = b.scale_render_size(node.outputs[0])
    fac = b.fac("Timestamp", s["strength"])
    return b.alpha_over(fac, img, fit)


def _bld_blocking(b, img, s):
    """Compression blocks: average each block, then cut the color depth."""
    block = max(int(s["block_px"]), 2)
    levels = 18.0
    if b.modern:
        pix = b.n('CompositorNodePixelate')
        b.link(img, pix.inputs[0])
        b.link(b.px(block), pix.inputs["Size"])
        blocky = pix.outputs[0]
    else:
        f = 1.0 / block
        blocky = b.scale_rel(img, f, f)
    sep = b.n('CompositorNodeSeparateColor', mode='RGB')
    b.link(blocky, sep.inputs[0])
    comb = b.n('CompositorNodeCombineColor', mode='RGB')
    for i in range(3):
        q = b.math('DIVIDE',
                   b.math('FLOOR',
                          b.math('MULTIPLY', sep.outputs[i], levels)),
                   levels)
        b.link(q, comb.inputs[i])
    b.link(sep.outputs[3], comb.inputs[3])
    result = comb.outputs[0]
    if not b.modern:
        result = b.scale_rel(result, block, block)
    return b.mix(b.fac("Blocking", s["strength"]), img, result)


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
    fringed = b.mix(1.0, img, purple, blend='ADD')
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
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
}

_STAMP_WIDTH = 960
_STAMP_SCALE = 3


def _timestamp_pixels(text, width, height):
    """Draw text with the 5x7 bitmap font. Return a flat RGBA float list."""
    import numpy as np
    canvas = np.zeros((height, width, 4), dtype=np.float32)  # bottom-up
    top = height - 40                        # 40 px below the top edge
    x = 40
    for char in text.upper()[:40]:
        glyph = _FONT.get(char, _FONT[" "])
        for row, bits in enumerate(glyph):
            y1 = top - row * _STAMP_SCALE
            y0 = y1 - _STAMP_SCALE
            for col in range(5):
                if bits & (1 << (4 - col)) and y0 >= 0:
                    x0 = x + col * _STAMP_SCALE
                    canvas[y0:y1, x0:x0 + _STAMP_SCALE] = 1.0
        x += 6 * _STAMP_SCALE
        if x + 5 * _STAMP_SCALE >= width:
            break
    return canvas.ravel()


def _timestamp_image(look_id, uid, text, aspect):
    """Render text into a generated image datablock with a 5x7 bitmap font.

    The image has the aspect ratio of the render, so the text does not
    stretch. No external files, so no absolute path goes into the .blend.
    """
    width = _STAMP_WIDTH
    height = max(int(round(width / max(aspect, 0.1))), 64)
    name = f"CK_{look_id}_{uid}_timestamp"
    img = bpy.data.images.get(name)
    if img is not None and tuple(img.size) != (width, height):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, width, height, alpha=True)
    img[K.TAG] = look_id
    img.pixels.foreach_set(_timestamp_pixels(text, width, height))
    try:
        img.pack()
    except RuntimeError:
        pass  # stays a generated image; still saved with the .blend
    return img


def refresh_timestamp(scene):
    """Redraw the burned-in text of the active look when it changed.

    The Security Cam text field calls this, so a change shows without a
    rebuild.
    """
    node = _scene_pipeline(scene)
    if node is None or node.node_tree is None:
        return
    look_node = _find_node(node.node_tree, "look")
    tree = look_node.node_tree if look_node is not None else None
    if tree is None or not tree.get("cinekit_timestamp_image"):
        return
    if (tree.get("cinekit_scene_uid") != _scene_uid(scene)
            and not _compositor_shared(scene) and scene.cinekit.active_look):
        # The tree of another scene: build a tree for this scene instead.
        try:
            apply_look(scene, scene.cinekit.active_look)
        except CKError as exc:
            print(f"CineKit: could not rebuild the look: {exc}")
        return
    text = scene.cinekit.security_timestamp or "CAM 01"
    if tree.get("cinekit_timestamp_text") == text:
        return
    img = bpy.data.images.get(tree["cinekit_timestamp_image"])
    if img is None:
        return
    img.pixels.foreach_set(_timestamp_pixels(text, img.size[0], img.size[1]))
    try:
        img.pack()
    except RuntimeError:
        pass
    tree["cinekit_timestamp_text"] = text
