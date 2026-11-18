"""Blender UI for post-recording review (sidebar > Recorder > Episode Review)."""

import os
import shutil

import bpy
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty,
                       StringProperty)

from .review import FLAGS, ReviewSession, describe_step

LABELS = [
    ("foot_sliding", "Foot sliding", ""), ("jitter", "Jitter / noise", ""), ("flip", "Rotation flip", ""),
    ("penetration", "Penetration", ""), ("pose_error", "Bad pose", ""), ("contact", "Contact / hands", ""),
    ("timing", "Timing", ""), ("balance", "Balance / weight", ""), ("cleanup", "Cleanup", ""),
    ("other", "Other", ""),
]
FLAG_ITEMS = [("normal", "Normal", "Part of the fix", 'CHECKMARK', 0),
              ("exploration", "Exploration", "Trying things out / looking around", 'VIEWZOOM', 1),
              ("mistake", "Mistake", "Wrong move (later undone or redone)", 'X', 2)]
FLAG_ICON = {"normal": 'DOT', "exploration": 'VIEWZOOM', "mistake": 'X'}

SESSION = None          # ReviewSession of the open episode
_PENDING = {}           # episode to attach after the review copy is loaded
_SYNCING = False        # suppress update callbacks while we write props ourselves


def _state(context=None):
    return (context or bpy.context).scene.ar_review


# ------------------------------------------------------------ property groups

def _on_active_row(self, context):
    if _SYNCING or SESSION is None or not (0 <= self.active_row < len(self.rows)):
        return
    row = self.rows[self.active_row]
    _goto(context, row.step, before=False)


def _on_active_segment(self, context):
    if _SYNCING or SESSION is None or not (0 <= self.active_segment < len(self.segments)):
        return
    seg = self.segments[self.active_segment]
    _goto(context, seg.end, before=False, select_row=True)


class ARReviewRow(bpy.types.PropertyGroup):
    step: IntProperty()
    kind: StringProperty()          # step | diagnosis | initial
    text: StringProperty()
    flag: StringProperty(default="normal")
    in_segment: BoolProperty()


class ARReviewSegment(bpy.types.PropertyGroup):
    sid: IntProperty()
    start: IntProperty()
    end: IntProperty()
    text: StringProperty()


class ARReviewState(bpy.types.PropertyGroup):
    rows: CollectionProperty(type=ARReviewRow)
    active_row: IntProperty(update=_on_active_row)
    segments: CollectionProperty(type=ARReviewSegment)
    active_segment: IntProperty(update=_on_active_segment, default=-1)
    showing: IntProperty(description="step whose result is shown")
    showing_before: BoolProperty()
    seg_start: IntProperty(name="From step", min=1, default=1)
    seg_end: IntProperty(name="To step", min=1, default=1)


# ------------------------------------------------------------ sync helpers

def _sync_ui(context):
    """Rebuild list props from SESSION."""
    global _SYNCING
    st = _state(context)
    _SYNCING = True
    try:
        st.rows.clear()
        r = st.rows.add()
        r.step, r.kind, r.text = 0, "initial", "initial state (before any edit)"
        diag_after = {}
        for d in SESSION.diagnoses:
            diag_after.setdefault(d.get("after_step", 0), []).append(d)
        for d in diag_after.get(0, []):
            _add_diag_row(st, 0, d)
        for i in range(1, SESSION.n_steps + 1):
            r = st.rows.add()
            r.step, r.kind = i, "step"
            r.text = f"#{i}  {describe_step(SESSION.step(i))}"
            r.flag = SESSION.flag(i)
            r.in_segment = bool(SESSION.segments_of(i))
            for d in diag_after.get(i, []):
                _add_diag_row(st, i, d)
        st.segments.clear()
        for s in SESSION.data["segments"]:
            item = st.segments.add()
            item.sid, item.start, item.end = s["id"], s["steps"][0], s["steps"][1]
            fr = s.get("frame_range")
            item.text = (f"{s['steps'][0]}-{s['steps'][1]}  {s['label']}"
                         f"{'  f' + str(fr[0]) + '-' + str(fr[1]) if fr else ''}  {s.get('problem', '')}")
        st.seg_start = max(1, min(st.seg_start, SESSION.n_steps or 1))
        st.seg_end = max(1, min(st.seg_end, SESSION.n_steps or 1))
    finally:
        _SYNCING = False


