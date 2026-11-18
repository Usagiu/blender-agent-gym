"""Tests for the low-level edit API and the observation layer.

  blender -b --factory-startup --python tests/test_api.py

Builds a small rig with known, injected problems:
  foot.L   planted 1-20, slides 20-30, swings 31-40, planted 41-60, one-frame pop at 50
  spine    smooth rotation, noise on frames 10-25, euler flip (+2*pi) from frame 45
"""

import json
import math
import os
import random
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from blender_action_recorder import api  # noqa: E402
from blender_action_recorder.api import observe  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def call(name, **args):
    r = api.execute({"name": name, "args": args})
    json.dumps(r)            # every result must be JSON-serialisable
    return r


# ------------------------------------------------------------ scene
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
arm_data = bpy.data.armatures.new("RigData")
rig = bpy.data.objects.new("Rig", arm_data)
bpy.context.scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
foot = arm_data.edit_bones.new("foot.L")
foot.head, foot.tail = (0, 0, 0), (0, 0.15, 0)      # along +Y, roll 0 -> local axes == world axes
spine = arm_data.edit_bones.new("spine")
spine.head, spine.tail = (1, 0, 0), (1, 0, 0.5)
bpy.ops.object.mode_set(mode='POSE')
sc = bpy.context.scene
sc.frame_start, sc.frame_end = 1, 60
pf, ps = rig.pose.bones["foot.L"], rig.pose.bones["spine"]
ps.rotation_mode = 'XYZ'
random.seed(3)
for f in range(1, 61):
    if f <= 20:
        loc = (0, 0, 0)
    elif f <= 30:
        loc = (0.01 * (f - 20), 0, 0)
    elif f <= 40:
        loc = (0.1 + 0.03 * (f - 30), 0, 0.2 * math.sin(math.pi * (f - 30) / 10))
    else:
        loc = (0.4, 0, 0)
    if f == 50:
        loc = (0.7, 0, 0)                               # pop
    pf.location = loc
    pf.keyframe_insert("location", frame=f)
    x = 0.5 * math.sin(f / 10)
    if 10 <= f <= 25:
        x += random.uniform(-0.15, 0.15)                 # jitter
    if f >= 45:
        x += 2 * math.pi                                 # euler flip
    ps.rotation_euler = (x, 0, 0)
    ps.keyframe_insert("rotation_euler", frame=f)
for fc in rig.animation_data.action.fcurves:
    for kp in fc.keyframe_points:
        kp.interpolation = 'LINEAR'
sc.frame_set(1)

# ------------------------------------------------------------ schema
print("schema")
sch = api.schema()
names = {s["name"] for s in sch}
check({"set_keys", "delete_keys", "set_key_props", "delete_channel", "set_channel_props", "set_pose",
       "key_pose", "set_frame", "set_frame_range", "assign_action"} <= names, "all edit actions registered")
check({"list_objects", "list_channels", "get_keys", "sample_curves", "get_pose", "sample_world",
       "detect_issues"} <= names, "all observe actions registered")
check(all(s["description"] for s in sch), "every action has a description")
json.dumps(sch)

# ------------------------------------------------------------ edit actions
print("edit")
r = call("set_keys", object="Rig", bone="spine", channel="location", index=2,
         keys=[{"frame": 1, "value": 0.0}, {"frame": 10, "value": 1.0}, {"frame": 20, "value": 0.0}])
fc = rig.animation_data.action.fcurves.find('pose.bones["spine"].location', index=2)
check(r["ok"] and fc is not None and len(fc.keyframe_points) == 3, "set_keys creates a channel with 3 keys")
check(fc.group is not None and fc.group.name == "spine", "new channel grouped under the bone name")
k10 = fc.keyframe_points[1]
check(abs(k10.handle_left[1] - 1.0) < 1e-6 and abs(k10.handle_right[1] - 1.0) < 1e-6,
      "auto-clamped handles recalculated (peak key has flat handles)")

r = call("set_keys", object="Rig", bone="spine", channel="location", index=2, keys=[{"frame": 10, "value": 2.0}])
check(r["ok"] and abs(fc.keyframe_points[1].co[1] - 2.0) < 1e-6, "set_keys updates an existing key's value")

r = call("set_key_props", object="Rig", bone="spine", channel="location", index=2, frames=[1, 10],
         interp="CONSTANT")
check(r["ok"] and [k.interpolation for k in fc.keyframe_points] == ["CONSTANT", "CONSTANT", "BEZIER"],
      "set_key_props changes interpolation on chosen frames only")

r = call("set_keys", object="Rig", bone="spine", channel="location", index=2,
         keys=[{"frame": 15, "value": 0.5, "hlt": "FREE", "hrt": "FREE", "hl": [14, 0.2], "hr": [16, 0.8]}],
         recalc_handles=False)
k15 = fc.keyframe_points[2]
check(r["ok"] and tuple(k15.handle_left) == (14.0, 0.2 if False else k15.handle_left[1])
      and abs(k15.handle_left[1] - 0.2) < 1e-6 and abs(k15.handle_right[1] - 0.8) < 1e-6,
      "explicit FREE handles kept exactly")

r = call("delete_keys", object="Rig", bone="spine", channel="location", index=2, frame_range=[9, 16])
check(r["ok"] and r["result"]["removed"] == 2 and len(fc.keyframe_points) == 2, "delete_keys by frame range")
r = call("delete_keys", object="Rig", bone="spine", channel="location", index=2, frames=[1, 20])
check(r["ok"] and r["result"].get("channel_removed")
      and rig.animation_data.action.fcurves.find('pose.bones["spine"].location', index=2) is None,
      "deleting the last keys removes the channel")

