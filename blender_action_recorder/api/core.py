"""Low-level edit actions: complete, deterministic, no hidden context.

Every animation change can be expressed with these.  Channels are addressed by
names and frames, never by selection or indices into Blender collections.
Values are raw channel units (radians for rotations).

Handle semantics mirror Blender: after an edit, automatic handles
(AUTO / AUTO_CLAMPED / VECTOR) are recalculated unless recalc_handles=False.
Explicit handle coordinates are kept exactly when recalc_handles=False.
"""

import re

import bpy

from .. import compat
from .. import snapshot as snap
from .registry import (ApiError, EASINGS, HANDLE_TYPES, INTERPOLATIONS, KEY_TYPES, P_ACTION, P_BONE,
                       P_CHANNEL, P_FRAME_RANGE, P_FRAMES, P_INDEX, P_KEY, P_OBJECT, P_RECALC, action)

POSE_CHANNELS = ("location", "rotation_quaternion", "rotation_euler", "rotation_axis_angle", "scale")
AUTO_HANDLES = ("AUTO", "AUTO_CLAMPED", "VECTOR")
# what Blender gives a freshly inserted keyframe (4.3, default preferences)
KEY_DEFAULTS = {"interp": "BEZIER", "hlt": "AUTO_CLAMPED", "hrt": "AUTO_CLAMPED", "easing": "AUTO",
                "type": "KEYFRAME", "amp": 0.8, "back": 1.70158, "period": 4.1}
_ENUM_FIELDS = {"interp": INTERPOLATIONS, "hlt": HANDLE_TYPES, "hrt": HANDLE_TYPES,
                "easing": EASINGS, "type": KEY_TYPES}
_UNESCAPE = re.compile(r"\\(.)")

# row field <-> FCurveState.to_json() field
_JSON_ENUM = {"interp": "interpolation", "hlt": "handle_left_type", "hrt": "handle_right_type",
              "easing": "easing", "type": "type"}
_JSON_FLOAT1 = {"amp": "amplitude", "back": "back", "period": "period"}


# ---------------------------------------------------------------- addressing

def data_path_for(bone, channel):
    if not bone:
        return channel
    base = f'pose.bones["{bpy.utils.escape_identifier(bone)}"]'
    return base + channel if channel.startswith("[") else f"{base}.{channel}"


def split_data_path(data_path):
    """'pose.bones["Thigh"].rotation_euler' -> ('Thigh', 'rotation_euler')"""
    bone, rest = snap.bone_of(data_path)
    if bone is None:
        return None, data_path
    return _UNESCAPE.sub(r"\1", bone), rest


def owner_of_action(action_name):
    for obj in bpy.data.objects:
        ad = obj.animation_data
        if ad and ad.action and ad.action.name == action_name:
            return obj.name
    return None


def address_for(action_name, data_path, index):
    """Recorded F-Curve -> API address args."""
    bone, channel = split_data_path(data_path)
    owner = owner_of_action(action_name)
    args = {"object": owner} if owner else {"action": action_name}
    if bone:
        args["bone"] = bone
    args["channel"] = channel
    args["index"] = index
    return args


def _resolve_action(object=None, action=None, create=False):
    if action:
        act = bpy.data.actions.get(action)
        if act is None:
            if not create:
                raise ApiError(f"no action named {action!r}")
            act = bpy.data.actions.new(action)
        return act
    if not object:
        raise ApiError("give `object` (or `action`)")
    obj = bpy.data.objects.get(object)
    if obj is None:
        raise ApiError(f"no object named {object!r}")
    ad = obj.animation_data
    if ad is None or ad.action is None:
        if not create:
            raise ApiError(f"object {object!r} has no action")
        compat.assign_action(obj, f"{obj.name}Action")
    return obj.animation_data.action


def _check_bone(object, bone):
    if bone and object:
        obj = bpy.data.objects.get(object)
        if obj is not None and obj.type == 'ARMATURE' and bone not in obj.pose.bones:
            raise ApiError(f"armature {object!r} has no bone {bone!r}")


def _channel(object, action, bone, channel, index, create=False):
    _check_bone(object, bone)
    act = _resolve_action(object, action, create=create)
    dp = data_path_for(bone, channel)
    fc = compat.find_fcurve(act, "", dp, index)
    if fc is None and not create:
        raise ApiError(f"no animation channel {dp}[{index}] in action {act.name!r}")
    return act, dp, fc


