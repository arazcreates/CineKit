# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Look preset registry: shipped JSON + user JSON from the extension prefs dir.

Importable without bpy so tests can `import looks.schema`.
"""

import json
import os

try:
    import bpy
except ImportError:  # pure-Python test environment
    bpy = None

from . import schema

_registry = {}     # id -> normalized look dict
_errors = []       # (source_path, message)
# EnumProperty items callbacks must return strings we keep alive.
_enum_cache = []


def builtin_dir():
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "data", "looks"))


def user_looks_dir(create=False):
    if bpy is None:
        return None
    pkg = __package__.rsplit(".", 1)[0]  # parent extension package
    try:
        return bpy.utils.extension_path_user(pkg, path="looks", create=create)
    except (ValueError, RuntimeError):
        return None


def _load_dir(path, source):
    if not path or not os.path.isdir(path):
        return
    for fname in sorted(os.listdir(path)):
        if not fname.lower().endswith(".json"):
            continue
        fpath = os.path.join(path, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            look = schema.normalize_look(raw)
        except (OSError, ValueError, schema.LookError) as exc:
            _errors.append((fpath, str(exc)))
            print(f"CineKit: skipped invalid look '{fpath}': {exc}")
            continue
        look["_source"] = source
        _registry[look["id"]] = look


def reload_registry():
    _registry.clear()
    _errors.clear()
    _load_dir(builtin_dir(), "builtin")
    _load_dir(user_looks_dir(), "user")


def get(look_id):
    if not _registry:
        reload_registry()
    return _registry.get(look_id)


def all_looks():
    if not _registry:
        reload_registry()
    return dict(_registry)


def load_errors():
    return list(_errors)


def enum_items(_self=None, _context=None):
    """Items callback for EnumProperties, with a leading 'None' entry."""
    if not _registry:
        reload_registry()
    _enum_cache.clear()
    _enum_cache.append(("NONE", "None", "No look"))
    for lid, look in sorted(_registry.items(), key=lambda kv: kv[1]["name"]):
        _enum_cache.append((lid, look["name"], look["description"]))
    return _enum_cache