def _add_diag_row(st, step, d):
    r = st.rows.add()
    r.step, r.kind = step, "diagnosis"
    fr = d.get("frame_range")
    r.text = f"{d['label']}{' ' + str(fr[0]) + '-' + str(fr[1]) if fr else ''}: {d.get('note', '')}"


def _goto(context, step, before=False, select_row=False):
    global _SYNCING
    st = _state(context)
    target = step - 1 if (before and step > 0) else step
    SESSION.goto(target)
    st.showing, st.showing_before = step, before and step > 0
    if select_row:
        _SYNCING = True
        try:
            for idx, r in enumerate(st.rows):
                if r.kind == "step" and r.step == step:
                    st.active_row = idx
                    break
        finally:
            _SYNCING = False
    for area in context.screen.areas if context.screen else ():
        area.tag_redraw()


# ------------------------------------------------------------ open / close

@persistent
def _on_load_post(*_args):
    global SESSION
    ep = _PENDING.pop("episode", None)
    if ep is None:
        # a different file was opened: the review session no longer applies
        SESSION = None
        return
    SESSION = ReviewSession(ep, reviewer=_PENDING.pop("reviewer", ""))
    SESSION.cur = 0          # review.blend is a copy of initial.blend
    ctx = bpy.context
    _sync_ui(ctx)
    st = _state(ctx)
    st.showing, st.showing_before = 0, False


class AR_OT_review_open(bpy.types.Operator):
    """Open a recorded episode for review (loads a copy of its initial.blend -- the recording is never changed)"""
    bl_idname = "action_recorder.review_open"
    bl_label = "Open Episode for Review"

    directory: StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        from .recorder import RECORDER
        if RECORDER.active:
            self.report({'ERROR'}, "Stop recording first")
            return {'CANCELLED'}
        ep = os.path.abspath(bpy.path.abspath(self.directory)).rstrip(os.sep)
        if not os.path.exists(os.path.join(ep, "steps.jsonl")):
            parent = os.path.dirname(ep)        # file browser may hand us a file inside the episode
            if os.path.exists(os.path.join(parent, "steps.jsonl")):
                ep = parent
            else:
                self.report({'ERROR'}, "Not an episode folder (no steps.jsonl)")
                return {'CANCELLED'}
        src = os.path.join(ep, "initial.blend")
        copy = os.path.join(ep, "review.blend")     # same folder keeps relative library paths valid
        shutil.copyfile(src, copy)
        addon = context.preferences.addons.get(__package__)
        _PENDING["episode"] = ep
        _PENDING["reviewer"] = addon.preferences.annotator if addon else ""
        bpy.ops.wm.open_mainfile(filepath=copy)
        return {'FINISHED'}


class AR_OT_review_close(bpy.types.Operator):
    """Stop reviewing (annotations are already saved)"""
    bl_idname = "action_recorder.review_close"
    bl_label = "Close Review"

    def execute(self, context):
        global SESSION
        SESSION = None
        st = _state(context)
        st.rows.clear()
        st.segments.clear()
        return {'FINISHED'}


def _need_session(cls):
    cls.poll = classmethod(lambda c, context: SESSION is not None)
    return cls


# ------------------------------------------------------------ navigation

@_need_session
class AR_OT_review_step(bpy.types.Operator):
    """Go to the previous / next step"""
    bl_idname = "action_recorder.review_step"
    bl_label = "Step"
    delta: IntProperty(default=1)

    def execute(self, context):
        st = _state(context)
        _goto(context, max(0, min(SESSION.n_steps, st.showing + self.delta)), select_row=True)
        return {'FINISHED'}


@_need_session
class AR_OT_review_before_after(bpy.types.Operator):
    """Toggle between the scene before and after the current step"""
    bl_idname = "action_recorder.review_before_after"
    bl_label = "Before / After"

    def execute(self, context):
        st = _state(context)
        _goto(context, st.showing, before=not st.showing_before)
        return {'FINISHED'}


