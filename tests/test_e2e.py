"""End-to-end headless test: record a scripted "animation repair" session,
then replay it in state mode and ops mode and check the results.

  blender -b --factory-startup --python tests/test_e2e.py -- OUT_DIR

Operators called from Python never reach ``wm.operators``, so the test
executes each operator and appends the matching record itself (exactly what
OpWatcher would produce for a UI-invoked operator).  The handler/timer/event
listener path needs a real GUI session to exercise.
"""

import json
import math
import os
import random
import subprocess
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from blender_action_recorder import compat, ophistory  # noqa: E402
from blender_action_recorder.recorder import RECORDER  # noqa: E402

OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "/tmp/ar_test"
os.makedirs(OUT, exist_ok=True)
WIN = bpy.context.window_manager.windows[0]


def override(editor):
    area, region = compat.find_area(WIN, editor)
    return dict(window=WIN, screen=WIN.screen, area=area, region=region)


def run_op(idname, editor, record_class=None, **props):
    """Execute an operator like the UI would, then log it like OpWatcher would."""
    mod, fn = idname.split(".")
    with bpy.context.temp_override(**override(editor)):
        res = getattr(getattr(bpy.ops, mod), fn)('EXEC_DEFAULT', **props)
    assert 'FINISHED' in res, (idname, res)
    log_op(idname, editor, **props)


def log_op(idname, editor, **props):
    RECORDER.pending_ops.append({
        "idname": idname, "name": idname, "props": props, "set_props": list(props),
        "class": ophistory.classify(idname), "editor": editor, "t": RECORDER.now()})


# ------------------------------------------------------------ scene
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
arm_data = bpy.data.armatures.new("Rig")
arm = bpy.data.objects.new("Character", arm_data)
bpy.context.scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
arm.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
prev = None
for i, name in enumerate(("Hips", "Thigh", "Shin", "Foot")):
    eb = arm_data.edit_bones.new(name)
    eb.head = (0, 0, 1.0 - i * 0.3)
    eb.tail = (0, 0, 0.7 - i * 0.3)
    if prev:
        eb.parent = prev
        eb.use_connect = True
    prev = eb
bpy.ops.object.mode_set(mode='POSE')
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 48
random.seed(7)
for pb in arm.pose.bones:
    pb.rotation_mode = 'XYZ'
    pb["ik_fk"] = 0.0
for f in range(1, 49, 2):          # noisy mocap-like curves with a foot slide
    for pb in arm.pose.bones:
        pb.rotation_euler = (math.sin(f / 6) * 0.4 + random.uniform(-0.08, 0.08), 0,
                             random.uniform(-0.05, 0.05))
        pb.keyframe_insert("rotation_euler", frame=f)
    foot = arm.pose.bones["Foot"]
    foot.location = (0, 0.01 * f if 20 <= f <= 32 else 0, 0)
    foot.keyframe_insert("location", frame=f)
scene.frame_set(1)
for b in arm_data.bones:
    b.select = False

# ------------------------------------------------------------ record
ep = RECORDER.start(OUT, {"task": "e2e test: fix jitter + foot slide", "character": "TestRig"},
                    use_handlers=False)
print("recording to", ep)


def select_bone(name):
    for b in arm_data.bones:
        b.select = b.name == name
    arm_data.bones.active = arm_data.bones[name]


def select_keys(bone, lo, hi, only=None):
    for fc in arm.animation_data.action.fcurves:
        on = f'"{bone}"' in fc.data_path and (only is None or only in fc.data_path)
        fc.select = on
        for kp in fc.keyframe_points:
            kp.select_control_point = on and lo <= kp.co[0] <= hi
    commit_selection()


def commit_selection():
    """In the UI, box/click select are registered operators, so the recorder
    commits them as a (context-op) step.  Mimic that."""
    log_op("graph.select_box", "GRAPH_EDITOR")
    RECORDER.capture()


# 1 context: look at the thigh around frame 10 (selection ops are context ops)
select_bone("Thigh")
log_op("pose.select_all", "VIEW_3D", action='DESELECT')
scene.frame_set(10)
RECORDER.capture()

# 2 rotate the thigh (a GUI drag) -> unkeyed pose edit
arm.pose.bones["Thigh"].rotation_euler[0] -= 0.3   # what transform.rotate(value=0.3, X, LOCAL) really does
log_op("transform.rotate", "VIEW_3D", value=0.3, orient_axis='X', orient_type='LOCAL')
RECORDER.capture()

# 3 key it
run_op("anim.keyframe_insert_by_name", "VIEW_3D", type="Rotation")
RECORDER.capture()

