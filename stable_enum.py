# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Stable numbers for dynamic EnumProperty items.

Pure Python, no bpy. The tests import this module directly.

Blender stores the integer value of an enum item, not its identifier. A
dynamic items callback without explicit numbers uses the list position as
the value. When a user adds a lens or saves a custom look, the list order
changes, and every stored selection then points at a different item.

This module gives each identifier a number that does not depend on the
list order. The number comes from a CRC32 of the identifier. "NONE" always
gets 0. Numbers below LEGACY_LIMIT never occur, so older files that stored
a list position are easy to detect and to migrate.
"""

import zlib

NONE_ID = "NONE"
LEGACY_LIMIT = 1000        # stored values below this are old list positions
_SPAN = 2_000_000_000      # keeps every number inside a signed 32-bit int


def number_for(identifier, taken=None):
    """Return the stable number for an identifier.

    `taken` is a set of numbers already in use by the same list. A CRC32
    collision moves to the next free number. The caller must process the
    identifiers in a fixed order (sorted) to keep the result stable.
    """
    if identifier == NONE_ID:
        return 0
    number = LEGACY_LIMIT + (zlib.crc32(identifier.encode("utf-8")) % _SPAN)
    if taken is not None:
        while number in taken:
            number += 1
        taken.add(number)
    return number


def numbered_items(entries):
    """Add stable numbers to (identifier, name, description) tuples.

    The display order of `entries` stays the same. Numbers come from the
    sorted identifiers, so the display order does not affect them.
    """
    taken = {0}
    numbers = {}
    for identifier in sorted(e[0] for e in entries if e[0] != NONE_ID):
        numbers[identifier] = number_for(identifier, taken)
    numbers[NONE_ID] = 0
    return [(ident, name, desc, numbers[ident])
            for ident, name, desc in entries]


def legacy_identifier(stored_value, legacy_order):
    """Map an old list-position value to an identifier.

    Return None when the value is not a legacy position or when it is out
    of range for `legacy_order`.
    """
    if not isinstance(stored_value, int) or isinstance(stored_value, bool):
        return None
    if stored_value <= 0 or stored_value >= LEGACY_LIMIT:
        return None
    if stored_value >= len(legacy_order):
        return None
    return legacy_order[stored_value]
