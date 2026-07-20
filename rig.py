# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Central apply layer for cameras and rigs.

Translates high-level PropertyGroup settings (properties.py) into Blender
data: camera params, scene exposure, motion blur, white balance, drivers,
rig hierarchies. The UI and operators never set low-level values directly —
they call into this module.

Engine support: everything here is engine-agnostic (Cycles + EEVEE Next).
Motion blur writes the unified render setting and the EEVEE-specific one
when present.
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


# =============================================================== physical cam
def shutter_time_s(ck, fps):
    return optics.shutter_time_s(ck.shutter_mode, ck.shutter_speed,
                                 ck.shutter_angle, fps)


def compute_ev100(cam_data, scene):
    ck = cam_data.cinekit
    fps = scene.render.fps / scene.render.fps_base
    return optics.ev100(ck.aperture, shutter_time_s(ck, fps), ck.iso)


def dof_readout(cam_data, scene):
    """(hyperfocal_m, near_m, far_m, total_m) for the UI panel."""
    ck = cam_data.cinekit
    coc = optics.coc_mm(cam_data.sensor_width, cam_data.sensor_height)
    focus_m = cam_data.dof.focus_distance
    if cam_data.dof.focus_object:
        cam_obj = _object_of_camera(cam_data)
        if cam_obj:
            focus_m = (cam_obj.matrix_world.translation -
                       cam_data.dof.focus_object.matrix_world.translation
                       ).length
    focus_mm = max(focus_m, 0.001) * 1000.0
    h = optics.hyperfocal_mm(cam_data.lens, ck.aperture, coc)
    near, far = optics.dof_limits_mm(cam_data.lens, ck.aperture,
                                     focus_mm, coc)
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
    """Push camera-local settings: DOF f-stop, breathing driver refresh."""
    ck = cam_data.cinekit
    if not ck.enabled or utils.is_linked(cam_data):
        return
    with utils.suppress_updates():
        cam_data.dof.aperture_fstop = ck.aperture
    if ck.focus_breathing:
        try:
            set_breathing(cam_data)
        except CKError as exc:
            print(f"CineKit: breathing driver refresh failed: {exc}")


def sync_active_camera(scene):
    """Make the scene's active camera the exposure camera.

    Called on active-camera change (msgbus / marker switch) and on physical
    setting edits. One camera owns scene exposure at a time.
    """
    cam = scene.camera
    if (cam is None or cam.type != 'CAMERA'
            or not cam.data.cinekit.enabled
            or utils.is_linked(cam.data)):
        return
    ck = cam.data.cinekit
    take_exposure_control(scene)
    ev = compute_ev100(cam.data, scene)
    scene.view_settings.exposure = optics.scene_exposure_for_ev100(
        ev, ck.exposure_comp)

    if ck.drive_motion_blur:
        fps = scene.render.fps / scene.render.fps_base
        shutter = optics.motion_blur_shutter(
            ck.shutter_mode, ck.shutter_speed, ck.shutter_angle, fps)
        shutter = min(max(shutter, 0.01), 2.0)
        scene.render.motion_blur_shutter = shutter
        if hasattr(scene, "eevee") and hasattr(scene.eevee,
                                               "motion_blur_shutter"):
            scene.eevee.motion_blur_shutter = shutter  # pre-4.2 fallback

    apply_white_balance(scene, cam.data)


def on_camera_settings_changed(cam_data, scene):
    apply_camera(cam_data, scene)
    if scene and scene.camera and scene.camera.data == cam_data:
        sync_active_camera(scene)


def take_exposure_control(scene):
    """Store the user's exposure once, before CineKit starts driving it."""
    if utils.fetch_state(scene, K.PREV_EXPOSURE) is None:
        utils.store_state(scene, K.PREV_EXPOSURE, {
            "exposure": scene.view_settings.exposure,
        })


def release_exposure_control(scene):
    """Restore pre-CineKit exposure. Also drops compositor WB."""
    prev = utils.fetch_state(scene, K.PREV_EXPOSURE)
    if prev is not None:
        scene.view_settings.exposure = prev["exposure"]
        utils.clear_state(scene, K.PREV_EXPOSURE)
    _set_native_wb(scene, None)
    from .looks import engine
    engine.set_white_balance(scene, None)


