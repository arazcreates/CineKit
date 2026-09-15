# CineKit

> A film-set workflow for Blender: physical cameras, shot management,
> camera rigs, focus tools, lighting presets, gobos and camera **Looks**
> (VHS, 16mm, Digicam and more) built from real optics.

**Author:** Araz Creates · **License:** GPL-3.0-or-later · **Blender:** 5.2 LTS or newer · **Engines:** Cycles and EEVEE

CineKit is a cinematography, lighting and camera-look toolkit for **Blender
5.2 LTS** (Extensions platform, `blender_manifest.toml`). It is pure Python
with no external dependencies. The looks come from real optical and sensor
behavior, applied to the camera and the compositor, not from a LUT.

## New in 1.2

Version 1.2 is a quality release. Every feature from 1.1 is still here.

- **Looks keep your framing.** A look with a smaller sensor also scales the
  focal length. The field of view stays the same, and the depth of field
  gets deeper, as on a real small camera.
- **Bright lights stay bright.** The glow effects add light. Before, a
  Screen blend turned lights brighter than white into black spots.
- **Correct physical exposure.** The default camera (f/2.8, 1/50 s, ISO 100)
  now exposes a scene lit by a sun lamp of strength 1. Before, the image
  was almost black.
- **Previews match the final render.** Blur sizes, offsets and scan lines
  follow the render height. A 50% preview looks like the full render.
- **Rigs keep the camera in place.** A new rig no longer moves the camera,
  also when the camera has a parent.
- **Handheld shake goes on top of your animation.** The camera keeps its
  keyframes, its parent and its constraints.
- **Each scene has its own look.** Two scenes with the same look no longer
  share their settings.
- **Saved selections stay correct.** A new custom look or user lens no
  longer changes the look of a saved shot or the lens of a camera. CineKit
  converts files from 1.1.1 and older when you open them.
- **Light linking works in EEVEE too.**

See [CHANGELOG.md](CHANGELOG.md) for the full list.

## Blender version

CineKit needs **Blender 5.2 LTS** or newer. The tests run on Blender 5.2:
59 unit tests, a headless smoke test with 148 checks, and a render
comparison of every look.

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

## Install

1. Build the zip with `blender --command extension build`, or download it
   from the releases page.
2. In Blender, open *Edit > Preferences > Get Extensions*.
3. Click the drop-down arrow at the top right.
4. Click *Install from Disk* and select the zip file.

## Engine support

| Feature | Cycles | EEVEE | Notes |
|---|---|---|---|
| Physical camera (exposure, depth of field, white balance) | ✅ | ✅ | scene exposure and camera depth of field |
| Motion blur from the shutter | ✅ | ✅ | sets `render.motion_blur_shutter` |
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

**Lens presets** come from `data/lenses.json` (16 primes and zooms with
focal length, maximum aperture and breathing amount). Add your own lenses
in the add-on preferences.

**Auto Expose** measures the viewport through the active camera. It then
sets the ISO, so the average brightness is middle grey. It works in
Rendered shading, and in Material Preview with the scene lights on. It sets
the values one time and does not animate them. The exposure bias of the
active look stays on top of the metered result.

**Focus breathing** (off by default) adds a driver on the focal length:
`lens = base * (1 + amount * 0.05 / max(focus_m, 0.25))`. The lens preset
sets the amount, and the active look can scale it.

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
  total depth of field, from thin-lens formulas in `optics.py`.

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
# Pure Python, no Blender: optics, look schema and enum numbers (59 tests)
python -m unittest discover tests

# Headless Blender smoke test (148 checks)
blender -b --factory-startup -P tests/smoke.py
```

The smoke test applies every look and adds and removes every rig, lighting
setup and gobo. It checks that the datablock counts return to the start. It
also checks the camera positions, the exposure values, the framing, the
per-scene looks, and that CineKit uses no deprecated Blender API.

## Author

**Araz Creates**: [araz.design3d@gmail.com](mailto:araz.design3d@gmail.com)

If you make something with CineKit, I would like to see it.

## Contributing

Issues and pull requests are welcome. The pure-Python parts (`optics.py`,
`stable_enum.py`, `looks/schema.py`) have unit tests that run without
Blender. Keep all tests passing with `python -m unittest discover tests`,
and add tests for new optics or schema logic. For a change in the Blender
code, run the headless smoke test. Make sure that it passes.

## License

Copyright © 2026 **Araz Creates**.

CineKit is free software. It uses the **GNU General Public License v3.0 or
later (GPL-3.0-or-later)**. See [LICENSE](LICENSE) for the full text. Every
Blender add-on uses the GPL Python API of Blender, so an add-on must be
GPL-compatible. CineKit is, and it always will be.

The generated gobo textures (`data/gobos/*.png`) and their generator script
(`tools/generate_gobos.py`) are also in the public domain under
**CC0-1.0**. You can use them in any project with no restriction.
