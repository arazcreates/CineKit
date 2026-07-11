# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Look preset schema: validation and normalisation.

Pure Python, no bpy — unit-testable outside Blender (tests/test_schema.py).

A Look is data. It configures three layers together:
  camera : real optical/sensor characteristics applied to the shot camera
  color  : view transform / exposure bias (only when explicitly set)
  chain  : ordered compositor artifacts, built into one node group

Each chain entry exposes one "strength" group-input socket named after the
artifact (see ARTIFACTS[type]["socket"]); the engine multiplies every
strength by the master Intensity socket, so the whole look fades as one.
"""

VALID_ASPECTS = ("4:3", "16:9", "1.66:1", "1.85:1", "2.39:1", None)

# type -> {socket: exposed strength socket name,
#          settings: {key: (default, min, max)}}
ARTIFACTS = {
    "soft_res": {
        "socket": "Softness",
        "settings": {"lines": (330, 100, 2160)},
    },
    "chroma_bleed": {
        "socket": "Chroma Bleed",
        "settings": {"blur_px": (24, 1, 128), "offset_px": (4, 0, 32)},
    },
    "luma_noise": {
        "socket": "Noise",
        "settings": {"amount": (0.06, 0.0, 1.0)},
    },
    "dropouts": {
        "socket": "Dropouts",
        "settings": {"density": (0.08, 0.0, 1.0)},
    },
    "wobble": {
        "socket": "Wobble",
        "settings": {"pixels": (3.0, 0.0, 40.0)},
    },
    "head_bar": {
        "socket": "Head Switch",
        "settings": {"height": (0.03, 0.005, 0.15)},
    },
    "sharpen": {
        "socket": "Sharpen",
        "settings": {"radius_px": (2.0, 0.5, 16.0), "amount": (1.0, 0.0, 4.0)},
    },
    "levels": {
        "socket": "Levels",
        "settings": {"black": (0.0627, 0.0, 0.3), "white": (0.9216, 0.5, 1.0)},
    },
    "grain": {
        "socket": "Grain",
        "settings": {"size": (2.0, 0.5, 12.0), "amount": (0.12, 0.0, 1.0)},
    },
    "weave": {
        "socket": "Gate Weave",
        "settings": {"pixels": (2.0, 0.0, 20.0)},
    },
    "halation": {
        "socket": "Halation",
        "settings": {"threshold": (0.85, 0.2, 3.0), "size": (7, 3, 9)},
    },
    "film_curve": {
        "socket": "Tone",
        "settings": {"contrast": (0.15, 0.0, 0.5), "warmth": (0.0, -0.5, 0.5)},
    },
    "vignette": {
        "socket": "Vignette",
        "settings": {"amount": (0.5, 0.0, 1.0)},
    },
    "color_cast": {
        "socket": "Cast",
        "settings": {"r": (1.0, 0.0, 2.0), "g": (1.0, 0.0, 2.0),
                     "b": (1.0, 0.0, 2.0)},
    },
    "dust": {
        "socket": "Dust",
        "settings": {"density": (0.05, 0.0, 1.0)},
    },
    "desaturate": {
        "socket": "Desaturate",
        "settings": {"amount": (0.9, 0.0, 1.0)},
    },
    "gamma": {
        "socket": "Gamma",
        "settings": {"value": (1.6, 0.2, 4.0)},
    },
    "interlace": {
        "socket": "Interlace",
        "settings": {"amount": (0.2, 0.0, 1.0)},
    },
    "timestamp": {
        "socket": "Timestamp",
        "settings": {},
    },
    "blocking": {
        "socket": "Blocking",
        "settings": {"block_px": (8, 2, 64)},
    },
    "fringing": {
        "socket": "Fringing",
        "settings": {"width_px": (2.0, 0.5, 8.0)},
    },
    "shadow_noise": {
        "socket": "Shadow Noise",
        "settings": {"amount": (0.15, 0.0, 1.0)},
    },
    "highlight_bloom": {
        "socket": "Highlight Bloom",
        "settings": {"threshold": (0.9, 0.2, 3.0), "magenta": (0.15, 0.0, 1.0)},
    },
}

CAMERA_KEYS = {
    "sensor_width": (None, 3.0, 70.0),   # mm; None = leave camera alone
    "aspect": None,                       # one of VALID_ASPECTS
    "min_fstop": (None, 0.5, 64.0),       # force aperture >= this (deep DOF)
    "breathing_scale": (1.0, 0.0, 4.0),
}

COLOR_KEYS = {
    "view_transform": None,   # str or None = don't touch user's transform
    "look": None,             # color-management look name or None
    "exposure_bias": (0.0, -5.0, 5.0),
}


class LookError(ValueError):
    """Raised by validate_look for a structurally invalid preset."""


def _check_num(errors, where, key, value, lo, hi):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"{where}: '{key}' must be a number, got {value!r}")
    elif not (lo <= value <= hi):
        errors.append(f"{where}: '{key}'={value} outside [{lo}, {hi}]")


def validate_look(data):
    """Return a list of error strings; empty list means valid."""
    errors = []
    if not isinstance(data, dict):
        return ["look preset must be a JSON object"]
    for key in ("id", "name"):
        if not isinstance(data.get(key), str) or not data.get(key):
            errors.append(f"'{key}' is required and must be a non-empty string")
    if "id" in data and isinstance(data["id"], str):
        if not all(c.isalnum() or c in "_-" for c in data["id"]):
            errors.append(f"id '{data['id']}' may only contain [a-zA-Z0-9_-]")

    cam = data.get("camera", {})
    if not isinstance(cam, dict):
        errors.append("'camera' must be an object")
        cam = {}
    for key, value in cam.items():
        if key not in CAMERA_KEYS:
            errors.append(f"camera: unknown key '{key}'")
        elif key == "aspect":
            if value not in VALID_ASPECTS:
                errors.append(f"camera: aspect '{value}' not one of "
                              f"{[a for a in VALID_ASPECTS if a]}")
        elif value is not None:
            _, lo, hi = CAMERA_KEYS[key]
            _check_num(errors, "camera", key, value, lo, hi)

    col = data.get("color", {})
    if not isinstance(col, dict):
        errors.append("'color' must be an object")
        col = {}
    for key, value in col.items():
        if key not in COLOR_KEYS:
            errors.append(f"color: unknown key '{key}'")
        elif key == "exposure_bias" and value is not None:
            _check_num(errors, "color", key, value, -5.0, 5.0)
        elif key in ("view_transform", "look"):
            if value is not None and not isinstance(value, str):
                errors.append(f"color: '{key}' must be a string or null")

    chain = data.get("chain", [])
    if not isinstance(chain, list):
        errors.append("'chain' must be a list")
        chain = []
    seen_types = set()
    for i, entry in enumerate(chain):
        where = f"chain[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            continue
        atype = entry.get("type")
        if atype not in ARTIFACTS:
            errors.append(f"{where}: unknown artifact type '{atype}'")
            continue
        if atype in seen_types:
            errors.append(f"{where}: duplicate artifact '{atype}' "
                          "(each type may appear once)")
        seen_types.add(atype)
        spec = ARTIFACTS[atype]["settings"]
        for key, value in entry.items():
            if key in ("type", "strength"):
                if key == "strength":
                    _check_num(errors, where, key, value, 0.0, 2.0)
                continue
            if key not in spec:
                errors.append(f"{where} ({atype}): unknown setting '{key}'")
            else:
                _, lo, hi = spec[key]
                _check_num(errors, where, key, value, lo, hi)
    return errors


def normalize_look(data):
    """Validate and return a deep copy with all defaults filled in.

    Raises LookError listing every problem if validation fails.
    """
    errors = validate_look(data)
    if errors:
        raise LookError("; ".join(errors))

    out = {
        "id": data["id"],
        "name": data["name"],
        "description": data.get("description", ""),
        "rationale": data.get("rationale", ""),
        "camera": {},
        "color": {},
        "chain": [],
    }
    cam = data.get("camera", {})
    for key, spec in CAMERA_KEYS.items():
        if key == "aspect":
            out["camera"][key] = cam.get(key)
        else:
            default = spec[0]
            out["camera"][key] = cam.get(key, default)
    col = data.get("color", {})
    out["color"]["view_transform"] = col.get("view_transform")
    out["color"]["look"] = col.get("look")
    out["color"]["exposure_bias"] = col.get("exposure_bias", 0.0)

    for entry in data.get("chain", []):
        atype = entry["type"]
        spec = ARTIFACTS[atype]["settings"]
        norm = {"type": atype, "strength": float(entry.get("strength", 1.0))}
        for key, (default, _lo, _hi) in spec.items():
            norm[key] = entry.get(key, default)
        out["chain"].append(norm)
    return out


def aspect_ratio(aspect):
    """'4:3' -> 1.333..., '1.85:1' -> 1.85, None -> None."""
    if not aspect:
        return None
    w, _, h = aspect.partition(":")
    return float(w) / float(h)
