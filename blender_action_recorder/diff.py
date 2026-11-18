"""Diff two snapshots, apply a delta, and compare states with tolerance."""

import bpy
import numpy as np

from . import snapshot as snap


def _round_frames(arr):
    return {round(float(f), 3) for f in arr}


def _keyframe_change(before, after):
    """Human/ML-friendly summary of what happened to one F-Curve."""
    if before is None:
        return {"kind": "created", "added": sorted(_round_frames(after.frames))}
    if after is None:
        return {"kind": "removed", "removed": sorted(_round_frames(before.frames))}
    fb, fa = _round_frames(before.frames), _round_frames(after.frames)
    common = fb & fa
    modified = []
    if common:
        ib = {round(float(f), 3): i for i, f in enumerate(before.frames)}
        ia = {round(float(f), 3): i for i, f in enumerate(after.frames)}
        for f in sorted(common):
            i, j = ib[f], ia[f]
            same = all(
                np.array_equal(before.arrays[n].reshape(len(before.frames), -1)[i],
                               after.arrays[n].reshape(len(after.frames), -1)[j])
                for n in before.arrays)
            if not same:
                modified.append(f)
    out = {"kind": "edited", "added": sorted(fa - fb), "removed": sorted(fb - fa), "modified": modified}
    if before.meta != after.meta:
        out["meta"] = {"before": before.meta, "after": after.meta}
    return out


def diff(prev, cur):
    """Return a JSON-serialisable delta from snapshot *prev* to *cur*."""
    fcurves = []
    for key in sorted(set(prev.fcurves) | set(cur.fcurves)):
        b, a = prev.fcurves.get(key), cur.fcurves.get(key)
        if b is not None and a is not None and b.digest == a.digest:
            continue
        action, slot, data_path, index = snap.split_fc_key(key)
        bone, channel = snap.bone_of(data_path)
        fcurves.append({
            "key": key, "action": action, "slot": slot, "data_path": data_path, "index": index,
            "bone": bone, "channel": channel,
            "change": _keyframe_change(b, a),
            "before": b.to_json() if b is not None else None,
            "after": a.to_json() if a is not None else None,
        })

    objects = {}
    for name in sorted(set(prev.obj_authored) | set(cur.obj_authored)):
        b, a = prev.obj_authored.get(name), cur.obj_authored.get(name)
        if b != a:
            objects[name] = {"before": b, "after": a}

    scene = None
    if prev.scene != cur.scene:
        scene = {"before": prev.scene, "after": cur.scene}

    w_objects = {}
    for name, vals in cur.obj_working.items():
        if prev.obj_working.get(name) != vals:
            w_objects[name] = vals
    poses = {}
    for name, ps in cur.poses.items():
        pp = prev.poses.get(name)
        if pp is not None and pp.digest == ps.digest:
            continue
        changed = {}
        if pp is None or pp.names != ps.names:
            idx = range(len(ps.names))
        else:
            mask = np.zeros(len(ps.names), dtype=bool)
            for arr_name in ps.arrays:
                d = ps.arrays[arr_name] != pp.arrays[arr_name]
                mask |= d.any(axis=1) if d.ndim == 2 else d
            for i, bname in enumerate(ps.names):
                if ps.props.get(bname) != pp.props.get(bname):
                    mask[i] = True
            idx = np.nonzero(mask)[0]
        for i in idx:
            changed[ps.names[i]] = ps.bone_json(int(i))
        if changed:
            poses[name] = changed

    return {
        "fcurves": fcurves,
        "objects": objects,
        "scene": scene,
        "working": {"objects": w_objects, "poses": poses},
    }


def authored_changed(delta):
    return bool(delta["fcurves"] or delta["objects"] or delta["scene"])


def working_changed(delta):
    w = delta["working"]
    return bool(w["objects"] or w["poses"])


def summarize(delta):
    """Compact per-step summary stored inline in steps.jsonl."""
    bones = {}
    for fc in delta["fcurves"]:
        key = fc["bone"] or fc["data_path"]
        ch = fc["change"]
        b = bones.setdefault(key, {"channels": set(), "added": 0, "removed": 0, "modified": 0, "frames": set()})
        b["channels"].add(f'{fc["channel"]}[{fc["index"]}]')
        for k in ("added", "removed", "modified"):
            b[k] += len(ch.get(k, ()))
            b["frames"].update(ch.get(k, ()))
    out = {}
    for k, b in bones.items():
        fr = sorted(b["frames"])
        out[k] = {"channels": sorted(b["channels"]), "added": b["added"], "removed": b["removed"],
                  "modified": b["modified"], "frame_range": [fr[0], fr[-1]] if fr else None}
    return {
        "fcurves_changed": len(delta["fcurves"]),
        "by_bone": out,
        "objects_changed": sorted(delta["objects"]),
        "scene_changed": delta["scene"] is not None,
        "pose_bones_changed": {o: sorted(b) for o, b in delta["working"]["poses"].items()},
    }


