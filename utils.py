# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Shared helpers: tagging, collections, drivers, linked-data checks."""

import contextlib
import json
import os
import uuid

import bpy

from . import constants as K


class CKError(Exception):
    """Raised by the apply layer. Operators catch it and report it."""


# ---------------------------------------------------------------- suppression
# Engine-driven property writes must not re-trigger update callbacks.
_suppress_depth = 0


@contextlib.contextmanager
def suppress_updates():
    global _suppress_depth
    _suppress_depth += 1
    try:
        yield
    finally:
        _suppress_depth -= 1


def updates_suppressed():
    return _suppress_depth > 0


# ------------------------------------------------------------------- identity
def new_id(prefix=""):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def is_linked(id_block):
    """True for library-linked or override data — CineKit must not edit it."""
    return bool(id_block is not None
                and (id_block.library or id_block.override_library))


def require_editable(id_block, what="data"):
    if is_linked(id_block):
        raise CKError(f"{what} '{id_block.name}' comes from a linked "
                      "library. Make it local first. Use Object > "
                      "Relations > Make Local.")


# ---------------------------------------------------------------- collections
def ck_collection(scene):
    """Return the scene CineKit collection. Create and tag it if
    needed."""
    for child in scene.collection.children:
        if child.get(K.TAG):
            return child
    coll = bpy.data.collections.get(K.COLLECTION_NAME)
    if coll is None or not coll.get(K.TAG):
        coll = bpy.data.collections.new(K.COLLECTION_NAME)
        coll[K.TAG] = new_id("coll_")
        coll.color_tag = 'COLOR_05'
    if scene.collection.children.find(coll.name) == -1:
        scene.collection.children.link(coll)
    return coll


def link_to_ck(scene, obj, sub=None):
    """Link obj into the CineKit collection (or a tagged sub-collection)."""
    parent = ck_collection(scene)
    if sub:
        target = None
        for child in parent.children:
            if child.get(K.TAG) == sub:
                target = child
                break
        if target is None:
            target = bpy.data.collections.new(sub)
            target[K.TAG] = sub
            parent.children.link(target)
        parent = target
    if obj.name not in parent.objects:
        parent.objects.link(obj)
    return parent


def new_empty(scene, name, display='PLAIN_AXES', size=0.5, sub=None):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = display
    obj.empty_display_size = size
    obj[K.TAG] = new_id("obj_")
    link_to_ck(scene, obj, sub=sub)
    return obj


def find_tagged(pool, key, value=None):
    """All items in pool carrying custom-prop `key` (== value if given)."""
    out = []
    for item in pool:
        tag = item.get(key)
        if tag is None:
            continue
        if value is None or tag == value:
            out.append(item)
    return out


# -------------------------------------------------------------------- drivers
def add_driver(id_block, path, expression, variables=(), index=-1):
    """Create a scripted driver with clearly named variables.

    variables: iterable of (name, id, data_path) or
               (name, 'LOC_DIFF', obj_a, obj_b).
    Only built-in driver namespace ('frame', math functions) is used in
    expressions, so drivers keep evaluating even with the add-on disabled.
    Raises CKError on linked data.
    """
    require_editable(id_block, "Driver target")
    try:
        fcu = id_block.driver_add(path, index) if index >= 0 \
            else id_block.driver_add(path)
    except (TypeError, ValueError) as exc:
        raise CKError(f"Cannot add driver on '{path}': {exc}") from exc
    drv = fcu.driver
    drv.type = 'SCRIPTED'
    for var in drv.variables:
        drv.variables.remove(var)
    for spec in variables:
        v = drv.variables.new()
        v.name = spec[0]
        if spec[1] == 'LOC_DIFF':
            v.type = 'LOC_DIFF'
            v.targets[0].id = spec[2]
            v.targets[1].id = spec[3]
        else:
            v.type = 'SINGLE_PROP'
            v.targets[0].id = spec[1]
            v.targets[0].data_path = spec[2]
    drv.expression = expression
    if not drv.is_valid:
        id_block.driver_remove(path, index)
        raise CKError(f"The driver on '{path}' is not valid. A "
                      "dependency cycle is the usual cause. CineKit "
                      "changed nothing.")
    return fcu


def remove_driver(id_block, path, index=-1):
    with contextlib.suppress(TypeError, ValueError, RuntimeError):
        if index >= 0:
            id_block.driver_remove(path, index)
        else:
            id_block.driver_remove(path)


# -------------------------------------------------------------- actions
# Blender 5 removed the legacy Action.fcurves API. It uses slotted
# (layered) actions. These helpers work with both APIs.
def action_fcurve_factory(action, id_holder):
    """Callable(data_path, index) -> new FCurve on `action`."""
    if hasattr(action, "fcurves"):  # 4.x legacy API
        return lambda path, index: action.fcurves.new(data_path=path,
                                                      index=index)
    slot = action.slots.new(id_type='OBJECT', name=id_holder.name)
    layer = action.layers.new("CineKit")
    strip = layer.strips.new(type='KEYFRAME')
    bag = strip.channelbag(slot, ensure=True)
    if id_holder.animation_data:
        id_holder.animation_data.action_slot = slot
    return lambda path, index: bag.fcurves.new(data_path=path, index=index)


def action_fcurves(action):
    """All FCurves of an action, across legacy and slotted APIs."""
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                out.extend(bag.fcurves)
    return out


# ------------------------------------------------------------------ JSON, I/O
def addon_dir():
    return os.path.dirname(os.path.abspath(__file__))


def data_path(*parts):
    return os.path.join(addon_dir(), "data", *parts)


def user_dir(sub="", create=False):
    """Extension user-preferences directory (custom looks, user lenses)."""
    pkg = __package__
    return bpy.utils.extension_path_user(pkg, path=sub, create=create)


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# JSON blobs inside ID custom properties (prior-state storage).
def store_state(id_block, key, data):
    id_block[key] = json.dumps(data)


def fetch_state(id_block, key):
    raw = id_block.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def clear_state(id_block, key):
    if key in id_block.keys():
        del id_block[key]
