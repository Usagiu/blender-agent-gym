"""Capture the animation-relevant state of a scene.

State is split in two:

* **authored** -- what the artist actually produces: every F-Curve of every
  Action (keyframes, handles, interpolation...), which Action each object uses,
  and the scene frame range.  This is the ground truth for "same result".
* **working** -- frame-dependent pose data: object transforms and pose-bone
  channels (incl. numeric custom properties such as IK/FK switches).  Needed so
  unkeyed pose edits and the input of keyframe-insert operators replay exactly.

Arrays are read with ``foreach_get`` into float32/int32 buffers so values are
bit-exact and hashing is cheap.
"""

import hashlib
import re

import bpy
import numpy as np

from . import compat

# ---------------------------------------------------------------- keyframes

_KF_FLOAT = (("co", 2), ("handle_left", 2), ("handle_right", 2),
             ("amplitude", 1), ("back", 1), ("period", 1))
_KF_ENUM = ("interpolation", "handle_left_type", "handle_right_type", "easing", "type")
_ENUM_MAPS = {}


def _enum_maps(prop):
    if prop not in _ENUM_MAPS:
        items = bpy.types.Keyframe.bl_rna.properties[prop].enum_items
        _ENUM_MAPS[prop] = ({i.value: i.identifier for i in items},
                            {i.identifier: i.value for i in items})
    return _ENUM_MAPS[prop]


BONE_RE = re.compile(r'pose\.bones\["((?:[^"\\]|\\.)*)"\]\.?(.*)')


def make_fc_key(action_name, slot_id, data_path, index):
    return f"{action_name}|{slot_id}|{data_path}|{index}"


def split_fc_key(key):
    action, slot, rest = key.split("|", 2)
    data_path, index = rest.rsplit("|", 1)
    return action, slot, data_path, int(index)


def bone_of(data_path):
    m = BONE_RE.match(data_path)
    return (m.group(1), m.group(2)) if m else (None, data_path)


class FCurveState:
    __slots__ = ("arrays", "meta", "sel", "sel_l", "sel_r", "fsel", "digest")

    @classmethod
    def read(cls, fc):
        self = cls()
        kps = fc.keyframe_points
        n = len(kps)
        self.arrays = {}
        for name, width in _KF_FLOAT:
            buf = np.empty(n * width, dtype=np.float32)
            kps.foreach_get(name, buf)
            self.arrays[name] = buf
        for name in _KF_ENUM:
            buf = np.empty(n, dtype=np.int32)
            kps.foreach_get(name, buf)
            self.arrays[name] = buf
        # selection is context, not data: kept out of the digest
        self.sel, self.sel_l, self.sel_r = (np.empty(n, dtype=bool) for _ in range(3))
        kps.foreach_get("select_control_point", self.sel)
        kps.foreach_get("select_left_handle", self.sel_l)
        kps.foreach_get("select_right_handle", self.sel_r)
        self.fsel = bool(fc.select)
        self.meta = {
            "extrapolation": fc.extrapolation,
            "mute": bool(fc.mute),
            "lock": bool(fc.lock),
            "group": fc.group.name if fc.group else "",
            "modifiers": [m.type for m in fc.modifiers],
        }
        self.digest = self._digest()
        return self

    def _digest(self):
        h = hashlib.sha1()
        for name in sorted(self.arrays):
            h.update(name.encode())
            h.update(self.arrays[name].tobytes())
        h.update(repr(sorted(self.meta.items())).encode())
        return h.hexdigest()

    @property
    def frames(self):
        return self.arrays["co"][0::2]

    def to_json(self):
        out = {"n": int(len(self.arrays["interpolation"])), "meta": self.meta}
        for name, _ in _KF_FLOAT:
            out[name] = self.arrays[name].tolist()
        for name in _KF_ENUM:
            to_id, _ = _enum_maps(name)
            out[name] = [to_id.get(int(v), int(v)) for v in self.arrays[name]]
        return out

    @classmethod
    def from_json(cls, data):
        """Rebuild arrays from JSON (used for tolerance comparisons)."""
        self = cls()
        self.arrays = {}
        for name, _ in _KF_FLOAT:
            self.arrays[name] = np.asarray(data[name], dtype=np.float32)
        for name in _KF_ENUM:
            _, to_val = _enum_maps(name)
            self.arrays[name] = np.asarray([to_val.get(v, v) for v in data[name]], dtype=np.int32)
        self.meta = data["meta"]
        self.sel = self.sel_l = self.sel_r = np.zeros(data["n"], dtype=bool)
        self.fsel = False
        self.digest = self._digest()      # same digest as read() for identical data
        return self


