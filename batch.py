# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Batch render shots: modal operator with progress, cancel, and guaranteed
state restore (frame range, camera, look, output path) even on exceptions.
Writes //renders/cinekit_batch_summary.json when done."""

import json
import os
import time

import bpy

from .utils import CKError

# Module-level so load_post can clear a stale run.
_active = None


def cancel_stale():
    global _active
    _active = None


def _safe_name(name):
    return "".join(c if (c.isalnum() or c in "-_ ") else "_"
                   for c in name).strip() or "shot"


class CK_OT_batch_render(bpy.types.Operator):
    bl_idname = "cinekit.batch_render"
    bl_label = "Batch Render Shots"
    bl_description = ("Render every shot's frame range to "
                      "//renders/<shot>/ with its camera and look. "
                      "ESC stops after the current shot")
    bl_options = {'REGISTER'}

    _timer = None

    @classmethod
    def poll(cls, context):
        rendering = (bpy.app.is_job_running('RENDER')
                     if hasattr(bpy.app, "is_job_running") else False)
        return (len(context.scene.cinekit.shots) > 0 and _active is None
                and not rendering)

    # ------------------------------------------------------------ lifecycle
    def invoke(self, context, event):
        global _active
        scene = context.scene
        bad = [s.name for s in scene.cinekit.shots if s.camera is None
               or s.frame_end < s.frame_start]
        if bad:
            self.report({'ERROR'},
                        "These shots need a camera or a correct "
                        "frame range: " + ", ".join(bad))
            return {'CANCELLED'}
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Save the .blend file first. The "
                                   "batch writes to the //renders/ folder.")
            return {'CANCELLED'}

        self._scene_name = scene.name
        self._queue = list(range(len(scene.cinekit.shots)))
        self._results = []
        self._current = -1
        self._render_done = False
        self._render_failed = False
        self._user_cancel = False
        self._started = time.time()
        self._saved = {
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "frame_current": scene.frame_current,
            "camera": scene.camera.name if scene.camera else "",
            "filepath": scene.render.filepath,
            "look": scene.cinekit.active_look,
        }
        self._on_complete = lambda *args: self._flag(done=True)
        self._on_cancel = lambda *args: self._flag(failed=True)
        bpy.app.handlers.render_complete.append(self._on_complete)
        bpy.app.handlers.render_cancel.append(self._on_cancel)

        _active = self
        try:
            self._start_next(context)
        except Exception as exc:
            self._teardown(context)
            self.report({'ERROR'}, f"Could not start batch: {exc}")
            return {'CANCELLED'}
        self._timer = context.window_manager.event_timer_add(
            0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _flag(self, done=False, failed=False):
        if done:
            self._render_done = True
        if failed:
            self._render_failed = True

    def _start_next(self, context):
        scene = context.scene
        idx = self._queue.pop(0)
        self._current = idx
        shot = scene.cinekit.shots[idx]
        from . import shots as shots_mod
        shots_mod._apply_shot(scene, shot, set_frame=True)
        scene.frame_start = shot.frame_start
        scene.frame_end = shot.frame_end
        scene.render.filepath = f"//renders/{_safe_name(shot.name)}/"
        self._render_done = False
        self._render_failed = False
        self._shot_started = time.time()
        result = bpy.ops.render.render('INVOKE_DEFAULT', animation=True,
                                       scene=scene.name)
        if 'CANCELLED' in result:
            raise CKError(f"The render did not start for shot '{shot.name}'.")

    def modal(self, context, event):
        if event.type == 'ESC':
            self._user_cancel = True
            self.report({'WARNING'},
                        "The batch stops after the current shot. "
                        "Press ESC in the render window to stop it now.")
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if not (self._render_done or self._render_failed):
            return {'PASS_THROUGH'}

        scene = context.scene
        shot = scene.cinekit.shots[self._current]
        self._results.append({
            "shot": shot.name,
            "camera": shot.camera.name if shot.camera else "",
            "frames": [shot.frame_start, shot.frame_end],
            "look": shot.look if shot.look != 'NONE' else "",
            "status": "done" if self._render_done else "cancelled",
            "seconds": round(time.time() - self._shot_started, 1),
            "output": f"//renders/{_safe_name(shot.name)}/",
        })
        stop = (self._user_cancel or self._render_failed
                or not self._queue)
        if stop:
            self._finish(context)
            return {'FINISHED'}
        try:
            self._start_next(context)
        except Exception as exc:
            self.report({'ERROR'}, f"Batch stopped: {exc}")
            self._finish(context)
            return {'CANCELLED'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        self._finish(context)

    # -------------------------------------------------------------- cleanup
    def _finish(self, context):
        try:
            self._write_summary()
        finally:
            self._teardown(context)

    def _teardown(self, context):
        """Restore every changed scene value. This is safe to call twice."""
        global _active
        _active = None
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        for handler_list, fn in (
                (bpy.app.handlers.render_complete, self._on_complete),
                (bpy.app.handlers.render_cancel, self._on_cancel)):
            if fn in handler_list:
                handler_list.remove(fn)
        scene = bpy.data.scenes.get(self._scene_name)
        if scene is None:
            return
        try:
            saved = self._saved
            scene.frame_start = saved["frame_start"]
            scene.frame_end = saved["frame_end"]
            scene.frame_current = saved["frame_current"]
            scene.render.filepath = saved["filepath"]
            cam = bpy.data.objects.get(saved["camera"])
            if cam is not None:
                scene.camera = cam
            from .looks import engine
            if saved["look"]:
                engine.apply_look(scene, saved["look"])
            elif scene.cinekit.active_look:
                engine.remove_look(scene)
        except Exception as exc:  # never leave state half-restored silently
            print(f"CineKit: batch state restore problem: {exc}")

    def _write_summary(self):
        try:
            base = bpy.path.abspath("//renders/")
            os.makedirs(base, exist_ok=True)
            summary = {
                "blend": bpy.data.filepath,
                "scene": self._scene_name,
                "total_seconds": round(time.time() - self._started, 1),
                "cancelled": self._user_cancel or self._render_failed,
                "shots": self._results,
            }
            path = os.path.join(base, "cinekit_batch_summary.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2)
            self.report({'INFO'}, f"Batch summary written to {path}")
        except OSError as exc:
            self.report({'WARNING'}, f"Could not write batch summary: {exc}")


CLASSES = (CK_OT_batch_render,)
