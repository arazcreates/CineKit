# Changelog

All notable changes to CineKit are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/); version numbers match the
Blender extension version in `blender_manifest.toml`.

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

[1.0.0]: https://github.com/ArazCreates/CineKit/releases/tag/v1.0.0