def write_fcurve(key, data):
    """Make the F-Curve identified by *key* equal to *data* (to_json dict or
    FCurveState); ``None`` removes it."""
    action_name, slot_id, data_path, index = split_fc_key(key)
    action = bpy.data.actions.get(action_name)
    if data is None:
        if action is not None:
            fc = compat.find_fcurve(action, slot_id, data_path, index)
            if fc is not None:
                compat.remove_fcurve(action, slot_id, fc)
        return
    if isinstance(data, FCurveState):
        state = data
    else:
        state = FCurveState.from_json(data)
    if action is None:
        action = bpy.data.actions.new(action_name)
    fc = compat.ensure_fcurve(action, slot_id, data_path, index, state.meta.get("group", ""))
    # F-Curve level props FIRST: their RNA update recalculates auto handles,
    # which would overwrite the exact handle coordinates written below.
    meta = state.meta
    for prop in ("extrapolation", "mute", "lock"):
        if getattr(fc, prop) != meta[prop]:
            setattr(fc, prop, meta[prop])
    n = len(state.arrays["interpolation"])
    compat.clear_keyframes(fc)
    fc.keyframe_points.add(n)
    kps = fc.keyframe_points
    # foreach_set bypasses RNA update callbacks, so values stay bit-exact.
    for name in _KF_ENUM:
        kps.foreach_set(name, state.arrays[name])
    for name, _ in _KF_FLOAT:
        kps.foreach_set(name, state.arrays[name])


# ---------------------------------------------------------------- pose

_PB_FLOAT = (("location", 3), ("rotation_quaternion", 4), ("rotation_euler", 3),
             ("rotation_axis_angle", 4), ("scale", 3))
_PB_PROP_NAMES = {name for name, _ in _PB_FLOAT} | {"rotation_mode"}


def _custom_props(idprop_holder):
    out = {}
    for k in idprop_holder.keys():
        if k.startswith("_"):
            continue
        v = idprop_holder[k]
        if isinstance(v, (int, float, bool, str)):
            out[k] = v
    return out


class PoseState:
    __slots__ = ("names", "arrays", "props", "bone_sel", "active_bone", "digest")

    @classmethod
    def read(cls, obj):
        self = cls()
        pbs = obj.pose.bones
        n = len(pbs)
        self.names = [pb.name for pb in pbs]
        self.arrays = {}
        for name, width in _PB_FLOAT:
            buf = np.empty(n * width, dtype=np.float32)
            pbs.foreach_get(name, buf)
            self.arrays[name] = buf.reshape(n, width)
        modes = np.empty(n, dtype=np.int32)
        pbs.foreach_get("rotation_mode", modes)
        self.arrays["rotation_mode"] = modes
        self.props = {pb.name: p for pb in pbs if (p := _custom_props(pb))}
        bones = obj.data.bones
        sel = np.empty(len(bones), dtype=bool)
        bones.foreach_get("select", sel)
        self.bone_sel = [b.name for b, s in zip(bones, sel) if s]
        self.active_bone = bones.active.name if bones.active else None
        h = hashlib.sha1(repr(self.names).encode())
        for name in sorted(self.arrays):
            h.update(self.arrays[name].tobytes())
        h.update(repr(sorted(self.props.items())).encode())
        self.digest = h.hexdigest()
        return self

    def bone_json(self, i):
        out = {name: self.arrays[name][i].tolist() for name, _ in _PB_FLOAT}
        out["rotation_mode"] = int(self.arrays["rotation_mode"][i])
        name = self.names[i]
        if name in self.props:
            out["props"] = self.props[name]
        return out