@_need_session
class AR_OT_review_preview_frames(bpy.types.Operator):
    """Set the preview range to the frames this step (or segment) changed and jump there"""
    bl_idname = "action_recorder.review_preview_frames"
    bl_label = "Preview Changed Frames"
    segment: BoolProperty(default=False)

    def execute(self, context):
        st = _state(context)
        if self.segment and 0 <= st.active_segment < len(st.segments):
            seg = next(s for s in SESSION.data["segments"] if s["id"] == st.segments[st.active_segment].sid)
            fr = seg.get("frame_range") or SESSION.prefill(*seg["steps"])["frame_range"]
        elif st.showing > 0:
            fr = SESSION.prefill(st.showing, st.showing)["frame_range"]
        else:
            fr = None
        if not fr:
            self.report({'INFO'}, "No keyframe changes here")
            return {'CANCELLED'}
        sc = context.scene
        pad = 5
        sc.use_preview_range = True
        sc.frame_preview_start = max(int(fr[0]) - pad, 0)
        sc.frame_preview_end = int(fr[1]) + pad
        sc.frame_set(sc.frame_preview_start)
        return {'FINISHED'}


@_need_session
class AR_OT_review_flag(bpy.types.Operator):
    """Mark the current step"""
    bl_idname = "action_recorder.review_flag"
    bl_label = "Mark Step"
    flag: EnumProperty(items=FLAG_ITEMS)

    def execute(self, context):
        st = _state(context)
        if st.showing < 1:
            return {'CANCELLED'}
        SESSION.set_flag(st.showing, self.flag)
        _sync_ui(context)
        return {'FINISHED'}


@_need_session
class AR_OT_review_mark(bpy.types.Operator):
    """Use the current step as the start / end of the next segment"""
    bl_idname = "action_recorder.review_mark"
    bl_label = "Mark Segment Bound"
    which: EnumProperty(items=[("START", "Start", ""), ("END", "End", "")])

    def execute(self, context):
        st = _state(context)
        if st.showing < 1:
            return {'CANCELLED'}
        if self.which == "START":
            st.seg_start = st.showing
            st.seg_end = max(st.seg_end, st.showing)
        else:
            st.seg_end = st.showing
            st.seg_start = min(st.seg_start, st.showing)
        return {'FINISHED'}


# ------------------------------------------------------------ segments

class _SegmentFields:
    label: EnumProperty(name="Problem type", items=LABELS)
    problem: StringProperty(name="Problem", description="What was wrong (as you concluded after watching)")
    cause: StringProperty(name="Cause", description="Why it looked wrong")
    method: StringProperty(name="Fix", description="How these steps fix it")
    use_frames: BoolProperty(name="Frame range", default=True)
    frame_start: IntProperty(name="Start")
    frame_end: IntProperty(name="End")
    bones: StringProperty(name="Bones", description="comma separated")
    step_start: IntProperty(name="Steps from", min=1)
    step_end: IntProperty(name="to", min=1)

    def draw(self, context):
        col = self.layout.column()
        row = col.row(align=True)
        row.prop(self, "step_start")
        row.prop(self, "step_end")
        col.prop(self, "label")
        col.prop(self, "problem")
        col.prop(self, "cause")
        col.prop(self, "method")
        row = col.row()
        row.prop(self, "use_frames")
        sub = row.row(align=True)
        sub.enabled = self.use_frames
        sub.prop(self, "frame_start")
        sub.prop(self, "frame_end")
        col.prop(self, "bones")

    def _fields(self):
        return dict(label=self.label, problem=self.problem, cause=self.cause, method=self.method,
                    frame_range=[self.frame_start, self.frame_end] if self.use_frames else None,
                    bones=[b.strip() for b in self.bones.split(",") if b.strip()])

    def _validate(self):
        if not (1 <= self.step_start <= SESSION.n_steps and 1 <= self.step_end <= SESSION.n_steps):
            return f"steps must be within 1..{SESSION.n_steps}"
        if not self.problem.strip():
            return "Write what the problem was"
        return None


