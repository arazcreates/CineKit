# CineKit

> A film-set workflow for Blender — physical cameras, shot management,
> camera rigs, focus tools, lighting presets & gobos, and signature
> camera **Looks** (VHS, 16mm, Digicam…) built from real optics.

**Author:** Araz Creates · **License:** GPL-3.0-or-later · **Blender:** 4.2+ (incl. 5.x) · **Engines:** Cycles & EEVEE Next

A cinematography, lighting and camera-look toolkit for **Blender 4.2+,
including Blender 5.x** (Extensions platform, `blender_manifest.toml`).
Pure Python, no external dependencies. GPL-3.0-or-later; shipped gobo
textures are CC0-1.0.

## Blender version compatibility

Verified on **Blender 5.1** (headless smoke test + installed-extension
test). Blender 4.2-4.x is supported through compatibility fallbacks that
are exercised automatically when the legacy APIs are present:

| Area | 4.2-4.x | 5.x |
|---|---|---|
| Scene compositor | `scene.node_tree` + Composite node | `scene.compositing_node_group` + Group Output |
| Mix / Math / XYZ nodes | `CompositorNodeMixRGB/Math/…` | `ShaderNodeMix/Math/…` (unified nodes) |
| Animated noise | legacy Texture node + texture datablocks | `TexNoise`/`TexWhiteNoise`/`TexWave` + Image Coordinates, W driven by frame |
| Node settings | node properties | input sockets (incl. menu sockets) |
| Actions (handheld rig) | `action.fcurves` | slotted actions (slots/layers/strips) |
| White balance | CineKit compositor gain | native view-transform WB |
| EEVEE engine id | `BLENDER_EEVEE_NEXT` | `BLENDER_EEVEE` |

All of this lives behind small helpers (`looks/engine.py` node compat,
`utils.action_fcurve_factory`) — builders and operators are written once.

CineKit gives Blender a film-set workflow: physical cameras with real-world
controls, shot management, camera rigs, focus pulling, lighting presets and
gobos, and **Looks** — VHS, Digicam 2005, 16mm, Super 8, Security Cam,
Clean Digital — built from real optical/sensor characteristics applied at
the **camera and compositor level**, not a LUT slapped on afterward.

## Install

Zip the extension folder (or `blender --command extension build`) and
install via *Preferences > Get Extensions > Install from Disk*, or drop the
folder into your extensions repo. Requires Blender **4.2** or later.

## Engine support matrix

| Feature | Cycles | EEVEE Next | Notes |
|---|---|---|---|
| Physical camera (exposure/DOF/WB) | ✅ | ✅ | scene exposure + camera DOF |
| Motion blur from shutter | ✅ | ✅ | writes `render.motion_blur_shutter` |
| Shots, markers, batch render | ✅ | ✅ | |
| Rigs & focus tools | ✅ | ✅ | |
| Light setups & mixer | ✅ | ✅ | |
| Gobos — light node tree | ✅ | ❌ | EEVEE ignores light node trees |
| Gobos — shadow plane fallback | ✅ | ✅ | plane may show in reflections |
| Light linking | ✅ | ❌ | native Cycles-only feature (4.2) |
| Looks — compositor layer | ✅ | ✅ | enable the **Viewport Compositor** to preview |
| Looks — camera layer (crop, deep DOF) | ✅ | ✅ | visible in any viewport |

Cycles-only features hide or show an info label when EEVEE Next is active;
switching engines never errors, and re-applying a Look after an engine
switch reconciles cleanly.

## The pieces

### Physical Camera (Camera data panel + CineKit N-panel)
ISO, aperture (f-stop), shutter as 1/x or shutter angle. CineKit computes
**EV100** (`optics.py`, ISO 2720 with K = 12.5) and drives
`scene.view_settings.exposure` via update callbacks; the **active** camera
owns exposure and motion blur — switching cameras (including marker
switches) re-syncs via a msgbus subscription. "Release Exposure Control"
restores the exposure that was set before CineKit took over.

