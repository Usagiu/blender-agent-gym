"""Print a readable timeline of an episode (plain Python, no Blender needed).

  python3 tools/summarize.py EPISODE_DIR
"""

import json
import os
import sys
from collections import Counter


def main(ep):
    meta = json.load(open(os.path.join(ep, "meta.json"), encoding="utf-8"))
    recs = [json.loads(line) for line in open(os.path.join(ep, "steps.jsonl"), encoding="utf-8") if line.strip()]
    print(f"# {os.path.basename(os.path.normpath(ep))}")
    print(f"task: {meta.get('task')!r}  character: {meta.get('character')!r}  blender {meta.get('blender_version')}")
    print(f"counts: {meta.get('counts')}  duration: {meta.get('duration_s')} s  stop: {meta.get('stop_reason')}")
    ops = Counter()
    for r in recs:
        t = f"{r['t']:8.2f}s"
        if r["kind"] == "annotation":
            print(f"{t}  ★ {r['label']} frames {r['frame_range']} bones {r['bones']}  {r['note']}")
        elif r["kind"] == "context":
            c = r["ctx"]
            bones = {o: b["selected"] for o, b in c.get("bones", {}).items() if b["selected"]}
            print(f"{t}  · frame {c['frame']} mode {c['mode']} bones {bones}")
        else:
            names = [o["idname"] + ("*" if o.get("adjusted") else "") for o in r["ops"]] or ["<no operator>"]
            ops.update(o["idname"] for o in r["ops"])
            tags = f" [{','.join(r['tags'])}]" if r["tags"] else ""
            changes = []
            for bone, s in r["summary"]["by_bone"].items():
                parts = [f"{k}{s[k]}" for k, sym in (("added", "+"), ("removed", "-"), ("modified", "~")) if s[k]]
                changes.append(f"{bone}({' '.join(parts)} @{s['frame_range']})")
            pose = r["summary"]["pose_bones_changed"]
            if pose and not changes:
                changes.append(f"pose {pose}")
            print(f"{t}  #{r['i']:<3} {', '.join(names)}{tags}  frame {r['ctx_before']['frame']}  "
                  f"{'; '.join(changes)[:160]}")
    print("operators:", dict(ops.most_common()))


if __name__ == "__main__":
    main(sys.argv[1])
