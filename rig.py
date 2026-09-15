# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Central apply layer for cameras and rigs.

This module turns the high-level settings in properties.py into Blender
data: camera values, scene exposure, motion blur, white balance, drivers
and rig hierarchies. The UI and the operators never set low-level values.
They call this module.

Everything here works with Cycles and with EEVEE.
"""

import math

import bpy
from mathutils import Matrix, Vector

from . import constants as K
from . import lenses, optics, utils
from .utils import CKError

# Constraints cannot carry custom properties, so CineKit constraints are
# identified by this name prefix (documented in README).
CON_PREFIX = "CK "

# Camera transform channels that the handheld rig drives.
_DELTA_PATHS = ("delta_location", "delta_rotation_euler",
                "delta_rotation_quaternion")


def _flat(matrix):
    return [v for row in matrix for v in row]


def _unflat(values):
    return Matrix([values[i * 4:i * 4 + 4] for i in range(4)])


# =============================================================== physical cam
def shutter_time_s(ck, fps):
    return optics.shutter_time_s(ck.shutter_mode, ck.shutter_speed,
                                 ck.shutter_angle, fps)


def compute_ev100(cam_data, scene):
    ck = cam_data.cinekit
    fps = scene.render.fps / scene.render.fps_base
    return optics.ev100(ck.aperture, shutter_time_s(ck, fps), ck.iso)


def look_bias(scene):
    """Exposure bias of the active look, in stops."""
    return float(scene.get(K.LOOK_BIAS, 0.0))


def dof_floor(cam_data):
    """Lowest depth-of-field f-stop that the active look allows."""
    return float(cam_data.get(K.LOOK_MIN_FSTOP, 0.0))


def dof_readout(cam_data, scene):
    """(hyperfocal_m, near_m, far_m, total_m) for the UI panel."""
    fstop = cam_data.dof.aperture_fstop
    coc = optics.coc_mm(cam_data.sensor_width, cam_data.sensor_height)
    focus_m = cam_data.dof.focus_distance
    if cam_data.dof.focus_object:
        cam_obj = _object_of_camera(cam_data)
        if cam_obj:
            focus_m = (cam_obj.matrix_world.translation -
                       cam_data.dof.focus_object.matrix_world.translation
                       ).length
    focus_mm = max(focus_m, 0.001) * 1000.0
    h = optics.hyperfocal_mm(cam_data.lens, fstop, coc)
    near, far = optics.dof_limits_mm(cam_data.lens, fstop, focus_mm, coc)
    total = far - near
    mm = 1.0 / 1000.0
    return (h * mm, near * mm,
            far * mm if math.isfinite(far) else math.inf,
            total * mm if math.isfinite(total) else math.inf)


def _object_of_camera(cam_data):
    for obj in bpy.data.objects:
        if obj.type == 'CAMERA' and obj.data == cam_data:
            return obj
    return None


def apply_camera(cam_data, scene):
    """Push camera-local settings: DOF f-stop, breathing driver refresh.

    A look can set a depth-of-field floor. The floor changes only the
    depth of field. The exposure keeps using the aperture of the camera.
    """
    ck = cam_data.cinekit
    if not ck.enabled or utils.is_linked(cam_data):
        return
    with utils.suppress_updates():
        cam_data.dof.aperture_fstop = max(ck.aperture, dof_floor(cam_data))
    if ck.focus_breathing:
        try:
            set_breathing(cam_data)
        except CKError as exc:
            print(f"CineKit: could not refresh the breathing driver: {exc}")


def _is_physical(cam_obj):
    return (cam_obj is not None and cam_obj.type == 'CAMERA'
            and cam_obj.data.cinekit.enabled
            and not utils.is_linked(cam_obj.data))


def sync_active_camera(scene):
    """Set the scene exposure from the active camera and the active look.

    CineKit calls this when the active camera changes, when a physical
    setting changes, and when a look changes its exposure bias.

    - A physical camera owns the exposure: the calibration, its EV100, its
      exposure compensation and the look bias set the value.
    - Another camera with a look bias: CineKit adds the bias to the
      exposure as an offset. A change that the user makes to the exposure
      stays, also when the look goes away.
    - Another camera with no bias: CineKit gives the exposure back.
    """
    cam = scene.camera
    bias = look_bias(scene)
    if not _is_physical(cam):
        if abs(bias) > 1e-9:
            state = take_exposure_control(scene, mode="offset")
            delta = bias - state["bias"]
            if abs(delta) > 1e-9:
                scene.view_settings.exposure += delta
                state["bias"] = bias
                utils.store_state(scene, K.PREV_EXPOSURE, state)
            _set_native_wb(scene, None)
            _set_compositor_wb(scene, None)
        elif utils.fetch_state(scene, K.PREV_EXPOSURE) is not None:
            release_exposure_control(scene)
        return

    ck = cam.data.cinekit
    state = take_exposure_control(scene, mode="physical")
    ev = compute_ev100(cam.data, scene)
    scene.view_settings.exposure = optics.scene_exposure_for_ev100(
        ev, ck.exposure_comp + bias)

    if ck.drive_motion_blur:
        fps = scene.render.fps / scene.render.fps_base
        shutter = optics.motion_blur_shutter(
            ck.shutter_mode, ck.shutter_speed, ck.shutter_angle, fps)
        scene.render.motion_blur_shutter = min(max(shutter, 0.01), 2.0)
        if not state.get("shutter_driven"):
            state["shutter_driven"] = True
            utils.store_state(scene, K.PREV_EXPOSURE, state)

    apply_white_balance(scene, cam.data)


def on_camera_settings_changed(cam_data, scene):
    apply_camera(cam_data, scene)
    if scene and scene.camera and scene.camera.data == cam_data:
        sync_active_camera(scene)


def take_exposure_control(scene, mode="physical"):
    """Store the exposure and the motion-blur shutter one time, before
    CineKit changes them. Return the stored state.

    mode "physical": a physical camera sets the exposure value.
    mode "offset": CineKit only adds the look bias (state["bias"]) to the
    exposure of the user.
    """
    vs = scene.view_settings
    state = utils.fetch_state(scene, K.PREV_EXPOSURE)
    if state is None:
        state = {"exposure": vs.exposure,
                 "motion_blur_shutter": scene.render.motion_blur_shutter,
                 "shutter_driven": False, "mode": mode, "bias": 0.0}
        utils.store_state(scene, K.PREV_EXPOSURE, state)
        return state
    state.setdefault("bias", 0.0)
    old_mode = state.get("mode", "physical")  # older versions: physical
    if old_mode != mode:
        if mode == "physical":
            # The exposure of the user is the current value without the
            # offset that CineKit added.
            state["exposure"] = vs.exposure - state["bias"]
        else:
            vs.exposure = state["exposure"]
            if state.get("shutter_driven") and "motion_blur_shutter" in state:
                scene.render.motion_blur_shutter = state["motion_blur_shutter"]
            state["shutter_driven"] = False
        state["mode"] = mode
        state["bias"] = 0.0
        utils.store_state(scene, K.PREV_EXPOSURE, state)
    return state


def release_exposure_control(scene):
    """Give back the exposure, the shutter and the white balance.

    After physical control, CineKit restores the values from before. After
    offset control, it removes only its offset, so the edits of the user
    stay.
    """
    state = utils.fetch_state(scene, K.PREV_EXPOSURE)
    if state is not None:
        if state.get("mode", "physical") == "offset":
            scene.view_settings.exposure -= state.get("bias", 0.0)
        else:
            scene.view_settings.exposure = state["exposure"]
            if state.get("shutter_driven") and "motion_blur_shutter" in state:
                scene.render.motion_blur_shutter = state["motion_blur_shutter"]
        utils.clear_state(scene, K.PREV_EXPOSURE)
    _set_native_wb(scene, None)
    _set_compositor_wb(scene, None)


def _set_compositor_wb(scene, gains):
    from .looks import engine
    engine.set_white_balance(scene, gains)


def _set_native_wb(scene, spec):
    """Use the native view-transform white balance (Blender 4.3 and newer).

    spec is (kelvin, tint) to set it, or None to give it back. CineKit
    stores the user values before the first change. With spec None it
    restores those values, and it changes nothing when it never took
    control. So the white balance that a user sets by hand stays on.
    Return True when this Blender version has native white balance.
    """
    vs = scene.view_settings
    if not hasattr(vs, "white_balance_temperature"):
        return False
    prev = utils.fetch_state(scene, K.PREV_WB)
    if spec is None:
        if prev is not None:
            vs.use_white_balance = prev["use"]
            vs.white_balance_temperature = prev["temperature"]
            vs.white_balance_tint = prev["tint"]
            utils.clear_state(scene, K.PREV_WB)
        return True
    if prev is None:
        utils.store_state(scene, K.PREV_WB, {
            "use": vs.use_white_balance,
            "temperature": vs.white_balance_temperature,
            "tint": vs.white_balance_tint,
        })
    kelvin, tint = spec
    vs.use_white_balance = True
    vs.white_balance_temperature = kelvin
    vs.white_balance_tint = tint * 50.0  # CineKit -1..1 to Blender units
    return True


def apply_white_balance(scene, cam_data):
    """White balance approach.

    Blender 4.3 and newer have a native view-transform white balance. It
    shows live in every viewport and matches the render. Blender 4.2 has
    none, so CineKit multiplies a per-channel gain (optics.wb_gains) in its
    compositor pipeline. That gain shows in the viewport only with the
    Viewport Compositor, and it always shows in the render.
    """
    ck = cam_data.cinekit
    if not ck.wb_enabled:
        if not _set_native_wb(scene, None):
            _set_compositor_wb(scene, None)
        return
    if _set_native_wb(scene, (ck.wb_temperature, ck.wb_tint)):
        _set_compositor_wb(scene, None)
        return
    _set_compositor_wb(scene, optics.wb_gains(ck.wb_temperature, ck.wb_tint))


def apply_lens_preset(cam_data):
    ck = cam_data.cinekit
    lens = lenses.get(ck.lens_preset)
    if lens is None:  # 'NONE' / custom
        return
    if utils.is_linked(cam_data):
        return
    utils.remove_driver(cam_data, "lens")
    utils.clear_state(cam_data, K.BASE_LENS)
    cam_data.lens = lens["focal"]
    with utils.suppress_updates():
        ck.breathing_amount = lens["breathing"]
        if ck.aperture < lens["max_aperture"]:
            ck.aperture = lens["max_aperture"]  # the lens does not open wider


def base_lens(cam_data):
    """The focal length without the focus-breathing shift."""
    stored = utils.fetch_state(cam_data, K.BASE_LENS)
    return stored["lens"] if stored else cam_data.lens


def set_base_lens(cam_data, lens):
    """Set the focal length. Keep the breathing driver when it is on."""
    if utils.fetch_state(cam_data, K.BASE_LENS) is not None:
        utils.store_state(cam_data, K.BASE_LENS, {"lens": lens})
        set_breathing(cam_data)
    else:
        cam_data.lens = lens


def set_breathing(cam_data):
    """Create or remove the focus-breathing driver on the focal length.

    The model is stylized: the focal length grows by
    amount * 5% / max(focus_m, 0.25). The shift shows at close focus and
    is very small at a distance. The active look can scale the amount.
    """
    utils.require_editable(cam_data, "Camera")
    ck = cam_data.cinekit
    if not ck.focus_breathing:
        base = utils.fetch_state(cam_data, K.BASE_LENS)
        utils.remove_driver(cam_data, "lens")
        if base is not None:
            cam_data.lens = base["lens"]
            utils.clear_state(cam_data, K.BASE_LENS)
        return

    stored = utils.fetch_state(cam_data, K.BASE_LENS)
    base = stored["lens"] if stored else cam_data.lens
    if stored is None:
        utils.store_state(cam_data, K.BASE_LENS, {"lens": cam_data.lens})

    scale = float(cam_data.get("cinekit_breath_scale", 1.0))
    amt = ck.breathing_amount * scale
    if cam_data.dof.focus_object is not None:
        cam_obj = _object_of_camera(cam_data)
        if cam_obj is None:
            raise CKError("This camera data has no object in the file.")
        variables = [("ck_dist", 'LOC_DIFF', cam_obj,
                      cam_data.dof.focus_object)]
    else:
        variables = [("ck_dist", cam_data, "dof.focus_distance", 'CAMERA')]
    utils.add_driver(
        cam_data, "lens",
        f"{base:.4f} * (1.0 + {amt:.5f} * 0.05 / max(ck_dist, 0.25))",
        variables)


# ===================================================================== rigs
HANDHELD_PROFILES = {
    'BREATHING': {
        "label": "Locked-off breathing",
        "scale": 120.0,
        "loc": (0.002, 0.002, 0.004),   # metres; z strongest: breath lift
        "rot": (0.15, 0.10, 0.05),      # degrees
    },
    'DOCUMENTARY': {
        "label": "Documentary",
        "scale": 55.0,
        "loc": (0.010, 0.008, 0.012),
        "rot": (0.6, 0.5, 0.25),
    },
    'RUN_AND_GUN': {
        "label": "Run and gun",
        "scale": 16.0,
        "loc": (0.035, 0.030, 0.045),
        "rot": (1.8, 1.5, 0.9),
    },
    'CRASH_ZOOM': {
        "label": "Crash zoom",
        "scale": 7.0,
        "loc": (0.050, 0.045, 0.015),
        "rot": (2.5, 2.2, 1.6),         # violent, roll-heavy
    },
}


def rig_root_of(obj):
    """Find the CineKit rig root for an object, or None.

    A rigged camera keeps the rig id in a custom property. This lookup does
    not use the parent chain, because some rig members stay unparented. The
    dolly carrier is one example. The parent chain is the fallback.
    """
    if obj is None:
        return None
    rig_id = obj.get("cinekit_rig_member_of")
    if rig_id:
        for candidate in bpy.data.objects:
            if candidate.get("cinekit_rig_uid") == rig_id:
                return candidate
    node = obj
    while node is not None:
        if node.get(K.TAG_RIG):
            return node
        node = node.parent
    return None


def find_member(root, role):
    """The rig member with a point role (for example "Carrier"), or None."""
    rig_id = root.get("cinekit_rig_uid")
    return next((o for o in bpy.data.objects
                 if o.get(K.TAG_RIG_MEMBER) == rig_id
                 and o.get(K.TAG_POINT) == role), None)


def _evaluate(context):
    """Evaluate constraints and drivers now.

    A new rig member has no evaluated position until the depsgraph runs.
    Parenting a camera to it before this step moves the camera.
    """
    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()


def _attach(context, child, parent, world):
    """Parent `child` to `parent` and put the child at `world`.

    The parent must hold its final position first, so this evaluates the
    depsgraph before it reads the parent matrix. Setting the basis to the
    world matrix is exact because the parent inverse cancels the parent.
    """
    _evaluate(context)
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    child.matrix_basis = world


def _def_prop(obj, name, value, vmin, vmax, description):
    obj[name] = float(value)
    obj.id_properties_ui(name).update(
        min=vmin, max=vmax, soft_min=vmin, soft_max=vmax,
        description=description)


def _store_prerig(cam_obj, rig_id):
    utils.store_state(cam_obj, K.PREV_CAMERA_RIG, {
        "rig": rig_id,
        "matrix": _flat(cam_obj.matrix_world),
        "parent": cam_obj.parent.name if cam_obj.parent else None,
        "parent_inverse": _flat(cam_obj.matrix_parent_inverse),
        "basis": _flat(cam_obj.matrix_basis),
        "delta_location": list(cam_obj.delta_location),
        "delta_rotation_euler": list(cam_obj.delta_rotation_euler),
        "delta_rotation_quaternion": list(cam_obj.delta_rotation_quaternion),
    })


def _strip_rig_from_camera(cam_obj):
    """Remove CineKit constraints, drivers and tags from a camera."""
    for con in list(cam_obj.constraints):
        if con.name.startswith(CON_PREFIX):
            utils.remove_driver(cam_obj,
                                f'constraints["{con.name}"].influence')
            cam_obj.constraints.remove(con)
    for path in _DELTA_PATHS:
        utils.remove_driver(cam_obj, path)
    if "cinekit_rig_member_of" in cam_obj.keys():
        del cam_obj["cinekit_rig_member_of"]


def _clean_stale_membership(cam_obj):
    """A user can delete a rig root by hand. The camera then keeps the rig
    tag and the CineKit constraints. Remove them, so a new rig starts
    clean. The camera does not move."""
    rig_id = cam_obj.get("cinekit_rig_member_of")
    if not rig_id:
        return
    if any(o.get("cinekit_rig_uid") == rig_id for o in bpy.data.objects):
        return
    _strip_rig_from_camera(cam_obj)
    utils.clear_state(cam_obj, K.PREV_CAMERA_RIG)


def _prepare_rig(context, cam_obj, rig_type, root_name):
    """Start a rig. Return (root, rig_id, pre_world).

    pre_world is the world matrix of the camera before CineKit changed
    anything. The rig builders attach the camera with this matrix.
    """
    utils.require_editable(cam_obj, "Camera object")
    utils.require_editable(cam_obj.data, "Camera")
    _clean_stale_membership(cam_obj)
    if rig_root_of(cam_obj) is not None:
        raise CKError("This camera already belongs to a CineKit rig. "
                      "Open the Rigs panel and click Remove Rig first.")
    _evaluate(context)
    pre_world = cam_obj.matrix_world.copy()
    rig_id = utils.new_id("rig_")
    _store_prerig(cam_obj, rig_id)
    cam_obj["cinekit_rig_member_of"] = rig_id
    root = utils.new_empty(context.scene, root_name, display='CUBE', size=0.3)
    root[K.TAG_RIG] = rig_type
    root["cinekit_rig_uid"] = rig_id
    root.matrix_world = pre_world.copy()
    return root, rig_id, pre_world


def _member(scene, rig_id, name, display='PLAIN_AXES', size=0.35):
    obj = utils.new_empty(scene, name, display=display, size=size)
    obj[K.TAG_RIG_MEMBER] = rig_id
    return obj


def _point(obj, role, movable=True):
    """Tag an object as a named rig point for Advanced Mode.

    Use movable=False when a driver sets the location. The panel then
    offers Select only.
    """
    obj[K.TAG_POINT] = role
    if movable:
        obj[K.TAG_POINT_MOVE] = 1
    return obj


def rig_points(root):
    """[(role, obj, movable)] for a rig root, ordered for the UI."""
    rig_id = root.get("cinekit_rig_uid")
    out = []
    for obj in bpy.data.objects:
        role = obj.get(K.TAG_POINT)
        if role and (obj.get(K.TAG_RIG_MEMBER) == rig_id or obj is root):
            out.append((role, obj, bool(obj.get(K.TAG_POINT_MOVE))))
    order = {"Base": 0, "Target": 0, "Mount": 0, "Pivot": 0,
             "Carrier": 1, "Aim": 2, "Path": 3, "Head": 4}
    out.sort(key=lambda t: order.get(t[0], 9))
    return out


def _noise_channels(action, holder, prof, phase_seed=37):
    """Put F-modifier noise on the location and rotation of `holder`."""
    new_fcurve = utils.action_fcurve_factory(action, holder)
    for path, strengths, to_rad in (("location", prof["loc"], False),
                                    ("rotation_euler", prof["rot"], True)):
        for axis in range(3):
            fcu = new_fcurve(path, axis)
            fcu.keyframe_points.insert(0.0, 0.0)
            mod = fcu.modifiers.new('NOISE')
            mod.scale = prof["scale"] * (1.0 + 0.13 * axis)  # decorrelate
            mod.strength = (math.radians(strengths[axis]) if to_rad
                            else strengths[axis])
            mod.phase = float(axis * phase_seed + (101 if to_rad else 0))
            mod.depth = 1


# ------------------------------------------------- custom dolly path input
def stroke_points_world(gp_obj):
    """World-space points of a Grease Pencil object's first drawn stroke
    (Grease Pencil v3). Returns [] if nothing usable is found."""
    data = getattr(gp_obj, "data", None)
    if gp_obj.type != 'GREASEPENCIL' or data is None:
        return []
    mw = gp_obj.matrix_world
    layers = [data.layers.active] if getattr(data.layers, "active", None) \
        else list(data.layers)
    if not layers:
        layers = list(data.layers)
    for layer in layers:
        for frame in reversed(list(layer.frames)):
            drawing = getattr(frame, "drawing", None)
            if drawing is None:
                continue
            for stroke in drawing.strokes:
                pts = [mw @ p.position for p in stroke.points]
                if len(pts) >= 2:
                    return pts
    return []


def curve_points_world(curve_obj):
    """World-space control points of a curve object's first spline."""
    if curve_obj.type != 'CURVE' or not curve_obj.data.splines:
        return []
    mw = curve_obj.matrix_world
    spline = curve_obj.data.splines[0]
    if spline.type == 'BEZIER':
        return [mw @ bp.co for bp in spline.bezier_points]
    return [mw @ p.co.to_3d() for p in spline.points]


