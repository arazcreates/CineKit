# Changelog

All notable changes to CineKit are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/); version numbers match the
Blender extension version in `blender_manifest.toml`.

## [1.2.0] — 2026-09-15

Everything from 1.1.1 is still here. Verified on Blender 5.2 LTS with 59
unit tests, a smoke test with 148 checks and a render comparison of all 6
looks.

### Fixed
- **Black highlights in looks.** Halation and fringing used a Screen blend.
  In scene-linear light, a Screen blend turns values above 1.0 negative, so
  bright lights rendered black. These effects now add light.
- **Physical camera exposure.** The exposure calibration was about 9 stops
  too dark, so the default camera rendered an almost black image. The
  calibration now reads 1 W/m² as 683 lux and uses the incident-meter
  constant C = 250. A grey card at the metered EV renders at 0.18.
- **Look framing.** A look with a smaller sensor narrowed the field of
  view, so 16mm, Super 8 and Digicam 2005 zoomed in. CineKit now scales the
  focal length with the sensor, and the field of view stays the same.
- **Soft resolution in Blender 5.2.** The 5.2 compositor merges two Scale
  nodes into one transform. The effect cropped or zoomed the image instead
  of softening it. It is now a blur sized to the line count of the format.
- **Low-percentage previews.** Pixel sizes were fixed numbers, so a preview
  at 50% showed stronger effects than the final render. Blur sizes,
  offsets, weave, head-switch jitter and scan lines now follow the render
  height and percentage.
- **Camera jump when a rig attaches.** A rig read the world matrix of new
  members before Blender evaluated them, so the camera moved. CineKit now
  evaluates the rig first. This also covers cameras with a parent.
- **Handheld rig.** A Copy Transforms constraint replaced the camera
  animation with the shake, and intensities above 1 had no effect. The
  shake now goes into the delta transforms of the camera. Intensity 2
  doubles it.
- **Crane and orbit start positions.** The crane arm now reaches the camera
  at the start boom angle. An orbit now starts at the camera position at the
  current frame, and it keeps its speed after a frame-rate change.
- **Looks shared between scenes.** Two scenes with the same look shared one
  node tree, so a change in one scene changed the other. Each scene now has
  its own trees. A copied scene gets its own trees at its next look change.
- **Stored preset selections.** Blender stores the list position of a
  dynamic enum item. A new custom look or user lens moved the positions, so
  saved shots and cameras pointed at the wrong preset. Each item now has a
  stable number, and CineKit converts older files when they open.
- **Depth-of-field floor of a look.** A look raised the camera aperture, and
  that also changed the exposure. The floor now changes only the depth of
  field.
- **White balance release.** Release turned off a white balance that you
  had set by hand. CineKit now restores the values that it stored.
- **Video levels.** Levels used display values as linear values, so the
  blacks lifted too much, and a clamp cut every highlight above white.
  Levels now convert the values to linear first.
- **Sharpening halos.** Sharpening on linear light put black rings around
  bright lights. Sharpening now works on a display copy of the image.
- **Scan lines.** Interlace now uses pixel rows.
- **Strips at the frame edge.** Chroma offset, wobble, weave and the
  head-switch bar left green or black strips at the edge. The moved images
  now repeat their edge pixels.
- **Wobble at strength 0.** The barrel distortion of the wobble stayed on.
  It now fades with the strength.
- **Auto Expose with a look.** Auto Expose removed the exposure bias of the
  active look. The bias now stays on top of the metered value.
- **Gobos.** A light-node gobo replaced a node that fed the light color. It
  now goes on top of that color, and removal connects the node again.
  Remove Lighting Setup now also deletes gobo planes. Gobo images get a
  relative path and a tag, so CineKit never changes an image that you
  loaded.
- **Scene collections.** A second scene used the CineKit collection of the
  first scene. Each scene now gets its own collection.
- **Rack focus.** Old keys inside the range of a new rack bent the pull. A
  new rack now removes them. Rack focus and Remove Focus Targets now
  refresh the breathing driver.
- **Batch render.** The frame handler could switch to an overlapping shot
  during a batch render. The batch now also restores the look values.
- **Overlay.** Turning off the overlay in one scene removed it from every
  scene.
- **Custom look names.** A name with no ASCII letters or digits gave an
  empty id. CineKit now uses a short hash for such a name.
- **User lenses.** CineKit wrote over a damaged user lens file, and the
  other user lenses were lost. CineKit now stops with a message.

### Changed
- All drivers use simple expressions, so they work when Python auto-run is
  off.
