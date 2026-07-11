# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Viewport text overlay (gpu/blf draw handler): active shot, camera, lens,
f-stop, frame-in-shot. Engine-agnostic; pure UI drawing."""

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
        fstop = (cam.cinekit.aperture if cam.cinekit.enabled
                 else cam.dof.aperture_fstop)
        lines.append(f"CAM   {cam_obj.name}   {cam.lens:.0f}mm  f/{fstop:.1f}")
        if cam.cinekit.enabled:
            from . import rig
            try:
                ev = rig.compute_ev100(cam, scene)
                lines.append(f"EXPO  ISO {cam.cinekit.iso}   EV100 {ev:+.1f}")
            except ValueError:
                pass
    if ck.active_look:
        lines.append(f"LOOK  {ck.active_look}")
    if not lines:
        return

    font_id = 0
    blf.size(font_id, 13)
    blf.color(font_id, 1.0, 0.85, 0.3, 0.95)
    blf.enable(font_id, blf.SHADOW)
    blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 0.9)
    y = 24
    for line in reversed(lines):
        blf.position(font_id, 20, y, 0)
        blf.draw(font_id, line)
        y += 20
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