**White balance approach (documented):** on Blender 4.3+ CineKit uses the
native view-transform white balance (live everywhere, render-consistent).
On 4.2 it computes per-channel planckian gains (`optics.wb_gains`) and
applies them through a multiply node in the CineKit compositor pipeline —
live in the viewport only with the Viewport Compositor enabled, always in
the final render. We deliberately do **not** bend the user's view-transform
curves: that would fight custom OCIO configs.

Lens presets are JSON (`data/lenses.json`, ~16 primes/zooms with focal
length, max aperture, breathing amount); add your own in the add-on
preferences (stored in the extension user directory). **Auto Expose** is a
one-shot *assist*: it samples a low-res offscreen render of the camera view
(scene-referred, geometric-mean luminance) and sets ISO to hit the target
middle grey from the preferences. It sets values once — it never animates.

**Focus breathing** (off by default, per camera) adds a driver on focal
length: `lens = base * (1 + amount * 0.05 / max(focus_m, 0.25))` — a
stylised but distance-correct model, scaled by the lens preset's breathing
amount and the active look's `breathing_scale`.

### Shots
UIList of shots (camera + frame range + look + notes). *New Shot from
Active Camera*, *Jump to Shot* (sets camera, frame and look), *Duplicate*,
*Set Scene Range to Shot*. **Sync Markers** rebuilds `CK:`-prefixed
timeline markers with `marker.camera` bound — playback switches cameras
natively; user markers are never touched. The shot list stays the source of
truth. Per-shot looks apply automatically during playback via a
frame-change handler that does O(1) early-out checks (rescans only on
boundary crossings).

**Batch Render** renders each shot's range to `//renders/<shot>/` with its
camera and look, as a modal operator with ESC-to-stop; frame range, camera,
look and output path are restored afterwards even on cancel or exception,
and a `cinekit_batch_summary.json` is written.

The optional **viewport overlay** (gpu/blf draw handler) shows shot name,
camera, lens, f-stop, EV and frame-in-shot.

### Rigs
All rigs are empty hierarchies with drivers and custom properties, created
from the active camera, tracked with `cinekit_id` tags and removable with
exact restore of the camera's pre-rig transform and parent. CineKit
constraints are identified by the `CK ` name prefix (constraints can't
carry custom properties).

- **Dolly** — curve path + carrier; `ck_position` (0-1, keyframeable),
  banking (curve follow), `ck_track` aim tracking.
- **Crane/Jib** — pan → boom → arm length → tilt (tilt is
  level-compensated against the boom).
- **Handheld** — F-modifier noise (no per-frame handlers) on a shake
  empty, blended in by a Copy Transforms constraint whose influence is the
  keyframeable `ck_intensity` master. Profiles: Locked-off breathing,
  Documentary, Run and gun, Crash zoom.
- **Orbit/Turntable** — radius/height/speed/start-angle, plus a
  *One Revolution Over Frame Range* operator for product shots.

Drivers use only Blender's built-in driver namespace (`frame`, math), so
they keep evaluating even with the add-on disabled.

### Focus
- **Focus Picker**: modal click-to-focus via `scene.ray_cast` on the
  evaluated depsgraph; places/moves a Focus Target empty and points the
  camera's DOF at it (Ctrl-click adds a new target). ESC cancels; stale
  modal state is cleared on file load.
- **Rack Focus**: keyframes DOF distance from target A to B over N frames
  with linear / smooth / snap easing; the rack is stored on the active
  shot.
- **DOF readout**: hyperfocal, near/far limits and total depth of field
  from standard thin-lens formulas in `optics.py` (pure Python,
  unit-tested).

### Lighting
JSON light setups (`data/light_setups.json`): three-point,
butterfly/paramount, Rembrandt, split, rim+fill, product tabletop, window
daylight. *Add Lighting Setup* aims lights at the active object, scales
positions/sizes to its bounding box and power by the inverse-square law,
and groups them in a tagged collection — re-running **updates** the
existing setup, never duplicates. The **Lighting Mixer** lists every
CineKit light (across setups) with power/color/size in one place.

