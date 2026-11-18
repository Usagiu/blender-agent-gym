"""GUI test of the real recording path with simulated user input.

  blender --factory-startup --enable-event-simulate --python tests/test_gui_events.py -- OUT_DIR

Drives Blender through its keymap like a person would (R X .3 Enter, I),
so operators really land in wm.operators and the add-on's handlers, timer,
event listener and modal-starvation logic are exercised.  Opens a window
(real input is ignored while --enable-event-simulate is on) and quits.
"""

import json
import os
import sys

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import blender_action_recorder as ar  # noqa: E402
from blender_action_recorder.recorder import RECORDER  # noqa: E402

OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "/tmp/ar_gui"
os.makedirs(OUT, exist_ok=True)
RESULT = {}


def view3d():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return win, area, region


def key(win, k, x, y, uni=None):
    win.event_simulate(type=k, value='PRESS', x=x, y=y, unicode=uni)
    win.event_simulate(type=k, value='RELEASE', x=x, y=y)


def script():
    ar.register()
    win, area, region = view3d()
    cx, cy = area.x + area.width // 2, area.y + area.height // 2
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.object.select_all(action='DESELECT')
        bpy.ops.object.armature_add()
        bpy.ops.object.mode_set(mode='POSE')
        bpy.ops.pose.select_all(action='SELECT')
        pb = bpy.context.object.pose.bones[0]
        pb.rotation_mode = 'XYZ'
        pb.keyframe_insert("rotation_euler", frame=1)
        bpy.context.scene.frame_set(10)
    # warm-up: under --enable-event-simulate the first transform of a session
    # never starts (happens with or without the add-on), so burn one first
    win.event_simulate(type='MOUSEMOVE', value='NOTHING', x=cx, y=cy)
    yield 0.3
    key(win, 'R', cx, cy)
    yield 0.2
    key(win, 'ESC', cx, cy)
    yield 0.5
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.action_recorder.start(output_dir=OUT)
    RESULT["listener"] = RECORDER.listener_running
    yield 0.5
    win.event_simulate(type='MOUSEMOVE', value='NOTHING', x=cx, y=cy)
    yield 0.3
    # R X 30 Enter (degrees) -- a modal transform the listener cannot see into
    key(win, 'R', cx, cy)
    yield 0.2
    for i in range(4):                 # mouse drag inside the modal => depsgraph updates, no events for us
        win.event_simulate(type='MOUSEMOVE', value='NOTHING', x=cx + 15 * i, y=cy + 8 * i)
        yield 0.15
    key(win, 'X', cx, cy)
    yield 0.2
    key(win, 'THREE', cx, cy, '3')
    key(win, 'ZERO', cx, cy, '0')
    yield 0.6
    RESULT["steps_during_modal"] = RECORDER.writer.n_steps
    key(win, 'RET', cx, cy)
    yield 0.2
    win.event_simulate(type='MOUSEMOVE', value='NOTHING', x=cx + 5, y=cy)
    yield 1.0
    key(win, 'I', cx, cy)              # insert keyframe (anim.keyframe_insert)
    yield 0.3
    win.event_simulate(type='MOUSEMOVE', value='NOTHING', x=cx, y=cy)
    yield 1.0
    RESULT["rotation_after"] = [round(v, 4) for v in bpy.context.object.pose.bones[0].rotation_euler]
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.action_recorder.stop()
    ep = sorted(d for d in os.listdir(OUT) if d.startswith("episode_"))[-1]
    RESULT["episode"] = os.path.join(OUT, ep)
    yield 0.2


GEN = script()


def tick():
    try:
        return next(GEN)
    except StopIteration:
        ep = RESULT["episode"]
        recs = [json.loads(line) for line in open(os.path.join(ep, "steps.jsonl"))]
        steps = [r for r in recs if r["kind"] == "step"]
        RESULT["steps"] = [{"ops": [o["idname"] for o in r["ops"]], "editor": [o.get("editor") for o in r["ops"]],
                            "summary": r["summary"]["by_bone"] or r["summary"]["pose_bones_changed"]}
                           for r in steps]
        RESULT["n_events"] = sum(1 for _ in open(os.path.join(ep, "events.jsonl")))
        print("GUI_RESULT " + json.dumps(RESULT))
        bpy.ops.wm.quit_blender()
    except Exception:
        import traceback
        traceback.print_exc()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1.0)
