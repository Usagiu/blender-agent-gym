"""Translate a recorded episode into Action API calls and verify them.

  blender -b EP/initial.blend --python tools/translate_episode.py -- EP

Writes EP/actions.jsonl (one line per step / annotation, in order) and
EP/translate_report.json.  Every step's calls are executed on the scene and
compared with the recording; the scene is then resynced to the recorded state.
"""

import json
import os
import sys
import traceback

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blender_action_recorder import api  # noqa: E402,F401  (registers actions)
from blender_action_recorder.api import translate  # noqa: E402
from blender_action_recorder.episode import load_delta, load_episode  # noqa: E402


def run(ep):
    meta, records = load_episode(ep)
    scene = bpy.context.scene
    out_lines, steps = [], []
    agent_frame = scene.frame_current       # the frame the call sequence has put the scene at
    for rec in records:
        if rec["kind"] == "annotation":
            out_lines.append({k: rec[k] for k in rec if k not in ("seq",)})
            continue
        if rec["kind"] != "step":
            continue
        delta = load_delta(ep, rec)
        fb = rec["ctx_before"]["frame"]
        if scene.frame_current != fb:
            scene.frame_set(fb)
        res = translate.translate_step(rec, delta)
        calls = res["calls"]
        # pose edits depend on the current frame: make it explicit in the sequence
        if any(c["name"] == "set_pose" for c in calls) and agent_frame != fb:
            calls.insert(0, {"name": "set_frame", "args": {"frame": fb}})
            agent_frame = fb
        for c in calls:
            if c["name"] == "set_frame":
                agent_frame = c["args"]["frame"]
        delta_chars = os.path.getsize(os.path.join(ep, rec["delta_file"]))
        line = {"kind": "step", "i": rec["i"], "human_ops": [o["idname"] for o in rec["ops"]],
                "tags": rec["tags"], "calls": calls, "verified": res["verified"],
                "compact_channels": res["compact_channels"], "explicit_channels": res["explicit_channels"]}
        if res["problems"] or res["notes"]:
            line["problems"] = res["problems"] + res["notes"]
        out_lines.append(line)
        steps.append({"i": rec["i"], "verified": res["verified"], "calls": len(calls),
                      "call_chars": len(json.dumps(calls, separators=(",", ":"))), "delta_chars": delta_chars,
                      "compact": res["compact_channels"], "explicit": res["explicit_channels"]})

    with open(os.path.join(ep, "actions.jsonl"), "w", encoding="utf-8") as f:
        for line in out_lines:
            f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    by_action = {}
    for line in out_lines:
        for c in line.get("calls", ()):
            by_action[c["name"]] = by_action.get(c["name"], 0) + 1
    rep = {
        "episode": os.path.basename(os.path.normpath(ep)),
        "steps": len(steps),
        "verified": sum(s["verified"] for s in steps),
        "calls": sum(s["calls"] for s in steps),
        "channels_compact": sum(s["compact"] for s in steps),
        "channels_explicit": sum(s["explicit"] for s in steps),
        "call_chars": sum(s["call_chars"] for s in steps),
        "delta_chars": sum(s["delta_chars"] for s in steps),
        "by_action": by_action,
        "per_step": steps,
    }
    with open(os.path.join(ep, "translate_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    print("=" * 60)
    print(f"steps verified {rep['verified']}/{rep['steps']}   calls {rep['calls']}   "
          f"channels compact/explicit {rep['channels_compact']}/{rep['channels_explicit']}")
    print(f"size: calls {rep['call_chars']} chars vs raw deltas {rep['delta_chars']} chars")
    print(f"actions: {by_action}")
    for line in out_lines:
        if line.get("problems"):
            print(f"  step {line['i']} {line['human_ops']}: {line['problems'][:3]}")
    print("=" * 60)
    return rep


if __name__ == "__main__":
    ok = False
    try:
        ep = sys.argv[sys.argv.index("--") + 1]
        rep = run(ep)
        ok = rep["verified"] == rep["steps"]
    except Exception:
        traceback.print_exc()
    sys.exit(0 if ok else 1)