- Light linking works in Cycles and in EEVEE.
- On a camera that is not physical, the exposure bias of a look is an
  offset. Your own exposure changes stay when you remove the look.
- The Timestamp Text of the Security Cam look updates at once.
- A lens preset under an active look describes the camera without the
  look. CineKit applies the camera layer of the look again on top.
- The Shots panel shows a Lock Interface switch when shot changes can happen
  during a render.
- Rewrote the changed panel text, messages and documentation in plain,
  direct English.

### Added
- Automatic conversion of files from 1.1.1 and older.
- Unit tests for stable enum numbers, the exposure calibration and the
  display-to-linear conversion.
- Smoke-test checks for camera positions, parented cameras, exposure
  ownership, look framing, per-scene looks, the timestamp, gobo cleanup,
  rack focus, the frame handler and deprecated API use.

[1.2.0]: https://github.com/arazcreates/CineKit/releases/tag/v1.2.0

## [1.1.1] — 2026-07-21

### Fixed
- **Dolly rig position.** The carrier and the camera sat away from the
  dolly track. The Follow Path constraint returns a world-space point, and
  the parent then added the root offset a second time. The error grew with
  the distance from the world origin. The carrier now has no parent, so it
  sits on the track at every position.

### Changed
- Rewrote the documentation, the panel text, the error messages and the
  code comments in plain, direct English. Each message states the problem
  first, then the action to take.
- The dolly operator reads a custom path source before it builds the rig.
  A bad source no longer leaves rig objects in the scene.

### Added
- A smoke-test check for the dolly geometry. It builds a dolly far from
  the world origin. It then asserts that the carrier sits on the track and
  that the camera keeps its position.

[1.1.1]: https://github.com/arazcreates/CineKit/releases/tag/v1.1.1

## [1.1.0] — 2026-07-20

Now targets **Blender 5.2 LTS** (minimum raised to 5.2.0). Verified on
Blender 5.2 LTS with both the automated smoke test and hands-on testing in
the Blender UI.

### Added
- **Rig Advanced Mode** — a toggle in the Rigs panel. With it on, creating
  a rig opens a customization dialog (its settings are also editable in the
  bottom-left "Adjust Last Operation" redo panel): dolly path length /
  start position / aim distance / banking, crane base drop / boom / tilt,
  handheld intensity, orbit speed.
- **Rig Points editor** — select and move a rig's defining points (base,
  carrier, aim, path) straight from the panel and the 3D viewport.
- **Custom dolly path** — build the dolly track from a Grease Pencil stroke
  you drew, or from another selected curve object, instead of the generated
  straight track.

### Changed
- Minimum Blender version raised to **5.2.0** (Blender 5.2 LTS).

[1.1.0]: https://github.com/arazcreates/CineKit/releases/tag/v1.1.0

## [1.0.0] — 2026-07-07

First public release. Verified on Blender 5.1 (headless smoke test +
installed-extension test); Blender 4.2–4.x supported via compatibility
fallbacks.

### Added
- **Physical Camera** — ISO / aperture / shutter (as 1/x or shutter
  angle), computed EV100, one camera drives scene exposure and motion
  blur, temperature/tint white balance, JSON lens presets, and a one-shot
  auto-exposure assist.
- **Shot management** — shot list (camera + range + look + notes), jump /
  duplicate / set-range, timeline-marker sync, a modal batch renderer with
  guaranteed state restore, and an optional viewport overlay.
- **Camera rigs** — Dolly, Crane/Jib, Handheld (four F-modifier noise
  profiles), and Orbit/Turntable, each with driven N-panel properties and
  exact pre-rig restore.
- **Focus tools** — click-to-focus picker (raycast), rack focus with
  ease curves, focus-breathing driver, and a DOF readout (hyperfocal /
  near / far / total).
- **Lighting** — 7 JSON lighting setups, a cross-setup Lighting Mixer,
  20 procedurally generated CC0 gobos (Cycles light-node / EEVEE
  shadow-plane), and a light-linking front-end.
- **Looks** — six presets (VHS, Digicam 2005, 16mm, Super 8, Security Cam,
  Clean Digital) applied across three layers (camera optics, colour
  management, a generated compositor node group) with editable per-artifact
  sliders and a master intensity. Existing compositor setups are wrapped,
  never replaced.
- Pure-Python test suite for optics and look-schema (41 tests) plus a
  headless Blender smoke test.

[1.0.0]: https://github.com/arazcreates/CineKit/releases/tag/v1.0.0
