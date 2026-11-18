"""Post-recording review: step through an episode and annotate it.

The recording itself is never modified.  Annotations go to EP/review.json:

{
  "version": 1, "reviewer": "...", "updated_at": "...",
  "segments": [{"id": 1, "steps": [4, 9], "label": "foot_sliding",
                "problem": "...", "cause": "...", "method": "...",
                "frame_range": [20, 32], "bones": ["foot.L"]}],
  "step_flags": {"7": "mistake"},          # normal steps are not listed
  "summary": {"text": "...", "rating": 4, "usable": true}
}

Step 0 is the initial state; step i is the state after recorded step i.
"""

import json
import os
import time

import bpy

from . import diff as df
from . import snapshot as snap
from .episode import load_delta, load_episode

REVIEW_FILE = "review.json"
FLAGS = ("normal", "exploration", "mistake")


SHORT_NAMES = {
    "anim.keyframe_insert": "insert key", "anim.keyframe_insert_by_name": "insert key",
    "anim.keyframe_insert_menu": "insert key", "graph.keyframe_insert": "insert key",
    "action.keyframe_insert": "insert key", "anim.keyframe_delete": "delete key",
    "anim.keyframe_delete_v3d": "delete key", "anim.keyframe_delete_by_name": "delete key",
    "anim.keyframe_clear_v3d": "clear keys", "graph.delete": "delete keys", "action.delete": "delete keys",
    "graph.smooth": "smooth", "graph.clean": "clean", "action.clean": "clean", "graph.decimate": "decimate",
    "graph.interpolation_type": "interpolation", "action.interpolation_type": "interpolation",
    "graph.handle_type": "handle type", "action.handle_type": "handle type",
    "graph.easing_type": "easing", "action.easing_type": "easing", "graph.euler_filter": "euler filter",
    "graph.copy": "copy keys", "graph.paste": "paste keys", "action.copy": "copy keys",
    "action.paste": "paste keys", "graph.sample": "sample", "graph.bake_keys": "bake",
    "graph.duplicate_move": "duplicate keys", "action.duplicate_move": "duplicate keys",
    "pose.transforms_clear": "clear pose", "pose.rot_clear": "clear rotation", "pose.loc_clear": "clear location",
    "pose.scale_clear": "clear scale", "pose.copy": "copy pose", "pose.paste": "paste pose",
    "pose.breakdown": "breakdown", "pose.push": "push", "pose.relax": "relax", "pose.blend_to_neighbor": "blend",
}
_SELECT_SUFFIXES = ("select_all", "select_box", "clickselect", "select", "select_lasso", "select_circle",
                    "select_linked", "select_more", "select_less", "select_leftright", "select_column")
_KEY_EDITORS = ("GRAPH_EDITOR", "DOPESHEET_EDITOR", "NLA_EDITOR")


def short_op(op):
    idname = op["idname"]
    if idname.startswith("transform."):
        verb = {"translate": "move", "rotate": "rotate", "resize": "scale"}.get(idname[10:], idname[10:])
        name = f"{verb} keys" if op.get("editor") in _KEY_EDITORS else verb
    else:
        name = SHORT_NAMES.get(idname) or idname.split(".", 1)[-1].replace("_", " ")
    return name + ("*" if op.get("adjusted") else "")


def describe_step(rec):
    """One-line description of a recorded step for lists."""
    ops = [short_op(o) for o in rec["ops"] if not o["idname"].endswith(_SELECT_SUFFIXES)]
    summ = rec["summary"]
    frame_only = rec.get("frame_changed") and not summ["by_bone"] and not summ["objects_changed"]
    if not ops:
        if frame_only:
            return f"frame {rec['ctx_before']['frame']} → {rec['ctx_after']['frame']}"
        ops = ["select"] if rec["ops"] else ["direct edit"]
    tags = f" [{','.join(rec['tags'])}]" if rec["tags"] else ""
    parts = []
    for bone, s in summ["by_bone"].items():
        fr = s["frame_range"]
        parts.append(f"{bone} {int(fr[0])}-{int(fr[1])}" if fr else bone)
    if not parts and not frame_only:
        for oname, bones in summ["pose_bones_changed"].items():
            parts.append("pose " + ",".join(bones[:3]) + ("…" if len(bones) > 3 else ""))
    return f"{' + '.join(ops)}{tags}  {'; '.join(parts)}".strip()


