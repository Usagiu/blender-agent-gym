"""On-disk episode format.

episode_<timestamp>/
  meta.json            task / character / versions / timing
  initial.blend        scene before the first recorded step
  final.blend          scene when recording stopped
  state_initial.json   full authored+working state (for replay verification)
  state_final.json
  steps.jsonl          one record per line: kind = step | context | annotation
  deltas/000001.json   full delta (before/after F-Curves, pose) of step 1
  events.jsonl         raw input events (L1)
"""

import json
import os
import time

import bpy

SCHEMA_VERSION = 1


class EpisodeWriter:
    def __init__(self, root, meta):
        root = bpy.path.abspath(root) if root.startswith("//") else os.path.expanduser(root)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.dir = os.path.join(root, f"episode_{stamp}")
        n = 1
        while os.path.exists(self.dir):
            n += 1
            self.dir = os.path.join(root, f"episode_{stamp}_{n}")
        os.makedirs(os.path.join(self.dir, "deltas"))
        self.meta = dict(meta)
        self.meta.update({
            "schema_version": SCHEMA_VERSION,
            "blender_version": bpy.app.version_string,
            "source_file": bpy.data.filepath,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self._write_meta()
        self.steps_f = open(os.path.join(self.dir, "steps.jsonl"), "a", encoding="utf-8")
        self.events_f = open(os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8")
        self.n_records = 0
        self.n_steps = 0
        self.n_events = 0

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def _write_meta(self):
        with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=2)

    def save_blend(self, name):
        path = self.path(name)
        kwargs = dict(filepath=path, copy=True, check_existing=False)
        wm = bpy.context.window_manager
        if bpy.context.window is None and wm.windows:
            with bpy.context.temp_override(window=wm.windows[0]):
                bpy.ops.wm.save_as_mainfile(**kwargs)
        else:
            bpy.ops.wm.save_as_mainfile(**kwargs)
        return path

    def write_state(self, name, snapshot):
        with open(self.path(name), "w", encoding="utf-8") as f:
            json.dump(snapshot.to_json(), f)

    def write_record(self, rec, delta=None):
        self.n_records += 1
        rec["seq"] = self.n_records
        if delta is not None:
            self.n_steps += 1
            rec["i"] = self.n_steps
            fname = f"{self.n_steps:06d}.json"
            with open(self.path("deltas", fname), "w", encoding="utf-8") as f:
                json.dump(delta, f)
            rec["delta_file"] = f"deltas/{fname}"
        self.steps_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.steps_f.flush()
        self.events_f.flush()

    def write_event(self, ev):
        self.n_events += 1
        self.events_f.write(json.dumps(ev, separators=(",", ":")) + "\n")

    def close(self, **extra):
        self.meta.update(extra)
        self.meta["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.meta["counts"] = {"records": self.n_records, "steps": self.n_steps, "events": self.n_events}
        self._write_meta()
        self.steps_f.close()
        self.events_f.close()


def load_episode(path):
    with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    records = []
    with open(os.path.join(path, "steps.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return meta, records


def load_delta(episode_dir, rec):
    with open(os.path.join(episode_dir, rec["delta_file"]), encoding="utf-8") as f:
        return json.load(f)
