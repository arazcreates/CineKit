# CineKit reference

How each part works, for when the [README](../README.md) isn't enough.

## Blender version

CineKit needs **Blender 5.2 LTS** or newer.

The code keeps small helpers for the older Blender 4.x API. Only Blender
5.2 is tested.

| Area | Blender 4.x | Blender 5.x |
|---|---|---|
| Scene compositor | `scene.node_tree` and a Composite node | `scene.compositing_node_group` and a Group Output node |
| Mix, Math and XYZ nodes | `CompositorNodeMixRGB`, `CompositorNodeMath` | `ShaderNodeMix`, `ShaderNodeMath` (unified nodes) |
| Animated noise | legacy Texture node and texture datablocks | `TexNoise` and `TexWhiteNoise` with Image Coordinates, W driven by the frame |
| Node settings | node properties | input sockets, also menu sockets |
| Actions (handheld rig) | `action.fcurves` | slotted actions |
| White balance | CineKit compositor gain (Blender 4.2) | white balance of the view transform |
| EEVEE engine id | `BLENDER_EEVEE_NEXT` | `BLENDER_EEVEE` |

## Engine support

| Feature | Cycles | EEVEE | Notes |
|---|---|---|---|
| Physical camera (exposure, depth of field, white balance) | ✅ | ✅ | scene exposure and camera depth of field |
| T-stop and ND filter | ✅ | ✅ | part of the exposure |
| Motion blur from the shutter | ✅ | ✅ | sets `render.motion_blur_shutter` |
| Rolling shutter | ✅ | ❌ | sets the Cycles rolling shutter |
| Aperture shape, anamorphic bokeh | ✅ | ✅ | EEVEE draws the oval bokeh less exactly |
| Lens softness | ✅ | ✅ | compositor blur; turn on the **Viewport Compositor** to see it in the viewport |
| Focus breathing, entrance pupil | ✅ | ✅ | |
| Shots, markers, batch render | ✅ | ✅ | |
| Rigs and focus tools | ✅ | ✅ | |
| Light setups and mixer | ✅ | ✅ | |
| Gobos with the light node tree | ✅ | ❌ | EEVEE ignores light node trees |
| Gobos with a shadow plane | ✅ | ✅ | the plane can show in reflections |
| Light linking | ✅ | ✅ | |
| Looks, compositor layer | ✅ | ✅ | turn on the **Viewport Compositor** to see it in the viewport |
| Looks, camera layer (sensor, depth of field) | ✅ | ✅ | shows in every viewport |

A feature that needs Cycles shows an info label when EEVEE is active. An
engine switch never causes an error.

## The pieces

### Physical camera

Set the ISO, the aperture (f-stop) and the shutter as 1/x seconds or as an
angle. CineKit computes the **EV100** and sets
`scene.view_settings.exposure`. The active camera owns the exposure and the
motion blur. A camera switch, also from a timeline marker, updates the
exposure.

**Calibration.** CineKit reads 1 W/m² of Blender light as 683 lux. It uses
the incident-light meter constant C = 250 from ISO 2720. A grey card lit to
the metered EV renders at middle grey (0.18).

**Release Exposure Control** gives back the exposure, the motion blur
shutter and the white balance from before CineKit took control.

**White balance** uses the white balance of the view transform. It shows
live in every viewport and in the render. CineKit stores your own values
first and restores them when it gives the control back.

**Lens presets** come from `data/lenses.json` (17 primes, zooms and an
anamorphic lens). Add your own lenses in the add-on preferences. A lens in
`user_lenses.json` can also set the optional values that the shipped
presets use: `breathing_model`, `breathing_table`, `transmission`,
`blades`, `blade_rotation_deg`, `blade_rotation_per_stop_deg`,
`round_stops`, `min_focus_m`, `squeeze`, `softness_um` and
`entrance_pupil_mm`.

**Auto Expose** measures the viewport through the active camera. It then
sets the ISO, so the average brightness is middle grey. It works in
Rendered shading, and in Material Preview with the scene lights on. It sets
the values one time and does not animate them. The exposure bias of the
active look stays on top of the metered result.

**T-stop and ND filter.** The exposure uses the T-stop of the lens,
`T = N / sqrt(transmission)`. The depth of field uses the f-stop `N`. An
ND filter of `s` stops makes the exposure `s` stops darker. The depth of
field and the motion blur stay the same.

**Rolling shutter** (Cycles). A CMOS sensor reads its lines from the top to
the bottom. Set the readout time `t_r`. For an exposure time `t`, CineKit
sets the motion blur shutter to `(t + t_r) * fps` frames and the Cycles
rolling shutter duration to `t / (t + t_r)`. Each line then gets the
exposure time `t`, and the last line starts `t_r` after the first line.

**Lens Model** is a sub-panel of the camera. The lens preset fills it.

