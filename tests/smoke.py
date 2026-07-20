# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Headless smoke test — run inside Blender:

    blender -b -P tests/smoke.py

Applies every shipped Look, adds every rig and lighting preset, removes
them all, and asserts the scene datablock counts match the starting state.
Exits non-zero on failure.

The extension package is imported directly from the repo (parent dir added
to sys.path), so this works without installing the extension first.
"""

import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(ROOT))

PKG = os.path.basename(ROOT)
cinekit = __import__(PKG)

FAILURES = []


def check(label, condition, detail=""):
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label} {detail}")
    if not condition:
        FAILURES.append(f"{label} {detail}")


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
    }


def compare(label, before, after):
    diffs = {key: (before[key], after[key]) for key in before
             if before[key] != after[key]}
    check(label, not diffs, f"leaked: {diffs}" if diffs else "")


def build_stage():
    """A camera and a subject to work with."""
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    cam_data = bpy.data.cameras.new("SmokeCam")
    cam_obj = bpy.data.objects.new("SmokeCam", cam_data)
    scene.collection.objects.link(cam_obj)
    cam_obj.location = (0.0, -6.0, 2.0)
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


def run():
    utils = sys.modules[f"{PKG}.utils"]
    rig = sys.modules[f"{PKG}.rig"]
    looks = sys.modules[f"{PKG}.looks"]
    engine = sys.modules[f"{PKG}.looks.engine"]
    lighting = sys.modules[f"{PKG}.lighting"]

    scene, cam_obj, cube = build_stage()
    # Pre-create the CineKit collection so it doesn't count as a leak.
    utils.ck_collection(scene)
    comp_tree_before = engine._get_comp_tree(scene)

    # ---------------------------------------------------------------- looks
    print("\n== Looks ==")
    base = snapshot()
    for look_id in sorted(looks.all_looks()):
        engine.apply_look(scene, look_id)
        check(f"apply {look_id}", scene.cinekit.active_look == look_id)
        engine.apply_look(scene, look_id)  # idempotent re-apply
        pipeline_nodes = [n for n in engine._get_comp_tree(scene).nodes
                          if n.get("cinekit_id") == "pipeline"]
        check(f"{look_id}: exactly one pipeline node",
              len(pipeline_nodes) == 1)
    engine.remove_look(scene)
    compare("looks removed: datablock counts restored", base, snapshot())
    check("compositor state restored",
          engine._get_comp_tree(scene) == comp_tree_before,
          f"({engine._get_comp_tree(scene)} vs {comp_tree_before})")

    # ----------------------------------------------------------------- rigs
    print("\n== Rigs ==")
    for name, create in (("dolly", rig.create_dolly),
                         ("crane", rig.create_crane),
                         ("handheld", rig.create_handheld),
                         ("orbit", rig.create_orbit)):
        before = snapshot()
        matrix_before = cam_obj.matrix_world.copy()
        root = create(bpy.context, cam_obj)
        check(f"{name}: camera in rig", rig.rig_root_of(cam_obj) == root)
        rig.remove_rig(bpy.context, root)
        compare(f"{name}: counts restored", before, snapshot())
        delta = max(abs(a - b)
                    for row_a, row_b in zip(matrix_before,
                                            cam_obj.matrix_world)
                    for a, b in zip(row_a, row_b))
        check(f"{name}: camera transform restored", delta < 1e-5,
              f"(max delta {delta:.2e})")

    # ---------------------------------------------------- advanced rig mode
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
        rig.set_dolly_path(root, pts)
        check("gp path applied", len(path.data.splines[0].points) == 4,
              f"({len(path.data.splines[0].points)})")

    rig.remove_rig(bpy.context, root)
    if gp_ok:
        bpy.data.objects.remove(gp_obj, do_unlink=True)
        bpy.data.grease_pencils.remove(gp_data)
    compare("advanced rigs: counts restored", before, snapshot())

    # ------------------------------------------------------------- lighting
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

    # -------------------------------------------------------------- shots
    print("\n== Shots ==")
    ck = scene.cinekit
    shot = ck.shots.add()
    shot.name = "SH010"
    shot.camera = cam_obj
    shot.frame_start, shot.frame_end = 1, 48
    bpy.ops.cinekit.shots_sync_markers()
    bpy.ops.cinekit.shots_sync_markers()  # idempotent
    ck_markers = [m for m in scene.timeline_markers
                  if m.name.startswith("CK:")]
    check("marker sync idempotent", len(ck_markers) == 1,
          f"({len(ck_markers)} markers)")

    cinekit.unregister()
    print()
    if FAILURES:
        print(f"SMOKE TEST FAILED — {len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        sys.exit(1)
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    cinekit.register()
    run()
