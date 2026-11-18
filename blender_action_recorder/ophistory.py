"""Operator capture (from ``WindowManager.operators``) and operator replay.

Only operators with the REGISTER flag executed from the UI appear in
``wm.operators`` -- operators called from Python scripts never do.  Edits made
without an operator (dragging a slider in the N panel, typing a value) are not
captured here; the state diff catches those.
"""

import hashlib
import json

import bpy

# Operators that only change selection/view/UI.  On replay they are skipped
# because restore_context() reproduces their effect directly.
CONTEXT_PREFIXES = ("view3d.select", "view3d.view", "view3d.zoom", "view3d.rotate", "view3d.move",
                    "view3d.navigate", "view3d.localview", "view3d.toggle", "view2d.", "screen.",
                    "wm.", "ui.", "outliner.", "anim.change_frame", "anim.channels_select",
                    "anim.channels_click", "anim.channels_setting", "anim.channels_expand",
                    "anim.channels_collapse", "graph.select", "graph.clickselect",
                    "graph.view", "action.select", "action.clickselect", "action.view",
                    "pose.select", "object.select", "info.", "file.", "buttons.", "image.view",
                    "graph.reveal", "graph.hide", "action.frame_jump", "graph.frame_jump",
                    "screen.frame_jump", "screen.keyframe_jump", "object.mode_set",
                    "object.posemode_toggle", "pose.hide", "pose.reveal")

# Operators whose execution needs a drawn region (they crash or no-op in
# ``blender -b``).  They are replayed only in GUI mode.
GUI_ONLY_PREFIXES = ("transform.", "sculpt.", "paint.", "view3d.")

EDITOR_BY_PREFIX = (
    ("graph.", "GRAPH_EDITOR"), ("action.", "DOPESHEET_EDITOR"), ("nla.", "NLA_EDITOR"),
    ("pose.", "VIEW_3D"), ("anim.keyframe_insert", "VIEW_3D"), ("anim.keyframe_delete", "VIEW_3D"),
    ("anim.keyframe_clear", "VIEW_3D"), ("view3d.", "VIEW_3D"), ("object.", "VIEW_3D"),
    ("armature.", "VIEW_3D"), ("sculpt.", "VIEW_3D"),
)


def py_idname(bl_idname):
    """'POSE_OT_copy' -> 'pose.copy'"""
    if "_OT_" in bl_idname:
        mod, fn = bl_idname.split("_OT_", 1)
        return f"{mod.lower()}.{fn}"
    return bl_idname


def classify(idname):
    if idname.startswith(CONTEXT_PREFIXES):
        return "context"
    if idname.startswith(GUI_ONLY_PREFIXES):
        return "gui_only"
    return "data"


def infer_editor(idname, recorded_editor=None):
    """Editor to run an operator in.  Editor-specific prefixes win; generic
    ones (transform.*, anim.*) use the editor the user was actually in."""
    if idname.startswith(("transform.", "anim.")) and recorded_editor:
        return recorded_editor
    for prefix, editor in EDITOR_BY_PREFIX:
        if idname.startswith(prefix):
            return editor
    return recorded_editor or "VIEW_3D"


def _to_json(v, depth=0):
    if depth > 6:
        return repr(v)
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    if isinstance(v, set):
        return sorted(v)
    if isinstance(v, bpy.types.ID):
        return {"__id__": type(v).__name__, "name": v.name}
    if isinstance(v, bpy.types.bpy_struct):
        out = {}
        for p in v.bl_rna.properties:
            if p.identifier != "rna_type":
                out[p.identifier] = _to_json(getattr(v, p.identifier), depth + 1)
        return out
    if hasattr(v, "__len__") and not isinstance(v, (str, bytes)):
        try:
            return [_to_json(x, depth + 1) for x in v]
        except TypeError:
            pass
    return repr(v)


