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
    from .looks import engine
    cam = self.id_data
    engine.edit_base_camera(context.scene, cam,
                            lambda: rig.apply_lens_preset(cam))
    rig.on_camera_settings_changed(cam, context.scene)


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
    # One draw handler serves every scene. Keep it while any scene uses it.
    overlay.set_enabled(any(getattr(s, "cinekit", None)
                            and s.cinekit.overlay_enabled
                            for s in bpy.data.scenes))


def _timestamp_update(self, context):
    if utils.updates_suppressed():
        return
    from .looks import engine
    engine.refresh_timestamp(self.id_data)


def _camera_object_poll(self, obj):
    return obj.type == 'CAMERA'


# ------------------------------------------------------------------- camera
class CK_CameraSettings(bpy.types.PropertyGroup):
    """Physical camera settings on Camera data.

    Cycles and EEVEE both support this. Exposure, depth of field and
    white balance work in both engines. Motion blur uses the render
    settings.
    """

    enabled: BoolProperty(
        name="Physical Camera",
        description="Control this camera with real exposure settings. When "
                    "it is the active camera, it sets the scene exposure "
                    "and the motion blur",
        default=False, update=_physical_update)
    iso: IntProperty(
        name="ISO", description="Sensor sensitivity",
        default=100, min=25, max=204800, update=_physical_update)
    aperture: FloatProperty(
        name="Aperture", description="F-stop. It sets the depth of field "
                                     "and the exposure",
        default=2.8, min=0.5, max=64.0, precision=1, update=_physical_update)
    shutter_mode: EnumProperty(
        name="Shutter Mode",
        items=(('SPEED', "Speed 1/x", "Set the shutter as 1/x seconds"),
               ('ANGLE', "Angle", "Set the shutter as an angle. Film "
                                  "cameras usually use 180°")),
        default='SPEED', update=_physical_update)
    shutter_speed: IntProperty(
        name="Shutter 1/", description="Shutter speed as 1/x seconds",
        default=50, min=1, max=8000, update=_physical_update)
    shutter_angle: FloatProperty(
        name="Shutter Angle", description="Shutter angle in degrees",
        default=180.0, min=1.0, max=360.0, update=_physical_update)
    exposure_comp: FloatProperty(
        name="Exposure Comp", description="Exposure compensation in stops",
        default=0.0, min=-5.0, max=5.0, update=_physical_update)
    drive_motion_blur: BoolProperty(
        name="Drive Motion Blur",
        description="Set the motion blur shutter of the render from the "
                    "shutter of this camera while it is active",
        default=True, update=_physical_update)
    wb_enabled: BoolProperty(
        name="White Balance",
        description="Set the white balance from the temperature and the "
                    "tint. CineKit uses the white balance of the view "
                    "transform, so the result shows in every viewport",
        default=False, update=_physical_update)
    wb_temperature: IntProperty(
        name="Temperature", description="Color temperature of the scene "
                                        "light in kelvin. A white surface "
                                        "under light of this temperature "
                                        "shows as white",
        default=6500, min=1800, max=12000, update=_physical_update)
    wb_tint: FloatProperty(
        name="Tint", description="Shift to green (-) or to magenta (+)",
        default=0.0, min=-1.0, max=1.0, update=_physical_update)
    lens_preset: EnumProperty(
        name="Lens", description="Lens preset. Add your own lenses in the "
                                 "add-on preferences",
        items=lenses.enum_items, update=_lens_preset_update)
    focus_breathing: BoolProperty(
        name="Focus Breathing",
        description="Change the focal length a little when the focus "
                    "distance changes. The lens preset sets the amount. "
                    "CineKit adds a driver on the focal length",
        default=False, update=_breathing_update)
    breathing_amount: FloatProperty(
        name="Breathing Amount",
        description="Breathing strength. The lens preset sets this value",
        default=0.1, min=0.0, max=1.0, update=_breathing_update)
    look: StringProperty(
        name="Camera Look",
        description="Look id that a new shot with this camera gets",
        default="")


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
               ('SNAP', "Snap", "Hold, then jump. This is a whip focus")),
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
        name="Value", description="Effect strength. The master intensity "
                                  "multiplies it",
        min=0.0, max=2.0, soft_max=1.0, update=_param_update)


class CK_SceneSettings(bpy.types.PropertyGroup):
    shots: CollectionProperty(type=CK_Shot)
    shot_index: IntProperty(name="Active Shot", default=-1)
    auto_switch_shot_look: BoolProperty(
        name="Auto-Switch Looks",
        description="Apply the look of each shot when the playhead enters "
                    "the frame range of the shot", default=True)
    active_look: StringProperty(
        name="Active Look", description="Id of the look that is applied",
        default="")
    look_intensity: FloatProperty(
        name="Intensity", description="Master strength of the whole look",
        default=1.0, min=0.0, max=1.0, update=_intensity_update)
    look_params: CollectionProperty(type=CK_LookParam)
    overlay_enabled: BoolProperty(
        name="Shot Overlay",
        description="Show the shot, camera, lens, f-stop and frame in shot "
                    "as text in the viewport",
        default=False, update=_overlay_update)
    rig_advanced_mode: BoolProperty(
        name="Advanced Mode",
        description="Show a settings dialog when you create a rig, and a "
                    "Rig Points panel to select and move the rig points "
                    "(carrier, aim, path, base) in the viewport",
        default=False)
    security_timestamp: StringProperty(
        name="Timestamp Text",
        description="Text that the Security Cam look burns into the image",
        default="CAM 01  06.07.2026 14:32", update=_timestamp_update)


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