# ---------------------------------------------------------------- key rows

def decode(fjson):
    """FCurveState.to_json() -> list of key row dicts."""
    rows = []
    for i in range(fjson["n"]):
        row = {"frame": fjson["co"][2 * i], "value": fjson["co"][2 * i + 1],
               "hl": fjson["handle_left"][2 * i:2 * i + 2], "hr": fjson["handle_right"][2 * i:2 * i + 2]}
        for f, j in _JSON_ENUM.items():
            row[f] = fjson[j][i]
        for f, j in _JSON_FLOAT1.items():
            row[f] = fjson[j][i]
        rows.append(row)
    return rows


def encode(rows, meta):
    rows = sorted(rows, key=lambda r: r["frame"])
    out = {"n": len(rows), "meta": meta,
           "co": [v for r in rows for v in (r["frame"], r["value"])],
           "handle_left": [v for r in rows for v in r["hl"]],
           "handle_right": [v for r in rows for v in r["hr"]]}
    for f, j in _JSON_ENUM.items():
        out[j] = [r[f] for r in rows]
    for f, j in _JSON_FLOAT1.items():
        out[j] = [r[f] for r in rows]
    return out


def _fkey(frame):
    return round(float(frame), 3)


def _validate_fields(k):
    for f, allowed in _ENUM_FIELDS.items():
        if f in k and k[f] not in allowed:
            raise ApiError(f"{f}={k[f]!r} invalid; one of {allowed}")
    for h in ("hl", "hr"):
        if h in k and (not isinstance(k[h], (list, tuple)) or len(k[h]) != 2):
            raise ApiError(f"{h} must be [frame, value]")


def _frames_arg(frames, frame_range):
    if frames is None and frame_range is None:
        raise ApiError("give `frames` or `frame_range`")
    if frame_range is not None:
        lo, hi = frame_range
        return lambda f: lo - 1e-3 <= f <= hi + 1e-3
    wanted = {_fkey(f) for f in frames}
    return lambda f: _fkey(f) in wanted


def _read_rows(fc):
    st = snap.FCurveState.read(fc)
    return decode(st.to_json()), st.meta


def _write_rows(act, dp, index, rows, meta, recalc):
    key = snap.make_fc_key(act.name, "", dp, index)
    snap.write_fcurve(key, encode(rows, meta))
    if recalc:
        compat.find_fcurve(act, "", dp, index).update()


def _addr_result(dp, index, rows):
    frames = [r["frame"] for r in rows]
    return {"channel": f"{dp}[{index}]", "keys": len(rows),
            "frame_range": [min(frames), max(frames)] if frames else None}


# ---------------------------------------------------------------- edit actions

_ADDR = {"object": P_OBJECT, "action": P_ACTION, "bone": P_BONE, "channel": P_CHANNEL, "index": P_INDEX}


@action("edit", {**_ADDR, "keys": {"type": "array", "items": P_KEY},
                 "group": {"type": "string", "description": "Channel group when the channel is created "
                                                             "(default: bone name)."},
                 "recalc_handles": P_RECALC},
        required=("channel", "keys"))
def set_keys(object=None, action=None, bone=None, channel=None, index=0, keys=(), group=None,
             recalc_handles=True):
    """Insert or update keyframes on one channel (creates the channel if needed).
    Updating a key's value without giving handles moves its handles with it."""
    act, dp, fc = _channel(object, action, bone, channel, index, create=True)
    if fc is not None:
        rows, meta = _read_rows(fc)
    else:
        rows = []
        meta = {"extrapolation": "CONSTANT", "mute": False, "lock": False,
                "group": group if group is not None else (bone or ""), "modifiers": []}
    by_frame = {_fkey(r["frame"]): r for r in rows}
    need_recalc = False
    for k in keys:
        if "frame" not in k:
            raise ApiError("every key needs `frame`")
        _validate_fields(k)
        f = float(k["frame"])
        old = by_frame.get(_fkey(f))
        if old is None:
            if "value" not in k:
                raise ApiError(f"new key at frame {f} needs `value`")
            v = float(k["value"])
            row = dict(KEY_DEFAULTS, frame=f, value=v, hl=[f - 1.0, v], hr=[f + 1.0, v])
            if "hl" not in k or "hr" not in k:
                need_recalc = True
        else:
            row = dict(old)
            if "value" in k:
                dv = float(k["value"]) - row["value"]
                row["value"] = float(k["value"])
                row["hl"] = [row["hl"][0], row["hl"][1] + dv]
                row["hr"] = [row["hr"][0], row["hr"][1] + dv]
        for fld in ("interp", "hlt", "hrt", "easing", "type", "amp", "back", "period"):
            if fld in k:
                row[fld] = k[fld]
        for h in ("hl", "hr"):
            if h in k:
                row[h] = [float(k[h][0]), float(k[h][1])]
        by_frame[_fkey(f)] = row
    rows = list(by_frame.values())
    _write_rows(act, dp, index, rows, meta, recalc_handles or need_recalc)
    return _addr_result(dp, index, rows)


