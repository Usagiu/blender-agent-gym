bl_info = {
    "name": "Action Recorder (animation repair dataset)",
    "author": "",
    "version": (0, 1, 0),
    "blender": (4, 3, 0),
    "location": "3D View / Graph Editor > Sidebar > Recorder",
    "description": "Record operators, context and keyframe/pose state diffs into replayable episodes",
    "category": "Animation",
}

if "bpy" in locals():
    import importlib
    for _m in (compat, snapshot, diff, context_capture, ophistory, episode, recorder, review,  # noqa: F821
               review_ui):  # noqa: F821
        importlib.reload(_m)

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from . import compat, context_capture, diff, episode, ophistory, recorder, review, review_ui, snapshot  # noqa: F401,E501
from .recorder import RECORDER

ANNOTATION_LABELS = [
    ("foot_sliding", "Foot sliding", "Foot moves while it should be planted"),
    ("jitter", "Jitter / noise", "High-frequency noise"),
    ("flip", "Rotation flip", "Euler/quaternion flip or pop"),
    ("penetration", "Penetration", "Body intersects itself or the ground"),
    ("pose_error", "Bad pose", "Anatomically wrong or broken pose"),
    ("contact", "Contact / hands", "Hand or prop contact is off"),
    ("timing", "Timing", "Spacing / timing problem"),
    ("other", "Other", "Something else"),
]


def _prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


class ActionRecorderPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    output_dir: StringProperty(name="Episode folder", subtype='DIR_PATH', default="~/blender_episodes")
    annotator: StringProperty(name="Your name / ID", default="")

    def draw(self, context):
        self.layout.prop(self, "output_dir")
        self.layout.prop(self, "annotator")


class REC_OT_event_listener(bpy.types.Operator):
    """Pass-through modal operator that logs raw input events (L1)."""
    bl_idname = "action_recorder.event_listener"
    bl_label = "Action Recorder Event Listener"
    bl_options = {'INTERNAL'}

    _held = False

    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        RECORDER.listener_running = True
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if not RECORDER.active:
            RECORDER.listener_running = False
            return {'CANCELLED', 'PASS_THROUGH'}
        etype = event.type
        if etype.startswith("TIMER") or etype in ("NONE", "INBETWEEN_MOUSEMOVE", "WINDOW_DEACTIVATE"):
            return {'PASS_THROUGH'}
        if etype in ("LEFTMOUSE", "RIGHTMOUSE", "MIDDLEMOUSE", "PEN", "ERASER"):
            self._held = event.value == 'PRESS'
        area = None
        mx, my = event.mouse_x, event.mouse_y
        if context.window and context.window.screen:
            for a in context.window.screen.areas:
                if a.x <= mx < a.x + a.width and a.y <= my < a.y + a.height:
                    area = a
                    break
        ev = {"type": etype, "value": event.value, "x": mx, "y": my,
              "p": round(event.pressure, 4), "held": self._held,
              "mod": "".join(c for c, on in (("s", event.shift), ("c", event.ctrl),
                                             ("a", event.alt), ("o", event.oskey)) if on),
              "area": area.type if area else None}
        try:
            RECORDER.on_event(ev, area)
        except Exception as e:  # noqa: BLE001
            print("[action_recorder] event error:", repr(e))
        return {'PASS_THROUGH'}


class REC_OT_start(bpy.types.Operator):
    """Start recording an episode (saves initial.blend)"""
    bl_idname = "action_recorder.start"
    bl_label = "Start Recording"

    output_dir: StringProperty(name="Output folder", default="", options={'SKIP_SAVE', 'HIDDEN'},
                               description="Override the add-on preference (scripts/tests)")

    @classmethod
    def poll(cls, context):
        return not RECORDER.active

    def execute(self, context):
        wm = context.window_manager
        prefs = _prefs(context)
        meta = {
            "task": wm.action_recorder_task,
            "character": wm.action_recorder_character,
            "problem_tags": [t.strip() for t in wm.action_recorder_tags.split(",") if t.strip()],
            "annotator": prefs.annotator if prefs else "",
            "addon_version": ".".join(map(str, bl_info["version"])),
        }
        out = self.output_dir or (prefs.output_dir if prefs else "~/blender_episodes")
        path = RECORDER.start(out, meta)
        bpy.ops.action_recorder.event_listener('INVOKE_DEFAULT')
        self.report({'INFO'}, f"Recording to {path}")
        return {'FINISHED'}


class REC_OT_stop(bpy.types.Operator):
    """Stop recording (saves final.blend)"""
    bl_idname = "action_recorder.stop"
    bl_label = "Stop Recording"

    @classmethod
    def poll(cls, context):
        return RECORDER.active

    def execute(self, context):
        path = RECORDER.stop()
        self.report({'INFO'}, f"Episode saved: {path}")
        return {'FINISHED'}


