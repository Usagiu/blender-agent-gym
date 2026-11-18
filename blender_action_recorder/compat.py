"""Blender API helpers.  Target: Blender 4.3 only (legacy ``action.fcurves`` API;
4.4+ slotted/layered Actions are intentionally not supported).

The ``slot_id`` arguments kept throughout are always "" on 4.3; they stay in the
F-Curve key format so episodes keep a stable schema.
"""

import bpy


def iter_fcurves(action):
    """Yield (slot_identifier, fcurve) for every F-Curve of an action."""
    for fc in action.fcurves:
        yield "", fc


def find_fcurve(action, slot_id, data_path, index):
    return action.fcurves.find(data_path, index=index)


def ensure_fcurve(action, slot_id, data_path, index, group_name=""):
    fc = action.fcurves.find(data_path, index=index)
    if fc is None:
        fc = action.fcurves.new(data_path, index=index, action_group=group_name or "")
    elif group_name and (fc.group is None or fc.group.name != group_name):
        fc.group = action.groups.get(group_name) or action.groups.new(group_name)
    return fc


def remove_fcurve(action, slot_id, fc):
    action.fcurves.remove(fc)


def clear_keyframes(fc):
    fc.keyframe_points.clear()


def action_slot_identifier(anim_data):
    return ""


def assign_action(id_block, action_name, slot_id=""):
    if action_name is None:
        if id_block.animation_data:
            id_block.animation_data.action = None
        return
    ad = id_block.animation_data or id_block.animation_data_create()
    ad.action = bpy.data.actions.get(action_name) or bpy.data.actions.new(action_name)


def find_area(window, editor_type, create=True):
    """Return (area, region) of the given editor type, converting the largest
    area if none exists. Returns (None, None) without a window."""
    if window is None:
        return None, None
    areas = [a for a in window.screen.areas if a.type == editor_type]
    if not areas:
        if not create:
            return None, None
        area = max(window.screen.areas, key=lambda a: a.width * a.height)
        area.type = editor_type
        areas = [area]
    area = areas[0]
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    return area, region
