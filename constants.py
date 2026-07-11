# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Araz Creates
"""Shared constants. No bpy import — safe for pure-Python tests."""

# Custom-property tag carried by every datablock CineKit creates.
# Operators find-and-update via these tags and never touch untagged data.
TAG = "cinekit_id"                 # generic ownership tag (string id)
TAG_RIG = "cinekit_rig"            # on a rig root empty: rig type string
TAG_RIG_MEMBER = "cinekit_rig_member"  # on rig members: rig root name
TAG_LOOK = "cinekit_look"          # on look node groups: look id
TAG_LIGHT_SETUP = "cinekit_light_setup"  # on setup collections: setup id
TAG_FOCUS = "cinekit_focus"        # on focus target empties

# Keys for stored prior state (JSON strings in ID custom properties).
PREV_CAMERA_RIG = "cinekit_prerig"        # camera object: matrix + parent before rigging
PREV_LOOK_CAMERA = "cinekit_prelook"      # camera data: settings before a look's camera layer
PREV_EXPOSURE = "cinekit_prev_exposure"   # scene: view_settings.exposure before CK took over
PREV_COLOR = "cinekit_prev_color"         # scene: view transform/look before a Look set one
PREV_COMP = "cinekit_prev_comp"           # scene: compositor links before pipeline insertion
PREV_RENDER = "cinekit_prev_render"       # scene: resolution before a look aspect change
BASE_LENS = "cinekit_base_lens"           # camera data: focal length before breathing driver

COLLECTION_NAME = "CineKit"
PIPELINE_GROUP = "CK_Pipeline"     # inserted compositor group (WB + look slot)
LOOK_GROUP_PREFIX = "CK_Look_"     # per-look node group datablocks
MARKER_PREFIX = "CK:"              # timeline markers owned by the shot list

# EEVEE Next is 'BLENDER_EEVEE_NEXT' in 4.2-4.x; Blender 5 renamed it back
# to 'BLENDER_EEVEE'. Compare against EEVEE_IDS, never a single id.
EEVEE = "BLENDER_EEVEE_NEXT"
EEVEE_IDS = ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE")
CYCLES = "CYCLES"

RIG_DOLLY = "DOLLY"
RIG_CRANE = "CRANE"
RIG_HANDHELD = "HANDHELD"
RIG_ORBIT = "ORBIT"
RIG_TYPES = (RIG_DOLLY, RIG_CRANE, RIG_HANDHELD, RIG_ORBIT)
