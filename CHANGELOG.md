# Changelog

All notable changes to CineKit are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/); version numbers match the
Blender extension version in `blender_manifest.toml`.

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