def _read_path_source(opts):
    """Return world-space points from a custom path source, or an empty list.

    Raise CKError if the source has fewer than 2 points. This runs before
    the rig is built, so a bad source does not leave objects in the scene.
    """
    source = opts.get("path_source", 'GENERATED')
    src_obj = opts.get("path_object")
    if source == 'GENERATED' or src_obj is None:
        return []
    points = (stroke_points_world(src_obj) if source == 'GREASE_PENCIL'
              else curve_points_world(src_obj))
    if len(points) < 2:
        raise CKError(
            f"'{src_obj.name}' has no usable path. Draw a stroke, or pick a "
            "curve with 2 or more points.")
    return points


def set_dolly_path(root, world_pts):
    """Replace the shape of the dolly track with world_pts (2 or more).

    The points go into the local space of the path object, so the Follow
    Path constraint and the ck_position driver keep working.
    """
    rig_id = root.get("cinekit_rig_uid")
    path = next((o for o in bpy.data.objects
                 if o.get(K.TAG_RIG_MEMBER) == rig_id
                 and o.type == 'CURVE'), None)
    if path is None:
        raise CKError("The dolly path object no longer exists.")
    if len(world_pts) < 2:
        raise CKError("A path needs 2 or more points.")
    inv = path.matrix_world.inverted()
    curve = path.data
    curve.splines.clear()
    spline = curve.splines.new('POLY')
    spline.points.add(len(world_pts) - 1)
    for i, wp in enumerate(world_pts):
        local = inv @ wp
        spline.points[i].co = (local.x, local.y, local.z, 1.0)
    return path


