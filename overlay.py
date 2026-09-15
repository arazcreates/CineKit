# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Viewport text overlay with a gpu/blf draw handler.

It shows the active shot, camera, lens, f-stop and frame-in-shot.
This module only draws the UI. It works with every render engine.
"""

import blf
import bpy

_handle = None


def _draw():
    context = bpy.context
    scene = context.scene
    if scene is None or not getattr(scene, "cinekit", None):
        return
    ck = scene.cinekit
    if not ck.overlay_enabled:
        return
    from . import shots as shots_mod
    shot = shots_mod.active_shot(scene)
    cam_obj = scene.camera

    lines = []
    if shot is not None:
        total = shot.frame_end - shot.frame_start + 1
        rel = scene.frame_current - shot.frame_start + 1
        lines.append(f"SHOT  {shot.name}   [{rel}/{total}]")
    if cam_obj is not None and cam_obj.type == 'CAMERA':
        cam = cam_obj.data
        lens = f"{cam.lens:.1f}".rstrip("0").rstrip(".")
        if cam.cinekit.enabled:
            fstop = f"f/{cam.cinekit.aperture:.1f}"
            if abs(cam.dof.aperture_fstop - cam.cinekit.aperture) > 0.05:
                # A look sets a depth-of-field floor.
                fstop += f" (DOF f/{cam.dof.aperture_fstop:.1f})"
        else:
            fstop = f"f/{cam.dof.aperture_fstop:.1f}"
        lines.append(f"CAM   {cam_obj.name}   {lens}mm  {fstop}")
        if cam.cinekit.enabled:
            from . import rig
            try:
                ev = rig.compute_ev100(cam, scene)
                lines.append(f"EXPO  ISO {cam.cinekit.iso}   EV100 {ev:+.1f}")
            except ValueError:
                pass
    if ck.active_look:
        from . import looks
        look = looks.get(ck.active_look)
        lines.append(f"LOOK  {look['name'] if look else ck.active_look}")
    if not lines:
        return

    ui = context.preferences.system.ui_scale
    font_id = 0
    blf.size(font_id, 13 * ui)
    blf.color(font_id, 1.0, 0.85, 0.3, 0.95)
    blf.enable(font_id, blf.SHADOW)
    blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 0.9)
    y = 24 * ui
    for line in reversed(lines):
        blf.position(font_id, 20 * ui, y, 0)
        blf.draw(font_id, line)
        y += 20 * ui
    blf.disable(font_id, blf.SHADOW)


def set_enabled(flag):
    global _handle
    if flag and _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), 'WINDOW', 'POST_PIXEL')
    elif not flag and _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    _tag_redraw()


def cleanup():
    """load_post / unregister: never leave a stale draw handler around."""
    set_enabled(False)


def _tag_redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