def _set_native_wb(scene, gains_spec):
    """Blender 4.3+ has white balance in view settings; use it when present.

    gains_spec: (kelvin, tint) or None to disable/restore.
    Returns True if native WB was handled (no compositor WB needed).
    """
    vs = scene.view_settings
    if not hasattr(vs, "white_balance_temperature"):
        return False
    if gains_spec is None:
        vs.use_white_balance = False
        return True
    kelvin, tint = gains_spec
    vs.use_white_balance = True
    vs.white_balance_temperature = kelvin
    vs.white_balance_tint = tint * 50.0  # our -1..1 to Blender's -150..150
    return True


def apply_white_balance(scene, cam_data):
    """WB approach (documented): on Blender 4.3+ the native view-transform
    white balance gives a live, render-consistent result. On 4.2 we insert a
    per-channel gain (from optics.wb_gains, planckian) into the CineKit
    compositor pipeline — live in the viewport only with the Viewport
    Compositor enabled, always in the final render."""
    ck = cam_data.cinekit
    from .looks import engine
    if not ck.wb_enabled:
        if not _set_native_wb(scene, None):
            engine.set_white_balance(scene, None)
        return
    if _set_native_wb(scene, (ck.wb_temperature, ck.wb_tint)):
        engine.set_white_balance(scene, None)
        return
    gains = optics.wb_gains(ck.wb_temperature, ck.wb_tint)
    engine.set_white_balance(scene, gains)


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
            ck.aperture = lens["max_aperture"]  # lens can't open wider


def set_breathing(cam_data):
    """Create/remove the focus-breathing driver on focal length.

    Model (stylised, documented): focal length grows by
    amount * 5% / max(focus_m, 0.25) — visible on close focus, negligible
    at distance. `scale` comes from the active look's breathing_scale.
    """
    require = utils.require_editable
    require(cam_data, "Camera")
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
            raise CKError("Camera data has no object in the file")
        variables = [("ck_dist", 'LOC_DIFF', cam_obj,
                      cam_data.dof.focus_object)]
    else:
        variables = [("ck_dist", cam_data, "dof.focus_distance")]
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
    """Walk up the parent chain to a CineKit rig root, else None."""
    o = obj
    while o is not None:
        if o.get(K.TAG_RIG):
            return o
        o = o.parent
    return None


def _def_prop(obj, name, value, vmin, vmax, description):
    obj[name] = float(value)
    obj.id_properties_ui(name).update(
        min=vmin, max=vmax, soft_min=vmin, soft_max=vmax,
        description=description)


def _store_prerig(cam_obj, rig_id):
    utils.store_state(cam_obj, K.PREV_CAMERA_RIG, {
        "rig": rig_id,
        "matrix": [v for row in cam_obj.matrix_world for v in row],
        "parent": cam_obj.parent.name if cam_obj.parent else None,
    })


def _parent_keep_world(child, parent):
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()


def _prepare_rig(context, cam_obj, rig_type, root_name):
    utils.require_editable(cam_obj, "Camera object")
    utils.require_editable(cam_obj.data, "Camera")
    if rig_root_of(cam_obj) is not None:
        raise CKError("Camera already belongs to a CineKit rig — "
                      "remove it first (Rigs panel > Remove Rig)")
    rig_id = utils.new_id("rig_")
    _store_prerig(cam_obj, rig_id)
    cam_obj["cinekit_rig_member_of"] = rig_id
    root = utils.new_empty(context.scene, root_name, display='CUBE', size=0.3)
    root[K.TAG_RIG] = rig_type
    root["cinekit_rig_uid"] = rig_id
    root.matrix_world = cam_obj.matrix_world.copy()
    return root, rig_id


def _member(scene, rig_id, name, display='PLAIN_AXES', size=0.35):
    obj = utils.new_empty(scene, name, display=display, size=size)
    obj[K.TAG_RIG_MEMBER] = rig_id
    return obj


