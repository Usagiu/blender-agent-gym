"""Tests for the review tool (background).

  blender -b --factory-startup --python tests/test_review.py -- OUT_DIR

Records the e2e episode (tests/test_e2e.py, which also translates it), then
opens it for review through the operators, navigates, annotates, reopens, and
builds training samples.
"""

import glob
import hashlib
import json
import os
import subprocess
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import blender_action_recorder as ar  # noqa: E402
from blender_action_recorder import review_ui  # noqa: E402

OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "/tmp/ar_review"
FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


# ------------------------------------------------------------ make an episode
os.makedirs(OUT, exist_ok=True)
p = subprocess.run([bpy.app.binary_path, "-b", "--factory-startup", "--python",
                    os.path.join(ROOT, "tests", "test_e2e.py"), "--", OUT], capture_output=True, text=True)
check("TEST PASSED" in p.stdout, "e2e episode recorded and translated")
ep = sorted(glob.glob(os.path.join(OUT, "episode_*")))[-1]
untouched = {f: md5(os.path.join(ep, f)) for f in ("initial.blend", "steps.jsonl", "state_initial.json")}

ar.register()

print("open")
bpy.ops.action_recorder.review_open('EXEC_DEFAULT', directory=ep)
S = review_ui.SESSION
check(S is not None and S.n_steps == 15, "episode opened for review via operator (15 steps)")
check(os.path.basename(bpy.data.filepath) == "review.blend", "working on review.blend, not initial.blend")
st = bpy.context.scene.ar_review
kinds = [r.kind for r in st.rows]
check(kinds.count("step") == 15 and kinds.count("diagnosis") == 2 and kinds[0] == "initial",
      f"list has initial + 15 steps + 2 recorded diagnoses ({len(kinds)} rows)")

print("navigate (exact state after every jump)")
for target in (5, 2, 0, 15, 7, 1, 15):
    S.goto(target)
    check(S.verify_current(), f"goto({target}) matches recorded state")
row_of_9 = next(i for i, r in enumerate(st.rows) if r.kind == "step" and r.step == 9)
st.active_row = row_of_9
check(S.cur == 9 and st.showing == 9 and S.verify_current(), "selecting a list row shows that step")
bpy.ops.action_recorder.review_before_after()
check(S.cur == 8 and st.showing_before, "before/after toggle shows the state before the step")
bpy.ops.action_recorder.review_before_after()
check(S.cur == 9 and not st.showing_before, "toggle back to after")
bpy.ops.action_recorder.review_step(delta=1)
check(S.cur == 10 and st.showing == 10, "next step button")
bpy.ops.action_recorder.review_step(delta=-2)
check(S.cur == 8, "previous step button")

print("annotate")
pre = S.prefill(4, 6)
check(pre.get("label") == "jitter" and pre["bones"] == ["Thigh"] and pre["frame_range"] == [5, 30],
      f"prefill picks up the diagnosis written during recording: {pre}")
pre2 = S.prefill(9, 9)
check(pre2["bones"] == ["Foot"] and pre2["frame_range"] is not None, f"prefill from recorded changes: {pre2}")

st.active_row = next(i for i, r in enumerate(st.rows) if r.kind == "step" and r.step == 10)
bpy.ops.action_recorder.review_flag(flag='mistake')
st.seg_start, st.seg_end = 4, 7
r = bpy.ops.action_recorder.review_add_segment(
    'EXEC_DEFAULT', step_start=4, step_end=7, label='jitter', problem="大腿旋转在 5-30 帧抖动",
    cause="动捕噪声", method="选中关键帧平滑后改线性插值，再手调第 7 帧", use_frames=True,
    frame_start=5, frame_end=30, bones="Thigh")
check(r == {'FINISHED'}, "add segment (Chinese text)")
r = bpy.ops.action_recorder.review_add_segment(
    'EXEC_DEFAULT', step_start=8, step_end=12, label='foot_sliding', problem="左脚 20-32 帧滑步",
    cause="落脚后脚的位移没锁住", method="删掉滑动段的位移关键帧，在 25 帧补一个归零的帧", use_frames=True,
    frame_start=20, frame_end=32, bones="Foot")