- **Widest Aperture**: the lens does not open wider. The aperture shape and
  the lens softness count stops from this value.
- **Transmission**: the share of the light that the lens lets through.
- **Minimum Focus**: the closest focus distance, measured from the sensor
  plane. Focus breathing stops here.
- **Entrance Pupil**: the distance from the sensor plane to the entrance
  pupil. The Blender camera origin is the entrance pupil. The crane head
  turns about the sensor plane behind it.
- **Anamorphic Squeeze**: CineKit multiplies the sensor width by the
  squeeze and sets the aperture ratio, so the bokeh is oval. The depth of
  field readout uses the real sensor width.
- **Aperture Shape**: the blade count, the blade rotation, the rotation for
  each stop closed, and the stops that stay round. The shape follows the
  f-stop.
- **Lens Softness**: a Gaussian blur in the compositor.
  - Aberrations: `sigma_a = softness * (N_max / N)^3`. From a lens test,
    `softness = 187.4 / MTF50` micrometers, with MTF50 in line pairs per
    mm.
  - Diffraction: `sigma_d = 0.42 * 550 nm * N * (1 + m)`, with the
    magnification `m = f / (u - f)`, limited to 1:1.
  - Total: `sigma = sqrt(sigma_a^2 + sigma_d^2)`.

  CineKit converts the sigma to pixels with the sensor fit, the render size
  and the pixel aspect. The Blender blur node samples its kernel at whole
  pixels, so a small radius blurs less than it should. A curve on the
  driver sets the radius that gives the correct blur variance, from 0.01
  px up. The panel shows the sigma in micrometers and in pixels.

**Focus breathing** (off by default) adds a driver on the focal length.
`u` is the focus distance from the entrance pupil.

- **Stylized**: `lens = base * (1 + amount * 0.05 / max(u, 0.25))`. This is
  the model of earlier versions.
- **Physical**: `lens = base * (1 + amount * f / (u - f))`. Amount 1 is a
  lens that focuses as a whole, and 0 is a fully compensated lens. A
  negative amount widens the view at close focus, as many internal-focus
  macro and zoom lenses do.
- **Measured**: add points with a focus distance and a focal length from
  your own lens test. CineKit draws a smooth curve through the points.

The focus mark stops at the minimum focus distance. The lens preset sets
the model and the amount, and the active look can scale the breathing.
With no focus object, Blender prints a "Dependency cycle detected" message
for the camera in the system console. The driver reads the focus distance
and sets the focal length of the same camera. The values are correct.

**Keyframes** on the physical settings work during playback and renders. A
frame handler applies them, and it also follows camera switches from
timeline markers.

### Shots

Each shot holds a camera, a frame range, a look and notes. Use *New Shot
from Active Camera*, *Jump to Shot*, *Duplicate* and *Set Scene Range to
Shot* to work with the list.

**Sync Markers** builds `CK:` timeline markers with bound cameras, so
playback switches the cameras. CineKit changes only its own markers. A
frame handler applies the look of each shot when the playhead enters the
shot.

An animation render also runs the frame handler, and the handler changes
cameras and looks. Turn on **Render > Lock Interface** for these renders.
This prevents a crash. The Shots panel shows the switch when it is off.

**Batch Render** renders each shot to `//renders/<shot>/` with its camera
and look. Press Esc to stop after the current shot. CineKit restores the
frame range, the camera, the look, the look values and the output path,
also after a cancel or an error. It also writes a
`cinekit_batch_summary.json` file.

The optional **viewport overlay** shows the shot, camera, lens, f-stop, EV
and frame in the shot.

### Rigs

A rig is a set of empties with drivers and custom properties. CineKit
evaluates the rig before it attaches the camera, so the camera stays where
it is. **Remove Rig** restores the transform, the parent and the delta
transforms of the camera. CineKit constraints use the name prefix `CK `,
because a constraint cannot hold custom properties.

A real camera head turns the camera about a point near the sensor, not
about the lens. Set **Entrance Pupil** in the Lens Model panel. The crane
head then turns about the sensor plane, and a tilt shows parallax between
near and far objects.

- **Dolly**: a path and a carrier with `ck_position` (0 to 1), optional
  banking and optional aim tracking.
- **Crane**: pan, boom, arm length and a level tilt. The base sits behind
  and below the camera.
- **Handheld**: F-modifier noise on a shake empty. Drivers add the noise to
  the delta transforms of the camera, scaled by `ck_intensity` (0 to 2).
  The camera keeps its keyframes, its parent and its constraints. The
  profiles are locked-off breathing, documentary, run and gun, and crash
  zoom.
- **Orbit**: radius, height, speed and start angle. *One Revolution Over
  Frame Range* sets the speed for a turntable shot.

**Advanced Mode** is a switch at the top of the Rigs panel. With it on, a
new rig opens a settings dialog first. The panel also shows a **Rig Points**
list to select and move the points of a rig. The dolly path can come from a
**Grease Pencil** stroke or from a selected **curve**.