def _point(obj, role, movable=True):
    """Tag an object as a named, user-editable rig point (Advanced Mode).
    movable=False for points whose location is driver-controlled (the panel
    offers Select only, not location editing)."""
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


def set_dolly_path(root, world_pts):
    """Replace the dolly rig's path shape with world_pts (>= 2 points).
    Points are stored in the path object's local space so the existing
    Follow Path constraint and ck_position driver keep working."""
    rig_id = root.get("cinekit_rig_uid")
    path = next((o for o in bpy.data.objects
                 if o.get(K.TAG_RIG_MEMBER) == rig_id
                 and o.type == 'CURVE'), None)
    if path is None:
        raise CKError("Dolly path object not found")
    if len(world_pts) < 2:
        raise CKError("Need at least 2 points to define a path")
    inv = path.matrix_world.inverted()
    curve = path.data
    curve.splines.clear()
    spline = curve.splines.new('POLY')
    spline.points.add(len(world_pts) - 1)
    for i, wp in enumerate(world_pts):
        local = inv @ wp
        spline.points[i].co = (local.x, local.y, local.z, 1.0)
    return path


def create_dolly(context, cam_obj, opts=None):
    """Dolly: curve path + carrier. Props on root: ck_position (0-1,
    keyframeable), ck_track (0-1). Banking toggles the Follow Path
    constraint's curve-follow directly in the panel.

    opts (Advanced Mode): path_length, carrier_pos, aim_distance, track,
    banking, path_source ('GENERATED'|'CURVE'|'GREASE_PENCIL') and
    path_object (the source curve/GP for the latter two)."""
    opts = opts or {}
    half = opts.get("path_length", 6.0) / 2.0
    carrier_pos = opts.get("carrier_pos", 0.5)
    aim_distance = opts.get("aim_distance", 5.0)
    scene = context.scene
    root, rig_id = _prepare_rig(context, cam_obj, K.RIG_DOLLY, "CK_Dolly")
    _point(root, "Base")
    # Root sits at the camera; the path runs ±half m along the camera's local
    # X, so ck_position=0.5 keeps the camera at its original spot.
    root.matrix_world = Matrix.Translation(cam_obj.matrix_world.translation)

    curve = bpy.data.curves.new("CK_DollyPath", 'CURVE')
    curve.dimensions = '3D'
    curve[K.TAG] = rig_id
    spline = curve.splines.new('POLY')
    spline.points.add(1)
    spline.points[0].co = (-half, 0.0, 0.0, 1.0)
    spline.points[1].co = (half, 0.0, 0.0, 1.0)
    right = (cam_obj.matrix_world.to_quaternion() @ Vector((1, 0, 0)))
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
    _point(carrier, "Carrier", movable=False)  # location driven by ck_position
    carrier.parent = root
    con = carrier.constraints.new('FOLLOW_PATH')
    con.name = CON_PREFIX + "Follow Path"
    con.target = path
    con.use_fixed_location = True
    con.use_curve_follow = opts.get("banking", False)
    con.forward_axis = 'FORWARD_X'
    con.up_axis = 'UP_Z'

    aim = _member(scene, rig_id, "CK_Dolly_Aim", display='SPHERE', size=0.15)
    _point(aim, "Aim")
    fwd = cam_obj.matrix_world.to_quaternion() @ Vector((0, 0, -1))
    aim.matrix_world = Matrix.Translation(
        cam_obj.matrix_world.translation + fwd * aim_distance)

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

    _parent_keep_world(cam_obj, carrier)

    # Custom path from a drawn Grease Pencil stroke or an existing curve.
    source = opts.get("path_source", 'GENERATED')
    src_obj = opts.get("path_object")
    if source != 'GENERATED' and src_obj is not None:
        pts = (stroke_points_world(src_obj) if source == 'GREASE_PENCIL'
               else curve_points_world(src_obj))
        if len(pts) >= 2:
            set_dolly_path(root, pts)
        else:
            raise CKError("Selected path source has no usable stroke/points")
    return root