check(r == {'FINISHED'}, "add second segment")


def refused(**kw):
    """An operator that reports an error raises RuntimeError when called from Python."""
    try:
        return bpy.ops.action_recorder.review_add_segment('EXEC_DEFAULT', **kw) == {'CANCELLED'}
    except RuntimeError:
        return True


check(refused(step_start=14, step_end=14, label='flip', problem="", bones="Thigh"),
      "segment without a problem description is refused")
check(refused(step_start=14, step_end=99, label='flip', problem="x"), "segment beyond the last step is refused")
check(len(S.data["segments"]) == 2, "refused segments were not saved")
bpy.ops.action_recorder.review_add_segment('EXEC_DEFAULT', step_start=14, step_end=14, label='flip',
                                           problem="temp")
st.active_segment = len(st.segments) - 1
check(S.cur == 14, "selecting a segment jumps to its last step")
sid = st.segments[st.active_segment].sid
bpy.ops.action_recorder.review_edit_segment('EXEC_DEFAULT', sid=sid, step_start=14, step_end=14, label='flip',
                                            problem="欧拉角翻转", cause="", method="euler filter", bones="Thigh")
seg = next(s for s in S.data["segments"] if s["id"] == sid)
check(seg["problem"] == "欧拉角翻转" and seg["method"] == "euler filter", "edit segment")
bpy.ops.action_recorder.review_delete_segment('EXEC_DEFAULT')
check(len(S.data["segments"]) == 2, "delete segment")
bpy.ops.action_recorder.review_summary('EXEC_DEFAULT', text="修了大腿抖动和左脚滑步", rating=4, usable=True)

with open(os.path.join(ep, "review.json"), encoding="utf-8") as f:
    saved = json.load(f)
check(len(saved["segments"]) == 2 and saved["step_flags"] == {"10": "mistake"}
      and saved["summary"]["rating"] == 4, "review.json saved")
check(S.coverage()["uncovered"] == [2, 3, 14, 15], f"coverage lists un-annotated edit steps: {S.coverage()['uncovered']}")

print("reopen")
bpy.ops.action_recorder.review_open('EXEC_DEFAULT', directory=ep)
S2 = review_ui.SESSION
st = bpy.context.scene.ar_review
check(S2 is not S and len(S2.data["segments"]) == 2 and len(st.segments) == 2, "annotations load on reopen")
row10 = next(r for r in st.rows if r.kind == "step" and r.step == 10)
check(row10.flag == "mistake", "step flag shown in the list after reopen")
check(all(md5(os.path.join(ep, f)) == h for f, h in untouched.items()), "recording files are untouched")

print("samples")
out = os.path.join(OUT, "samples.jsonl")
p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_samples.py"), ep, "-o", out],
                   capture_output=True, text=True)
print("   ", p.stdout.strip().replace("\n", "\n    "))
samples = [json.loads(line) for line in open(out, encoding="utf-8")]
check(len(samples) == 2, "one sample per segment")
s0 = samples[0]
check(s0["label"] == "jitter" and s0["recorded_diagnosis"] and s0["recorded_diagnosis"][0]["label"] == "jitter",
      "sample carries the recorded diagnosis")
names = [c["name"] for a in s0["actions"] for c in a["calls"]]
check("set_keys" in names and "set_key_props" in names, f"sample has the API calls of its steps: {names}")
check(all(a["calls"] for a in s0["actions"]), "pure selection steps are dropped from samples")
s1 = samples[1]
check(any(a["step"] == 10 and a["flag"] == "mistake" for a in s1["actions"]), "mistake flag carried into sample")
check(all(s["verified"] for s in samples), "all sample steps verified")

ar.unregister()
print("TEST", "FAILED: " + "; ".join(FAILS) if FAILS else "PASSED")
sys.exit(1 if FAILS else 0)