class REC_OT_annotate(bpy.types.Operator):
    """Write a diagnosis after reviewing a passage: what is wrong, where, and
    how you intend to fix it.  Timing is loose -- just write it before (or
    while) making the related edits.  Detailed labelling is done afterwards
    in review."""
    bl_idname = "action_recorder.annotate"
    bl_label = "Write Diagnosis"

    label: EnumProperty(name="Problem type", items=ANNOTATION_LABELS)
    note: StringProperty(name="What is wrong / why",
                         description="Your conclusion after watching the passage")
    plan: StringProperty(name="How you will fix it (optional)")
    use_frames: BoolProperty(name="Frame range", default=True)
    frame_start: IntProperty(name="Start")
    frame_end: IntProperty(name="End")
    bones: StringProperty(name="Body parts / bones", description="comma separated; prefilled from selection")

    @classmethod
    def poll(cls, context):
        return RECORDER.active

    def invoke(self, context, event):
        sc = context.scene
        if sc.use_preview_range:
            self.frame_start, self.frame_end = sc.frame_preview_start, sc.frame_preview_end
        else:
            self.frame_start, self.frame_end = sc.frame_start, sc.frame_end
        obj = context.view_layer.objects.active
        if obj and obj.type == 'ARMATURE':
            self.bones = ", ".join(b.name for b in obj.data.bones if b.select)
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "label")
        col.prop(self, "note")
        col.prop(self, "plan")
        row = col.row()
        row.prop(self, "use_frames")
        sub = row.row(align=True)
        sub.enabled = self.use_frames
        sub.prop(self, "frame_start")
        sub.prop(self, "frame_end")
        col.prop(self, "bones")

    def execute(self, context):
        bones = [b.strip() for b in self.bones.split(",") if b.strip()]
        frames = (self.frame_start, self.frame_end) if self.use_frames else None
        RECORDER.annotate(self.label, self.note, frames, bones, plan=self.plan, phase="diagnosis")
        self.report({'INFO'}, f"Diagnosis saved: {self.label}")
        return {'FINISHED'}


def _draw_panel(self, context):
    layout = self.layout
    wm = context.window_manager
    if RECORDER.active:
        w = RECORDER.writer
        box = layout.box()
        box.label(text="● Recording", icon='REC')
        box.label(text=f"steps {w.n_steps}  records {w.n_records}  events {w.n_events}")
        box.label(text=w.dir.split("/")[-1])
        layout.operator("action_recorder.annotate", text="Write Diagnosis", icon='TEXT')
        layout.operator("action_recorder.stop", icon='SNAP_FACE')
    else:
        layout.prop(wm, "action_recorder_task")
        layout.prop(wm, "action_recorder_character")
        layout.prop(wm, "action_recorder_tags")
        layout.operator("action_recorder.start", icon='REC')


class VIEW3D_PT_action_recorder(bpy.types.Panel):
    bl_label = "Action Recorder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Recorder"
    draw = _draw_panel


class GRAPH_PT_action_recorder(bpy.types.Panel):
    bl_label = "Action Recorder"
    bl_space_type = 'GRAPH_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Recorder"
    draw = _draw_panel


CLASSES = (ActionRecorderPreferences, REC_OT_event_listener, REC_OT_start, REC_OT_stop,
           REC_OT_annotate, VIEW3D_PT_action_recorder, GRAPH_PT_action_recorder)
_keymaps = []


def _redraw_timer():
    if RECORDER.active:
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type in ('VIEW_3D', 'GRAPH_EDITOR'):
                    a.tag_redraw()
    return 1.0


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    review_ui.register()
    wm = bpy.types.WindowManager
    wm.action_recorder_task = StringProperty(name="Task", description="What is being fixed")
    wm.action_recorder_character = StringProperty(name="Character")
    wm.action_recorder_tags = StringProperty(name="Problem tags", description="comma separated")
    bpy.app.handlers.load_pre.append(recorder.on_load_pre)
    # no default hotkey: diagnoses are written after watching a passage, not
    # mid-operation.  Right-click the panel button > Assign Shortcut if wanted.
    if not bpy.app.background:
        bpy.app.timers.register(_redraw_timer, first_interval=1.0, persistent=True)


def unregister():
    if RECORDER.active:
        RECORDER.stop()
    if bpy.app.timers.is_registered(_redraw_timer):
        bpy.app.timers.unregister(_redraw_timer)
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    if recorder.on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(recorder.on_load_pre)
    wm = bpy.types.WindowManager
    for p in ("action_recorder_task", "action_recorder_character", "action_recorder_tags"):
        if hasattr(wm, p):
            delattr(wm, p)
    review_ui.unregister()
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