@action("edit", {**_ADDR, "frames": P_FRAMES, "frame_range": P_FRAME_RANGE, "recalc_handles": P_RECALC,
                 "remove_empty_channel": {"type": "boolean", "default": True}},
        required=("channel",))
def delete_keys(object=None, action=None, bone=None, channel=None, index=0, frames=None, frame_range=None,
                recalc_handles=True, remove_empty_channel=True):
    """Delete keyframes on one channel by frame list or inclusive range."""
    act, dp, fc = _channel(object, action, bone, channel, index)
    match = _frames_arg(frames, frame_range)
    rows, meta = _read_rows(fc)
    keep = [r for r in rows if not match(r["frame"])]
    removed = len(rows) - len(keep)
    if removed == 0:
        raise ApiError(f"no keys at the given frames on {dp}[{index}]")
    if not keep and remove_empty_channel:
        compat.remove_fcurve(act, "", fc)
        return {"channel": f"{dp}[{index}]", "removed": removed, "channel_removed": True}
    _write_rows(act, dp, index, keep, meta, recalc_handles)
    return dict(_addr_result(dp, index, keep), removed=removed)


@action("edit", {**_ADDR, "frames": P_FRAMES, "frame_range": P_FRAME_RANGE,
                 "interp": P_KEY["properties"]["interp"], "easing": P_KEY["properties"]["easing"],
                 "hlt": P_KEY["properties"]["hlt"], "hrt": P_KEY["properties"]["hrt"],
                 "type": P_KEY["properties"]["type"], "amp": {"type": "number"}, "back": {"type": "number"},
                 "period": {"type": "number"}, "recalc_handles": P_RECALC},
        required=("channel",))
def set_key_props(object=None, action=None, bone=None, channel=None, index=0, frames=None, frame_range=None,
                  interp=None, easing=None, hlt=None, hrt=None, type=None, amp=None, back=None, period=None,
                  recalc_handles=True):
    """Change interpolation / easing / handle types / key type of existing keys (values untouched)."""
    changes = {k: v for k, v in (("interp", interp), ("easing", easing), ("hlt", hlt), ("hrt", hrt),
                                 ("type", type), ("amp", amp), ("back", back), ("period", period))
               if v is not None}
    if not changes:
        raise ApiError("nothing to change")
    _validate_fields(changes)
    act, dp, fc = _channel(object, action, bone, channel, index)
    match = _frames_arg(frames, frame_range)
    rows, meta = _read_rows(fc)
    hit = 0
    for r in rows:
        if match(r["frame"]):
            r.update(changes)
            hit += 1
    if hit == 0:
        raise ApiError(f"no keys at the given frames on {dp}[{index}]")
    _write_rows(act, dp, index, rows, meta, recalc_handles)
    return dict(_addr_result(dp, index, rows), changed=hit)


@action("edit", dict(_ADDR), required=("channel",))
def delete_channel(object=None, action=None, bone=None, channel=None, index=0):
    """Remove an animation channel (F-Curve) entirely."""
    act, dp, fc = _channel(object, action, bone, channel, index)
    compat.remove_fcurve(act, "", fc)
    return {"channel": f"{dp}[{index}]", "removed": True}


@action("edit", {**_ADDR, "extrapolation": {"type": "string", "enum": ["CONSTANT", "LINEAR"]},
                 "mute": {"type": "boolean"}, "lock": {"type": "boolean"}},
        required=("channel",))