All rig drivers use simple expressions. Blender evaluates them without
Python, so they work when auto-run is off and when the add-on is disabled.

### Focus

- **Focus Picker**: click a surface to move the focus target there. The
  camera then focuses on the target. Ctrl-click adds a new target.
- **Rack Focus**: keyframes the focus distance from target A to target B
  with linear, smooth or snap easing. A new rack removes old keys inside its
  range. The active shot stores the rack settings.
- **DOF readout**: the hyperfocal distance, the near and far limits and the
  total depth of field, from thin-lens formulas in `optics.py`. A physical
  camera also shows the focus mark, the distance on the focus scale of the
  lens. The readout warns when the focus is closer than the minimum focus
  distance of the lens.

### Lighting

*Add Lighting Setup* has 7 setups: three-point, butterfly (paramount),
Rembrandt, split, rim and fill, product tabletop and window daylight.
CineKit aims the lights at the active object. It scales their positions,
sizes and power to the object. Run the same setup again to update it. The
**Lighting Mixer** shows every CineKit light in one list.

**Gobos**: 20 generated CC0 patterns (windows, blinds, foliage,
cucoloris). Regenerate them with `python tools/generate_gobos.py`. Cycles
projects the gobo through the light node tree. EEVEE uses a textured shadow
plane in front of the light. When a node already feeds the light color, the
gobo goes on top of that color. **Remove Gobo** connects the node again.
CineKit packs the gobo images and gives them a relative path, so no folder
path from your computer goes into the .blend file.

**Light linking**: add a receiver collection to a light, add the selected
objects, and remove members. It works in Cycles and in EEVEE.

### Looks

CineKit has 6 looks: VHS, Digicam 2005, 16mm, Super 8, Security Cam and
Clean Digital. A look sets three layers together. The `rationale` field of
each preset in `data/looks/*.json` explains its values.

1. **Camera**: the sensor width, the aspect ratio, a depth-of-field floor
   and a breathing scale. A smaller sensor also gets a matching focal
   length, so the field of view stays the same. The depth-of-field floor
   changes only the depth of field. The exposure keeps the aperture of the
   camera. This layer shows in every viewport.
2. **Color**: the view transform and an exposure bias, only when the look
   sets them. On a physical camera, the bias is part of the exposure value.
   On another camera, CineKit adds the bias as an offset, and your own
   exposure changes stay.
3. **Compositor**: a node group built from the look values, with YCC chroma
   bleed, animated noise, time-base wobble, glow, tone curves and more. The
   only image is the generated timestamp.

The compositor effects work in scene-linear light. The glow effects add
light, so bright highlights stay bright. Sharpening works on a display copy
of the image, so it puts no black rings around lights. Pixel sizes follow
the render height and the render percentage. Soft resolution is a blur that
removes detail finer than the line count of the format.

CineKit inserts **one** group node between the compositor input and the
output. It stores the original link and restores it exactly on removal. If
the scene had no compositor, CineKit deletes the one that it made. Each
scene gets its own look node trees. Applying the same look again only
updates its values. CineKit builds the node tree again when the look
changes or when the aspect ratio of the render changes.

Each effect has a strength slider in the 3D View and in the compositor
sidebar. A master Intensity scales all of them. **Save as Custom Look**
writes a user preset. The **Timestamp Text** of the Security Cam look
updates at once.

**Security Cam frame rate.** Do not change the scene frame rate. Hold keys
every 3 or 4 frames in the action, or use a Speed Control strip in the
Sequencer. Physics, motion blur and audio then stay correct.

## Data hygiene

Everything CineKit creates has a `cinekit_id` custom property and goes into
the **CineKit** collection of the scene. Operators find and update their
data, so nothing piles up. Each *Add* operator has a *Remove* operator. It
deletes only tagged data and restores the stored state: the camera
transform, the compositor links, the exposure, the white balance, the view
transform and the render size. For linked cameras, lights and node trees,
CineKit shows an explanation instead of an error.

When you open a file from an older CineKit version, CineKit converts the
stored preset selections and the exposure state of the looks.

## Tests

```
# Pure Python, no Blender
python -m unittest discover tests

# Headless Blender smoke test
blender -b --factory-startup -P tests/smoke.py
```

The smoke test applies every look and adds and removes every rig, lighting
setup and gobo. It checks that the datablock counts return to the start. It
also checks the camera positions, the exposure values, the framing, the
per-scene looks, and that CineKit uses no deprecated Blender API. For the
lens model, it checks the T-stop and ND exposure, the aperture shape, the
squeeze, the rolling shutter, the lens blur size at each sensor fit, the
breathing models and the head pivot. A unit test runs the lens blur driver
expression in Python and compares it with the optics functions.
