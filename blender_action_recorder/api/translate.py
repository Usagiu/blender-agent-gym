"""Translate recorded steps (state deltas) into low-level Action API calls,
executing and verifying them against the recording.

Per changed F-Curve two forms are tried:
  compact   only what a person would specify: values (6 decimals), changed
            interpolation/handle types, explicit handles only for non-auto
            handles; automatic handles are left to Blender's recalculation.
  explicit  every changed key with exact handles, no recalculation.
The compact form is kept when it reproduces the recorded curve within
tolerance; otherwise the explicit form is used (always exact).
"""

import bpy
import numpy as np

from .. import diff as df
from .. import snapshot as snap
from . import core
from .registry import execute

ENUM_FIELDS = ("interp", "hlt", "hrt", "easing", "type")
FLOAT_FIELDS = ("amp", "back", "period")


def _fmt(x, nd=6):
    x = float(x)
    return int(x) if x.is_integer() else round(x, nd)


def _is_auto(row):
    return row["hlt"] in core.AUTO_HANDLES and row["hrt"] in core.AUTO_HANDLES


def _non_default(row):
    out = {f: row[f] for f in ENUM_FIELDS if row[f] != core.KEY_DEFAULTS[f]}
    for f in FLOAT_FIELDS:
        if np.float32(row[f]) != np.float32(core.KEY_DEFAULTS[f]):
            out[f] = _fmt(row[f])
    return out


def _full(row):
    return {"frame": row["frame"], "value": row["value"], "hl": list(row["hl"]), "hr": list(row["hr"]),
            **{f: row[f] for f in ENUM_FIELDS}, **{f: row[f] for f in FLOAT_FIELDS}}


def fcurve_calls(change, mode="compact"):
    addr = core.address_for(change["action"], change["data_path"], change["index"])
    before, after = change["before"], change["after"]
    if after is None:
        return [{"name": "delete_channel", "args": addr}]
    calls = []
    if before is not None and before["meta"] != after["meta"]:
        props = {k: after["meta"][k] for k in ("extrapolation", "mute", "lock")
                 if before["meta"][k] != after["meta"][k]}
        if props:
            calls.append({"name": "set_channel_props", "args": {**addr, **props}})
    b_by = {core._fkey(r["frame"]): r for r in (core.decode(before) if before else [])}
    a_by = {core._fkey(r["frame"]): r for r in core.decode(after)}
    removed = sorted(set(b_by) - set(a_by))
    added = sorted(set(a_by) - set(b_by))
    common = sorted(set(a_by) & set(b_by))
    new_channel = {"group": after["meta"]["group"]} if before is None else {}

    if mode == "explicit":
        if removed:
            calls.append({"name": "delete_keys", "args": {**addr, "frames": removed, "recalc_handles": False,
                                                          "remove_empty_channel": False}})
        keys = [_full(a_by[f]) for f in added] + [_full(a_by[f]) for f in common if a_by[f] != b_by[f]]
        if keys or before is None:
            calls.append({"name": "set_keys", "args": {**addr, "keys": keys, "recalc_handles": False,
                                                       **new_channel}})
        return calls

    keys, groups = [], {}
    for f in added:
        r = a_by[f]
        k = {"frame": _fmt(r["frame"]), "value": _fmt(r["value"]), **_non_default(r)}
        if not _is_auto(r):
            k.update(hl=[_fmt(v) for v in r["hl"]], hr=[_fmt(v) for v in r["hr"]])
        keys.append(k)
    for f in common:
        ra, rb = a_by[f], b_by[f]
        if ra == rb:
            continue
        changed = {fld: (ra[fld] if fld in ENUM_FIELDS else _fmt(ra[fld]))
                   for fld in ENUM_FIELDS + FLOAT_FIELDS if ra[fld] != rb[fld]}
        value_changed = ra["value"] != rb["value"]
        handles_changed = ra["hl"] != rb["hl"] or ra["hr"] != rb["hr"]
        if value_changed or (handles_changed and not _is_auto(ra)):
            k = {"frame": _fmt(ra["frame"]), "value": _fmt(ra["value"]), **changed}
            if not _is_auto(ra):
                k.update(hl=[_fmt(v) for v in ra["hl"]], hr=[_fmt(v) for v in ra["hr"]])
            keys.append(k)
        elif changed:
            groups.setdefault(tuple(sorted(changed.items())), []).append(_fmt(ra["frame"]))
        # handle-only changes on auto handles come from the final recalculation
    if removed:
        calls.append({"name": "delete_keys", "args": {**addr, "frames": [_fmt(f) for f in removed],
                                                      "recalc_handles": False, "remove_empty_channel": False}})
    for items, frames in groups.items():
        calls.append({"name": "set_key_props", "args": {**addr, "frames": frames, **dict(items),
                                                        "recalc_handles": False}})
    if keys or before is None:
        calls.append({"name": "set_keys", "args": {**addr, "keys": keys, **new_channel}})
    edit_calls = [c for c in calls if c["name"] in ("delete_keys", "set_key_props", "set_keys")]
    if edit_calls:
        edit_calls[-1]["args"]["recalc_handles"] = True   # one recalculation at the end, like Blender
        if edit_calls[-1]["name"] == "set_keys":
            del edit_calls[-1]["args"]["recalc_handles"]  # default
    return calls


