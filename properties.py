# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""PropertyGroups on Camera data, Scene and Light data.

All settings live in the .blend. Update callbacks route through the apply
layer (rig.py / looks.engine) — the UI never sets low-level values directly.
"""

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, IntProperty, PointerProperty,
                       StringProperty)

from . import lenses, looks, utils


# --------------------------------------------------------------- update hooks
def _physical_update(self, context):
    if utils.updates_suppressed():
        return
    from . import rig
    rig.on_camera_settings_changed(self.id_data, context.scene)


def _lens_preset_update(self, context):
    if utils.updates_suppressed():
        return
    from . import rig
    rig.apply_lens_preset(self.id_data)
    rig.on_camera_settings_changed(self.id_data, context.scene)


def _breathing_update(self, context):
    if utils.updates_suppressed():
        return
    from . import rig
    try:
        rig.set_breathing(self.id_data)
    except utils.CKError as exc:
        print(f"CineKit: focus breathing refused: {exc}")
        with utils.suppress_updates():
            self.focus_breathing = False


def _intensity_update(self, context):
    if utils.updates_suppressed():
        return
    from .looks import engine
    engine.set_intensity(self.id_data)


def _param_update(self, context):
    if utils.updates_suppressed():
        return
    from .looks import engine
    engine.push_param(self.id_data, self)


def _overlay_update(self, context):
    from . import overlay
    overlay.set_enabled(self.overlay_enabled)


def _camera_object_poll(self, obj):
    return obj.type == 'CAMERA'


# ------------------------------------------------------------------- camera
class CK_CameraSettings(bpy.types.PropertyGroup):
    """Physical camera settings on Camera data. Engines: Cycles + EEVEE Next
    (exposure/DOF/WB are engine-agnostic; motion blur uses render settings)."""

    enabled: BoolProperty(
        name="Physical Camera",
        description="Drive this camera with real-world exposure controls. "
                    "When it is the scene's active camera it owns scene "
                    "exposure and motion blur",
        default=False, update=_physical_update)
    iso: IntProperty(
        name="ISO", description="Sensor sensitivity",
        default=100, min=25, max=204800, update=_physical_update)
    aperture: FloatProperty(
        name="Aperture", description="F-stop. Drives depth of field and "
                                     "exposure",
        default=2.8, min=0.5, max=64.0, precision=1, update=_physical_update)
    shutter_mode: EnumProperty(
        name="Shutter Mode",
        items=(('SPEED', "Speed 1/x", "Shutter as 1/x seconds"),
               ('ANGLE', "Angle", "Shutter as angle (film convention: 180°)")),
        default='SPEED', update=_physical_update)
    shutter_speed: IntProperty(
        name="Shutter 1/", description="Shutter speed denominator (1/x s)",
        default=50, min=1, max=8000, update=_physical_update)
    shutter_angle: FloatProperty(
        name="Shutter Angle", description="Shutter angle in degrees",
        default=180.0, min=1.0, max=360.0, update=_physical_update)
    exposure_comp: FloatProperty(
        name="Exposure Comp", description="Exposure compensation in stops",
        default=0.0, min=-5.0, max=5.0, update=_physical_update)
    drive_motion_blur: BoolProperty(
        name="Drive Motion Blur",
        description="Set render motion-blur shutter from this camera's "
                    "shutter when active", default=True,
        update=_physical_update)
    wb_enabled: BoolProperty(
        name="White Balance",
        description="Apply temperature/tint. Uses native view-transform white "
                    "balance on Blender 4.3+, else a compositor gain node "
                    "(enable the Viewport Compositor to preview it live)",
        default=False, update=_physical_update)
    wb_temperature: IntProperty(
        name="Temperature", description="Scene illuminant temperature in "
                                        "Kelvin (whites shot at this "
                                        "temperature come out neutral)",
        default=6500, min=1800, max=12000, update=_physical_update)
    wb_tint: FloatProperty(
        name="Tint", description="Green (-) to magenta (+) shift",
        default=0.0, min=-1.0, max=1.0, update=_physical_update)
    lens_preset: EnumProperty(
        name="Lens", description="JSON-backed lens preset (add your own in "
                                 "the add-on preferences)",
        items=lenses.enum_items, update=_lens_preset_update)
    focus_breathing: BoolProperty(
        name="Focus Breathing",
        description="Slightly shift focal length as focus distance changes "
                    "(amount from the lens preset). Adds a driver on the "
                    "focal length", default=False, update=_breathing_update)
    breathing_amount: FloatProperty(
        name="Breathing Amount",
        description="Breathing strength; prefilled from the lens preset",
        default=0.1, min=0.0, max=1.0, update=_breathing_update)
    look: StringProperty(
        name="Camera Look",
        description="Default look id used when a shot with this camera has "
                    "no explicit look", default="")


# --------------------------------------------------------------------- shots
class CK_Shot(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Shot")
    camera: PointerProperty(
        name="Camera", type=bpy.types.Object, poll=_camera_object_poll)
    frame_start: IntProperty(name="Start", default=1)
    frame_end: IntProperty(name="End", default=100)
    look: EnumProperty(
        name="Look", description="Look applied when this shot is active",
        items=looks.enum_items)
    notes: StringProperty(name="Notes", default="")
    # Rack-focus preset stored per shot.
    rack_from: StringProperty(
        name="Rack From", description="Focus target object name (A)")
    rack_to: StringProperty(
        name="Rack To", description="Focus target object name (B)")
    rack_frames: IntProperty(
        name="Rack Frames", default=24, min=1, max=1000)
    rack_ease: EnumProperty(
        name="Ease",
        items=(('LINEAR', "Linear", "Constant-speed pull"),
               ('SMOOTH', "Smooth", "Ease in and out"),
               ('SNAP', "Snap", "Hold then jump — whip focus")),
        default='SMOOTH')


class CK_LookParam(bpy.types.PropertyGroup):
    """One editable look parameter, mirrored onto a node-group input socket
    (or a camera field). Populated by the engine when a look is applied."""
    name: StringProperty()          # socket name / target key
    label: StringProperty()
    target: StringProperty()        # "group" or "camera:<field>"
    vmin: FloatProperty(default=0.0)
    vmax: FloatProperty(default=2.0)
    value: FloatProperty(
        name="Value", description="Artifact strength (× master intensity)",
        min=0.0, max=2.0, soft_max=1.0, update=_param_update)


class CK_SceneSettings(bpy.types.PropertyGroup):
    shots: CollectionProperty(type=CK_Shot)
    shot_index: IntProperty(name="Active Shot", default=-1)
    auto_switch_shot_look: BoolProperty(
        name="Auto-Switch Looks",
        description="Apply each shot's look when the playhead enters its "
                    "frame range (cheap frame-change check)", default=True)
    active_look: StringProperty(
        name="Active Look", description="Currently applied look id",
        default="")
    look_intensity: FloatProperty(
        name="Intensity", description="Master fade for the whole look",
        default=1.0, min=0.0, max=1.0, update=_intensity_update)
    look_params: CollectionProperty(type=CK_LookParam)
    overlay_enabled: BoolProperty(
        name="Shot Overlay",
        description="Viewport text overlay: shot, camera, lens, f-stop, "
                    "frame-in-shot", default=False, update=_overlay_update)
    security_timestamp: StringProperty(
        name="Timestamp Text",
        description="Burned-in text for the Security Cam look "
                    "(re-apply the look after changing)",
        default="CAM 01  06.07.2026 14:32")


# -------------------------------------------------------------------- lights
class CK_LightSettings(bpy.types.PropertyGroup):
    is_cinekit: BoolProperty(default=False)
    role: EnumProperty(
        name="Role",
        items=(('KEY', "Key", ""), ('FILL', "Fill", ""), ('RIM', "Rim", ""),
               ('PRACTICAL', "Practical", ""), ('OTHER', "Other", "")),
        default='OTHER')
    setup_id: StringProperty(default="")


CLASSES = (
    CK_CameraSettings,
    CK_Shot,
    CK_LookParam,
    CK_SceneSettings,
    CK_LightSettings,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Camera.cinekit = PointerProperty(type=CK_CameraSettings)
    bpy.types.Scene.cinekit = PointerProperty(type=CK_SceneSettings)
    bpy.types.Light.cinekit = PointerProperty(type=CK_LightSettings)


def unregister():
    del bpy.types.Light.cinekit
    del bpy.types.Scene.cinekit
    del bpy.types.Camera.cinekit
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