def apply_dolly_path(context, root, world_pts):
    """Give an existing dolly a new track, and put its camera on the track.

    The camera keeps its rotation. Its position moves to the carrier, so
    the camera rides the new track instead of a copy beside it.
    """
    set_dolly_path(root, world_pts)
    carrier = find_member(root, "Carrier")
    if carrier is None:
        raise CKError("The dolly carrier no longer exists. Remove the rig, "
                      "then add it again.")
    _evaluate(context)
    for cam_obj in _cameras_in_rig(root.get("cinekit_rig_uid")):
        world = cam_obj.matrix_world.copy()
        world.translation = carrier.matrix_world.translation
        _attach(context, cam_obj, carrier, world)


def create_dolly(context, cam_obj, opts=None):
    """Build a dolly rig: a curve path and a carrier that rides the path.

    The root holds ck_position (0-1, keyframeable) and ck_track (0-1). The
    panel toggles banking on the Follow Path constraint.

    Advanced Mode reads these opts: path_length, carrier_pos, aim_distance,
    track, banking, path_source ('GENERATED', 'CURVE' or 'GREASE_PENCIL')
    and path_object. path_object is the source curve or Grease Pencil
    object for the last two modes.
    """
    opts = opts or {}
    half = opts.get("path_length", 6.0) / 2.0
    carrier_pos = opts.get("carrier_pos", 0.5)
    aim_distance = opts.get("aim_distance", 5.0)
    # Read a custom path source first. A failure must not leave a
    # half-built rig in the scene.
    custom_points = _read_path_source(opts)
    scene = context.scene
    root, rig_id, pre_world = _prepare_rig(context, cam_obj, K.RIG_DOLLY,
                                           "CK_Dolly")
    _point(root, "Base")
    # The root sits at the camera. The generated track runs half a length
    # each way along the horizontal right axis of the camera, so a
    # ck_position of 0.5 keeps the camera at its first position.
    cam_loc = pre_world.translation
    root.matrix_world = Matrix.Translation(cam_loc)

    curve = bpy.data.curves.new("CK_DollyPath", 'CURVE')
    curve.dimensions = '3D'
    curve[K.TAG] = rig_id
    spline = curve.splines.new('POLY')
    spline.points.add(1)
    spline.points[0].co = (-half, 0.0, 0.0, 1.0)
    spline.points[1].co = (half, 0.0, 0.0, 1.0)
    right = pre_world.to_quaternion() @ Vector((1, 0, 0))
    right.z = 0.0
    path = bpy.data.objects.new("CK_Dolly_Path", curve)
    path[K.TAG] = rig_id
    path[K.TAG_RIG_MEMBER] = rig_id
    _point(path, "Path")
    utils.link_to_ck(scene, path)
    path.parent = root
    if right.length > 1e-4:
        path.rotation_euler = right.to_track_quat('X', 'Z').to_euler()

    carrier = _member(scene, rig_id, "CK_Dolly_Carrier", display='CUBE',
                      size=0.2)
    _point(carrier, "Carrier", movable=False)  # ck_position drives this
    # The carrier has no parent on purpose. The Follow Path constraint
    # returns a world-space point on the curve. A parent applies the root
    # offset a second time and moves the carrier off the track. The path
    # stays a child of the root, so the rig still moves as one unit.
    con = carrier.constraints.new('FOLLOW_PATH')
    con.name = CON_PREFIX + "Follow Path"
    con.target = path
    con.use_fixed_location = True
    con.use_curve_follow = opts.get("banking", False)
    con.forward_axis = 'FORWARD_X'
    con.up_axis = 'UP_Z'

    aim = _member(scene, rig_id, "CK_Dolly_Aim", display='SPHERE', size=0.15)
    _point(aim, "Aim")
    fwd = pre_world.to_quaternion() @ Vector((0, 0, -1))
    aim.matrix_world = Matrix.Translation(cam_loc + fwd * aim_distance)

    _def_prop(root, "ck_position", carrier_pos, 0.0, 1.0,
              "Position along the dolly path (keyframeable)")
    _def_prop(root, "ck_track", opts.get("track", 0.0), 0.0, 1.0,
              "Track the aim empty (0 = free, 1 = locked on)")

    utils.add_driver(carrier,
                     f'constraints["{con.name}"].offset_factor',
                     "ck_pos",
                     [("ck_pos", root, '["ck_position"]')])
    track = cam_obj.constraints.new('TRACK_TO')
    track.name = CON_PREFIX + "Track Target"
    track.target = aim
    track.track_axis = 'TRACK_NEGATIVE_Z'
    track.up_axis = 'UP_Y'
    utils.add_driver(cam_obj, f'constraints["{track.name}"].influence',
                     "ck_trk", [("ck_trk", root, '["ck_track"]')])

    world = pre_world
    if custom_points:
        # A custom track can start anywhere. Put the camera on it.
        set_dolly_path(root, custom_points)
        _evaluate(context)
        world = pre_world.copy()
        world.translation = carrier.matrix_world.translation
    _attach(context, cam_obj, carrier, world)
    return root