def set_channel_props(object=None, action=None, bone=None, channel=None, index=0, extrapolation=None,
                      mute=None, lock=None):
    """Set channel-level properties (extrapolation, mute, lock). Recalculates auto handles."""
    act, dp, fc = _channel(object, action, bone, channel, index)
    for k, v in (("extrapolation", extrapolation), ("mute", mute), ("lock", lock)):
        if v is not None:
            setattr(fc, k, v)
    return {"channel": f"{dp}[{index}]", "extrapolation": fc.extrapolation, "mute": fc.mute, "lock": fc.lock}


@action("edit", {"object": P_OBJECT, "bone": P_BONE,
                 "channel": {"type": "string", "description": "location | rotation_quaternion | rotation_euler"
                                                              " | rotation_axis_angle | scale | rotation_mode | "
                                                              "[\"custom_prop\"]"},
                 "value": {"description": "number, list of numbers, or rotation mode string"},
                 "index": {"type": "integer", "description": "Set a single component only."}},
        required=("object", "channel", "value"))
def set_pose(object=None, bone=None, channel=None, value=None, index=None):
    """Set the current (unkeyed) pose value of a bone or object. Lost on frame change unless keyed;
    use key_pose or set_keys to keep it."""
    obj = bpy.data.objects.get(object)
    if obj is None:
        raise ApiError(f"no object named {object!r}")
    if bone:
        _check_bone(object, bone)
        target = obj.pose.bones[bone]
    else:
        target = obj
    if channel.startswith('["') and channel.endswith('"]'):
        target[channel[2:-2]] = value
    elif channel == "rotation_mode":
        target.rotation_mode = value
    elif channel in POSE_CHANNELS:
        if index is not None:
            getattr(target, channel)[index] = float(value)
        else:
            cur = getattr(target, channel)
            if len(value) != len(cur):
                raise ApiError(f"{channel} needs {len(cur)} values")
            setattr(target, channel, [float(v) for v in value])
    else:
        raise ApiError(f"unknown channel {channel!r}")
    return {"object": object, "bone": bone, "channel": channel}


@action("edit", {"object": P_OBJECT, "bones": {"type": "array", "items": {"type": "string"}},
                 "channels": {"type": "array", "items": {"type": "string"},
                              "description": "default: location, rotation (current rotation mode), scale"},
                 "frame": {"type": "number", "description": "default: current frame"}},
        required=("object", "bones"))
def key_pose(object=None, bones=(), channels=None, frame=None):
    """Keyframe the bones' current pose values (like pressing I) at `frame`."""
    obj = bpy.data.objects.get(object)
    if obj is None:
        raise ApiError(f"no object named {object!r}")
    frame = bpy.context.scene.frame_current if frame is None else frame
    done = []
    for b in bones:
        _check_bone(object, b)
        pb = obj.pose.bones[b]
        rot = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}.get(
            pb.rotation_mode, "rotation_euler")
        for ch in channels or ("location", rot, "scale"):
            vals = getattr(pb, ch) if not ch.startswith("[") else [pb[ch[2:-2]]]
            vals = list(vals) if hasattr(vals, "__len__") else [vals]
            for i, v in enumerate(vals):
                set_keys(object=object, bone=b, channel=ch, index=i, keys=[{"frame": frame, "value": v}])
            done.append(f"{b}.{ch}")
    return {"frame": frame, "keyed": done}


@action("edit", {"frame": {"type": "integer"}}, required=("frame",))
def set_frame(frame):
    """Go to a frame. Re-evaluates animation, discarding unkeyed pose edits."""
    bpy.context.scene.frame_set(int(frame))
    return {"frame": bpy.context.scene.frame_current}


@action("edit", {"start": {"type": "integer"}, "end": {"type": "integer"},
                 "preview": {"type": "boolean", "default": False, "description": "set the preview range instead"}},
        required=("start", "end"))
def set_frame_range(start, end, preview=False):
    """Set the scene (or preview) frame range."""
    sc = bpy.context.scene
    if preview:
        sc.use_preview_range = True
        sc.frame_preview_start, sc.frame_preview_end = start, end
    else:
        sc.frame_start, sc.frame_end = start, end
    return {"start": start, "end": end, "preview": preview}


@action("edit", {"object": P_OBJECT, "action": {"type": ["string", "null"]}}, required=("object", "action"))
def assign_action(object=None, action=None):
    """Assign an Action (created if missing) to an object; null unassigns."""
    obj = bpy.data.objects.get(object)
    if obj is None:
        raise ApiError(f"no object named {object!r}")
    compat.assign_action(obj, action)
    return {"object": object, "action": action}
