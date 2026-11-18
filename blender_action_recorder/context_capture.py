"""Capture and restore the editing context an operator depends on.

An operator like ``transform.rotate`` or ``graph.smooth`` only reproduces when
selection, active object/bone, mode, frame, keyframe selection, pivot and
orientation match.  We store exactly that, compactly.
"""

import bpy
import numpy as np

from . import compat
from . import snapshot as snap


def _ranges(mask):
    """bool array -> [[start, end_inclusive], ...]"""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]]))
    return [[int(s), int(e)] for s, e in zip(starts, ends)]


def _mask_from_ranges(ranges, n):
    m = np.zeros(n, dtype=bool)
    for s, e in ranges:
        m[s:e + 1] = True
    return m


def _editor_filters(space):
    ds = getattr(space, "dopesheet", None)
    out = {}
    if ds is not None:
        for k in ("show_only_selected", "show_hidden", "show_only_errors", "filter_text"):
            if hasattr(ds, k):
                out[k] = getattr(ds, k)
    for k in ("mode", "ui_mode", "pivot_point", "use_normalization"):
        if hasattr(space, k):
            out[k] = getattr(space, k)
    return out


def _view3d_state(area):
    if area is None or area.type != 'VIEW_3D':
        return None
    r3d = area.spaces.active.region_3d
    return {
        "view_matrix": [list(row) for row in r3d.view_matrix],
        "view_perspective": r3d.view_perspective,
        "is_perspective": r3d.is_perspective,
    }


def capture_context(snapshot=None, area=None):
    """*snapshot*: reuse an existing Snapshot for selection data (cheap).
    *area*: the editor the user last interacted with (from the event listener)."""
    scene = bpy.context.scene
    vl = bpy.context.view_layer
    active = vl.objects.active
    ts = scene.tool_settings
    ctx = {
        "frame": scene.frame_current,
        "mode": active.mode if active else "OBJECT",
        "active_object": active.name if active else None,
        "selected_objects": sorted(o.name for o in vl.objects if o.select_get()),
        "tool": {
            "pivot": ts.transform_pivot_point,
            "orientation": scene.transform_orientation_slots[0].type,
            "auto_key": ts.use_keyframe_insert_auto,
            "keying_set": scene.keying_sets_all.active.bl_label if scene.keying_sets_all.active else None,
            "proportional": ts.use_proportional_edit,
            "use_transform_data_origin": ts.use_transform_data_origin,
        },
    }
    if snapshot is not None:
        ctx["bones"] = {o: {"selected": p.bone_sel, "active": p.active_bone} for o, p in snapshot.poses.items()}
        sel_keys = {}
        for key, fs in snapshot.fcurves.items():
            if fs.fsel or fs.sel.any() or fs.sel_l.any() or fs.sel_r.any():
                entry = {"fcurve": fs.fsel, "keys": _ranges(fs.sel)}
                # handles are stored only when their selection differs from the key's
                if not np.array_equal(fs.sel_l, fs.sel):
                    entry["left"] = _ranges(fs.sel_l)
                if not np.array_equal(fs.sel_r, fs.sel):
                    entry["right"] = _ranges(fs.sel_r)
                sel_keys[key] = entry
        ctx["selected_keys"] = sel_keys
    if area is not None:
        try:
            ctx["editor"] = {"type": area.type, "ui_type": area.ui_type,
                             "filters": _editor_filters(area.spaces.active)}
            v3d = _view3d_state(area)
            if v3d:
                ctx["view3d"] = v3d
        except ReferenceError:
            pass
    return ctx


def restore_context(ctx, override_area=None):
    """Make the current Blender state match a recorded context (selection,
    mode, frame, tool settings).  Pose/keyframe *data* is not touched here."""
    scene = bpy.context.scene
    vl = bpy.context.view_layer

    active = bpy.data.objects.get(ctx["active_object"]) if ctx.get("active_object") else None
    # leave edit/pose modes before changing the active object
    cur = vl.objects.active
    if cur is not None and cur.mode != 'OBJECT' and (active is None or cur != active):
        bpy.ops.object.mode_set(mode='OBJECT')
    sel = set(ctx.get("selected_objects", ()))
    for o in vl.objects:
        want = o.name in sel
        if o.select_get() != want:
            o.select_set(want)
    if active is not None:
        vl.objects.active = active
        mode = ctx.get("mode", "OBJECT")
        if active.mode != mode:
            try:
                bpy.ops.object.mode_set(mode=mode)
            except RuntimeError:
                pass

    if scene.frame_current != ctx["frame"]:
        scene.frame_set(ctx["frame"])

    for oname, b in ctx.get("bones", {}).items():
        obj = bpy.data.objects.get(oname)
        if obj is None or obj.type != 'ARMATURE':
            continue
        bones = obj.data.bones
        selected = set(b["selected"])
        mask = np.array([bb.name in selected for bb in bones], dtype=bool)
        bones.foreach_set("select", mask)
        bones.foreach_set("select_head", mask)
        bones.foreach_set("select_tail", mask)
        bones.active = bones.get(b["active"]) if b["active"] else None

    sel_keys = ctx.get("selected_keys")
    if sel_keys is not None:
        for action in bpy.data.actions:
            for slot_id, fc in compat.iter_fcurves(action):
                key = snap.make_fc_key(action.name, slot_id, fc.data_path, fc.array_index)
                n = len(fc.keyframe_points)
                info = sel_keys.get(key)
                mask = _mask_from_ranges(info["keys"], n) if info else np.zeros(n, dtype=bool)
                left = _mask_from_ranges(info["left"], n) if info and "left" in info else mask
                right = _mask_from_ranges(info["right"], n) if info and "right" in info else mask
                fc.keyframe_points.foreach_set("select_control_point", mask)
                fc.keyframe_points.foreach_set("select_left_handle", left)
                fc.keyframe_points.foreach_set("select_right_handle", right)
                fc.select = bool(info and info["fcurve"])

    tool = ctx.get("tool", {})
    ts = scene.tool_settings
    if "pivot" in tool:
        ts.transform_pivot_point = tool["pivot"]
    if "orientation" in tool:
        try:
            scene.transform_orientation_slots[0].type = tool["orientation"]
        except TypeError:
            pass
    if "auto_key" in tool:
        ts.use_keyframe_insert_auto = tool["auto_key"]
    if "proportional" in tool:
        ts.use_proportional_edit = tool["proportional"]

    if override_area is not None and "editor" in ctx:
        space = override_area.spaces.active
        filters = ctx["editor"].get("filters", {})
        ds = getattr(space, "dopesheet", None)
        for k, v in filters.items():
            target = ds if ds is not None and hasattr(ds, k) else space
            if hasattr(target, k):
                try:
                    setattr(target, k, v)
                except (TypeError, AttributeError):
                    pass
    if override_area is not None and override_area.type == 'VIEW_3D' and ctx.get("view3d"):
        from mathutils import Matrix
        r3d = override_area.spaces.active.region_3d
        r3d.view_perspective = ctx["view3d"]["view_perspective"]
        r3d.view_matrix = Matrix(ctx["view3d"]["view_matrix"])