def create_crane(context, cam_obj, opts=None):
    """Crane/jib: base (pan) -> arm (boom) -> head (tilt, length).

    opts (Advanced Mode): base_drop, boom, tilt."""
    opts = opts or {}
    scene = context.scene
    root, rig_id = _prepare_rig(context, cam_obj, K.RIG_CRANE, "CK_Crane")
    _point(root, "Base")
    cam_loc = cam_obj.matrix_world.translation
    base_loc = Vector((cam_loc.x, cam_loc.y,
                       max(cam_loc.z - opts.get("base_drop", 1.5), 0.0)))
    root.matrix_world = Matrix.Translation(base_loc)

    arm = _member(scene, rig_id, "CK_Crane_Arm", display='SINGLE_ARROW')
    arm.parent = root
    head = _member(scene, rig_id, "CK_Crane_Head", display='CUBE', size=0.15)
    head.parent = arm

    length0 = max((cam_loc - base_loc).length, 0.5)
    _def_prop(root, "ck_pan", 0.0, -360.0, 360.0, "Crane pan (degrees)")
    _def_prop(root, "ck_boom", opts.get("boom", 45.0), -80.0, 80.0,
              "Boom up/down (degrees)")
    _def_prop(root, "ck_length", length0, 0.5, 20.0, "Arm length (metres)")
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
    _parent_keep_world(cam_obj, head)
    return root


def create_handheld(context, cam_obj, profile='DOCUMENTARY', opts=None):
    """Handheld: F-modifier noise on a shake empty, blended into the camera
    by a Copy Transforms constraint whose influence is the keyframeable
    ck_intensity master prop. No per-frame handlers.

    opts (Advanced Mode): intensity."""
    opts = opts or {}
    if profile not in HANDHELD_PROFILES:
        raise CKError(f"Unknown handheld profile '{profile}'")
    scene = context.scene
    root, rig_id = _prepare_rig(context, cam_obj, K.RIG_HANDHELD,
                                "CK_Handheld")
    _point(root, "Base")
    prof = HANDHELD_PROFILES[profile]
    root["cinekit_handheld_profile"] = profile

    shake = _member(scene, rig_id, "CK_Handheld_Shake", size=0.2)
    shake.parent = root
    shake.animation_data_create()
    action = bpy.data.actions.new(f"CK_HandheldNoise_{rig_id}")
    action[K.TAG] = rig_id
    shake.animation_data.action = action
    new_fcurve = utils.action_fcurve_factory(action, shake)
    for path, strengths, to_rad in (("location", prof["loc"], False),
                                    ("rotation_euler", prof["rot"], True)):
        for axis in range(3):
            fcu = new_fcurve(path, axis)
            fcu.keyframe_points.insert(0.0, 0.0)
            mod = fcu.modifiers.new('NOISE')
            mod.scale = prof["scale"] * (1.0 + 0.13 * axis)  # decorrelate
            mod.strength = (math.radians(strengths[axis]) if to_rad
                            else strengths[axis])
            mod.phase = float(axis * 37 + (101 if to_rad else 0))
            mod.depth = 1

    _def_prop(root, "ck_intensity", opts.get("intensity", 1.0), 0.0, 2.0,
              "Master handheld intensity — keyframe it to settle or "
              "escalate across a shot")
    con = cam_obj.constraints.new('COPY_TRANSFORMS')
    con.name = CON_PREFIX + "Handheld Shake"
    con.target = shake
    utils.add_driver(cam_obj, f'constraints["{con.name}"].influence',
                     "ck_int", [("ck_int", root, '["ck_intensity"]')])
    _parent_keep_world(cam_obj, root)
    return root