class ReviewSession:
    def __init__(self, ep_dir, reviewer=""):
        self.dir = os.path.abspath(ep_dir)
        self.meta, records = load_episode(self.dir)
        self.steps = [r for r in records if r["kind"] == "step"]
        self.diagnoses = [r for r in records if r["kind"] == "annotation"]
        with open(os.path.join(self.dir, "state_initial.json"), encoding="utf-8") as f:
            self.initial = json.load(f)
        self.cur = None            # index of the state currently shown (None = unknown)
        self.data = self._load_review(reviewer)

    # ------------------------------------------------------------ persistence
    def _load_review(self, reviewer):
        path = os.path.join(self.dir, REVIEW_FILE)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"version": 1, "segments": [], "step_flags": {}, "summary": {"text": "", "rating": 0,
                                                                                "usable": True}}
        if reviewer:
            data["reviewer"] = reviewer
        return data

    def save(self):
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        path = os.path.join(self.dir, REVIEW_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------ navigation
    @property
    def n_steps(self):
        return len(self.steps)

    def step(self, i):
        """Recorded step record i (1-based)."""
        return self.steps[i - 1]

    def reset_to_initial(self):
        """Make the scene equal to state_initial.json (rewrites only what differs)."""
        init = self.initial
        cur = snap.take_snapshot()
        for key in set(cur.fcurves) - set(init["fcurves"]):
            snap.write_fcurve(key, None)
        for key, fj in init["fcurves"].items():
            want = snap.FCurveState.from_json(fj)
            have = cur.fcurves.get(key)
            if have is None or have.digest != want.digest:
                snap.write_fcurve(key, want)
        from . import compat
        for name, a in init["obj_authored"].items():
            obj = bpy.data.objects.get(name)
            if obj is not None and cur.obj_authored.get(name) != a:
                compat.assign_action(obj, a["action"])
        if cur.scene != init["scene"]:
            sc = bpy.context.scene
            for k, v in init["scene"].items():
                setattr(sc.render if k in ("fps", "fps_base") else sc, k, v)
        sc = bpy.context.scene
        if sc.frame_current != init["frame"]:
            sc.frame_set(init["frame"])
        for name, vals in init["obj_working"].items():
            obj = bpy.data.objects.get(name)
            if obj is not None:
                snap.write_object_working(obj, vals)
        for name, bones in init["poses"].items():
            obj = bpy.data.objects.get(name)
            if obj is not None and obj.type == 'ARMATURE':
                snap.write_pose_bones(obj, bones)
        self.cur = 0

    def goto(self, i):
        """Show the state after step i (0 = initial)."""
        i = max(0, min(int(i), self.n_steps))
        if self.cur is None or i < self.cur:
            self.reset_to_initial()
        for k in range(self.cur + 1, i + 1):
            rec = self.step(k)
            df.apply_delta(load_delta(self.dir, rec), frame_after=rec["ctx_after"]["frame"])
        self.cur = i
        return i

    def verify_current(self):
        """True if the scene matches the recorded hashes for the current step."""
        s = snap.take_snapshot()
        if self.cur == 0:
            return s.authored_hash == self.initial["authored_hash"]
        rec = self.step(self.cur)
        return s.authored_hash == rec["authored_hash_after"] and s.working_hash == rec["working_hash_after"]

    # ------------------------------------------------------------ annotations
    def flag(self, i):
        return self.data["step_flags"].get(str(i), "normal")

    def set_flag(self, i, flag):
        if flag not in FLAGS:
            raise ValueError(flag)
        if flag == "normal":
            self.data["step_flags"].pop(str(i), None)
        else:
            self.data["step_flags"][str(i)] = flag
        self.save()

    def segments_of(self, i):
        return [s for s in self.data["segments"] if s["steps"][0] <= i <= s["steps"][1]]

    def prefill(self, a, b):
        """Suggested frame range, bones and text for a segment over steps a..b,
        seeded from the recorded changes and the nearest recorded diagnosis."""
        a, b = sorted((a, b))
        frames, bones = [], []
        for i in range(a, b + 1):
            summ = self.step(i)["summary"]
            for bone, s in summ["by_bone"].items():
                if bone not in bones:
                    bones.append(bone)
                if s["frame_range"]:
                    frames += s["frame_range"]
            for _, bl in summ["pose_bones_changed"].items():
                for bn in bl:
                    if bn not in bones and not summ["by_bone"]:
                        bones.append(bn)
        out = {"frame_range": [int(min(frames)), int(max(frames))] if frames else None, "bones": bones}
        # recorded diagnosis written just before / inside this range
        cands = [d for d in self.diagnoses if a - 1 <= d.get("after_step", -1) <= b]
        if cands:
            d = cands[-1]
            out.update(label=d["label"], problem=d.get("note", ""), method=d.get("plan", ""))
            if d.get("frame_range"):
                out["frame_range"] = d["frame_range"]
            if d.get("bones"):
                out["bones"] = d["bones"]
        return out

    def add_segment(self, a, b, label, problem="", cause="", method="", frame_range=None, bones=()):
        a, b = sorted((int(a), int(b)))
        if not (1 <= a <= b <= self.n_steps):
            raise ValueError(f"steps must be within 1..{self.n_steps}")
        sid = max([s["id"] for s in self.data["segments"]] + [0]) + 1
        seg = {"id": sid, "steps": [a, b], "label": label, "problem": problem, "cause": cause,
               "method": method, "frame_range": list(frame_range) if frame_range else None,
               "bones": list(bones)}
        self.data["segments"].append(seg)
        self.data["segments"].sort(key=lambda s: (s["steps"][0], s["id"]))
        self.save()
        return seg

    def update_segment(self, sid, **fields):
        for s in self.data["segments"]:
            if s["id"] == sid:
                s.update({k: v for k, v in fields.items() if v is not None})
                if "steps" in fields:
                    s["steps"] = sorted(s["steps"])
                self.save()
                return s
        raise KeyError(sid)

    def delete_segment(self, sid):
        self.data["segments"] = [s for s in self.data["segments"] if s["id"] != sid]
        self.save()

    def set_summary(self, text=None, rating=None, usable=None):
        summ = self.data["summary"]
        if text is not None:
            summ["text"] = text
        if rating is not None:
            summ["rating"] = int(rating)
        if usable is not None:
            summ["usable"] = bool(usable)
        self.save()

    def coverage(self):
        """Steps not covered by any segment and not flagged (to nudge the reviewer)."""
        covered = set()
        for s in self.data["segments"]:
            covered.update(range(s["steps"][0], s["steps"][1] + 1))
        def edits(rec):   # same rule as tools/build_samples.py: a frame change alone is not an edit
            summ = rec["summary"]
            return bool(summ["by_bone"] or summ["objects_changed"] or summ["scene_changed"]
                        or (summ["pose_bones_changed"] and not rec.get("frame_changed")))
        meaningful = [i for i in range(1, self.n_steps + 1) if edits(self.step(i))]
        return {"segments": len(self.data["segments"]),
                "uncovered": [i for i in meaningful if i not in covered and self.flag(i) == "normal"]}