r = call("set_pose", object="Rig", bone="spine", channel="rotation_euler", value=[0.3, 0, 0])
r2 = call("key_pose", object="Rig", bones=["spine"], channels=["rotation_euler"], frame=5)
fcx = rig.animation_data.action.fcurves.find('pose.bones["spine"].rotation_euler', index=0)
check(r["ok"] and r2["ok"] and abs(fcx.evaluate(5) - 0.3) < 1e-6, "set_pose + key_pose keys the pose")
call("set_keys", object="Rig", bone="spine", channel="rotation_euler", index=0,
     keys=[{"frame": 5, "value": 0.5 * math.sin(0.5)}])          # put the curve back

r = call("set_pose", object="Rig", bone="spine", channel='["ik_fk"]', value=1.0)
check(r["ok"] and ps["ik_fk"] == 1.0, "set_pose sets custom properties")

print("errors")
check(not call("set_keys", object="Rig", bone="nope", channel="location", keys=[])["ok"], "unknown bone rejected")
check(not call("set_keys", object="Rig", bone="spine", channel="location", index=0,
               keys=[{"frame": 3}])["ok"], "new key without value rejected")
check(not call("set_key_props", object="Rig", bone="foot.L", channel="location", frames=[1],
               interp="SPLINE")["ok"], "invalid interpolation rejected")
r = call("set_keys", object="Rig", bone="spine", bogus=1, channel="location", keys=[])
check(not r["ok"] and "unknown args" in r["error"], "unknown argument rejected with a readable message")
check(not call("nope")["ok"], "unknown action rejected")

# ------------------------------------------------------------ observe
print("observe")
r = call("list_objects")
check(r["ok"] and any(o["name"] == "Rig" and o["bones"] == 2 for o in r["result"]["objects"]), "list_objects")
r = call("list_channels", object="Rig")
ch = r["result"]["channels"]
check(r["ok"] and ch["foot.L"]["location"]["keys"] == 180 and ch["spine"]["rotation_euler"]["indices"] == [0, 1, 2],
      "list_channels groups channels by bone")
r = call("get_keys", object="Rig", bone="foot.L", channel="location", index=0, frame_range=[20, 22])
keys = r["result"]["components"]["0"]["keys"]
check(r["ok"] and [k["f"] for k in keys] == [20, 21, 22] and keys[1]["v"] == 0.01, "get_keys returns a window")
r = call("sample_curves", object="Rig", bones=["foot.L"], channels=["location"], frame_range=[31, 33])
check(r["ok"] and len(r["result"]["values"]["foot.L"]["location"]["2"]) == 3, "sample_curves")

ps.rotation_euler = (0.123, 0, 0)        # unkeyed edit: must survive observation
sc.frame_set(1)
ps.rotation_euler = (0.123, 0, 0)
r = call("sample_world", object="Rig", bones=["foot.L"], frame_range=[35, 35])
check(r["ok"] and r["result"]["positions"]["foot.L"][0][2] > 0.1, "sample_world sees the lifted foot")
check(sc.frame_current == 1 and abs(ps.rotation_euler[0] - 0.123) < 1e-6,
      "observation restores frame and unkeyed pose")
r = call("get_pose", object="Rig", bones=["spine"])
check(r["ok"] and r["result"]["bones"]["spine"]["rotation_euler"][0] == 0.123, "get_pose shows unkeyed values")

print("detect_issues")
r = call("detect_issues", object="Rig", ground_z=0.0)
check(r["ok"], "detect_issues runs")
res = r["result"]
print("   ", json.dumps(res["issues"])[:600])
by = {}
for i in res["issues"]:
    by.setdefault(i["type"], []).append(i)


def overlaps(issue, lo, hi):
    a, b = issue["frames"]
    return a <= hi and b >= lo


slides = [i for i in by.get("foot_sliding", []) if i["bone"] == "foot.L"]
check(len(slides) == 1 and overlaps(slides[0], 20, 30) and slides[0]["frames"][0] >= 19
      and slides[0]["frames"][1] <= 31, f"foot sliding found at 20-30: {[s['frames'] for s in slides]}")
check(not any(overlaps(s, 32, 39) for s in slides), "swing (foot lifted) not reported as sliding")
jit = [i for i in by.get("jitter", []) if i["bone"] == "spine"]
check(jit and all(overlaps(j, 9, 26) for j in jit), f"jitter found within 10-25: {[j['frames'] for j in jit]}")
flips = [i for i in by.get("rotation_flip", []) if i["bone"] == "spine"]
check(len(flips) == 1 and flips[0]["frames"] == [44, 45], f"euler flip at 44-45: {[f['frames'] for f in flips]}")
pops = [i for i in by.get("pop", []) if i["bone"] == "foot.L"]
check(pops and all(overlaps(p, 49, 51) for p in pops), f"pop found at 50 only: {[p['frames'] for p in pops]}")
check(not by.get("below_ground"), "nothing below ground")
check(sc.frame_current == 1 and abs(ps.rotation_euler[0] - 0.123) < 1e-6, "detect_issues leaves the scene as it was")

r = call("detect_issues", object="Rig", checks=["foot_sliding"], frame_range=[1, 19])
check(r["ok"] and not r["result"]["issues"], "planted range reports no sliding")

print("TEST", "FAILED: " + "; ".join(FAILS) if FAILS else "PASSED")
sys.exit(1 if FAILS else 0)
