"""Observation actions: read the animation without changing it.

Outputs are compact JSON (rounded numbers, frame ranges instead of long lists)
so they fit in a model's context.  Anything that has to evaluate the rig at
other frames (world positions, detectors) restores the current frame and the
unkeyed pose afterwards.
"""

import re
from contextlib import contextmanager

import bpy
import numpy as np

from .. import snapshot as snap
from .core import decode, split_data_path
from .registry import ApiError, P_FRAME_RANGE, P_OBJECT, action

ROUND = 4


def _r(x, n=ROUND):
    if isinstance(x, (list, tuple, np.ndarray)):
        return [_r(v, n) for v in x]
    x = float(x)
    return int(x) if x.is_integer() else round(x, n)


def _obj(name, armature=False):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ApiError(f"no object named {name!r}")
    if armature and obj.type != 'ARMATURE':
        raise ApiError(f"{name!r} is not an armature")
    return obj


def _action(obj):
    ad = obj.animation_data
    if ad is None or ad.action is None:
        raise ApiError(f"{obj.name!r} has no action")
    return ad.action


def _frame_range(frame_range):
    sc = bpy.context.scene
    if frame_range is None:
        if sc.use_preview_range:
            return sc.frame_preview_start, sc.frame_preview_end
        return sc.frame_start, sc.frame_end
    return int(frame_range[0]), int(frame_range[1])


def _channels(obj, bones=None, channels=None):
    """{(bone, channel): {index: fcurve}} for the object's action."""
    out = {}
    for fc in _action(obj).fcurves:
        bone, ch = split_data_path(fc.data_path)
        if bones is not None and bone not in bones:
            continue
        if channels is not None and ch not in channels:
            continue
        out.setdefault((bone, ch), {})[fc.array_index] = fc
    return out


@contextmanager
def preserve_state():
    """Restore frame, unkeyed pose and object transforms after evaluating other frames."""
    sc = bpy.context.scene
    f0 = sc.frame_current
    saved = snap.take_snapshot()
    try:
        yield
    finally:
        if sc.frame_current != f0:
            sc.frame_set(f0)
        for name, ps in saved.poses.items():
            obj = bpy.data.objects.get(name)
            if obj is not None:
                snap.write_pose_bones(obj, {ps.names[i]: ps.bone_json(i) for i in range(len(ps.names))})
        for name, vals in saved.obj_working.items():
            obj = bpy.data.objects.get(name)
            if obj is not None:
                snap.write_object_working(obj, vals)


def _segments(mask, frames, gap=1, min_len=1):
    """Runs of True (merging gaps <= gap) -> [(start_idx, end_idx_inclusive)]."""
    idx = np.flatnonzero(mask)
    segs = []
    for i in idx:
        if segs and i - segs[-1][1] <= gap + 1:
            segs[-1][1] = i
        else:
            segs.append([i, i])
    return [(int(s), int(e)) for s, e in segs if e - s + 1 >= min_len]


# ---------------------------------------------------------------- simple reads

@action("observe", {})
def list_objects():
    """Objects in the scene with their type, assigned action and key counts."""
    out = []
    for o in bpy.context.scene.objects:
        ad = o.animation_data
        act = ad.action if ad else None
        item = {"name": o.name, "type": o.type}
        if act:
            item["action"] = act.name
            item["channels"] = len(act.fcurves)
            item["keys"] = sum(len(fc.keyframe_points) for fc in act.fcurves)
        if o.type == 'ARMATURE':
            item["bones"] = len(o.pose.bones)
        out.append(item)
    sc = bpy.context.scene
    return {"objects": out, "frame": sc.frame_current, "frame_range": [sc.frame_start, sc.frame_end],
            "fps": sc.render.fps}


@action("observe", {"object": P_OBJECT,
                    "bones": {"type": "array", "items": {"type": "string"}}},
        required=("object",))