def create_orbit(context, cam_obj, opts=None):
    """Orbit/turntable: pivot rotates with time, carrier holds the camera at
    radius/height. fps is baked into the driver at creation time.

    opts (Advanced Mode): speed. Radius/height/start-angle stay derived from
    the camera and are editable afterwards in the Rig Settings panel."""
    opts = opts or {}
    scene = context.scene
    root, rig_id = _prepare_rig(context, cam_obj, K.RIG_ORBIT, "CK_Orbit")
    _point(root, "Target")
    cam_loc = cam_obj.matrix_world.translation
    fwd = cam_obj.matrix_world.to_quaternion() @ Vector((0, 0, -1))
    focus = cam_obj.data.dof.focus_distance or 5.0
    pivot = cam_loc + fwd * focus
    root.matrix_world = Matrix.Translation(pivot)

    rel = cam_loc - pivot
    radius = max(math.hypot(rel.x, rel.y), 0.1)
    azimuth = math.degrees(math.atan2(rel.y, rel.x))
    fps = scene.render.fps / scene.render.fps_base

    carrier = _member(scene, rig_id, "CK_Orbit_Carrier", display='CUBE',
                      size=0.15)
    _point(carrier, "Carrier", movable=False)
    carrier.parent = root
    _def_prop(root, "ck_radius", radius, 0.1, 200.0, "Orbit radius (metres)")
    _def_prop(root, "ck_height", rel.z, -100.0, 100.0,
              "Camera height above the target (metres)")
    _def_prop(root, "ck_speed", opts.get("speed", 30.0), -720.0, 720.0,
              "Orbit speed (degrees per second)")
    _def_prop(root, "ck_offset", azimuth, -360.0, 360.0,
              "Start angle (degrees)")

    utils.add_driver(root, "rotation_euler",
                     f"radians(ck_speed) * frame / {fps:.4f} "
                     "+ radians(ck_offset)",
                     [("ck_speed", root, '["ck_speed"]'),
                      ("ck_offset", root, '["ck_offset"]')], index=2)
    utils.add_driver(carrier, "location", "ck_rad",
                     [("ck_rad", root, '["ck_radius"]')], index=0)
    utils.add_driver(carrier, "location", "ck_hgt",
                     [("ck_hgt", root, '["ck_height"]')], index=2)

    track = cam_obj.constraints.new('TRACK_TO')
    track.name = CON_PREFIX + "Track Target"
    track.target = root
    track.track_axis = 'TRACK_NEGATIVE_Z'
    track.up_axis = 'UP_Y'
    _parent_keep_world(cam_obj, carrier)
    return root


def orbit_one_revolution(scene, root):
    """Set ck_speed so the orbit completes exactly 360° over the scene
    frame range (product-shot turntable)."""
    frames = scene.frame_end - scene.frame_start + 1
    if frames < 2:
        raise CKError("Scene frame range is too short for a revolution")
    fps = scene.render.fps / scene.render.fps_base
    root["ck_speed"] = 360.0 * fps / frames
    root.update_tag()


def _cameras_in_rig(rig_id):
    return [o for o in bpy.data.objects
            if o.type == 'CAMERA'
            and o.get("cinekit_rig_member_of") == rig_id]


def remove_rig(context, root):
    """Delete only tagged rig data; restore each camera's pre-rig transform,
    parent, and remove CineKit constraints/drivers."""
    rig_id = root.get("cinekit_rig_uid")
    if not rig_id:
        raise CKError("Selected object is not a CineKit rig root")

    for cam_obj in _cameras_in_rig(rig_id):
        for con in list(cam_obj.constraints):
            if con.name.startswith(CON_PREFIX):
                utils.remove_driver(
                    cam_obj, f'constraints["{con.name}"].influence')
                cam_obj.constraints.remove(con)
        prev = utils.fetch_state(cam_obj, K.PREV_CAMERA_RIG)
        cam_obj.parent = None
        if prev:
            parent_name = prev.get("parent")
            if parent_name and parent_name in bpy.data.objects:
                cam_obj.parent = bpy.data.objects[parent_name]
            vals = prev["matrix"]
            cam_obj.matrix_world = Matrix(
                [vals[i * 4:i * 4 + 4] for i in range(4)])
        utils.clear_state(cam_obj, K.PREV_CAMERA_RIG)
        if "cinekit_rig_member_of" in cam_obj.keys():
            del cam_obj["cinekit_rig_member_of"]

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
