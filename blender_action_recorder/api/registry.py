"""Action registry: one place that defines every callable action, its JSON
schema (for MCP tools / RL action space) and how to execute a call.

A *call* is plain JSON:  {"name": "set_keys", "args": {...}}
"""

import json

ACTIONS = {}


class ApiError(Exception):
    """Raised for invalid calls; the message is meant for the model to read."""


def action(kind, params, required=()):
    """Register a function.  kind: 'edit' (changes the scene) | 'observe'."""
    def deco(fn):
        doc = (fn.__doc__ or "").strip()
        ACTIONS[fn.__name__] = {"fn": fn, "kind": kind, "doc": doc,
                                "params": params, "required": list(required)}
        return fn
    return deco


def execute(call):
    """Run one call.  Never raises: returns {"ok": bool, "result"|"error": ...}."""
    if not isinstance(call, dict) or "name" not in call:
        return {"ok": False, "error": "call must be {'name': ..., 'args': {...}}"}
    name, args = call["name"], call.get("args") or {}
    spec = ACTIONS.get(name)
    if spec is None:
        return {"ok": False, "error": f"unknown action {name!r}; available: {sorted(ACTIONS)}"}
    missing = [p for p in spec["required"] if p not in args]
    if missing:
        return {"ok": False, "error": f"{name}: missing required args {missing}"}
    unknown = [p for p in args if p not in spec["params"]]
    if unknown:
        return {"ok": False, "error": f"{name}: unknown args {unknown}; allowed {sorted(spec['params'])}"}
    try:
        return {"ok": True, "result": _jsonable(spec["fn"](**args))}
    except ApiError as e:
        return {"ok": False, "error": f"{name}: {e}"}


def _jsonable(v):
    """numpy scalars/arrays, tuples, int dict keys -> plain JSON types."""
    if isinstance(v, dict):
        return {str(k) if not isinstance(k, str) else k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "tolist"):          # numpy scalar or array
        return _jsonable(v.tolist())
    return v


def schema(kind=None):
    """Tool definitions in the usual LLM tool-calling shape."""
    out = []
    for name, a in ACTIONS.items():
        if kind and a["kind"] != kind:
            continue
        out.append({
            "name": name,
            "kind": a["kind"],
            "description": a["doc"],
            "input_schema": {"type": "object", "properties": a["params"], "required": a["required"]},
        })
    return out


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- shared param specs

P_OBJECT = {"type": "string", "description": "Object name (usually the armature). Its assigned Action is edited."}
P_ACTION = {"type": "string", "description": "Action name; use instead of `object` for an unassigned Action."}
P_BONE = {"type": "string", "description": "Pose bone name. Omit for object-level channels."}
P_CHANNEL = {"type": "string",
             "description": "location | rotation_euler | rotation_quaternion | rotation_axis_angle | scale, "
                            "or a custom property written as [\"name\"] (e.g. [\"ik_fk\"])."}
P_INDEX = {"type": "integer", "description": "Component index (x=0,y=1,z=2; quaternion w=0,x=1,y=2,z=3). "
                                             "Custom properties use 0.", "default": 0}
P_FRAMES = {"type": "array", "items": {"type": "number"}, "description": "Explicit frame numbers."}
P_FRAME_RANGE = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
                 "description": "Inclusive [start, end] frame range."}
P_RECALC = {"type": "boolean", "default": True,
            "description": "Recalculate automatic handles afterwards (what Blender does after an edit)."}
INTERPOLATIONS = ["CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART", "QUINT", "EXPO",
                  "CIRC", "BACK", "BOUNCE", "ELASTIC"]
HANDLE_TYPES = ["FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"]
EASINGS = ["AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"]
KEY_TYPES = ["KEYFRAME", "BREAKDOWN", "MOVING_HOLD", "EXTREME", "JITTER", "GENERATED"]
P_KEY = {
    "type": "object",
    "description": "One keyframe. Only `frame` is required for existing keys; new keys also need `value`. "
                   "Omitted fields keep their current value (new keys: Blender defaults). Values are raw "
                   "channel units: radians for rotations.",
    "properties": {
        "frame": {"type": "number"},
        "value": {"type": "number"},
        "interp": {"type": "string", "enum": INTERPOLATIONS},
        "hl": {"type": "array", "items": {"type": "number"}, "description": "left handle [frame, value]"},
        "hr": {"type": "array", "items": {"type": "number"}, "description": "right handle [frame, value]"},
        "hlt": {"type": "string", "enum": HANDLE_TYPES},
        "hrt": {"type": "string", "enum": HANDLE_TYPES},
        "easing": {"type": "string", "enum": EASINGS},
        "type": {"type": "string", "enum": KEY_TYPES},
        "amp": {"type": "number"}, "back": {"type": "number"}, "period": {"type": "number"},
    },
    "required": ["frame"],
}