**Gobos**: 20 procedurally generated CC0 textures (windows, blinds,
foliage, cucolorises — regenerate with `python tools/generate_gobos.py`).
On Cycles the gobo is projected through the light's node tree; on EEVEE
Next a textured shadow plane is parented in front of the light (stated in
the UI). Images are packed into the .blend — no absolute paths.

**Light linking** (Cycles only): a small front-end for receiver
collections — create, add selected objects, remove members.

### Looks — the signature feature
A Look configures three layers together (see each preset's `rationale`
field in `data/looks/*.json` for the parameter reasoning):

1. **Camera/optics** — real sensor width, aspect crop, forced minimum
   f-stop, breathing scale. *This* is why Digicam 2005 has deep focus (a
   5.76 mm sensor at f/8, not fake blur) and VHS is 4:3 (sensor crop +
   render resolution) — and it's visible in any viewport.
2. **Render/color** — view transform / exposure bias, only when the look
   explicitly sets one; the previous transform is stored and restored.
3. **Compositor** — a generated node group `CK_Look_<Name>` built from
   parameters (YCC chroma bleed, animated noise/dropouts, displaced
   time-base wobble, glare halation, programmatic tone curves…), no baked
   images except the generated timestamp bitmap.

The pipeline is **one** group node inserted in-line before the Composite
node. Existing compositor setups are wrapped, never replaced: the original
link into Composite is stored and *exactly* restored on removal; if
`use_nodes` was off, removal returns the scene to `use_nodes = False`.
Applying the same look twice is idempotent; re-applying updates socket
values in place (no rebuild, no hitch) — rebuilds happen only on look-type
switches. Every artifact exposes a strength slider (scene-level, editable
in the 3D View and compositor N-panels) multiplied by a master Intensity.
**Save as Custom Look** writes a user JSON preset to the extension's user
preferences directory.

**Security Cam frame-rate feel**: don't change the scene FPS. Render
normally, then either hold keys every 3–4 frames in the action, or use a
Sequencer *Speed Control* effect strip in "Frame Number" mode to step
time. This keeps physics, motion blur and audio sane.

## Data hygiene
Everything CineKit creates carries a `cinekit_id` custom property and goes
in the **CineKit** collection; operators find-and-update instead of
piling up, and every *Add X* has a *Remove X* that deletes only tagged data
and restores stored prior state (camera transform, compositor links, scene
exposure, view transform, render resolution). Linked/library cameras,
lights and node trees are detected and grayed out with an explanation
instead of raising. Shot lists, looks and exposure are per-scene.

## Tests

```
# Pure-Python (no Blender): optics + look schema, 41 tests
cd CineKit
python -m unittest discover tests

# Headless Blender smoke test: applies every look, adds/removes every rig
# and lighting preset, asserts datablock counts return to the start state
blender -b -P tests/smoke.py
```

## Author

**Araz Creates** — [araz.design3d@gmail.com](mailto:araz.design3d@gmail.com)

If you build something with CineKit I'd love to see it.

## Contributing

Issues and pull requests are welcome. The pure-Python parts (`optics.py`,
`looks/schema.py`) have unit tests that run without Blender — please keep
them green (`python -m unittest discover tests`) and add tests for new
optics/schema logic. For Blender-side changes, run the headless smoke test
(`blender -b -P tests/smoke.py`) and make sure datablock counts still
return to the start state.

## License

Copyright © 2026 **Araz Creates**.

CineKit is free software licensed under the **GNU General Public License
v3.0 or later (GPL-3.0-or-later)** — see [LICENSE](LICENSE) for the full
text. Blender add-ons use Blender's GPL-licensed Python API, so they must
themselves be GPL-compatible; CineKit is and always will be.

The procedurally generated gobo textures (`data/gobos/*.png`) and their
generator script (`tools/generate_gobos.py`) are released separately into
the public domain under **CC0-1.0**, so you may reuse them in any project
without restriction.
