# Changelog

## 1.3.0 (2026-09-15)

A physical lens model.

- Added T-stops, ND filters, rolling shutter (Cycles), aperture blades, and anamorphic lenses, with a new 50mm 2x preset.
- Added lens softness from aberrations and diffraction.
- Added Physical and Measured focus breathing, and an entrance pupil setting for the crane head.
- Fixed keyframes on ISO, aperture, and shutter, which had no effect during playback or renders.
- Fixed camera switches from timeline markers, which didn't always update the exposure.
- Fixed film grain leaving cyan and black specks in very bright lights in the 16mm and Super 8 looks.
- Lens presets now include transmission, so they can render up to 1/3 stop darker. Files from 1.2 keep their exposure until you pick a preset again.

## 1.2.0 (2026-09-15)

A bug-fix release.

- Fixed exposure, which was about 9 stops too dark.
- Fixed bright lights turning black in looks.
- Fixed 16mm, Super 8, and Digicam 2005 zooming in. They now keep your framing.
- Fixed previews looking different from the final render.
- Fixed rigs moving the camera, and the handheld rig replacing camera animation.
- Fixed looks being shared between scenes.
- Fixed saved shots and cameras pointing at the wrong preset after adding a custom one. Older files convert when you open them.
- Light linking now works in EEVEE too.
- Drivers now work with Python auto-run turned off.

## 1.1.1 (2026-07-21)

- Fixed the dolly rig sitting off its track when it was far from the world origin.
- A bad custom path no longer leaves a half-built rig in the scene.

## 1.1.0 (2026-07-20)

- Now needs Blender 5.2.
- Added Rig Advanced Mode, with a settings dialog for new rigs and a list for moving rig points.
- Dolly paths can come from a Grease Pencil stroke or a curve.

## 1.0.0 (2026-07-07)

First release: physical camera, shots, four rigs, focus tools, lighting, gobos, and six looks.