def list_channels(object, bones=None):
    """Animated channels grouped by bone: indices, key count, frame range, interpolations."""
    obj = _obj(object)
    out = {}
    for (bone, ch), fcs in sorted(_channels(obj, bones).items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        frames = [kp.co[0] for fc in fcs.values() for kp in fc.keyframe_points]
        interps = sorted({kp.interpolation for fc in fcs.values() for kp in fc.keyframe_points})
        out.setdefault(bone or "<object>", {})[ch] = {
            "indices": sorted(fcs), "keys": len(frames),
            "frame_range": [_r(min(frames)), _r(max(frames))] if frames else None,
            "interp": interps,
        }
    return {"object": object, "channels": out}


@action("observe", {"object": P_OBJECT, "bone": {"type": "string"}, "channel": {"type": "string"},
                    "index": {"type": "integer", "description": "omit for all components"},
                    "frame_range": P_FRAME_RANGE,
                    "with_handles": {"type": "boolean", "default": False},
                    "max_keys": {"type": "integer", "default": 200}},
        required=("object", "channel"))
def get_keys(object, channel, bone=None, index=None, frame_range=None, with_handles=False, max_keys=200):
    """Keyframes of one channel: frame, value, interpolation (+ handles and types if asked)."""
    obj = _obj(object)
    chans = _channels(obj, [bone] if bone else None, [channel])
    fcs = chans.get((bone, channel))
    if not fcs:
        raise ApiError(f"no animation on {bone or object}.{channel}")
    lo, hi = frame_range if frame_range else (-1e9, 1e9)
    out = {}
    for i, fc in sorted(fcs.items()):
        if index is not None and i != index:
            continue
        rows = []
        for r in decode(snap.FCurveState.read(fc).to_json()):
            if not lo <= r["frame"] <= hi:
                continue
            row = {"f": _r(r["frame"]), "v": _r(r["value"], 5), "interp": r["interp"]}
            if r["type"] != "KEYFRAME":
                row["type"] = r["type"]
            if with_handles:
                row.update(hl=_r(r["hl"], 5), hr=_r(r["hr"], 5), hlt=r["hlt"], hrt=r["hrt"])
            rows.append(row)
        out[i] = {"keys": rows[:max_keys], "truncated": len(rows) > max_keys}
    return {"bone": bone, "channel": channel, "components": out}


@action("observe", {"object": P_OBJECT, "bones": {"type": "array", "items": {"type": "string"}},
                    "channels": {"type": "array", "items": {"type": "string"}},
                    "frame_range": P_FRAME_RANGE, "step": {"type": "integer", "default": 1}},
        required=("object",))
def sample_curves(object, bones=None, channels=None, frame_range=None, step=1):
    """Evaluated channel values per frame (local space, from the curves; no rig evaluation)."""
    obj = _obj(object)
    lo, hi = _frame_range(frame_range)
    frames = list(range(lo, hi + 1, max(1, step)))
    out = {}
    for (bone, ch), fcs in sorted(_channels(obj, bones, channels).items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        out.setdefault(bone or "<object>", {})[ch] = {
            i: [_r(fc.evaluate(f)) for f in frames] for i, fc in sorted(fcs.items())}
    return {"frames": [frames[0], frames[-1], max(1, step)], "values": out}


@action("observe", {"object": P_OBJECT, "bones": {"type": "array", "items": {"type": "string"}}},
        required=("object",))
def get_pose(object, bones=None):
    """Current pose values (including unkeyed edits) of bones."""
    obj = _obj(object, armature=True)
    ps = snap.PoseState.read(obj)
    out = {}
    for i, name in enumerate(ps.names):
        if bones is not None and name not in bones:
            continue
        pb = obj.pose.bones[name]
        b = ps.bone_json(i)
        rot = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}.get(
            pb.rotation_mode, "rotation_euler")
        item = {"location": _r(b["location"]), rot: _r(b[rot]), "scale": _r(b["scale"]),
                "rotation_mode": pb.rotation_mode}
        if "props" in b:
            item["props"] = b["props"]
        out[name] = item
    return {"frame": bpy.context.scene.frame_current, "bones": out}


# ---------------------------------------------------------------- world-space sampling

def sample_world_positions(obj, bones, frames):
    """{bone: (heads[F,3], tails[F,3])} in world space, evaluating the rig per frame."""
    sc = bpy.context.scene
    heads = {b: np.zeros((len(frames), 3)) for b in bones}
    tails = {b: np.zeros((len(frames), 3)) for b in bones}
    with preserve_state():
        for j, f in enumerate(frames):
            sc.frame_set(f)
            dg = bpy.context.evaluated_depsgraph_get()
            oe = obj.evaluated_get(dg)
            mw = oe.matrix_world
            for b in bones:
                pb = oe.pose.bones[b]
                heads[b][j] = mw @ pb.head
                tails[b][j] = mw @ pb.tail
    return {b: (heads[b], tails[b]) for b in bones}


@action("observe", {"object": P_OBJECT, "bones": {"type": "array", "items": {"type": "string"}},
                    "frame_range": P_FRAME_RANGE, "step": {"type": "integer", "default": 1},
                    "point": {"type": "string", "enum": ["head", "tail"], "default": "head"}},
        required=("object", "bones"))
def sample_world(object, bones, frame_range=None, step=1, point="head"):
    """World-space bone positions per frame (full rig evaluation: constraints, IK, parents)."""
    obj = _obj(object, armature=True)
    for b in bones:
        if b not in obj.pose.bones:
            raise ApiError(f"no bone {b!r}")
    lo, hi = _frame_range(frame_range)
    frames = list(range(lo, hi + 1, max(1, step)))
    pos = sample_world_positions(obj, bones, frames)
    k = 0 if point == "head" else 1
    return {"frames": [frames[0], frames[-1], max(1, step)], "point": point,
            "positions": {b: _r(pos[b][k], 4) for b in bones}}


# ---------------------------------------------------------------- detectors

FOOT_RE = re.compile(r"(foot|toe|ankle|heel)", re.I)
NOT_FOOT_RE = re.compile(r"(^mch|^org|pole|target|tweak|roll|spin|_ik$|ik_|\.ik|^vis)", re.I)


def guess_feet(obj):
    names = [b.name for b in obj.data.bones if FOOT_RE.search(b.name) and not NOT_FOOT_RE.search(b.name)]
    deform = [n for n in names if obj.data.bones[n].use_deform]
    return deform or names


def _detect_feet(pos, frames, feet, contact_height, slide_speed, ground_z):
    issues, metrics = [], {}
    for b in feet:
        h, t = pos[b]
        low_is_tail = t[:, 2] < h[:, 2]
        p = np.where(low_is_tail[:, None], t, h)           # the lower end of the bone touches the ground
        z = p[:, 2]
        ground = ground_z if ground_z is not None else float(z.min())
        contact = z <= ground + contact_height
        step = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1)
        both = contact[:-1] & contact[1:]
        sliding = both & (step > slide_speed)
        total = float(step[both].sum())
        metrics[b] = {"contact_frames": int(contact.sum()), "slide_distance": _r(total),
                      "ground": _r(ground)}
        for s, e in _segments(sliding, frames, gap=1):
            dist = float(step[s:e + 1].sum())
            issues.append({"type": "foot_sliding", "bone": b, "frames": [frames[s], frames[e + 1]],
                           "slide_distance": _r(dist), "max_speed": _r(step[s:e + 1].max()),
                           "severity": _r(step[s:e + 1].max() / slide_speed, 2)})
    return issues, metrics