def create_crane(context, cam_obj, opts=None):
    """Crane/jib: base (pan) -> arm (boom) -> head (tilt).

    The base sits behind and below the camera, so the arm reaches the
    camera at the start boom angle. Pan swings the arm around the base,
    boom raises it and ck_length extends it. The head stays level, and
    ck_tilt tilts it.

    opts (Advanced Mode): base_drop (vertical distance from the base to
    the camera), boom (start angle, 10 to 80 degrees), tilt.
    """
    opts = opts or {}
    scene = context.scene
    root, rig_id, pre_world = _prepare_rig(context, cam_obj, K.RIG_CRANE,
                                           "CK_Crane")
    _point(root, "Base")
    cam_loc = pre_world.translation
    boom = min(max(opts.get("boom", 45.0), 10.0), 80.0)
    drop = max(opts.get("base_drop", 1.5), 0.2)
    length = drop / math.sin(math.radians(boom))

    fwd = pre_world.to_quaternion() @ Vector((0, 0, -1))
    heading = Vector((fwd.x, fwd.y, 0.0))
    if heading.length < 1e-4:          # the camera looks straight up or down
        up = pre_world.to_quaternion() @ Vector((0, 1, 0))
        heading = Vector((up.x, up.y, 0.0))
    if heading.length < 1e-4:
        heading = Vector((0.0, 1.0, 0.0))
    heading.normalize()
    reach = length * math.cos(math.radians(boom))
    base_loc = cam_loc - heading * reach - Vector((0.0, 0.0, drop))
    root.matrix_world = Matrix.Translation(base_loc)
    # The arm points along local +Y. This pan turns +Y to the heading.
    pan = math.degrees(math.atan2(-heading.x, heading.y))

    arm = _member(scene, rig_id, "CK_Crane_Arm", display='SINGLE_ARROW')
    arm.parent = root
    head = _member(scene, rig_id, "CK_Crane_Head", display='CUBE', size=0.15)
    head.parent = arm

    _def_prop(root, "ck_pan", pan, -360.0, 360.0, "Crane pan (degrees)")
    _def_prop(root, "ck_boom", boom, -80.0, 80.0, "Boom up/down (degrees)")
    _def_prop(root, "ck_length", length, 0.1, max(30.0, length),
              "Arm length (metres)")
    _def_prop(root, "ck_tilt", opts.get("tilt", 0.0), -90.0, 90.0,
              "Head tilt, level-compensated (degrees)")

    utils.add_driver(root, "rotation_euler", "radians(ck_pan)",
                     [("ck_pan", root, '["ck_pan"]')], index=2)
    utils.add_driver(arm, "rotation_euler", "radians(ck_boom)",
                     [("ck_boom", root, '["ck_boom"]')], index=0)
    utils.add_driver(head, "location", "ck_len",
                     [("ck_len", root, '["ck_length"]')], index=1)
    # Counter-rotate the boom so ck_tilt is absolute pitch.
    utils.add_driver(head, "rotation_euler",
                     "radians(ck_tilt - ck_boom)",
                     [("ck_tilt", root, '["ck_tilt"]'),
                      ("ck_boom", root, '["ck_boom"]')], index=0)
    _attach(context, cam_obj, head, pre_world)
    return root