def _fc_matches(change, atol):
    action = bpy.data.actions.get(change["action"])
    fc = action.fcurves.find(change["data_path"], index=change["index"]) if action else None
    exp = change["after"]
    if exp is None or fc is None:
        return (exp is None) == (fc is None)
    got = snap.FCurveState.read(fc)
    want = snap.FCurveState.from_json(exp)
    if len(got.arrays["interpolation"]) != len(want.arrays["interpolation"]):
        return False
    for name, arr in want.arrays.items():
        if arr.dtype.kind == "f":
            if not np.allclose(arr, got.arrays[name], atol=atol, rtol=0):
                return False
        elif not np.array_equal(arr, got.arrays[name]):
            return False
    return {k: v for k, v in got.meta.items()} == want.meta


def _run(calls):
    for c in calls:
        r = execute(c)
        if not r["ok"]:
            return r["error"]
    return None


def _pose_calls(delta, before):
    calls = []
    for oname, bones in delta["working"]["poses"].items():
        ps = before.poses.get(oname)
        index = {n: i for i, n in enumerate(ps.names)} if ps else {}
        for bname, vals in bones.items():
            prev = ps.bone_json(index[bname]) if bname in index else {}
            if prev.get("rotation_mode") != vals["rotation_mode"]:
                mode = bpy.types.PoseBone.bl_rna.properties["rotation_mode"].enum_items
                ident = next(i.identifier for i in mode if i.value == vals["rotation_mode"])
                calls.append({"name": "set_pose", "args": {"object": oname, "bone": bname,
                                                           "channel": "rotation_mode", "value": ident}})
            for ch in core.POSE_CHANNELS:
                if prev.get(ch) != vals[ch]:
                    calls.append({"name": "set_pose", "args": {"object": oname, "bone": bname, "channel": ch,
                                                               "value": [_fmt(v) for v in vals[ch]]}})
            for k, v in vals.get("props", {}).items():
                if prev.get("props", {}).get(k) != v:
                    calls.append({"name": "set_pose", "args": {"object": oname, "bone": bname,
                                                               "channel": f'["{k}"]', "value": v}})
    for oname, vals in delta["working"]["objects"].items():
        prev = before.obj_working.get(oname, {})
        for ch in ("location", "rotation_euler", "rotation_quaternion", "scale"):
            if prev.get(ch) != vals[ch]:
                calls.append({"name": "set_pose", "args": {"object": oname, "channel": ch,
                                                           "value": [_fmt(v) for v in vals[ch]]}})
    return calls


def translate_step(rec, delta, atol=1e-4):
    """Translate + execute + verify one recorded step on the live scene, which
    must be in the step's pre-state.  Leaves the scene in the recorded
    post-state.  Returns a dict with calls and verification info."""
    before = snap.take_snapshot()
    calls, notes = [], []
    compact_n = explicit_n = 0

    if delta["scene"] is not None:
        a, b = delta["scene"]["after"], delta["scene"]["before"]
        if (a["frame_start"], a["frame_end"]) != (b["frame_start"], b["frame_end"]):
            calls.append({"name": "set_frame_range", "args": {"start": a["frame_start"], "end": a["frame_end"]}})
        if (a["frame_preview_start"], a["frame_preview_end"]) != (b["frame_preview_start"], b["frame_preview_end"]):
            calls.append({"name": "set_frame_range", "args": {"start": a["frame_preview_start"],
                                                              "end": a["frame_preview_end"], "preview": True}})
    for oname, ch in delta["objects"].items():
        if ch["after"] is not None and (ch["before"] or {}).get("action") != ch["after"]["action"]:
            calls.append({"name": "assign_action", "args": {"object": oname, "action": ch["after"]["action"]}})
    err = _run(calls)
    if err:
        notes.append(err)

    for change in delta["fcurves"]:
        compact = fcurve_calls(change, "compact")
        err = _run(compact)
        if err is None and _fc_matches(change, atol):
            calls += compact
            compact_n += 1
            continue
        snap.write_fcurve(change["key"], before.fcurves.get(change["key"]))   # undo the attempt
        explicit = fcurve_calls(change, "explicit")
        err = _run(explicit)
        if err is not None or not _fc_matches(change, atol):
            notes.append(f"{change['key']}: explicit form failed ({err or 'mismatch'})")
        calls += explicit
        explicit_n += 1

    if rec.get("frame_changed"):
        # the person moved to another frame within this step; the pose there is
        # re-evaluated from the curves, so the frame change is the action
        fc_call = {"name": "set_frame", "args": {"frame": rec["ctx_after"]["frame"]}}
        err = _run([fc_call])
        if err:
            notes.append(err)
        calls.append(fc_call)

    pose_calls = []
    if not delta["fcurves"] and not rec.get("frame_changed"):
        pose_calls = _pose_calls(delta, before)
        err = _run(pose_calls)
        if err:
            notes.append(err)
    calls += pose_calls

    bpy.context.view_layer.update()
    actual = snap.take_snapshot()
    problems = df.compare_to_expected(before, actual, delta, atol=atol)
    # resync to the exact recorded state so later steps start from truth
    df.restore_unexpected(before, actual, delta)
    df.apply_delta(delta, frame_after=rec["ctx_after"]["frame"])
    return {"calls": calls, "verified": not problems and not notes, "problems": problems[:10],
            "notes": notes, "compact_channels": compact_n, "explicit_channels": explicit_n}