def _detect_curves(obj, frames, bones, jitter_rot, jitter_loc, flip_threshold):
    issues = []
    metrics = {"jitter_frames": 0, "flips": 0}
    f_arr = np.asarray(frames, dtype=float)
    for (bone, ch), fcs in _channels(obj, bones).items():
        if ch not in ("location", "rotation_euler", "rotation_quaternion", "scale"):
            continue
        vals = {i: np.array([fc.evaluate(f) for f in f_arr]) for i, fc in fcs.items()}
        flip_mask = np.zeros(len(frames) - 1, dtype=bool)
        if ch == "rotation_euler":
            for i, v in vals.items():
                jump = np.abs(np.diff(v)) > flip_threshold
                flip_mask |= jump
        if ch == "rotation_quaternion" and len(vals) == 4:
            q = np.stack([vals[i] for i in range(4)], axis=1)
            flip_mask |= np.einsum("ij,ij->i", q[:-1], q[1:]) < 0
        for s, e in _segments(flip_mask, frames, gap=0):
            metrics["flips"] += 1
            issues.append({"type": "rotation_flip", "bone": bone, "channel": ch,
                           "frames": [frames[s], frames[e + 1]], "severity": 10.0})
        thr = jitter_rot if ch.startswith("rotation") else jitter_loc
        if len(frames) < 3:
            continue
        # jitter = zig-zag: consecutive accelerations of opposite sign.  Smooth
        # motion (even fast arcs) keeps the same sign over several frames.
        acc = np.zeros(len(frames) - 2)
        for v in vals.values():
            a = np.diff(v, n=2)
            zz = np.zeros_like(a)
            flip = a[:-1] * a[1:] < 0
            m = np.minimum(np.abs(a[:-1]), np.abs(a[1:])) * flip
            zz[:-1] = np.maximum(zz[:-1], m)
            zz[1:] = np.maximum(zz[1:], m)
            acc = np.maximum(acc, zz)
        bad = acc > thr
        # ignore accelerations caused by a flip
        near_flip = np.zeros_like(bad)
        for k in np.flatnonzero(flip_mask):
            near_flip[max(0, k - 1):k + 1] = True
        bad &= ~near_flip
        for s, e in _segments(bad, frames, gap=2, min_len=2):
            metrics["jitter_frames"] += e - s + 1
            issues.append({"type": "jitter", "bone": bone, "channel": ch,
                           "frames": [frames[s + 1], frames[e + 1]], "max_accel": _r(acc[s:e + 1].max()),
                           "severity": _r(acc[s:e + 1].max() / thr, 2)})
    return issues, metrics