def create_handheld(context, cam_obj, profile='DOCUMENTARY', opts=None):
    """Handheld: procedural shake on top of the camera's own movement.

    F-modifier noise animates a shake empty. Drivers copy that noise, scaled
    by the keyframeable ck_intensity (0-2), into the delta transforms of the
    camera. The delta transforms add to the normal transforms. So the
    camera keeps its keyframes, its parent and its constraints, and the
    shake plays on top. The rig needs no per-frame handler.

    A camera in axis-angle rotation mode gets location shake only.

    opts (Advanced Mode): intensity.
    """
    opts = opts or {}
    if profile not in HANDHELD_PROFILES:
        raise CKError(f"There is no handheld profile named '{profile}'.")
    scene = context.scene
    root, rig_id, _pre_world = _prepare_rig(context, cam_obj, K.RIG_HANDHELD,
                                            "CK_Handheld")
    _point(root, "Base", movable=False)
    prof = HANDHELD_PROFILES[profile]
    root["cinekit_handheld_profile"] = profile

    shake = _member(scene, rig_id, "CK_Handheld_Shake", size=0.2)
    shake.parent = root
    shake.animation_data_create()
    action = bpy.data.actions.new(f"CK_HandheldNoise_{rig_id}")
    action[K.TAG] = rig_id
    shake.animation_data.action = action
    _noise_channels(action, shake, prof)

    _def_prop(root, "ck_intensity", opts.get("intensity", 1.0), 0.0, 2.0,
              "Master handheld intensity. Keyframe it to settle or "
              "escalate across a shot")

    intensity = ("ck_int", root, '["ck_intensity"]')
    for axis, channel in enumerate("XYZ"):
        utils.add_driver(cam_obj, "delta_location", "ck_noise * ck_int",
                         [("ck_noise", 'TRANSFORMS', shake, f"LOC_{channel}"),
                          intensity], index=axis)
    mode = cam_obj.rotation_mode
    if mode == 'QUATERNION':
        # Small-angle quaternion: w stays 1, x/y/z are half the angle.
        # Blender normalizes the quaternion before it builds the matrix.
        for axis, channel in enumerate("XYZ"):
            utils.add_driver(
                cam_obj, "delta_rotation_quaternion",
                "ck_noise * ck_int * 0.5",
                [("ck_noise", 'TRANSFORMS', shake, f"ROT_{channel}"),
                 intensity], index=axis + 1)
    elif mode != 'AXIS_ANGLE':
        for axis, channel in enumerate("XYZ"):
            utils.add_driver(
                cam_obj, "delta_rotation_euler", "ck_noise * ck_int",
                [("ck_noise", 'TRANSFORMS', shake, f"ROT_{channel}"),
                 intensity], index=axis)
    return root