def serialize_operator(op):
    props = {}
    set_props = []
    p_rna = op.properties
    for p in p_rna.bl_rna.properties:
        ident = p.identifier
        if ident == "rna_type":
            continue
        try:
            props[ident] = _to_json(getattr(p_rna, ident))
        except AttributeError:
            continue
        if p_rna.is_property_set(ident):
            set_props.append(ident)
    idname = py_idname(op.bl_idname)
    return {"idname": idname, "name": op.name, "props": props, "set_props": set_props,
            "class": classify(idname)}


def _hash(rec):
    return hashlib.sha1(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()


class OpWatcher:
    """Detect operators newly appended to ``wm.operators`` and in-place edits
    of the last one (the "Adjust Last Operation" / F9 redo panel)."""

    def __init__(self, wm):
        self._sync([(op.as_pointer(), op) for op in wm.operators])

    def _sync(self, cur):
        self.prev_ptrs = {p for p, _ in cur}
        self.last = (cur[-1][0], _hash(serialize_operator(cur[-1][1]))) if cur else None

    def poll(self, wm):
        cur = [(op.as_pointer(), op) for op in wm.operators]
        new_start = 0
        for i, (ptr, _) in enumerate(cur):
            if ptr in self.prev_ptrs:
                new_start = i + 1
        events = []
        if new_start == len(cur) and cur and self.last and cur[-1][0] == self.last[0]:
            rec = serialize_operator(cur[-1][1])
            if _hash(rec) != self.last[1]:
                rec["adjusted"] = True   # redo panel changed the last operator's props
                events.append(rec)
        events.extend(serialize_operator(op) for _, op in cur[new_start:])
        self._sync(cur)
        return events


# ---------------------------------------------------------------- replay

def run_operator(rec, window=None, editor=None):
    """Execute a recorded operator.  Returns (status, message).

    status: 'ok' | 'skipped_context' | 'skipped_gui_only' | 'failed'
    """
    from . import compat
    idname = rec["idname"]
    cls = rec.get("class") or classify(idname)
    if cls == "context":
        return "skipped_context", ""
    if cls == "gui_only" and bpy.app.background:
        return "skipped_gui_only", "needs a drawn region; replay with --gui"
    mod, fn = idname.split(".", 1)
    try:
        op = getattr(getattr(bpy.ops, mod), fn)
    except AttributeError:
        return "failed", f"unknown operator {idname}"

    editor = infer_editor(idname, editor)
    override = {}
    area = None
    if window is not None:
        area, region = compat.find_area(window, editor)
        override = {"window": window, "screen": window.screen, "area": area, "region": region}

    def _call(props):
        with bpy.context.temp_override(**override):
            return op('EXEC_DEFAULT', **props)

    props = _props_from_json(op, rec["props"])
    try:
        res = _call(props)
    except (TypeError, ValueError, AttributeError):
        # some recorded props are read-only/hidden; retry with explicitly-set ones
        try:
            res = _call({k: props[k] for k in rec.get("set_props", []) if k in props})
        except Exception as e:  # noqa: BLE001
            return "failed", str(e).strip().splitlines()[-1]
    except RuntimeError as e:
        return "failed", str(e).strip().splitlines()[-1]
    if 'FINISHED' in res:
        return "ok", ""
    return "failed", f"returned {sorted(res)}"


def _props_from_json(op, recorded):
    """Restore Python types the operator expects (enum-flag sets, etc.)."""
    try:
        rna_props = op.get_rna_type().properties
    except Exception:  # noqa: BLE001
        rna_props = {}
    out = {}
    for k, v in recorded.items():
        p = rna_props.get(k) if rna_props else None
        if p is not None and p.type == 'ENUM' and getattr(p, "is_enum_flag", False) and isinstance(v, list):
            v = set(v)
        elif p is not None and p.type == 'POINTER':
            continue    # pointer props cannot be passed as call arguments
        out[k] = _from_json(v)
    return out


def _from_json(v):
    if isinstance(v, dict) and "__id__" in v:
        coll = {"Object": bpy.data.objects, "Action": bpy.data.actions,
                "Armature": bpy.data.armatures, "Mesh": bpy.data.meshes}.get(v["__id__"])
        return coll.get(v["name"]) if coll else None
    return v
