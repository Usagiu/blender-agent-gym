"""Replay an episode and verify it reproduces the recorded result.

Modes
  state  apply every recorded delta (L3).  Must reproduce the final state
         bit-exactly; validates that the recording is complete & consistent.
  ops    restore each step's context and re-execute its operators (L2), compare
         against the recorded delta with tolerance, then resync to the recorded
         state so later steps are independent.  Measures how much of the human
         trajectory is reproducible as *actions* -- the number that matters for
         learning.  Operators needing a drawn region (transform.*) only run in
         GUI mode; in background they fall back to the state delta.

Usage
  blender -b EP/initial.blend --python tools/replay.py -- EP [--mode state|ops] [--report r.json]
  blender    EP/initial.blend --python tools/replay.py -- EP --mode ops     # GUI mode, quits when done
"""

import argparse
import json
import os
import sys
import traceback

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blender_action_recorder import context_capture as cc  # noqa: E402
from blender_action_recorder import diff as df  # noqa: E402
from blender_action_recorder import ophistory, compat  # noqa: E402
from blender_action_recorder import snapshot as snap  # noqa: E402
from blender_action_recorder.episode import load_delta, load_episode  # noqa: E402


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("episode")
    p.add_argument("--mode", choices=("state", "ops"), default="state")
    p.add_argument("--report", default=None)
    p.add_argument("--atol", type=float, default=1e-4)
    return p.parse_args(argv)


def _update():
    bpy.context.view_layer.update()


def replay(ep_dir, mode="state", atol=1e-4):
    meta, records = load_episode(ep_dir)
    with open(os.path.join(ep_dir, "state_initial.json"), encoding="utf-8") as f:
        initial = json.load(f)
    final = None
    final_path = os.path.join(ep_dir, "state_final.json")
    if os.path.exists(final_path):
        with open(final_path, encoding="utf-8") as f:
            final = json.load(f)

    scene = bpy.context.scene
    wm = bpy.context.window_manager
    window = wm.windows[0] if wm.windows else None

    s0 = snap.take_snapshot()
    report = {
        "episode": os.path.basename(os.path.normpath(ep_dir)), "mode": mode,
        "background": bpy.app.background, "blender": bpy.app.version_string,
        "recorded_with": meta.get("blender_version"),
        "initial_authored_match": s0.authored_hash == initial["authored_hash"],
        "steps": [], "annotations": [r for r in records if r["kind"] == "annotation"],
    }

    for rec in records:
        if rec["kind"] != "step":
            continue
        delta = load_delta(ep_dir, rec)
        runnable = [o for o in rec["ops"] if ophistory.classify(o["idname"]) != "context"]
        frame_before = rec["ctx_before"]["frame"]
        frame_after = rec["ctx_after"]["frame"]
        use_ops = (mode == "ops" and runnable and not rec["tags"]
                   and not any(o.get("adjusted") for o in runnable))

        # 1. bring the scene to the recorded pre-step context (selection, mode,
        #    frame -> re-evaluated pose) BEFORE taking the reference snapshot
        if use_ops:
            editor = runnable[0].get("editor") or ophistory.infer_editor(runnable[0]["idname"])
            area = compat.find_area(window, ophistory.infer_editor(runnable[0]["idname"], editor))[0] \
                if window else None
            cc.restore_context(rec["ctx_before"], override_area=area)
        elif scene.frame_current != frame_before:
            scene.frame_set(frame_before)

        before = snap.take_snapshot()
        entry = {
            "i": rec["i"], "ops": [o["idname"] for o in rec["ops"]], "tags": rec["tags"],
            "pre_authored_match": before.authored_hash == rec["authored_hash_before"],
            "pre_working_match": before.working_hash == rec["working_hash_before"],
        }

        # 2. execute
        if use_ops:
            results = []
            for o in runnable:
                status, msg = ophistory.run_operator(o, window, o.get("editor"))
                results.append({"op": o["idname"], "status": status, "msg": msg})
            _update()
            actual = snap.take_snapshot()
            problems = df.compare_to_expected(before, actual, delta, atol=atol)
            if any(r["status"] != "ok" for r in results):
                entry["status"] = "fallback"
            else:
                entry["status"] = "match" if not problems else "diverged"
            entry["op_results"] = results
            if problems:
                entry["problems"] = problems[:20]
            # resync to the recorded result so later steps start from truth
            df.restore_unexpected(before, actual, delta)
            df.apply_delta(delta, frame_after=frame_after)
        else:
            df.apply_delta(delta, frame_after=frame_after)
            if mode == "ops":
                entry["status"] = "undo_redo" if rec["tags"] else (
                    "adjusted" if runnable else "no_operator")
            else:
                entry["status"] = "state_applied"

        post = snap.take_snapshot()
        entry["post_authored_match"] = post.authored_hash == rec["authored_hash_after"]
        entry["post_working_match"] = post.working_hash == rec["working_hash_after"]
        report["steps"].append(entry)

    end = snap.take_snapshot()
    report["final_authored_match"] = (final is not None and end.authored_hash == final["authored_hash"])

    steps = report["steps"]
    by_status = {}
    for e in steps:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
    op_stats = {}
    for e in steps:
        for r in e.get("op_results", ()):
            s = op_stats.setdefault(r["op"], {})
            s[r["status"]] = s.get(r["status"], 0) + 1
    attempted = sum(by_status.get(k, 0) for k in ("match", "diverged", "fallback"))
    report["summary"] = {
        "steps": len(steps),
        "by_status": by_status,
        "state_post_match": sum(e["post_authored_match"] and e["post_working_match"] for e in steps),
        "op_fidelity": round(by_status.get("match", 0) / attempted, 4) if attempted else None,
        "op_coverage": round(attempted / len(steps), 4) if steps else None,
        "by_operator": op_stats,
    }
    return report


def _print(report):
    s = report["summary"]
    print("=" * 60)
    print(f"episode {report['episode']}  mode={report['mode']}  background={report['background']}")
    print(f"initial state matches recording: {report['initial_authored_match']}")
    print(f"steps: {s['steps']}  status: {s['by_status']}")
    print(f"post-step state exact match: {s['state_post_match']}/{s['steps']}")
    if report["mode"] == "ops":
        print(f"operator fidelity: {s['op_fidelity']}  (steps replayed by operators: {s['op_coverage']})")
        for op, st in sorted(s["by_operator"].items()):
            print(f"   {op:40s} {st}")
        for e in report["steps"]:
            if e["status"] in ("diverged", "fallback"):
                print(f"   step {e['i']} {e['status']}: {e.get('op_results')} {e.get('problems', [])[:3]}")
    print(f"final authored state matches: {report['final_authored_match']}")
    print("=" * 60)


def main():
    args = _args()
    ok = True
    try:
        win = bpy.context.window_manager.windows[0] if bpy.context.window_manager.windows else None
        if win is not None:
            with bpy.context.temp_override(window=win, screen=win.screen):
                report = replay(args.episode, args.mode, args.atol)
        else:
            report = replay(args.episode, args.mode, args.atol)
        _print(report)
        out = args.report or os.path.join(args.episode, f"replay_{args.mode}{'' if bpy.app.background else '_gui'}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print("report:", out)
        ok = report["final_authored_match"]
    except Exception:
        traceback.print_exc()
        ok = False
    return ok


if bpy.app.background:
    sys.exit(0 if main() else 1)
else:
    def _gui_run():
        ok = main()
        bpy.ops.wm.quit_blender() if ok is not None else None
        return None
    bpy.app.timers.register(_gui_run, first_interval=1.0)