def create_orbit(context, cam_obj, opts=None):
    """Orbit/turntable: the pivot turns with time, and the carrier holds
    the camera at a radius and a height.

    The drivers read the frame rate of the scene, so a later frame-rate
    change keeps the speed in degrees per second.

    opts (Advanced Mode): speed. The radius, height and start angle come
    from the camera. You can edit them later in the Rig Settings panel.
    """
    opts = opts or {}
    scene = context.scene
    root, rig_id, pre_world = _prepare_rig(context, cam_obj, K.RIG_ORBIT,
                                           "CK_Orbit")
    _point(root, "Target")
    cam_loc = pre_world.translation
    fwd = pre_world.to_quaternion() @ Vector((0, 0, -1))
    focus = cam_obj.data.dof.focus_distance or 5.0
    pivot = cam_loc + fwd * focus
    root.matrix_world = Matrix.Translation(pivot)

    rel = cam_loc - pivot
    # No minimum: a camera straight above the target has radius 0, and a
    # minimum would move it.
    radius = math.hypot(rel.x, rel.y)
    azimuth = math.degrees(math.atan2(rel.y, rel.x))
    fps = scene.render.fps / scene.render.fps_base
    speed = opts.get("speed", 30.0)
    # The rotation at the current frame must equal the camera azimuth, so
    # the camera starts exactly where it is.
    offset = azimuth - speed * scene.frame_current / fps
    offset = (offset + 180.0) % 360.0 - 180.0

    carrier = _member(scene, rig_id, "CK_Orbit_Carrier", display='CUBE',
                      size=0.15)
    _point(carrier, "Carrier", movable=False)
    carrier.parent = root
    _def_prop(root, "ck_radius", radius, 0.0, max(200.0, radius),
              "Orbit radius (metres)")
    _def_prop(root, "ck_height", rel.z, -100.0, 100.0,
              "Camera height above the target (metres)")
    _def_prop(root, "ck_speed", speed, -720.0, 720.0,
              "Orbit speed (degrees per second)")
    _def_prop(root, "ck_offset", offset, -360.0, 360.0,
              "Start angle at frame 0 (degrees)")

    utils.add_driver(root, "rotation_euler",
                     "radians(ck_speed) * frame * ck_fpsb / ck_fps "
                     "+ radians(ck_offset)",
                     [("ck_speed", root, '["ck_speed"]'),
                      ("ck_offset", root, '["ck_offset"]'),
                      ("ck_fps", scene, "render.fps", 'SCENE'),
                      ("ck_fpsb", scene, "render.fps_base", 'SCENE')],
                     index=2)
    utils.add_driver(carrier, "location", "ck_rad",
                     [("ck_rad", root, '["ck_radius"]')], index=0)
    utils.add_driver(carrier, "location", "ck_hgt",
                     [("ck_hgt", root, '["ck_height"]')], index=2)

    track = cam_obj.constraints.new('TRACK_TO')
    track.name = CON_PREFIX + "Track Target"
    track.target = root
    track.track_axis = 'TRACK_NEGATIVE_Z'
    track.up_axis = 'UP_Y'
    _attach(context, cam_obj, carrier, pre_world)
    return root