# ---------------------------------------------------------------- apply

def apply_authored(delta):
    for fc in delta["fcurves"]:
        snap.write_fcurve(fc["key"], fc["after"])
    from . import compat
    for name, ch in delta["objects"].items():
        obj = bpy.data.objects.get(name)
        if obj is not None and ch["after"] is not None:
            compat.assign_action(obj, ch["after"]["action"], ch["after"]["slot"])
    if delta["scene"] is not None:
        scene = bpy.context.scene
        for k, v in delta["scene"]["after"].items():
            if k in ("fps", "fps_base"):
                setattr(scene.render, k, v)
            else:
                setattr(scene, k, v)


def apply_working(delta):
    for name, vals in delta["working"]["objects"].items():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            snap.write_object_working(obj, vals)
    for name, bones in delta["working"]["poses"].items():
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == 'ARMATURE':
            snap.write_pose_bones(obj, bones)


def apply_delta(delta, frame_after=None):
    """State replay of one step: authored data, then frame, then working pose."""
    apply_authored(delta)
    scene = bpy.context.scene
    if frame_after is not None and scene.frame_current != frame_after:
        scene.frame_set(frame_after)
    apply_working(delta)


def restore_unexpected(before, actual, delta):
    """After an operator replay, revert anything it touched that the recording
    did not (so later steps start from the recorded state)."""
    expected_keys = {fc["key"] for fc in delta["fcurves"]}
    for key in set(before.fcurves) | set(actual.fcurves):
        if key in expected_keys:
            continue
        b, a = before.fcurves.get(key), actual.fcurves.get(key)
        if (b is None) != (a is None) or (b is not None and b.digest != a.digest):
            snap.write_fcurve(key, b)
    exp_pose = delta["working"]["poses"]
    for name, pb_after in actual.poses.items():
        pb_before = before.poses.get(name)
        obj = bpy.data.objects.get(name)
        if pb_before is None or obj is None or pb_before.names != pb_after.names:
            continue
        revert = {}
        for i, bname in enumerate(pb_before.names):
            if bname in exp_pose.get(name, {}):
                continue
            if pb_before.bone_json(i) != pb_after.bone_json(i):
                revert[bname] = pb_before.bone_json(i)
        if revert:
            snap.write_pose_bones(obj, revert)


# ---------------------------------------------------------------- compare

def compare_to_expected(before, actual, delta, atol=1e-4):
    """Compare an operator-replayed state against the recorded delta.

    Returns a list of human-readable mismatches (empty = match)."""
    problems = []
    expected = {fc["key"]: fc["after"] for fc in delta["fcurves"]}
    keys = set(expected)
    for key in set(before.fcurves) | set(actual.fcurves):
        b, a = before.fcurves.get(key), actual.fcurves.get(key)
        if (b is None) != (a is None) or (b is not None and b.digest != a.digest):
            keys.add(key)
    for key in sorted(keys):
        a = actual.fcurves.get(key)
        if key in expected:
            exp = snap.FCurveState.from_json(expected[key]) if expected[key] is not None else None
        else:
            exp = before.fcurves.get(key)
        if exp is None or a is None:
            if (exp is None) != (a is None):
                problems.append(f"{key}: expected {'absent' if exp is None else 'present'}, "
                                f"got {'absent' if a is None else 'present'}")
            continue
        if len(exp.arrays["interpolation"]) != len(a.arrays["interpolation"]):
            problems.append(f"{key}: keyframe count {len(a.arrays['interpolation'])} "
                            f"!= expected {len(exp.arrays['interpolation'])}")
            continue
        for name, arr in exp.arrays.items():
            got = a.arrays[name]
            if arr.dtype.kind == "f":
                if not np.allclose(arr, got, atol=atol, rtol=0):
                    err = float(np.max(np.abs(arr - got)))
                    problems.append(f"{key}: {name} max err {err:.3g}")
            elif not np.array_equal(arr, got):
                problems.append(f"{key}: {name} differs")
    for name, bones in delta["working"]["poses"].items():
        ps = actual.poses.get(name)
        if ps is None:
            continue
        index = {n: i for i, n in enumerate(ps.names)}
        for bname, vals in bones.items():
            i = index.get(bname)
            if i is None:
                continue
            got = ps.bone_json(i)
            for k, v in vals.items():
                if k == "props":
                    if got.get("props") != v:
                        problems.append(f"{name}:{bname}: custom props differ")
                elif k == "rotation_mode":
                    if got[k] != v:
                        problems.append(f"{name}:{bname}: rotation_mode differs")
                elif not np.allclose(v, got[k], atol=atol, rtol=0):
                    problems.append(f"{name}:{bname}: {k} differs")
    return problems
