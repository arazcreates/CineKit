# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Headless smoke test — run inside Blender:

    blender -b --factory-startup -P tests/smoke.py

Applies every shipped Look, adds every rig and lighting preset, removes
them all, and asserts the scene datablock counts match the starting state.
It also checks the behavior that earlier versions got wrong: camera
positions when a rig attaches, exposure ownership, look framing, per-scene
look trees, stored enum values and gobo cleanup. Exits non-zero on failure.

The extension package is imported directly from the repo (parent dir added
to sys.path), so this works without installing the extension first.
"""

import math
import os
import sys
import warnings

import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(ROOT))

FAILURES = []
DEPRECATIONS = []


def _record_warning(message, category, filename, lineno, file=None,
                    line=None):
    path = os.path.abspath(filename)
    if (issubclass(category, DeprecationWarning)
            and path.startswith(ROOT + os.sep)
            and os.sep + "tests" + os.sep not in path):
        DEPRECATIONS.append(f"{os.path.relpath(path, ROOT)}:{lineno}: "
                            f"{message}")


# Report deprecated Blender API that CineKit code uses, not only warnings
# from this script.
warnings.showwarning = _record_warning
warnings.simplefilter("always", DeprecationWarning)

PKG = os.path.basename(ROOT)
cinekit = __import__(PKG)


def check(label, condition, detail=""):
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label} {detail}")
    if not condition:
        FAILURES.append(f"{label} {detail}")


def close(a, b, eps=1e-4):
    return abs(a - b) <= eps


def snapshot():
    return {
        "objects": len(bpy.data.objects),
        "lights": len(bpy.data.lights),
        "cameras": len(bpy.data.cameras),
        "curves": len(bpy.data.curves),
        "actions": len(bpy.data.actions),
        "node_groups": len(bpy.data.node_groups),
        "textures": len(bpy.data.textures),
        "images": len(bpy.data.images),
        "materials": len(bpy.data.materials),
        "meshes": len(bpy.data.meshes),
        "collections": len(bpy.data.collections),
        "scenes": len(bpy.data.scenes),
    }


def compare(label, before, after):
    diffs = {key: (before[key], after[key]) for key in before
             if before[key] != after[key]}
    check(label, not diffs, f"leaked: {diffs}" if diffs else "")


def matrix_delta(a, b):
    return max(abs(x - y) for row_a, row_b in zip(a, b)
               for x, y in zip(row_a, row_b))


def update():
    bpy.context.view_layer.update()


def not_simple_drivers():
    """Scripted drivers that need the Python interpreter."""
    bad = []
    pools = (bpy.data.objects, bpy.data.cameras, bpy.data.node_groups,
             bpy.data.scenes)
    for pool in pools:
        for id_block in pool:
            anim = id_block.animation_data
            if anim is None:
                continue
            for fcu in anim.drivers:
                drv = fcu.driver
                if drv.type == 'SCRIPTED' and not drv.is_simple_expression:
                    bad.append(f"{id_block.name}:{fcu.data_path}="
                               f"{drv.expression}")
    return bad


def clear_animation(*holders):
    for holder in holders:
        anim = holder.animation_data
        if anim is None:
            continue
        action = anim.action
        holder.animation_data_clear()
        if action is not None and action.users == 0:
            bpy.data.actions.remove(action)


def build_stage():
    """A camera and a subject to work with."""
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    scene.render.resolution_percentage = 100
    cam_data = bpy.data.cameras.new("SmokeCam")
    cam_obj = bpy.data.objects.new("SmokeCam", cam_data)
    scene.collection.objects.link(cam_obj)
    cam_obj.location = (0.0, -6.0, 2.0)
    cam_obj.rotation_euler = (math.radians(80.0), 0.0, math.radians(8.0))
    cam_data.dof.focus_distance = 5.0
    scene.camera = cam_obj
    mesh = bpy.data.meshes.new("SmokeCube")
    mesh.from_pydata(
        [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
         (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
        [],
        [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1), (1, 5, 6, 2),
         (2, 6, 7, 3), (3, 7, 4, 0)])
    mesh.validate()
    cube = bpy.data.objects.new("SmokeCube", mesh)
    scene.collection.objects.link(cube)
    return scene, cam_obj, cube


def look_tree(engine, scene):
    node = engine._find_pipeline_node(scene)
    if node is None or node.node_tree is None:
        return None
    look_node = engine._find_node(node.node_tree, "look")
    return look_node.node_tree if look_node is not None else None


def horizontal_fov(cam, scene):
    aspect = (scene.render.resolution_x * scene.render.pixel_aspect_x) / \
        (scene.render.resolution_y * scene.render.pixel_aspect_y)
    if cam.sensor_fit == 'VERTICAL':
        width = cam.sensor_height * aspect
    elif cam.sensor_fit == 'AUTO' and aspect < 1.0:
        width = cam.sensor_width * aspect
    else:
        width = cam.sensor_width
    return 2.0 * math.atan(width / (2.0 * cam.lens))


# =================================================================== sections
def test_looks(m, scene):
    engine, looks = m["engine"], m["looks"]
    print("\n== Looks ==")
    comp_tree_before = engine._get_comp_tree(scene)
    base = snapshot()
    for look_id in sorted(looks.all_looks()):
        engine.apply_look(scene, look_id)
        check(f"apply {look_id}", scene.cinekit.active_look == look_id)
        engine.apply_look(scene, look_id)  # idempotent re-apply
        pipeline_nodes = [n for n in engine._get_comp_tree(scene).nodes
                          if n.get("cinekit_id") == "pipeline"]
        check(f"{look_id}: exactly one pipeline node",
              len(pipeline_nodes) == 1)
        tree = look_tree(engine, scene)
        screen = [n.name for n in tree.nodes
                  if getattr(n, "blend_type", "") == 'SCREEN']
        check(f"{look_id}: no Screen blend on scene-linear light",
              not screen, f"({screen})")
    bad = not_simple_drivers()
    check("look drivers are simple expressions", not bad, f"({bad})")
    engine.remove_look(scene)
    compare("looks removed: datablock counts restored", base, snapshot())
    check("compositor state restored",
          engine._get_comp_tree(scene) == comp_tree_before,
          f"({engine._get_comp_tree(scene)} vs {comp_tree_before})")


def test_hdr_highlights(m, scene):
    engine = m["engine"]
    print("\n== Looks on very bright pixels ==")
    import tempfile
    before = snapshot()
    render = scene.render
    ims = render.image_settings
    saved = (render.resolution_x, render.resolution_y, render.filepath,
             ims.file_format, ims.color_depth, scene.cycles.samples,
             scene.compositing_node_group)
    render.resolution_x, render.resolution_y = 160, 90
    render.resolution_percentage = 100
    ims.file_format = 'OPEN_EXR'
    ims.color_depth = '32'
    scene.cycles.samples = 1
    tree = bpy.data.node_groups.new("ck_hdr", 'CompositorNodeTree')
    tree.interface.new_socket("Image", in_out='OUTPUT',
                              socket_type='NodeSocketColor')
    feed = tree.nodes.new('CompositorNodeImage')
    tree.links.new(feed.outputs[0], tree.nodes.new('NodeGroupOutput').inputs[0])
    scene.compositing_node_group = tree
    folder = tempfile.TemporaryDirectory(prefix="ck_smoke_")

    def shot(background, spot=None):
        """The scene-linear result for an image at the size of the frame."""
        height, width = render.resolution_y, render.resolution_x
        pixels = np.ones((height, width, 4), dtype=np.float32)
        pixels[..., :3] = background
        if spot is not None:
            pixels[height // 3:height * 2 // 3,
                   width // 3:width * 2 // 3, :3] = spot
        image = bpy.data.images.new("ck_hdr", width, height, float_buffer=True)
        image.colorspace_settings.name = 'Non-Color'
        image.pixels.foreach_set(pixels.ravel())
        feed.image = image
        render.filepath = os.path.join(folder.name, "shot.exr")
        bpy.ops.render.render(write_still=True)
        out = bpy.data.images.load(render.filepath)
        out.colorspace_settings.name = 'Non-Color'
        data = np.empty(len(out.pixels), dtype=np.float32)
        out.pixels.foreach_get(data)
        feed.image = None
        bpy.data.images.remove(out)
        bpy.data.images.remove(image)
        return data.reshape(-1, 4)[:, :3]

    def only(name):
        for param in scene.cinekit.look_params:
            param.value = 1.0 if param.name == name else 0.0
            engine.push_param(scene, param)

    for look_id in ("16mm", "super8"):
        engine.apply_look(scene, look_id)
        rgb = shot(100.0)
        check(f"{look_id}: a pixel at 100 stays positive in every channel",
              rgb.min() > 0.0, f"(min {rgb.min():.3f})")
        check(f"{look_id}: a pixel at 100 stays bright", rgb.mean() > 50.0,
              f"(mean {rgb.mean():.3f})")
        only("Grain")
        rgb = shot(100.0)
        spread = rgb.std() / max(rgb.mean(), 1e-6)
        check(f"{look_id}: a pixel at 100 keeps its grain",
              0.001 < spread < 0.2, f"(relative spread {spread:.4f})")
        engine.remove_look(scene)

    engine.apply_look(scene, "vhs")
    only("Chroma Bleed")
    rgb = shot(0.18, (100.0, 0.0, 0.0))
    check("vhs: chroma bleed of a bright red light stays positive",
          rgb.min() >= 0.0, f"(min {rgb.min():.3f})")
    engine.remove_look(scene)

    folder.cleanup()
    (render.resolution_x, render.resolution_y, render.filepath,
     ims.file_format, ims.color_depth, scene.cycles.samples,
     scene.compositing_node_group) = saved
    bpy.data.node_groups.remove(tree)
    compare("bright pixels: counts restored", before, snapshot())


def test_look_camera_layer(m, scene, cam_obj):
    engine, rig, optics = m["engine"], m["rig"], m["optics"]
    print("\n== Look camera layer ==")
    cam = cam_obj.data
    ck = cam.cinekit
    vs = scene.view_settings
    vs.exposure = 0.0
    cam.lens, cam.sensor_width, cam.sensor_fit = 50.0, 36.0, 'AUTO'
    cam.dof.aperture_fstop = 2.8
    ck.enabled = True
    ck.aperture = 2.8
    fov = horizontal_fov(cam, scene)

    look_id = "digicam_2005" if "digicam_2005" in m["looks"].all_looks() \
        else None
    if look_id is None:
        print("  (digicam_2005 not shipped, section skipped)")
        ck.enabled = False
        return
    engine.apply_look(scene, look_id)
    check("look keeps the horizontal field of view",
          close(horizontal_fov(cam, scene), fov, 1e-6),
          f"({math.degrees(horizontal_fov(cam, scene)):.3f} vs "
          f"{math.degrees(fov):.3f} deg)")
    check("look sets its sensor", close(cam.sensor_width, 5.76))
    check("look DOF floor raises the depth-of-field f-stop",
          cam.dof.aperture_fstop >= 8.0 - 1e-6,
          f"(f/{cam.dof.aperture_fstop:.2f})")
    check("look DOF floor keeps the exposure aperture",
          close(ck.aperture, 2.8), f"(f/{ck.aperture:.2f})")
    ev = rig.compute_ev100(cam, scene)
    expected = optics.scene_exposure_for_ev100(ev, ck.exposure_comp + 0.3)
    check("physical exposure includes the look bias",
          close(vs.exposure, expected), f"({vs.exposure:.4f} vs "
                                        f"{expected:.4f})")
    engine.remove_look(scene)
    check("remove look restores the lens", close(cam.lens, 50.0),
          f"({cam.lens:.4f})")
    check("remove look restores the sensor",
          close(cam.sensor_width, 36.0) and cam.sensor_fit == 'AUTO')
    check("remove look restores the render size",
          scene.render.resolution_x == 1920
          and scene.render.resolution_y == 1080,
          f"({scene.render.resolution_x}x{scene.render.resolution_y})")
    check("remove look restores the f-stop",
          close(cam.dof.aperture_fstop, 2.8))
    expected = optics.scene_exposure_for_ev100(ev, ck.exposure_comp)
    check("remove look removes the bias", close(vs.exposure, expected))

    # A lens preset during a look describes the camera without the look.
    lens_id = next((lid for lid in m["lenses"].legacy_ids()
                    if lid != "NONE"), None)
    if lens_id is not None:
        focal = m["lenses"].get(lens_id)["focal"]
        engine.apply_look(scene, "16mm")          # sensor 12.52 mm
        ck.lens_preset = lens_id
        check("lens preset under a look keeps its framing",
              close(cam.lens, focal * 12.52 / 36.0, 1e-3),
              f"({cam.lens:.3f} vs {focal * 12.52 / 36.0:.3f})")
        engine.remove_look(scene)
        check("lens preset value after the look", close(cam.lens, focal),
              f"({cam.lens:.3f} vs {focal:.3f})")
        ck.lens_preset = 'NONE'
        cam.lens = 50.0

    # Release: the physical camera gives the exposure back.
    ck.enabled = False
    check("release restores the exposure", close(vs.exposure, 0.0),
          f"({vs.exposure:.4f})")

    # A camera that is not physical: the bias is an offset, and the edits
    # of the user stay.
    vs.exposure = 0.5
    engine.apply_look(scene, look_id)
    check("offset bias on a non-physical camera", close(vs.exposure, 0.8),
          f"({vs.exposure:.4f})")
    check("non-physical DOF floor", close(cam.dof.aperture_fstop, 8.0))
    vs.exposure = 1.0
    cam.dof.aperture_fstop = 11.0
    rig.sync_active_camera(scene)
    check("a camera sync keeps the user exposure", close(vs.exposure, 1.0),
          f"({vs.exposure:.4f})")
    engine.remove_look(scene)
    check("remove look takes only the offset away", close(vs.exposure, 0.7),
          f"({vs.exposure:.4f})")
    check("remove look keeps the f-stop that the user set",
          close(cam.dof.aperture_fstop, 11.0))
    cam.dof.aperture_fstop = 2.8
    vs.exposure = 0.0

    # White balance: CineKit gives back the native values of the user.
    ck.enabled = True
    vs.use_white_balance = False
    ck.wb_temperature = 3200
    ck.wb_enabled = True
    check("white balance on", vs.use_white_balance
          and close(vs.white_balance_temperature, 3200, 1.0))
    ck.wb_enabled = False
    check("white balance given back", not vs.use_white_balance)
    ck.enabled = False
    check("no stored exposure state after release",
          m["utils"].fetch_state(scene, m["K"].PREV_EXPOSURE) is None)


def test_scene_isolation(m, scene):
    engine = m["engine"]
    print("\n== Per-scene looks ==")
    before = snapshot()
    scene2 = bpy.data.scenes.new("SmokeScene2")
    cam2_data = bpy.data.cameras.new("SmokeCam2")
    cam2 = bpy.data.objects.new("SmokeCam2", cam2_data)
    scene2.collection.objects.link(cam2)
    scene2.camera = cam2
    scene2.render.resolution_x, scene2.render.resolution_y = 1280, 720

    engine.apply_look(scene, "vhs")
    engine.apply_look(scene2, "vhs")
    t1, t2 = look_tree(engine, scene), look_tree(engine, scene2)
    check("two scenes get two look trees", t1 is not None and t1 != t2)
    p1 = engine._find_pipeline_node(scene).node_tree
    p2 = engine._find_pipeline_node(scene2).node_tree
    check("two scenes get two pipeline trees", p1 != p2)
    targets = set()
    for fcu in t2.animation_data.drivers:
        for var in fcu.driver.variables:
            if var.targets[0].id_type == 'SCENE':
                targets.add(var.targets[0].id)
    check("scene drivers read their own scene", targets == {scene2},
          f"({[t.name for t in targets]})")
    param = scene.cinekit.look_params[0]
    param.value = 0.25
    look_node2 = engine._find_node(p2, "look")
    check("a parameter change stays in its scene",
          not close(look_node2.inputs[param.name].default_value, 0.25))

    # A copy of a scene with its own compositor tree.
    scene3 = scene.copy()
    comp3 = scene.compositing_node_group.copy()
    comp3_name = comp3.name
    scene3.compositing_node_group = comp3
    engine.apply_look(scene3, "super8")
    check("the copied scene gets its own id",
          scene3.get("cinekit_scene_uid") != scene.get("cinekit_scene_uid"))
    check("a look in the copy leaves the original look",
          look_tree(engine, scene).get("cinekit_look") == "vhs")
    engine.remove_look(scene3)
    bpy.data.scenes.remove(scene3)
    # The copy took over the stored compositor state of the original, so
    # Remove Look can delete the copied compositor tree.
    leftover = bpy.data.node_groups.get(comp3_name)
    if leftover is not None and leftover.users == 0:
        bpy.data.node_groups.remove(leftover)

    engine.remove_look(scene2)
    engine.remove_look(scene)
    bpy.data.objects.remove(cam2, do_unlink=True)
    bpy.data.cameras.remove(cam2_data)
    bpy.data.scenes.remove(scene2)
    compare("per-scene looks: counts restored", before, snapshot())


def test_timestamp(m, scene):
    engine = m["engine"]
    print("\n== Timestamp ==")
    before = snapshot()
    engine.apply_look(scene, "security_cam")
    tree = look_tree(engine, scene)
    image = bpy.data.images.get(tree.get("cinekit_timestamp_image", ""))
    check("timestamp image exists", image is not None)
    if image is not None:
        aspect = scene.render.resolution_x / scene.render.resolution_y
        check("timestamp image matches the render aspect",
              close(image.size[0] / image.size[1], aspect, 0.01),
              f"({image.size[0]}x{image.size[1]})")
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(pixels)
        sum_before = float(pixels.sum())
        scene.cinekit.security_timestamp = "CAM 07  12.12.2012 23:59"
        image.pixels.foreach_get(pixels)
        check("timestamp text updates without a rebuild",
              float(pixels.sum()) != sum_before
              and tree.get("cinekit_timestamp_text")
              == "CAM 07  12.12.2012 23:59")
    engine.remove_look(scene)
    compare("timestamp: counts restored", before, snapshot())


def test_enums(m, scene, cam_obj):
    looks, stable_enum, migrate = m["looks"], m["stable_enum"], m["migrate"]
    print("\n== Stored enum values ==")
    ck = scene.cinekit
    shot = ck.shots.add()
    shot.look = "vhs"
    raw = shot.get("look")
    check("shot look stores a stable number",
          raw == stable_enum.number_for("vhs"), f"({raw})")
    legacy = looks.legacy_ids()
    shot["look"] = legacy.index("super8")
    migrate.migrate_file()
    check("migration converts an old list position", shot.look == "super8",
          f"({shot.look})")
    migrate.migrate_file()
    check("migration runs one time only", shot.look == "super8")
    ck.shots.remove(len(ck.shots) - 1)


def test_rigs(m, scene, cam_obj, cube):
    rig = m["rig"]
    print("\n== Rigs ==")
    creators = [
        ("dolly", rig.create_dolly),
        ("crane", rig.create_crane),
        ("handheld", rig.create_handheld),
        ("orbit", rig.create_orbit),
    ]
    if hasattr(rig, "create_tripod"):
        creators += [
            ("tripod", rig.create_tripod),
            ("pedestal", rig.create_pedestal),
            ("overhead", rig.create_overhead),
            ("snorricam", lambda ctx, cam: rig.create_snorricam(ctx, cam,
                                                                cube)),
        ]
    for parented in (False, True):
        holder = None
        if parented:
            holder = bpy.data.objects.new("SmokeHolder", None)
            scene.collection.objects.link(holder)
            holder.location = (3.0, 1.0, 0.5)
            holder.rotation_euler = (0.2, -0.3, 0.9)
            update()
            world = cam_obj.matrix_world.copy()
            cam_obj.parent = holder
            cam_obj.matrix_world = world
            update()
        tag = "parented " if parented else ""
        for name, create in creators:
            before = snapshot()
            update()  # matrix_world is stale until the depsgraph runs
            matrix_before = cam_obj.matrix_world.copy()
            parent_before = cam_obj.parent
            root = create(bpy.context, cam_obj)
            check(f"{tag}{name}: camera in rig",
                  rig.rig_root_of(cam_obj) == root)
            if "ck_intensity" in root.keys():
                root["ck_intensity"] = 0.0
            if "ck_shake" in root.keys():
                root["ck_shake"] = 0.0
            root.update_tag()
            update()
            if name == "overhead":
                pivot = root.matrix_world.translation
                height = cam_obj.matrix_world.translation - pivot
                check(f"{tag}overhead: camera above the target",
                      close(height.x, 0.0, 1e-3) and close(height.y, 0.0, 1e-3)
                      and close(height.z, root["ck_height"], 1e-3),
                      f"({height})")
            elif name == "snorricam":
                mount = cube.matrix_world @ Vector(
                    (0.0, -root["ck_distance"], root["ck_height"]))
                off = (cam_obj.matrix_world.translation - mount).length
                check(f"{tag}snorricam: camera on the mount", off < 1e-3,
                      f"(off by {off:.4f} m)")
            elif name == "orbit":
                moved = (cam_obj.matrix_world.translation
                         - matrix_before.translation).length
                check(f"{tag}orbit: camera keeps its position", moved < 1e-3,
                      f"(moved {moved:.4f} m)")
            else:
                delta = matrix_delta(matrix_before, cam_obj.matrix_world)
                check(f"{tag}{name}: camera does not jump", delta < 1e-4,
                      f"(max delta {delta:.2e})")
            bad = not_simple_drivers()
            check(f"{tag}{name}: drivers are simple expressions", not bad,
                  f"({bad})")
            rig.remove_rig(bpy.context, root)
            update()
            compare(f"{tag}{name}: counts restored", before, snapshot())
            delta = matrix_delta(matrix_before, cam_obj.matrix_world)
            check(f"{tag}{name}: camera transform restored", delta < 1e-5,
                  f"(max delta {delta:.2e})")
            check(f"{tag}{name}: parent restored",
                  cam_obj.parent == parent_before)
        if parented:
            world = cam_obj.matrix_world.copy()
            cam_obj.parent = None
            cam_obj.matrix_world = world
            bpy.data.objects.remove(holder, do_unlink=True)
            update()


def test_dolly_geometry(m, scene, cam_obj):
    rig = m["rig"]
    # Regression guard for the dolly carrier fix. A parented carrier applied the
    # root offset a second time and left the carrier off the track. The
    # error grew with the distance from the world origin, so this test
    # puts the camera far out.
    print("\n== Dolly geometry ==")
    before = snapshot()
    saved_cam = cam_obj.matrix_world.copy()
    cam_obj.matrix_world = Matrix.Translation(Vector((25.0, -8.0, 15.0)))
    update()
    start = cam_obj.matrix_world.translation.copy()
    root = rig.create_dolly(bpy.context, cam_obj)
    update()
    carrier = rig.find_member(root, "Carrier")
    off_track = (carrier.matrix_world.translation
                 - root.matrix_world.translation).length
    check("dolly: carrier sits on the track", off_track < 1e-3,
          f"(off by {off_track:.3f} m)")
    moved = (cam_obj.matrix_world.translation - start).length
    check("dolly: camera keeps its position", moved < 1e-3,
          f"(moved {moved:.3f} m)")
    root["ck_position"] = 1.0
    root.update_tag()  # a custom-property write needs an explicit tag
    update()
    travel = (carrier.matrix_world.translation
              - root.matrix_world.translation).length
    check("dolly: ck_position drives travel", travel > 1.0,
          f"(travelled {travel:.2f} m)")
    rig.remove_rig(bpy.context, root)
    cam_obj.matrix_world = saved_cam
    update()
    compare("dolly geometry: counts restored", before, snapshot())


def test_advanced_dolly(m, scene, cam_obj):
    rig = m["rig"]
    print("\n== Advanced rigs ==")
    before = snapshot()
    root = rig.create_dolly(bpy.context, cam_obj,
                            opts={"path_length": 10.0, "aim_distance": 8.0,
                                  "carrier_pos": 0.25})
    rig_uid = root.get("cinekit_rig_uid")
    path = next(o for o in bpy.data.objects
                if o.get("cinekit_rig_member") == rig_uid
                and o.type == 'CURVE')
    span = abs(path.data.splines[0].points[1].co.x
               - path.data.splines[0].points[0].co.x)
    check("advanced dolly: custom path length", abs(span - 10.0) < 1e-4,
          f"(span {span:.2f})")
    roles = {r for r, _o, _m in rig.rig_points(root)}
    check("advanced dolly: points tagged",
          {"Base", "Carrier", "Aim", "Path"} <= roles, f"({roles})")

    gp_ok = hasattr(bpy.data, "grease_pencils")
    if gp_ok:
        gp_data = bpy.data.grease_pencils.new("CK_test_gp")
        gp_obj = bpy.data.objects.new("CK_test_gp", gp_data)
        scene.collection.objects.link(gp_obj)
        drawing = gp_data.layers.new("L").frames.new(1).drawing
        drawing.add_strokes([4])
        for i, co in enumerate(((0, 0, 0), (1, 0, 0), (2, 1, 0), (3, 1, 0))):
            drawing.strokes[0].points[i].position = co
        pts = rig.stroke_points_world(gp_obj)
        check("grease pencil read: 4 points", len(pts) == 4, f"({len(pts)})")
        rig.apply_dolly_path(bpy.context, root, pts)
        update()
        check("gp path applied", len(path.data.splines[0].points) == 4,
              f"({len(path.data.splines[0].points)})")
        carrier = rig.find_member(root, "Carrier")
        gap = (cam_obj.matrix_world.translation
               - carrier.matrix_world.translation).length
        check("gp path: camera rides the new track", gap < 1e-3,
              f"(gap {gap:.4f} m)")

    rig.remove_rig(bpy.context, root)
    update()
    if gp_ok:
        bpy.data.objects.remove(gp_obj, do_unlink=True)
        bpy.data.grease_pencils.remove(gp_data)
    compare("advanced rigs: counts restored", before, snapshot())


def test_handheld_animation(m, scene, cam_obj):
    rig = m["rig"]
    print("\n== Handheld on an animated camera ==")
    before = snapshot()
    saved = cam_obj.matrix_world.copy()
    cam_obj.location = (0.0, -6.0, 2.0)
    cam_obj.keyframe_insert("location", frame=1)
    cam_obj.location = (4.0, -6.0, 2.0)
    cam_obj.keyframe_insert("location", frame=25)
    root = rig.create_handheld(bpy.context, cam_obj, 'DOCUMENTARY')
    scene.frame_set(13)
    base = Vector((2.0, -6.0, 2.0))       # linear-ish midpoint of the keys
    fcu_x = next(f for f in m["utils"].action_fcurves(
        cam_obj.animation_data.action)
        if f.data_path == "location" and f.array_index == 0)
    base.x = fcu_x.evaluate(13)
    offsets = {}
    for intensity in (0.0, 1.0, 2.0):
        root["ck_intensity"] = intensity
        root.update_tag()
        scene.frame_set(13)
        offsets[intensity] = cam_obj.matrix_world.translation - base
    check("handheld keeps the keyframed move",
          offsets[0.0].length < 1e-5, f"({offsets[0.0].length:.2e})")
    check("handheld adds shake", offsets[1.0].length > 1e-5,
          f"({offsets[1.0].length:.2e})")
    ratio = offsets[2.0].length / max(offsets[1.0].length, 1e-12)
    check("intensity 2 doubles the shake", close(ratio, 2.0, 0.01),
          f"(ratio {ratio:.3f})")
    rig.remove_rig(bpy.context, root)
    clear_animation(cam_obj)
    cam_obj.matrix_world = saved
    scene.frame_set(1)
    update()
    compare("handheld animation: counts restored", before, snapshot())


def test_lens_model(m, scene, cam_obj):
    rig, optics, engine = m["rig"], m["optics"], m["engine"]
    cam = cam_obj.data
    ck = cam.cinekit
    if not hasattr(ck, "nd_stops"):
        print("\n  (no physical lens model, section skipped)")
        return
    print("\n== Physical lens model ==")
    before = snapshot()
    vs = scene.view_settings
    cam.lens, cam.sensor_width, cam.sensor_height = 50.0, 36.0, 24.0
    cam.sensor_fit = 'AUTO'
    cam.dof.focus_distance = 5.0
    ck.enabled = True
    ck.aperture = 2.8
    # An earlier section applied a lens preset with its own transmission.
    ck.transmission, ck.nd_stops = 1.0, 0.0
    exposure = vs.exposure

    ck.nd_stops = 2.0
    check("ND filter lowers the exposure by its stops",
          close(vs.exposure, exposure - 2.0), f"({vs.exposure:.4f})")
    ck.nd_stops = 0.0
    ck.transmission = 0.81
    check("transmission: the exposure uses the T-stop",
          close(vs.exposure, exposure + math.log2(0.81)),
          f"({vs.exposure:.4f})")
    ck.transmission = 1.0

    # Aperture shape from a lens preset and the f-stop.
    ck.lens_preset = "prime_35"
    check("lens preset turns on the aperture shape",
          ck.aperture_shape and ck.blades == 9 and close(ck.max_aperture, 1.4))
    ck.aperture = 1.4
    check("aperture is round wide open", cam.dof.aperture_blades == 0,
          f"({cam.dof.aperture_blades})")
    ck.aperture = 5.6
    check("aperture is a polygon stopped down", cam.dof.aperture_blades == 9)
    ck.lens_preset = "prime_58_vintage"
    ck.aperture = 8.0                       # 4 stops closed from f/2
    check("aperture polygon turns per stop",
          close(cam.dof.aperture_rotation, math.radians(16.0), 1e-5),
          f"({math.degrees(cam.dof.aperture_rotation):.2f} deg)")

    # Anamorphic squeeze.
    has_sensors = hasattr(ck, "sensor_preset")   # CineKit 1.x has none
    if has_sensors:
        ck.sensor_preset = "super35"
    width = cam.sensor_width
    ck.lens_preset = "anamorphic_50_2x"
    check("anamorphic squeeze widens the effective sensor",
          close(cam.sensor_width, width * 2.0), f"({cam.sensor_width:.3f})")
    check("anamorphic bokeh ratio", close(cam.dof.aperture_ratio, 2.0))
    if has_sensors:
        ck.sensor_preset = "full_frame"
        check("sensor preset keeps the squeeze", close(cam.sensor_width, 72.0))
        width = 36.0
    ck.lens_preset = "prime_50"
    check("a spherical lens gives the sensor width back",
          close(cam.sensor_width, width) and close(cam.dof.aperture_ratio, 1.0),
          f"({cam.sensor_width:.3f})")
    if has_sensors:
        ck.sensor_preset = 'NONE'
        cam.sensor_width = 36.0

    # Rolling shutter.
    cycles = scene.cycles
    cycles.rolling_shutter_type, cycles.rolling_shutter_duration = 'NONE', 0.1
    ck.shutter_mode, ck.shutter_speed = 'SPEED', 50
    ck.drive_motion_blur = True
    ck.readout_ms = 20.0
    ck.rolling_shutter = True
    fps = scene.render.fps / scene.render.fps_base
    window, duration = optics.rolling_shutter(0.02, 0.02, fps)
    check("rolling shutter drives Cycles",
          cycles.rolling_shutter_type == 'TOP'
          and close(cycles.rolling_shutter_duration, duration)
          and close(scene.render.motion_blur_shutter, min(window, 2.0)),
          f"({cycles.rolling_shutter_duration:.3f}, "
          f"{scene.render.motion_blur_shutter:.3f})")
    ck.rolling_shutter = False
    check("rolling shutter off gives the values back",
          cycles.rolling_shutter_type == 'NONE'
          and close(cycles.rolling_shutter_duration, 0.1))

    # Lens softness in the compositor.
    ck.lens_preset = "prime_35"
    ck.aperture = 8.0
    ck.lens_softness = True
    node = engine._find_pipeline_node(scene)
    size = engine._find_node(node.node_tree, engine._LENS_SIZE_TAG) \
        if node is not None else None
    check("lens softness adds the blur", size is not None)

    def expected_sigmas(data):
        """(horizontal, vertical) blur sigma in pixels, worked out apart
        from CineKit."""
        render = scene.render
        aspect = (render.resolution_x * render.pixel_aspect_x
                  / (render.resolution_y * render.pixel_aspect_y))
        if data.sensor_fit == 'VERTICAL':
            width_mm = data.sensor_height * aspect
        elif data.sensor_fit == 'AUTO' and aspect < 1.0:
            width_mm = data.sensor_width * aspect
        else:
            width_mm = data.sensor_width
        lens = data.cinekit
        sigma = optics.lens_blur_sigma_mm(
            data.dof.aperture_fstop, lens.max_aperture, lens.softness_um,
            data.lens, data.dof.focus_distance, lens.diffraction)
        sigma_px = (sigma * render.resolution_x
                    * render.resolution_percentage / 100.0 / width_mm)
        return (sigma_px * lens.squeeze,
                sigma_px * render.pixel_aspect_x / render.pixel_aspect_y)

    def radii_match(label, data):
        """The blur node gives the sigma of the lens, also below 1 px."""
        update()
        radii = (size.inputs[0].default_value, size.inputs[1].default_value)
        got = [math.sqrt(optics.blur_node_variance(r)) for r in radii]
        want = expected_sigmas(data)
        check(f"lens blur: {label}",
              all(close(g, w, max(w * 0.01, 1e-4))
                  for g, w in zip(got, want)),
              f"(sigma {got[0]:.4f} x {got[1]:.4f}, want {want[0]:.4f} x "
              f"{want[1]:.4f} px, node radius {radii[0]:.3f})")

    if size is not None:
        render = scene.render
        radii_match("f/8", cam)
        ck.aperture = 1.4
        radii_match("wide open", cam)
        ck.aperture = 22.0
        radii_match("f/22", cam)
        render.resolution_percentage = 50
        radii_match("50% render", cam)
        render.resolution_percentage = 100
        cam.sensor_fit = 'VERTICAL'
        radii_match("vertical fit", cam)
        render.resolution_x, render.resolution_y = 1080, 1920
        cam.sensor_fit = 'HORIZONTAL'
        radii_match("horizontal fit, portrait frame", cam)
        cam.sensor_fit = 'AUTO'
        radii_match("auto fit, portrait frame", cam)
        render.resolution_x, render.resolution_y = 1920, 1080
        render.pixel_aspect_x = 2.0
        radii_match("wide pixels", cam)
        render.pixel_aspect_x = 1.0
        ck.lens_preset = "anamorphic_50_2x"
        ck.aperture = 8.0
        radii_match("anamorphic lens", cam)
        cam.dof.focus_distance = 0.02
        radii_match("focus inside the focal length", cam)
        cam.dof.focus_distance = 5.0
        ck.lens_preset = "prime_35"
        ck.aperture = 8.0

        # A driver that is already correct stays: a new driver rebuilds
        # the dependency graph. driver_add returns the same F-curve, so
        # check the condition that skips the rebuild.
        ck.aperture = 5.6
        update()
        variables = engine._lens_blur_variables(scene, cam)
        current = [m["utils"].driver_matches(
            node.node_tree, f'nodes["{size.name}"].inputs[{index}]'
            '.default_value', optics.lens_blur_driver(index == 0),
            variables, optics.blur_curve()) for index in (0, 1)]
        counts = [len(fcu.keyframe_points)
                  for fcu in node.node_tree.animation_data.drivers
                  if size.name in fcu.data_path]
        check("the lens blur drivers stay current after a setting change",
              all(current) and counts == [len(optics.blur_curve())] * 2,
              f"({current}, keys {counts})")
        ck.aperture = 8.0

        # A camera switch points the drivers at the new camera.
        cam2_data = bpy.data.cameras.new("SmokeLensCam2")
        cam2 = bpy.data.objects.new("SmokeLensCam2", cam2_data)
        scene.collection.objects.link(cam2)
        cam2_data.cinekit.enabled = True
        cam2_data.cinekit.aperture = 16.0
        cam2_data.cinekit.lens_softness = True
        scene.camera = cam2
        rig.sync_active_camera(scene)
        radii_match("after a camera switch", cam2_data)
        scene.camera = cam_obj
        rig.sync_active_camera(scene)
        radii_match("after the switch back", cam)

        # A timeline marker switches the camera during playback.
        marker = scene.timeline_markers.new("SmokeLensMarker", frame=5)
        marker.camera = cam2
        scene.frame_set(5)
        check("a marker switches the active camera", scene.camera == cam2)
        radii_match("after a marker camera switch", cam2_data)
        scene.timeline_markers.remove(marker)
        scene.camera = cam_obj
        scene.frame_set(1)
        rig.sync_active_camera(scene)
        radii_match("after the marker test", cam)
        bpy.data.objects.remove(cam2, do_unlink=True)
        bpy.data.cameras.remove(cam2_data)
    bad = not_simple_drivers()
    check("lens drivers are simple expressions", not bad, f"({bad})")
    ck.lens_softness = False
    check("lens softness off removes the pipeline",
          engine._find_pipeline_node(scene) is None)

    # Focus breathing models.
    ck.lens_preset = 'NONE'
    ck.min_focus, ck.pupil_offset = 0.0, 0.0
    cam.lens = 50.0
    ck.breathing_model, ck.breathing_amount = 'UNIT', 1.0
    ck.focus_breathing = True
    base = rig.base_lens(cam)
    cam.dof.focus_distance = 0.5
    update()
    check("physical breathing follows the thin lens",
          close(cam.lens, optics.unit_focus_focal(base, 0.5), 1e-3),
          f"({cam.lens:.4f} vs {optics.unit_focus_focal(base, 0.5):.4f})")

    def breathing_driver_stays(label):
        cam.animation_data.drivers.find("lens").hide = True
        ck.aperture = ck.aperture           # runs the update of the camera
        fcu = cam.animation_data.drivers.find("lens")
        check(f"a setting change keeps the {label} breathing driver",
              fcu is not None and fcu.hide)
        if fcu is not None:
            fcu.hide = False

    breathing_driver_stays("physical")
    for distance, focal in ((0.3, 54.0), (1.0, 51.0), (100.0, 50.0)):
        point = ck.breathing_points.add()
        point.distance, point.focal = distance, focal
    ck.breathing_model = 'TABLE'
    cam.dof.focus_distance = 1.0
    update()
    check("measured breathing hits a table point",
          close(cam.lens, base * 51.0 / 50.0, 1e-3), f"({cam.lens:.4f})")
    breathing_driver_stays("measured")
    ck.min_focus = 0.3
    cam.dof.focus_distance = 0.1
    update()
    check("measured breathing stops at the minimum focus",
          close(cam.lens, base * 54.0 / 50.0, 1e-3), f"({cam.lens:.4f})")
    # Blender merges keys 0.01 apart. The table must match the merged keys,
    # or CineKit makes the driver again at each change.
    extra = ck.breathing_points.add()
    extra.distance, extra.focal = 0.31, 53.5
    breathing_driver_stays("measured, two points 0.01 m apart")
    ck.breathing_points.remove(len(ck.breathing_points) - 1)
    count = len(ck.breathing_points)
    cam.dof.focus_distance = 2.0
    result = bpy.ops.cinekit.breathing_point_add()
    added = ck.breathing_points[ck.breathing_index]
    check("add a breathing point at the focus mark",
          result == {'FINISHED'} and len(ck.breathing_points) == count + 1
          and close(added.distance, 2.0) and close(added.focal, base))
    result = bpy.ops.cinekit.breathing_point_remove()
    check("remove the breathing point",
          result == {'FINISHED'} and len(ck.breathing_points) == count
          and 0 <= ck.breathing_index < count)
    ck.focus_breathing = False
    ck.breathing_points.clear()
    ck.min_focus = 0.0
    check("breathing off gives the focal length back", close(cam.lens, base))
    cam.dof.focus_distance = 5.0

    # Keyframed physical settings apply on frame changes.
    ck.aperture = 2.8
    cam.keyframe_insert("cinekit.aperture", frame=1)
    ck.aperture = 11.0
    cam.keyframe_insert("cinekit.aperture", frame=10)
    scene.frame_set(10)
    check("animated aperture drives the depth of field",
          close(cam.dof.aperture_fstop, 11.0, 1e-3),
          f"(f/{cam.dof.aperture_fstop:.2f})")
    scene.frame_set(1)
    clear_animation(cam)

    # Entrance pupil offset: a pan shows parallax.
    ck.pupil_offset = 0.1
    update()
    start = cam_obj.matrix_world.translation.copy()
    forward = cam_obj.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
    heads = [(rig.create_crane, "crane")]
    if hasattr(rig, "create_tripod"):          # CineKit 1.x has no tripod
        heads.insert(0, (rig.create_pedestal, "pedestal"))
        root = rig.create_tripod(bpy.context, cam_obj)
        update()
        check("pupil offset: the camera stays when the rig attaches",
              (cam_obj.matrix_world.translation - start).length < 1e-4)
        root["ck_pan"] = 90.0
        root.update_tag()
        update()
        horizontal = Vector((forward.x, forward.y, 0.0)).length * 0.1
        moved = (cam_obj.matrix_world.translation - start).length
        check("pupil offset: a 90 degree pan moves the lens on a circle",
              close(moved, math.sqrt(2.0) * horizontal, 1e-4),
              f"({moved:.4f} vs {math.sqrt(2.0) * horizontal:.4f} m)")
        rig.remove_rig(bpy.context, root)
        update()
        check("pupil offset: the camera returns",
              (cam_obj.matrix_world.translation - start).length < 1e-5)
    for create, label in heads:
        root = create(bpy.context, cam_obj)
        update()
        head = cam_obj.parent
        pivot = head.matrix_world.translation.copy()
        check(f"pupil offset, {label}: the camera stays when the rig attaches",
              (cam_obj.matrix_world.translation - start).length < 1e-4)
        check(f"pupil offset, {label}: the head turns 0.1 m behind the lens",
              close((cam_obj.matrix_world.translation - pivot).length, 0.1))
        root["ck_tilt"] = root["ck_tilt"] + 30.0
        root.update_tag()
        update()
        lever = (cam_obj.matrix_world.translation
                 - head.matrix_world.translation).length
        check(f"pupil offset, {label}: a tilt turns the lens about the head",
              (head.matrix_world.translation - pivot).length < 1e-5
              and close(lever, 0.1)
              and (cam_obj.matrix_world.translation - start).length > 0.01,
              f"(lever {lever:.4f} m)")
        rig.remove_rig(bpy.context, root)
        update()
        check(f"pupil offset, {label}: the camera returns",
              (cam_obj.matrix_world.translation - start).length < 1e-5)
    ck.pupil_offset = 0.0

    ck.aperture_shape = False
    ck.enabled = False
    cam.lens, cam.sensor_width, cam.sensor_height = 50.0, 36.0, 24.0
    cam.sensor_fit = 'AUTO'
    cam.dof.aperture_blades, cam.dof.aperture_rotation = 0, 0.0
    cam.dof.aperture_ratio = 1.0
    compare("physical lens model: counts restored", before, snapshot())


def test_effects(m, scene, cam_obj):
    print("\n== Effects ==")
    before = snapshot()
    saved = cam_obj.matrix_world.copy()
    saved_mode = cam_obj.rotation_mode
    cam_obj.data.dof.focus_distance = 5.0
    with bpy.context.temp_override(active_object=cam_obj):
        for op, kwargs in (
                ("effect_vertigo", {"frames": 24, "factor": 2.0}),
                ("effect_crash_zoom", {"frames": 6, "factor": 2.0}),
                ("effect_whip_pan", {"frames": 6, "degrees": 90.0}),
                ("effect_dutch", {"angle": 10.0, "frames": 8})):
            result = getattr(bpy.ops.cinekit, op)('EXEC_DEFAULT', **kwargs)
            check(f"{op} runs", result == {'FINISHED'})
    clear_animation(cam_obj, cam_obj.data)
    cam_obj.matrix_world = saved
    cam_obj.data.lens = 50.0

    # A big whip pan in quaternion mode must turn the requested way.
    cam_obj.rotation_mode = 'QUATERNION'
    cam_obj.matrix_world = saved
    scene.frame_set(1)
    start = cam_obj.matrix_world.to_quaternion()
    with bpy.context.temp_override(active_object=cam_obj):
        bpy.ops.cinekit.effect_whip_pan('EXEC_DEFAULT', frames=12,
                                        degrees=270.0)
    for frame, angle in ((5, 90.0), (9, 180.0), (13, 270.0)):
        scene.frame_set(frame)
        want = Matrix.Rotation(math.radians(angle), 3, 'Z').to_quaternion() \
            @ start
        got = cam_obj.matrix_world.to_quaternion()
        diff = math.degrees(want.rotation_difference(got).angle)
        diff = min(diff, 360.0 - diff)   # q and -q are the same rotation
        check(f"whip pan 270 in quaternion mode at {angle:.0f} degrees",
              diff < 0.05, f"(off by {diff:.3f} deg)")
    clear_animation(cam_obj)
    scene.frame_set(1)

    # Dutch angle in axis-angle mode keys the right channel.
    cam_obj.rotation_mode = 'AXIS_ANGLE'
    cam_obj.matrix_world = saved
    with bpy.context.temp_override(active_object=cam_obj):
        bpy.ops.cinekit.effect_dutch('EXEC_DEFAULT', angle=12.0, frames=6)
    paths = {f.data_path for f in m["utils"].action_fcurves(
        cam_obj.animation_data.action)} if cam_obj.animation_data else set()
    check("dutch keys rotation_axis_angle in axis-angle mode",
          "rotation_axis_angle" in paths, f"({paths})")
    clear_animation(cam_obj)
    cam_obj.rotation_mode = saved_mode
    cam_obj.matrix_world = saved
    update()
    compare("effects: no stray datablocks", before, snapshot())


def test_focus(m, scene, cam_obj):
    print("\n== Focus ==")
    before = snapshot()
    focus = m["focus"]
    cam = cam_obj.data
    obj_a = focus._new_focus_target(bpy.context, Vector((0.0, 0.0, 1.0)))
    obj_b = focus._new_focus_target(bpy.context, Vector((0.0, 4.0, 1.0)))
    scene.frame_set(1)
    bpy.ops.cinekit.rack_focus('EXEC_DEFAULT', target_a=obj_a.name,
                               target_b=obj_b.name, frames=24)
    bpy.ops.cinekit.rack_focus('EXEC_DEFAULT', target_a=obj_b.name,
                               target_b=obj_a.name, frames=10)
    fcu = next(f for f in m["utils"].action_fcurves(cam.animation_data.action)
               if f.data_path == "dof.focus_distance")
    inside = [kp.co.x for kp in fcu.keyframe_points if 1.5 < kp.co.x < 10.5]
    check("a new rack removes old keys inside its range", not inside,
          f"({inside})")
    bpy.ops.cinekit.focus_clear_targets('EXEC_DEFAULT')
    clear_animation(cam)
    compare("focus: counts restored", before, snapshot())


def test_gobos(m, scene):
    lighting = m["lighting"]
    print("\n== Gobos ==")
    gobos = [item[0] for item in lighting.gobo_items() if item[0]]
    if not gobos or not hasattr(lighting, "CK_OT_gobo_add"):
        print("  (no gobos, section skipped)")
        return
    before = snapshot()
    light_data = bpy.data.lights.new("SmokeSpot", 'SPOT')
    light = bpy.data.objects.new("SmokeSpot", light_data)
    scene.collection.objects.link(light)
    if light_data.node_tree is None:   # older Blender versions
        light_data.use_nodes = True
    nt = light_data.node_tree
    emission = next(n for n in nt.nodes if n.type == 'EMISSION')
    blackbody = nt.nodes.new('ShaderNodeBlackbody')
    nt.links.new(blackbody.outputs[0], emission.inputs["Color"])
    with bpy.context.temp_override(active_object=light,
                                   selected_objects=[light]):
        bpy.ops.cinekit.gobo_add('EXEC_DEFAULT', gobo=gobos[0],
                                 method='NODES')
        image = next((i for i in bpy.data.images
                      if i.get(lighting.GOBO_TAG) == gobos[0]), None)
        check("gobo image is packed and tagged",
              image is not None and image.packed_file is not None)
        check("gobo image path is relative",
              image is not None and image.filepath_raw.startswith("//"),
              f"({image.filepath_raw if image else None})")
        feeds = emission.inputs["Color"].links[0].from_node
        check("gobo chains the user color node",
              feeds.get("cinekit_id") == "gobo"
              and any(l.from_node == blackbody for l in nt.links
                      if l.to_node == feeds))
        bpy.ops.cinekit.gobo_add('EXEC_DEFAULT', gobo=gobos[0],
                                 method='PLANE')
        check("gobo plane added",
              any(c.get("cinekit_id") == "gobo" for c in light.children))
        bpy.ops.cinekit.gobo_remove('EXEC_DEFAULT')
        link = emission.inputs["Color"].links[0] \
            if emission.inputs["Color"].is_linked else None
        check("remove gobo reconnects the user color node",
              link is not None and link.from_node == blackbody)
        check("remove gobo deletes the plane",
              not any(c.get("cinekit_id") == "gobo" for c in light.children))
    bpy.data.objects.remove(light, do_unlink=True)
    bpy.data.lights.remove(light_data)
    compare("gobos: counts restored", before, snapshot())


def test_reflectors_and_setups(m, scene, cube):
    lighting = m["lighting"]
    if hasattr(lighting, "REFLECTOR_TYPES"):
        print("\n== Reflectors ==")
        with bpy.context.temp_override(active_object=cube,
                                       selected_objects=[cube]):
            before = snapshot()
            for rtype in lighting.REFLECTOR_TYPES:
                bpy.ops.cinekit.reflector_add(rtype=rtype)
                bpy.ops.cinekit.reflector_add(rtype=rtype)  # idempotent
            count = len(lighting.scene_reflectors(scene))
            check("reflectors: one per type",
                  count == len(lighting.REFLECTOR_TYPES),
                  f"({count} vs {len(lighting.REFLECTOR_TYPES)})")
            bpy.ops.cinekit.light_setup_remove(
                'EXEC_DEFAULT', setup_uid=f"reflectors:{cube.name}")
            # Shared reflector materials stay by design. Purge, then compare.
            for mat in [mt for mt in bpy.data.materials
                        if mt.get("cinekit_id") == "reflector"
                        and mt.users == 0]:
                bpy.data.materials.remove(mat)
            compare("reflectors: counts restored", before, snapshot())

    print("\n== Lighting setups ==")
    with bpy.context.temp_override(active_object=cube,
                                   selected_objects=[cube]):
        for setup_id in lighting.get_setups():
            before = snapshot()
            bpy.ops.cinekit.light_setup_add(setup=setup_id)
            bpy.ops.cinekit.light_setup_add(setup=setup_id)  # idempotent
            uid = f"{setup_id}:{cube.name}"
            count = len([o for o in bpy.data.objects
                         if o.get("cinekit_id") == uid])
            expected = len(lighting.get_setups()[setup_id]["lights"])
            check(f"{setup_id}: no duplicate lights", count == expected,
                  f"({count} vs {expected})")
            bpy.ops.cinekit.light_setup_remove('EXEC_DEFAULT',
                                               setup_uid=uid)
            compare(f"{setup_id}: counts restored", before, snapshot())


def test_shots(m, scene, cam_obj):
    engine = m["engine"]
    print("\n== Shots ==")
    ck = scene.cinekit
    before = snapshot()
    for name, start, end, look in (("SH010", 1, 10, "vhs"),
                                   ("SH020", 11, 20, "super8")):
        shot = ck.shots.add()
        shot.name = name
        shot.camera = cam_obj
        shot.frame_start, shot.frame_end = start, end
        shot.look = look
    m["shots"].invalidate_cache(scene)
    scene.frame_set(5)
    check("frame handler applies the first shot look",
          ck.active_look == "vhs", f"({ck.active_look})")
    scene.frame_set(15)
    check("frame handler switches the look at the shot boundary",
          ck.active_look == "super8", f"({ck.active_look})")
    bpy.ops.cinekit.shots_sync_markers()
    bpy.ops.cinekit.shots_sync_markers()  # idempotent
    ck_markers = [mk for mk in scene.timeline_markers
                  if mk.name.startswith("CK:")]
    check("marker sync idempotent", len(ck_markers) == 2,
          f"({len(ck_markers)} markers)")
    for marker in ck_markers:
        scene.timeline_markers.remove(marker)
    ck.auto_switch_shot_look = False
    engine.remove_look(scene)
    ck.shots.clear()
    ck.auto_switch_shot_look = True
    scene.frame_set(1)
    compare("shots: counts restored", before, snapshot())


def test_overlay(m, scene):
    overlay = m["overlay"]
    print("\n== Overlay ==")
    scene2 = bpy.data.scenes.new("SmokeOverlay")
    scene.cinekit.overlay_enabled = True
    scene2.cinekit.overlay_enabled = True
    scene2.cinekit.overlay_enabled = False
    check("overlay stays while another scene uses it",
          overlay._handle is not None)
    scene.cinekit.overlay_enabled = False
    check("overlay stops when no scene uses it", overlay._handle is None)
    bpy.data.scenes.remove(scene2)


def run():
    names = ("utils", "rig", "looks", "looks.engine", "lighting", "optics",
             "lenses", "stable_enum", "migrate", "focus", "shots", "overlay",
             "constants", "effects")
    m = {}
    for name in names:
        module = sys.modules.get(f"{PKG}.{name}")
        if module is None:
            try:
                module = __import__(f"{PKG}.{name}", fromlist=["_"])
            except ImportError:
                module = None
        m[name.split(".")[-1]] = module
    m["K"] = m["constants"]

    scene, cam_obj, cube = build_stage()
    # Create the CineKit collection first, so it is not a leak.
    m["utils"].ck_collection(scene)

    test_looks(m, scene)
    test_hdr_highlights(m, scene)
    test_look_camera_layer(m, scene, cam_obj)
    test_scene_isolation(m, scene)
    test_timestamp(m, scene)
    test_enums(m, scene, cam_obj)
    test_rigs(m, scene, cam_obj, cube)
    test_dolly_geometry(m, scene, cam_obj)
    test_advanced_dolly(m, scene, cam_obj)
    test_handheld_animation(m, scene, cam_obj)
    test_lens_model(m, scene, cam_obj)
    if m["effects"] is not None:        # CineKit 1.x has no effects
        test_effects(m, scene, cam_obj)
    test_focus(m, scene, cam_obj)
    test_gobos(m, scene)
    test_reflectors_and_setups(m, scene, cube)
    test_shots(m, scene, cam_obj)
    test_overlay(m, scene)

    cinekit.unregister()
    unique = sorted(set(DEPRECATIONS))
    check("CineKit code uses no deprecated Blender API", not unique,
          "\n    " + "\n    ".join(unique) if unique else "")
    print()
    if FAILURES:
        print(f"SMOKE TEST FAILED — {len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        sys.exit(1)
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    cinekit.register()
    try:
        run()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