def write_pose_bones(obj, bones_json):
    for bname, vals in bones_json.items():
        pb = obj.pose.bones.get(bname)
        if pb is None:
            continue
        _set_rotation_mode(pb, vals["rotation_mode"])
        for name, _ in _PB_FLOAT:
            setattr(pb, name, vals[name])
        for k, v in vals.get("props", {}).items():
            pb[k] = v


_ROT_MODE_BY_VALUE = None


def _set_rotation_mode(pb, value):
    global _ROT_MODE_BY_VALUE
    if _ROT_MODE_BY_VALUE is None:
        items = bpy.types.PoseBone.bl_rna.properties["rotation_mode"].enum_items
        _ROT_MODE_BY_VALUE = {i.value: i.identifier for i in items}
    ident = _ROT_MODE_BY_VALUE.get(int(value))
    if ident and pb.rotation_mode != ident:
        pb.rotation_mode = ident


# ---------------------------------------------------------------- objects

def object_authored(obj):
    ad = obj.animation_data
    return {
        "action": ad.action.name if ad and ad.action else None,
        "slot": compat.action_slot_identifier(ad) if ad else "",
    }


def object_working(obj):
    return {
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "rotation_quaternion": list(obj.rotation_quaternion),
        "scale": list(obj.scale),
        "rotation_mode": obj.rotation_mode,
    }


def write_object_working(obj, vals):
    if obj.rotation_mode != vals["rotation_mode"]:
        obj.rotation_mode = vals["rotation_mode"]
    for k in ("location", "rotation_euler", "rotation_quaternion", "scale"):
        setattr(obj, k, vals[k])


# ---------------------------------------------------------------- snapshot

class Snapshot:
    __slots__ = ("fcurves", "obj_authored", "obj_working", "poses", "scene", "frame",
                 "authored_hash", "working_hash")

    def to_json(self):
        """Full serialisation (used for state_initial/state_final)."""
        return {
            "fcurves": {k: v.to_json() for k, v in self.fcurves.items()},
            "obj_authored": self.obj_authored,
            "obj_working": self.obj_working,
            "poses": {o: {p.names[i]: p.bone_json(i) for i in range(len(p.names))}
                      for o, p in self.poses.items()},
            "scene": self.scene,
            "frame": self.frame,
            "authored_hash": self.authored_hash,
            "working_hash": self.working_hash,
        }


def _scene_state(scene):
    return {
        "frame_start": scene.frame_start, "frame_end": scene.frame_end,
        "fps": scene.render.fps, "fps_base": scene.render.fps_base,
        "use_preview_range": scene.use_preview_range,
        "frame_preview_start": scene.frame_preview_start,
        "frame_preview_end": scene.frame_preview_end,
    }


def take_snapshot(scene=None):
    scene = scene or bpy.context.scene
    s = Snapshot()
    s.fcurves = {}
    for action in bpy.data.actions:
        for slot_id, fc in compat.iter_fcurves(action):
            key = make_fc_key(action.name, slot_id, fc.data_path, fc.array_index)
            s.fcurves[key] = FCurveState.read(fc)
    s.obj_authored = {o.name: object_authored(o) for o in scene.objects}
    s.obj_working = {o.name: object_working(o) for o in scene.objects}
    s.poses = {o.name: PoseState.read(o) for o in scene.objects if o.type == 'ARMATURE'}
    s.scene = _scene_state(scene)
    s.frame = scene.frame_current

    h = hashlib.sha1()
    for k in sorted(s.fcurves):
        h.update(k.encode())
        h.update(s.fcurves[k].digest.encode())
    h.update(repr(sorted(s.obj_authored.items())).encode())
    h.update(repr(sorted(s.scene.items())).encode())
    s.authored_hash = h.hexdigest()

    h = hashlib.sha1()
    for k in sorted(s.poses):
        h.update(k.encode())
        h.update(s.poses[k].digest.encode())
    h.update(np.asarray([v for k in sorted(s.obj_working) for name, vals in sorted(s.obj_working[k].items())
                         if name != "rotation_mode" for v in vals], dtype=np.float32).tobytes())
    h.update(repr(sorted((k, v["rotation_mode"]) for k, v in s.obj_working.items())).encode())
    s.working_hash = h.hexdigest()
    return s