# 4 annotate the problem the artist sees
RECORDER.annotate("jitter", "thigh rotation noisy 5-30", (5, 30), ["Thigh"])

# 5 smooth selected thigh keys in the graph editor
select_keys("Thigh", 5, 30)
run_op("graph.smooth", "GRAPH_EDITOR")
RECORDER.capture()

# 6 interpolation -> linear
run_op("graph.interpolation_type", "GRAPH_EDITOR", type='LINEAR')
RECORDER.capture()

# 7 typed a value in the N panel (no operator)
fc = arm.animation_data.action.fcurves.find('pose.bones["Thigh"].rotation_euler', index=0)
fc.keyframe_points[3].co[1] = 0.123
fc.keyframe_points[3].handle_left[1] = 0.123
fc.keyframe_points[3].handle_right[1] = 0.123
RECORDER.capture()

# 8 foot slide: delete the sliding foot location keys 21..31
RECORDER.annotate("foot_sliding", "left foot slides 20-32", (20, 32), ["Foot"])
select_bone("Foot")
log_op("pose.select_all", "VIEW_3D", action='DESELECT')
select_keys("Foot", 21, 31, only="location")
run_op("graph.delete", "GRAPH_EDITOR")
RECORDER.capture()

# 9 an undo (data reverts to before step 8's last tweak: simulate by re-adding a key)
RECORDER.tags.add("undo")
fc = arm.animation_data.action.fcurves.find('pose.bones["Foot"].location', index=1)
fc.keyframe_points.insert(25, 0.0)
RECORDER.capture()

# 10 go to frame 25, clear the foot pose, key it
scene.frame_set(25)
RECORDER.capture()
run_op("pose.transforms_clear", "VIEW_3D")
RECORDER.capture()
run_op("anim.keyframe_insert_by_name", "VIEW_3D", type="Location")
RECORDER.capture()

# 11 euler filter across all rotation curves
select_bone("Thigh")
select_keys("Thigh", -1e9, 1e9, only="rotation_euler")
run_op("graph.euler_filter", "GRAPH_EDITOR")
RECORDER.capture()

# 12 custom property switch (IK/FK) without operator
arm.pose.bones["Foot"]["ik_fk"] = 1.0
RECORDER.capture()

ep = RECORDER.stop()
print("episode:", ep)

# ------------------------------------------------------------ replay
replay = os.path.join(ROOT, "tools", "replay.py")
results = {}
for mode in ("state", "ops"):
    out = os.path.join(ep, f"replay_{mode}.json")
    p = subprocess.run([bpy.app.binary_path, "-b", "--factory-startup", os.path.join(ep, "initial.blend"),
                        "--python", replay, "--", ep, "--mode", mode, "--report", out],
                       capture_output=True, text=True)
    tail = [ln for ln in p.stdout.splitlines() if not ln.startswith(("Blender", "Read blend", "Writing"))]
    print("\n".join(tail[-30:]))
    if p.returncode not in (0, 1):
        print(p.stderr[-3000:])
    with open(out) as f:
        results[mode] = json.load(f)

st, op = results["state"], results["ops"]
fails = []
if not st["final_authored_match"]:
    fails.append("state replay final mismatch")
if st["summary"]["state_post_match"] != st["summary"]["steps"]:
    bad = [e["i"] for e in st["steps"] if not (e["post_authored_match"] and e["post_working_match"])]
    fails.append(f"state replay per-step mismatch at steps {bad}")
if not op["final_authored_match"]:
    fails.append("ops replay final mismatch")
bad_ops = [e for e in op["steps"] if e["status"] == "diverged"]
if bad_ops:
    fails.append(f"ops replay diverged: {[(e['i'], e['ops'], e.get('problems', [])[:2]) for e in bad_ops]}")
# translate the recording into Action API calls and verify them step by step
p = subprocess.run([bpy.app.binary_path, "-b", "--factory-startup", os.path.join(ep, "initial.blend"),
                    "--python", os.path.join(ROOT, "tools", "translate_episode.py"), "--", ep],
                   capture_output=True, text=True)
print("\n".join(ln for ln in p.stdout.splitlines() if ln.startswith(("steps verified", "size:", "actions:", "  step"))))
with open(os.path.join(ep, "translate_report.json")) as f:
    tr = json.load(f)
if tr["verified"] != tr["steps"]:
    fails.append(f"translation verified only {tr['verified']}/{tr['steps']} steps")
with open(os.path.join(ep, "steps.jsonl")) as f:
    kinds = [json.loads(line)["kind"] for line in f]
print("record kinds:", {k: kinds.count(k) for k in set(kinds)})
print("TEST", "FAILED: " + "; ".join(fails) if fails else "PASSED")
sys.exit(1 if fails else 0)