@_need_session
class AR_OT_review_add_segment(_SegmentFields, bpy.types.Operator):
    """Annotate the selected range of steps: problem, cause and fix (prefilled from the recording)"""
    bl_idname = "action_recorder.review_add_segment"
    bl_label = "Annotate Segment"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        st = _state(context)
        a, b = sorted((st.seg_start, st.seg_end))
        self.step_start, self.step_end = a, b
        pre = SESSION.prefill(a, b)
        if pre.get("label") in {i[0] for i in LABELS}:
            self.label = pre["label"]
        self.problem = pre.get("problem", "")
        self.method = pre.get("method", "")
        self.cause = ""
        fr = pre.get("frame_range")
        self.use_frames = fr is not None
        if fr:
            self.frame_start, self.frame_end = int(fr[0]), int(fr[1])
        self.bones = ", ".join(pre.get("bones", []))
        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, context):
        err = self._validate()
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        SESSION.add_segment(self.step_start, self.step_end, **self._fields())
        _sync_ui(context)
        return {'FINISHED'}


@_need_session
class AR_OT_review_edit_segment(_SegmentFields, bpy.types.Operator):
    """Edit the selected segment"""
    bl_idname = "action_recorder.review_edit_segment"
    bl_label = "Edit Segment"
    bl_options = {'INTERNAL'}
    sid: IntProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        st = _state(context)
        if not (0 <= st.active_segment < len(st.segments)):
            return {'CANCELLED'}
        self.sid = st.segments[st.active_segment].sid
        s = next(x for x in SESSION.data["segments"] if x["id"] == self.sid)
        self.step_start, self.step_end = s["steps"]
        self.label, self.problem, self.cause, self.method = s["label"], s["problem"], s["cause"], s["method"]
        self.use_frames = bool(s.get("frame_range"))
        if s.get("frame_range"):
            self.frame_start, self.frame_end = s["frame_range"]
        self.bones = ", ".join(s.get("bones", []))
        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, context):
        err = self._validate()
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        SESSION.update_segment(self.sid, steps=[self.step_start, self.step_end], **self._fields())
        _sync_ui(context)
        return {'FINISHED'}


@_need_session
class AR_OT_review_delete_segment(bpy.types.Operator):
    """Delete the selected segment"""
    bl_idname = "action_recorder.review_delete_segment"
    bl_label = "Delete Segment"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        st = _state(context)
        if not (0 <= st.active_segment < len(st.segments)):
            return {'CANCELLED'}
        SESSION.delete_segment(st.segments[st.active_segment].sid)
        _sync_ui(context)
        st.active_segment = -1
        return {'FINISHED'}


@_need_session
class AR_OT_review_summary(bpy.types.Operator):
    """Overall conclusion for this episode"""
    bl_idname = "action_recorder.review_summary"
    bl_label = "Episode Summary"
    bl_options = {'INTERNAL'}

    text: StringProperty(name="Summary", description="What was fixed overall, anything notable")
    rating: IntProperty(name="Fix quality", min=0, max=5, description="0 = not rated, 5 = excellent")
    usable: BoolProperty(name="Use for training", default=True)

    def invoke(self, context, event):
        s = SESSION.data["summary"]
        self.text, self.rating, self.usable = s.get("text", ""), s.get("rating", 0), s.get("usable", True)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, context):
        SESSION.set_summary(self.text, self.rating, self.usable)
        return {'FINISHED'}


# ------------------------------------------------------------ UI