def _detect_pops(pos, frames, pop_factor, pop_min, ground_z, ground_tol):
    issues = []
    for b, (h, t) in pos.items():
        speed = np.linalg.norm(np.diff(h, axis=0), axis=1)
        # a pop is a speed spike relative to the *neighbouring* frames (a global
        # median would flag every step of a mostly-planted foot)
        n = len(speed)
        local = np.zeros(n)
        for k in range(n):
            nb = np.concatenate((speed[max(0, k - 3):max(0, k - 1)], speed[k + 2:k + 4]))
            local[k] = np.median(nb) if len(nb) else 0.0
        spikes = (speed > pop_min) & (speed > pop_factor * np.maximum(local, pop_min / pop_factor))
        for s, e in _segments(spikes, frames, gap=0):
            ref = float(np.maximum(local[s:e + 1], pop_min / pop_factor).max())
            issues.append({"type": "pop", "bone": b, "frames": [frames[s], frames[e + 1]],
                           "max_speed": _r(speed[s:e + 1].max()), "neighbour_speed": _r(local[s:e + 1].max()),
                           "severity": _r(speed[s:e + 1].max() / ref / pop_factor, 2)})
        if ground_z is not None:
            below = np.minimum(h[:, 2], t[:, 2]) < ground_z - ground_tol
            for s, e in _segments(below, frames, gap=1):
                depth = float(ground_z - np.minimum(h[s:e + 1, 2], t[s:e + 1, 2]).min())
                issues.append({"type": "below_ground", "bone": b, "frames": [frames[s], frames[e]],
                               "depth": _r(depth), "severity": _r(depth / ground_tol, 2)})
    return issues


