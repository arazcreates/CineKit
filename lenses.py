# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Lens preset registry: shipped JSON + user lenses from the prefs dir."""

import json
import os
import zlib

try:
    import bpy
except ImportError:
    bpy = None

try:
    from . import stable_enum
except ImportError:  # the pure-Python tests import this module on its own
    import stable_enum

_lenses = {}       # id -> normalized lens dict (see _normalize)
_enum_cache = []

BREATHING_MODELS = ("STYLIZED", "UNIT", "TABLE")

# Optional lens values and their defaults. The defaults keep the behavior of
# a preset that does not set the value.
OPTIONAL = {
    "breathing": 0.1,
    "breathing_model": "STYLIZED",
    "breathing_table": [],        # [[focus distance m, focal length mm], ...]
    "transmission": 1.0,          # T-stop = f-stop / sqrt(transmission)
    "blades": 0,                  # 0 = round aperture
    "blade_rotation_deg": 0.0,
    "blade_rotation_per_stop_deg": 0.0,
    "round_stops": 0.0,           # round up to this many stops closed
    "min_focus_m": 0.0,           # 0 = unknown, measured from the sensor
    "squeeze": 1.0,               # anamorphic squeeze factor
    "softness_um": 0.0,           # aberration blur sigma wide open
    "entrance_pupil_mm": 0.0,     # sensor plane to entrance pupil, 0 = unknown
}


def _number(value, lo, hi):
    value = float(value)
    if not lo <= value <= hi:
        raise ValueError(f"{value} is outside [{lo}, {hi}]")
    return value


def _normalize(lens):
    """Validate one lens entry and fill the optional values."""
    out = {
        "name": str(lens["name"]),
        "focal": _number(lens["focal"], 1.0, 5000.0),
        "max_aperture": _number(lens["max_aperture"], 0.5, 64.0),
        # Version that added a shipped preset. Older files stored list
        # positions without these presets (see legacy_ids).
        "added": str(lens.get("added", "")),
    }
    for key, default in OPTIONAL.items():
        out[key] = lens.get(key, default)
    out["breathing"] = _number(out["breathing"], -1.0, 1.0)
    if out["breathing_model"] not in BREATHING_MODELS:
        raise ValueError(f"unknown breathing model {out['breathing_model']!r}")
    out["transmission"] = _number(out["transmission"], 0.3, 1.0)
    out["blades"] = int(_number(out["blades"], 0, 16))   # the Blender limit
    out["blade_rotation_deg"] = float(out["blade_rotation_deg"])
    out["blade_rotation_per_stop_deg"] = float(out["blade_rotation_per_stop_deg"])
    out["round_stops"] = _number(out["round_stops"], 0.0, 10.0)
    out["min_focus_m"] = _number(out["min_focus_m"], 0.0, 100.0)
    out["squeeze"] = _number(out["squeeze"], 1.0, 2.5)
    out["softness_um"] = _number(out["softness_um"], 0.0, 50.0)
    out["entrance_pupil_mm"] = _number(out["entrance_pupil_mm"], 0.0, 1000.0)
    table = []
    for pair in out["breathing_table"]:
        distance, focal = pair
        table.append((_number(distance, 0.01, 100000.0),
                      _number(focal, 1.0, 5000.0)))
    table.sort()
    if out["breathing_model"] == "TABLE" and len(table) < 2:
        raise ValueError("a measured breathing table needs 2 or more points")
    out["breathing_table"] = table
    return out


def _builtin_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "lenses.json")


def user_lenses_path(create_dir=False):
    if bpy is None:
        return None
    try:
        base = bpy.utils.extension_path_user(__package__, create=create_dir)
    except (ValueError, RuntimeError):
        return None
    return os.path.join(base, "user_lenses.json")


def _load_file(path):
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data.get("lenses", [])
    except (OSError, ValueError) as exc:
        print(f"CineKit: could not read lenses from '{path}': {exc}")
        return
    for lens in entries:
        try:
            lid = str(lens["id"])
            _lenses[lid] = _normalize(lens)
        except (KeyError, TypeError, ValueError) as exc:
            print(f"CineKit: skipped invalid lens entry in '{path}': {exc}")


def reload_registry():
    _lenses.clear()
    _load_file(_builtin_path())
    _load_file(user_lenses_path())


def get(lens_id):
    if not _lenses:
        reload_registry()
    return _lenses.get(lens_id)


def add_user_lens(name, focal, max_aperture, breathing):
    """Append a lens to the user JSON in the extension prefs dir."""
    path = user_lenses_path(create_dir=True)
    if path is None:
        raise RuntimeError("CineKit cannot open the user folder of the "
                           "extension.")
    data = {"lenses": []}
    if os.path.isfile(path):
        # Stop on a file that CineKit cannot read. Writing over it would
        # delete the other user lenses.
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"CineKit cannot read '{path}'. Repair the "
                               f"file or move it away: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"'{path}' does not contain a lens list. "
                               "Repair the file or move it away.")
    slug = "".join(c for c in name.lower()
                   if c.isascii() and (c.isalnum() or c == "_"))
    lid = "user_" + (slug or f"{zlib.crc32(name.encode('utf-8')):08x}")
    entry = {"id": lid, "name": name, "focal": focal,
             "max_aperture": max_aperture, "breathing": breathing}
    entries = [e for e in data.setdefault("lenses", [])
               if isinstance(e, dict) and e.get("id") != lid]
    entries.append(entry)       # a lens with the same name replaces the old one
    data["lenses"] = entries
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    reload_registry()
    return lid


def _display_order():
    return sorted(_lenses.items(), key=lambda kv: kv[1]["focal"])


def legacy_ids():
    """The item order that older CineKit versions stored as positions.

    Presets added in a later version did not exist in those files, so they
    are not part of the order.
    """
    if not _lenses:
        reload_registry()
    return [stable_enum.NONE_ID] + [lid for lid, lens in _display_order()
                                    if not lens["added"]]


def enum_items(_self=None, _context=None):
    if not _lenses:
        reload_registry()
    entries = [(stable_enum.NONE_ID, "Custom",
                "No preset. Set the focal length yourself")]
    for lid, lens in _display_order():
        desc = f"{lens['focal']:g}mm, f/{lens['max_aperture']:g}"
        if lens["transmission"] < 0.999:
            tstop = lens["max_aperture"] / lens["transmission"] ** 0.5
            desc += f" (T{tstop:.1f})"
        if lens["squeeze"] > 1.0:
            desc += f", {lens['squeeze']:g}x anamorphic"
        desc += f", breathing {lens['breathing']:.2f}"
        entries.append((lid, lens["name"], desc))
    _enum_cache[:] = stable_enum.numbered_items(entries)
    return _enum_cache