class AR_UL_review_rows(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if item.kind == "diagnosis":
            layout.label(text=item.text, icon='TEXT')
            return
        if item.kind == "initial":
            layout.label(text=item.text, icon='HOME')
            return
        row = layout.row(align=True)
        row.label(text="", icon=FLAG_ICON.get(item.flag, 'DOT') if item.flag != "normal"
                  else ('BOOKMARKS' if item.in_segment else 'DOT'))
        row.label(text=item.text)


class AR_UL_review_segments(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.text, icon='BOOKMARKS')


def _draw_review(self, context):
    layout = self.layout
    if SESSION is None:
        layout.operator("action_recorder.review_open", icon='FILE_FOLDER')
        layout.label(text="Opens a copy; the recording is never modified.")
        return
    st = _state(context)
    box = layout.box()
    box.label(text=os.path.basename(SESSION.dir), icon='SEQUENCE')
    task = SESSION.meta.get("task")
    if task:
        box.label(text=f"Task: {task}")
    cov = SESSION.coverage()
    box.label(text=f"{SESSION.n_steps} steps · {cov['segments']} segments · "
                   f"{len(cov['uncovered'])} steps not annotated")

    layout.template_list("AR_UL_review_rows", "", st, "rows", st, "active_row", rows=12)

    row = layout.row(align=True)
    row.operator("action_recorder.review_step", text="", icon='TRIA_LEFT').delta = -1
    showing = "initial state" if st.showing == 0 else (
        f"BEFORE step {st.showing}" if st.showing_before else f"after step {st.showing}")
    row.operator("action_recorder.review_before_after", text=showing,
                 icon='LOOP_BACK' if st.showing_before else 'LOOP_FORWARDS', depress=st.showing_before)
    row.operator("action_recorder.review_step", text="", icon='TRIA_RIGHT').delta = 1
    layout.operator("action_recorder.review_preview_frames", icon='PREVIEW_RANGE').segment = False

    if st.showing > 0:
        rec = SESSION.step(st.showing)
        b = layout.box()
        b.label(text=f"Step {st.showing}  ·  t={rec['t']:.1f}s  ·  frame {rec['ctx_before']['frame']}")
        ops = ", ".join(o["idname"] for o in rec["ops"])
        b.label(text=ops if rec["ops"] else "(no operator: direct edit)")
        for bone, s in list(rec["summary"]["by_bone"].items())[:6]:
            b.label(text=f"  {bone}: +{s['added']} -{s['removed']} ~{s['modified']}  frames {s['frame_range']}")
        r = b.row(align=True)
        cur = SESSION.flag(st.showing)
        for ident, name, _, icon, _ in FLAG_ITEMS:
            r.operator("action_recorder.review_flag", text=name, icon=icon, depress=(cur == ident)).flag = ident

    seg = layout.box()
    seg.label(text="Segments", icon='BOOKMARKS')
    r = seg.row(align=True)
    r.prop(st, "seg_start")
    r.operator("action_recorder.review_mark", text="", icon='TRIA_DOWN_BAR').which = "START"
    r.prop(st, "seg_end")
    r.operator("action_recorder.review_mark", text="", icon='TRIA_UP_BAR').which = "END"
    seg.operator("action_recorder.review_add_segment", icon='ADD')
    seg.template_list("AR_UL_review_segments", "", st, "segments", st, "active_segment", rows=4)
    r = seg.row(align=True)
    r.operator("action_recorder.review_edit_segment", icon='GREASEPENCIL')
    r.operator("action_recorder.review_preview_frames", text="Preview", icon='PREVIEW_RANGE').segment = True
    r.operator("action_recorder.review_delete_segment", text="", icon='TRASH')

    r = layout.row(align=True)
    r.operator("action_recorder.review_summary", icon='TEXT')
    r.operator("action_recorder.review_close", icon='X')


class VIEW3D_PT_action_review(bpy.types.Panel):
    bl_label = "Episode Review"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Recorder"
    draw = _draw_review


class GRAPH_PT_action_review(bpy.types.Panel):
    bl_label = "Episode Review"
    bl_space_type = 'GRAPH_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Recorder"
    draw = _draw_review


CLASSES = (ARReviewRow, ARReviewSegment, ARReviewState, AR_OT_review_open, AR_OT_review_close,
           AR_OT_review_step, AR_OT_review_before_after, AR_OT_review_preview_frames, AR_OT_review_flag,
           AR_OT_review_mark, AR_OT_review_add_segment, AR_OT_review_edit_segment, AR_OT_review_delete_segment,
           AR_OT_review_summary, AR_UL_review_rows, AR_UL_review_segments, VIEW3D_PT_action_review,
           GRAPH_PT_action_review)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.ar_review = PointerProperty(type=ARReviewState)
    bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    global SESSION
    SESSION = None
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    del bpy.types.Scene.ar_review
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