def orbit_one_revolution(scene, root):
    """Set ck_speed so the orbit makes exactly 360 degrees over the scene
    frame range. The camera keeps its angle at the first frame."""
    frames = scene.frame_end - scene.frame_start + 1
    if frames < 2:
        raise CKError("The scene frame range is too short for one "
                      "revolution.")
    fps = scene.render.fps / scene.render.fps_base
    start = scene.frame_start
    angle_at_start = root["ck_offset"] + root["ck_speed"] * start / fps
    speed = 360.0 * fps / frames
    root["ck_speed"] = speed
    root["ck_offset"] = (angle_at_start - speed * start / fps
                         + 180.0) % 360.0 - 180.0
    root.update_tag()


def _cameras_in_rig(rig_id):
    return [o for o in bpy.data.objects
            if o.type == 'CAMERA'
            and o.get("cinekit_rig_member_of") == rig_id]


def _restore_camera(cam_obj):
    prev = utils.fetch_state(cam_obj, K.PREV_CAMERA_RIG)
    if prev is None:
        cam_obj.parent = None
        return
    parent_name = prev.get("parent")
    parent = bpy.data.objects.get(parent_name) if parent_name else None
    if "basis" in prev:
        cam_obj.parent = parent
        cam_obj.matrix_parent_inverse = _unflat(prev["parent_inverse"])
        cam_obj.matrix_basis = _unflat(prev["basis"])
        cam_obj.delta_location = prev["delta_location"]
        cam_obj.delta_rotation_euler = prev["delta_rotation_euler"]
        cam_obj.delta_rotation_quaternion = prev["delta_rotation_quaternion"]
    else:                     # state stored by an older CineKit
        cam_obj.parent = parent
        cam_obj.matrix_world = _unflat(prev["matrix"])
    utils.clear_state(cam_obj, K.PREV_CAMERA_RIG)


def remove_rig(context, root):
    """Delete only tagged rig data. Restore the parent, the transforms and
    the delta transforms of each camera, and remove CineKit constraints
    and drivers."""
    rig_id = root.get("cinekit_rig_uid")
    if not rig_id:
        raise CKError("The selected object is not a CineKit rig root.")

    for cam_obj in _cameras_in_rig(rig_id):
        _strip_rig_from_camera(cam_obj)
        _restore_camera(cam_obj)

    members = [o for o in bpy.data.objects
               if o.get(K.TAG_RIG_MEMBER) == rig_id]
    for obj in members + [root]:
        bpy.data.objects.remove(obj, do_unlink=True)
    for curve in [c for c in bpy.data.curves if c.get(K.TAG) == rig_id]:
        if curve.users == 0:
            bpy.data.curves.remove(curve)
    for action in [a for a in bpy.data.actions if a.get(K.TAG) == rig_id]:
        if action.users == 0:
            bpy.data.actions.remove(action)
