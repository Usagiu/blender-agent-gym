"""The recording loop.

Step boundary heuristic (Python cannot hook Blender's undo push):
  * depsgraph/frame/undo handlers and new entries in ``wm.operators`` mark the
    recorder dirty;
  * a timer commits a step once things have been quiet for DEBOUNCE seconds;
  * while another modal operator (transform drag, slider drag) is consuming
    events, our pass-through event listener is "starved" -- updates keep
    arriving but we see no events -- so we wait for the drag to finish.
"""

import time

import bpy
from bpy.app.handlers import persistent

from . import context_capture as cc
from . import diff as df
from . import ophistory
from . import snapshot as snap
from .episode import EpisodeWriter

DEBOUNCE = 0.3
TICK = 0.1
STARVE_GAP = 0.2     # updates this long after the last seen event => someone else is modal
STARVE_MAX = 5.0     # give up waiting after this much silence
_CTX_COMPARE = ("frame", "mode", "active_object", "selected_objects", "bones", "selected_keys")


class Recorder:
    def __init__(self):
        self.active = False
        self.writer = None
        self.listener_running = False

    # ------------------------------------------------------------ lifecycle
    def start(self, out_root, meta, use_handlers=True):
        if self.active:
            raise RuntimeError("already recording")
        self.writer = EpisodeWriter(out_root, meta)
        self.writer.save_blend("initial.blend")
        self.snap = snap.take_snapshot()
        self.writer.write_state("state_initial.json", self.snap)
        self.wm = bpy.context.window_manager
        self.ops = ophistory.OpWatcher(self.wm)
        self.last_area = None
        self.ctx = cc.capture_context(self.snap)
        self.pending_ops = []
        self.tags = set()
        self.dirty = False
        self.last_update = 0.0
        self.last_event = 0.0
        self.last_move = 0.0
        self.t0 = time.monotonic()
        self.use_handlers = use_handlers
        self.active = True
        if use_handlers:
            self._register()
        return self.writer.dir

    def stop(self, save_final=True, reason="stopped"):
        if not self.active:
            return None
        self.capture()
        final = snap.take_snapshot()
        self.writer.write_state("state_final.json", final)
        if save_final:
            self.writer.save_blend("final.blend")
        self.writer.close(stop_reason=reason, final_authored_hash=final.authored_hash,
                          duration_s=round(time.monotonic() - self.t0, 3), has_final_blend=save_final)
        if self.use_handlers:
            self._unregister()
        self.active = False
        d = self.writer.dir
        self.writer = None
        return d

    def _register(self):
        h = bpy.app.handlers
        h.depsgraph_update_post.append(_on_update)
        h.frame_change_post.append(_on_update)
        h.undo_post.append(_on_undo)
        h.redo_post.append(_on_redo)
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=TICK)

    def _unregister(self):
        h = bpy.app.handlers
        for lst, fn in ((h.depsgraph_update_post, _on_update), (h.frame_change_post, _on_update),
                        (h.undo_post, _on_undo), (h.redo_post, _on_redo)):
            while fn in lst:
                lst.remove(fn)
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)

    # ------------------------------------------------------------ signals
    def now(self):
        return round(time.monotonic() - self.t0, 4)

    def touch(self):
        self.dirty = True
        self.last_update = time.monotonic()

    def on_event(self, ev, area):
        """Called by the pass-through modal listener for every input event."""
        t = time.monotonic()
        self.last_event = t
        if ev["type"] in ("MOUSEMOVE",) and t - self.last_move < 1 / 30 and not ev.get("held"):
            return
        if ev["type"] == "MOUSEMOVE":
            self.last_move = t
        if ev["value"] == "PRESS" and area is not None:
            self.last_area = area
        ev["t"] = self.now()
        self.writer.write_event(ev)

    def _starved(self):
        if not self.listener_running:
            return False
        now = time.monotonic()
        return (self.last_update - self.last_event) > STARVE_GAP and (now - self.last_update) < STARVE_MAX

    def _playing(self):
        return any(w.screen and w.screen.is_animation_playing for w in self.wm.windows)

    def tick(self):
        try:
            new = self.ops.poll(self.wm)
        except ReferenceError:
            new = []
        if new:
            editor = self._area_type()
            for rec in new:
                rec["editor"] = editor
                rec["t"] = self.now()
            self.pending_ops.extend(new)
            self.touch()
        if self._playing():
            return
        if self.dirty and time.monotonic() - self.last_update >= DEBOUNCE and not self._starved():
            self.capture()

    def _area_type(self):
        try:
            return self.last_area.type if self.last_area else None
        except ReferenceError:
            return None

    # ------------------------------------------------------------ capture
    def capture(self):
        """Commit whatever changed since the last capture as one record."""
        if not self.active:
            return None
        if self.use_handlers:
            try:   # catch operators finished since the last tick
                for rec in self.ops.poll(self.wm):
                    rec["editor"] = self._area_type()
                    rec["t"] = self.now()
                    self.pending_ops.append(rec)
            except ReferenceError:
                pass
        new_snap = snap.take_snapshot()
        delta = df.diff(self.snap, new_snap)
        ops, tags = self.pending_ops, sorted(self.tags)
        frame_changed = new_snap.frame != self.snap.frame
        area = None
        try:
            area = self.last_area if self.last_area and self.last_area.type else None
        except ReferenceError:
            pass
        ctx_after = cc.capture_context(new_snap, area)
        authored = df.authored_changed(delta)
        working = df.working_changed(delta)
        rec = None
        if authored or ops or tags or (working and not frame_changed):
            rec = {
                "kind": "step", "t": self.now(), "tags": tags, "ops": ops,
                "ctx_before": self.ctx, "ctx_after": ctx_after,
                "authored_hash_before": self.snap.authored_hash,
                "authored_hash_after": new_snap.authored_hash,
                "working_hash_before": self.snap.working_hash,
                "working_hash_after": new_snap.working_hash,
                "frame_changed": frame_changed,
                "summary": df.summarize(delta),
            }
            self.writer.write_record(rec, delta)
        elif any(ctx_after.get(k) != self.ctx.get(k) for k in _CTX_COMPARE):
            rec = {"kind": "context", "t": self.now(), "ctx": ctx_after,
                   "working_hash_after": new_snap.working_hash}
            self.writer.write_record(rec)
        self.snap = new_snap
        self.ctx = ctx_after
        self.pending_ops = []
        self.tags = set()
        self.dirty = False
        return rec

    def annotate(self, label, note="", frame_range=None, bones=None, plan="", phase="diagnosis"):
        """phase: 'diagnosis' (written during recording, before the fix) or
        'review' (added afterwards over a range of steps)."""
        self.capture()
        rec = {"kind": "annotation", "phase": phase, "t": self.now(), "label": label, "note": note,
               "plan": plan, "frame_range": list(frame_range) if frame_range else None,
               "bones": list(bones or ()), "after_step": self.writer.n_steps}
        self.writer.write_record(rec)
        return rec


RECORDER = Recorder()


@persistent
def _on_update(*_args):
    if RECORDER.active:
        RECORDER.touch()


@persistent
def _on_undo(*_args):
    if RECORDER.active:
        RECORDER.tags.add("undo")
        RECORDER.touch()


@persistent
def _on_redo(*_args):
    if RECORDER.active:
        RECORDER.tags.add("redo")
        RECORDER.touch()


def _tick():
    if not RECORDER.active:
        return None
    try:
        RECORDER.tick()
    except Exception as e:  # never let the timer die silently mid-session
        print("[action_recorder] tick error:", repr(e))
    return TICK


@persistent
def on_load_pre(*_args):
    # loading another file destroys the data we are diffing against
    if RECORDER.active:
        RECORDER.stop(save_final=False, reason="file_load")