@action("observe", {
    "object": P_OBJECT, "frame_range": P_FRAME_RANGE,
    "checks": {"type": "array", "items": {"type": "string",
                                          "enum": ["foot_sliding", "jitter", "rotation_flip", "pop",
                                                   "below_ground"]},
               "description": "default: all that apply"},
    "bones": {"type": "array", "items": {"type": "string"},
              "description": "bones for jitter/flip/pop checks (default: all animated / all deform bones)"},
    "feet": {"type": "array", "items": {"type": "string"}, "description": "default: guessed from names"},
    "ground_z": {"type": "number", "description": "ground height; default: lowest foot point in range"},
    "contact_height": {"type": "number", "default": 0.03},
    "slide_speed": {"type": "number", "default": 0.004, "description": "units per frame while in contact"},
    "jitter_rot": {"type": "number", "default": 0.05, "description": "rad/frame^2"},
    "jitter_loc": {"type": "number", "default": 0.01, "description": "units/frame^2"},
    "flip_threshold": {"type": "number", "default": 2.5, "description": "rad jump between frames"},
    "pop_factor": {"type": "number", "default": 6.0},
    "pop_min": {"type": "number", "default": 0.05},
    "max_issues": {"type": "integer", "default": 30},
}, required=("object",))
def detect_issues(object, frame_range=None, checks=None, bones=None, feet=None, ground_z=None,
                  contact_height=0.03, slide_speed=0.004, jitter_rot=0.05, jitter_loc=0.01,
                  flip_threshold=2.5, pop_factor=6.0, pop_min=0.05, max_issues=30):
    """Find common motion problems: foot sliding, jitter, euler/quaternion flips, pops, ground penetration.
    Thresholds are in scene units; tune them for the character's scale."""
    obj = _obj(object, armature=True)
    lo, hi = _frame_range(frame_range)
    frames = list(range(lo, hi + 1))
    if len(frames) < 3:
        raise ApiError("frame range too short")
    checks = set(checks or ("foot_sliding", "jitter", "rotation_flip", "pop", "below_ground"))
    notes, issues, metrics = [], [], {}

    if checks & {"jitter", "rotation_flip"}:
        ci, cm = _detect_curves(obj, frames, bones, jitter_rot, jitter_loc, flip_threshold)
        issues += [i for i in ci if i["type"] in checks]
        metrics.update(cm)

    world_bones = set()
    if "foot_sliding" in checks:
        feet = feet or guess_feet(obj)
        if not feet:
            notes.append("foot_sliding skipped: no foot bones found, pass feet=[...]")
        world_bones |= set(feet or ())
    if checks & {"pop", "below_ground"}:
        pb = bones or [b.name for b in obj.data.bones if b.use_deform]
        world_bones |= set(pb)
    if world_bones:
        for b in world_bones:
            if b not in obj.pose.bones:
                raise ApiError(f"no bone {b!r}")
        pos = sample_world_positions(obj, sorted(world_bones), frames)
        if "foot_sliding" in checks and feet:
            fi, fm = _detect_feet(pos, frames, feet, contact_height, slide_speed, ground_z)
            issues += fi
            metrics["feet"] = fm
        if checks & {"pop", "below_ground"}:
            body = {b: pos[b] for b in (bones or [b.name for b in obj.data.bones if b.use_deform])}
            pi = _detect_pops(body, frames, pop_factor, pop_min,
                              ground_z if "below_ground" in checks else None, contact_height)
            issues += [i for i in pi if i["type"] in checks]
        if "below_ground" in checks and ground_z is None:
            notes.append("below_ground skipped: pass ground_z")

    # a pop also shows up as a slide and as jitter on the same bone: report it once
    pops = [(i["bone"], i["frames"]) for i in issues if i["type"] == "pop"]
    if pops:
        def explained(i):
            return i["type"] in ("foot_sliding", "jitter") and any(
                b == i["bone"] and fr[0] - 1 <= i["frames"][0] and i["frames"][1] <= fr[1] + 1 for b, fr in pops)
        issues = [i for i in issues if not explained(i)]
    issues.sort(key=lambda i: -i["severity"])
    metrics["issue_counts"] = {t: sum(1 for i in issues if i["type"] == t) for t in sorted({i["type"] for i in issues})}
    return {"object": object, "frame_range": [lo, hi], "issues": issues[:max_issues],
            "truncated": len(issues) > max_issues, "metrics": metrics, "notes": notes}
