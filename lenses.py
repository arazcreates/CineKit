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

_lenses = {}       # id -> dict(name, focal, max_aperture, breathing)
_enum_cache = []


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
            _lenses[lid] = {
                "name": str(lens["name"]),
                "focal": float(lens["focal"]),
                "max_aperture": float(lens["max_aperture"]),
                "breathing": float(lens.get("breathing", 0.1)),
            }
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
    """The item order that older CineKit versions stored as positions."""
    if not _lenses:
        reload_registry()
    return [stable_enum.NONE_ID] + [lid for lid, _lens in _display_order()]


def enum_items(_self=None, _context=None):
    if not _lenses:
        reload_registry()
    entries = [(stable_enum.NONE_ID, "Custom",
                "No preset. Set the focal length yourself")]
    for lid, lens in _display_order():
        desc = (f"{lens['focal']:g}mm, f/{lens['max_aperture']:g}, "
                f"breathing {lens['breathing']:.2f}")
        entries.append((lid, lens["name"], desc))
    _enum_cache[:] = stable_enum.numbered_items(entries)
    return _enum_cache
