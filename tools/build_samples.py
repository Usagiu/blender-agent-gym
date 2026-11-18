"""Merge reviewed episodes into training samples (plain Python, no Blender).

  python3 tools/build_samples.py EPISODE_DIR_OR_ROOT [...] -o samples.jsonl [--include-unusable]

Each episode needs review.json (from the review panel) and actions.jsonl
(from tools/translate_episode.py).  One sample per reviewed segment:

  problem / cause / fix written by the reviewer
  + the diagnosis written during recording (if any)
  + the Action API calls of every step in the segment, each with its flag
    (normal / exploration / mistake) so consumers can drop or keep detours
"""

import argparse
import json
import os
import sys


def find_episodes(paths):
    out = []
    for p in paths:
        if os.path.exists(os.path.join(p, "steps.jsonl")):
            out.append(p)
            continue
        for name in sorted(os.listdir(p)):
            d = os.path.join(p, name)
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "steps.jsonl")):
                out.append(d)
    return out


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build(ep, include_unusable=False):
    name = os.path.basename(os.path.normpath(ep))
    rev_path, act_path = os.path.join(ep, "review.json"), os.path.join(ep, "actions.jsonl")
    if not os.path.exists(rev_path):
        return [], f"{name}: not reviewed (no review.json)"
    if not os.path.exists(act_path):
        return [], f"{name}: run tools/translate_episode.py first (no actions.jsonl)"
    with open(rev_path, encoding="utf-8") as f:
        review = json.load(f)
    with open(os.path.join(ep, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    summary = review.get("summary", {})
    if not summary.get("usable", True) and not include_unusable:
        return [], f"{name}: marked not usable, skipped"
    actions = load_jsonl(act_path)
    steps = {a["i"]: a for a in actions if a["kind"] == "step"}
    diagnoses = [a for a in actions if a["kind"] == "annotation"]
    flags = review.get("step_flags", {})
    samples = []
    for seg in review.get("segments", []):
        a, b = seg["steps"]
        seg_steps = []
        for i in range(a, b + 1):
            st = steps.get(i)
            if st is None:
                continue
            if not st["calls"] and flags.get(str(i), "normal") == "normal":
                continue        # pure selection / context steps carry no action
            seg_steps.append({"step": i, "flag": flags.get(str(i), "normal"), "human_ops": st["human_ops"],
                              "calls": st["calls"], "verified": st["verified"]})
        samples.append({
            "episode": name,
            "task": meta.get("task"), "character": meta.get("character"),
            "blender": meta.get("blender_version"),
            "segment": seg["id"], "label": seg["label"],
            "problem": seg.get("problem", ""), "cause": seg.get("cause", ""), "fix": seg.get("method", ""),
            "frame_range": seg.get("frame_range"), "bones": seg.get("bones", []),
            "steps": [a, b],
            "recorded_diagnosis": [{k: d.get(k) for k in ("label", "note", "plan", "frame_range", "bones")}
                                   for d in diagnoses if a - 1 <= d.get("after_step", -1) <= b],
            "actions": seg_steps,
            "verified": all(s["verified"] for s in seg_steps),
            "episode_summary": summary.get("text", ""), "episode_rating": summary.get("rating", 0),
            "reviewer": review.get("reviewer", ""),
        })
    covered = {i for s in review.get("segments", []) for i in range(s["steps"][0], s["steps"][1] + 1)}
    def edits(st):
        return any(c["name"] != "set_frame" for c in st["calls"])
    uncovered = [i for i, st in steps.items() if edits(st) and i not in covered
                 and flags.get(str(i), "normal") == "normal"]
    note = f"{name}: {len(samples)} segments"
    if uncovered:
        note += f", {len(uncovered)} steps with edits not in any segment: {uncovered[:12]}"
    return samples, note


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("-o", "--out", default="samples.jsonl")
    ap.add_argument("--include-unusable", action="store_true")
    args = ap.parse_args(argv)
    all_samples = []
    for ep in find_episodes(args.paths):
        samples, note = build(ep, args.include_unusable)
        print(note)
        all_samples += samples
    with open(args.out, "w", encoding="utf-8") as f:
        for s in all_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    unverified = sum(not s["verified"] for s in all_samples)
    print(f"wrote {len(all_samples)} samples to {args.out}" + (f" ({unverified} contain unverified steps)"
                                                                if unverified else ""))
    return all_samples


if __name__ == "__main__":
    main(sys.argv[1:])
