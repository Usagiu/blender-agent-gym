# blender-agent-gym

让模型学会在 Blender 里干活的数据与环境：录制美术的操作、逐步回放核对、翻译成人和模型共用的 Action API、复盘标注，最终用于 SFT / RL。当前任务是**角色动作修复**，仅支持 **Blender 4.3**。录制插件在 Blender 里显示为 “Action Recorder”。

把美术在 Blender 里修动画的过程录成**可回放的 episode**，给 SFT / RL 用。
同时录三层，回放时逐步核对：

| 层 | 内容 | 文件 |
|---|---|---|
| L1 原始输入 | 键鼠/压感事件、所在编辑器 | `events.jsonl` |
| L2 语义操作 | 算子 idname + 最终参数（含 F9 调整）+ 执行时上下文 | `steps.jsonl` 的 `ops` / `ctx_before` |
| L3 状态差异 | 每步前后 F-Curve 全量（关键帧/手柄/插值）、pose、自定义属性 | `deltas/NNNNNN.json` |

## 安装 / 使用

1. 打包：在本目录执行 `zip -r blender_action_recorder.zip blender_action_recorder`（`dist/` 里已有一份）
2. Blender → Preferences → Add-ons → Install… 选 zip，勾选启用；在插件偏好里设置 episode 输出目录
3. 3D 视图或 Graph Editor 侧栏 → **Recorder** 标签：填任务/角色/问题标签 → **Start Recording**
4. 正常修动画。看完一段、得出结论后（动手修之前），点面板里的 **Write Diagnosis** 写一条诊断：问题类型、结论/原因、打算怎么修、帧范围（默认是预览范围或整段）、骨骼（从当前选择预填）。时间点不需要精确，也没有默认快捷键；如果需要，可以在按钮上右键选 Assign Shortcut 自己绑。更细的分段标注计划放到录制后的复盘工具里做
5. **Stop Recording** → 生成 `episode_<时间戳>/`

```
episode_20261117_152203/
  meta.json  initial.blend  final.blend  state_initial.json  state_final.json
  steps.jsonl      # kind = step | context | annotation
  deltas/000001.json
  events.jsonl
```

## 录制后复盘标注

录完后，在 3D 视图或 Graph Editor 侧栏 → **Recorder** 标签 → **Episode Review** → **Open Episode for Review**，选择 episode 文件夹。

- 打开的是 `review.blend`（`initial.blend` 的副本，放在同一个文件夹里，保证库文件的相对路径不失效），原始录制永远不会被改动
- **步骤列表**：每一步显示简短说明（如 `smooth  Thigh 5-29`、`frame 1 → 10`）；录制时写的诊断也按时间插在列表里。点任意一行，场景就跳到那一步之后的状态（和录制逐位一致）
- **◀ / ▶** 上一步、下一步；中间按钮在“这一步之前 / 之后”之间切换，方便对比；**Preview Changed Frames** 把预览范围设到这一步改动的帧，直接播放查看
- **步骤标记**：Normal / Exploration（试探、到处看看）/ Mistake（走错了，后来撤销或重做）
- **分段标注**：用 ⤓ ⤒ 把当前步设为起点或终点 → **Annotate Segment**，填问题、原因、做法。帧范围和骨骼根据这几步的改动自动预填；如果附近有录制时写的诊断，问题类型和描述也会预填
- **Episode Summary**：整体结论、质量评分（1–5）、是否用于训练
- 面板顶部显示还有几步有改动但没被任何分段覆盖，提醒补标
- 所有标注随改随存到 `EP/review.json`

### 生成训练样本

```bash
blender -b EP/initial.blend --python tools/translate_episode.py -- EP   # 先翻译成 API 调用
python3 tools/build_samples.py EP_OR_ROOT -o samples.jsonl            # 再合并复盘标注
```

每个分段生成一个样本：复盘写的问题、原因、做法，录制时的诊断，以及这些步骤的 API 调用。每一步都带标记（normal / exploration / mistake），由使用方决定保留还是丢弃弯路。被标为“不用于训练”的 episode 默认跳过。

## 回放与核对

```bash
# state 模式：逐步应用 L3，必须与录制逐位一致（验证录制完整）
blender -b EP/initial.blend --python tools/replay.py -- EP --mode state
# ops 模式（后台）：恢复上下文 + 重新执行算子；transform.* 等需要界面的算子降级为 state
blender -b EP/initial.blend --python tools/replay.py -- EP --mode ops
# ops 模式（GUI）：包括 transform 在内的所有算子都真实执行，跑完自动退出
blender EP/initial.blend --python tools/replay.py -- EP --mode ops
# 人看的时间线
python3 tools/summarize.py EP
```

报告里的关键指标：
- `post-step state exact match`：每步之后的状态是否与录制一致
- `operator fidelity`：用算子重放、结果与录制一致的步数占比
- `op_coverage`：有可执行算子的步数占比（其余是直接改值、撤销等，只能用 state 回放）

## Action API（底层编辑 + 观察层）

人和模型共用的动作空间。调用是纯 JSON：`{"name": "set_keys", "args": {...}}`，用 `api.execute(call)` 执行，永远不抛异常，返回 `{"ok": true, "result": ...}` 或 `{"ok": false, "error": "给模型看的错误信息"}`。完整定义见 [api_schema.json](api_schema.json)（用 `tools/export_api_schema.py` 生成）。

| 类别 | 动作 |
|---|---|
| 编辑 | `set_keys` `delete_keys` `set_key_props` `delete_channel` `set_channel_props` `set_pose` `key_pose` `set_frame` `set_frame_range` `assign_action` |
| 观察 | `list_objects` `list_channels` `get_keys` `sample_curves` `get_pose` `sample_world` `detect_issues` |

约定：用骨骼名、通道名、帧号寻址，不依赖选择状态；数值用通道原始单位（旋转是弧度）；编辑后按 Blender 的方式重算自动手柄（可用 `recalc_handles=false` 关闭，并给出精确手柄）。观察类动作会恢复当前帧和未插帧的 pose。

`detect_issues` 检测：滑步（接触地面时脚的水平移动）、抖动（加速度锯齿）、欧拉/四元数翻转、单帧突变、穿地（需给 `ground_z`）。阈值以场景单位计，换角色比例时需要调整。

### 把录制翻译成 API 调用

```bash
blender -b EP/initial.blend --python tools/translate_episode.py -- EP
```

逐步把状态差异翻译成编辑调用，在场景上执行，并与录制核对（容差 1e-4）。每条通道先尝试**紧凑形式**：只给数值（6 位小数）和改动的插值/手柄类型，自动手柄交给重算。重算对不上时，退回**显式形式**（精确手柄，不重算）。输出 `EP/actions.jsonl`（按顺序包含诊断标注和每步的调用）以及 `translate_report.json`。

## 测试

```bash
# 后台端到端：脚本模拟一次修复（选择/旋转/插帧/平滑/改插值/直接改值/删帧/撤销/欧拉滤波/IK-FK 开关）
blender -b --factory-startup --python tests/test_e2e.py -- /tmp/ar_test
# GUI 真实链路：用 --enable-event-simulate 注入键鼠（R X 30 回车、I），会弹出窗口后自动退出
blender --factory-startup --enable-event-simulate --python tests/test_gui_events.py -- /tmp/ar_gui
# Action API：编辑/错误处理/观察，以及检测器在注入问题（滑步、抖动、翻转、突变）上的准确性
blender -b --factory-startup --python tests/test_api.py
# 复盘：打开、跳转精确性、标注存取、重新打开、样本导出（内部会先跑 test_e2e）
blender -b --factory-startup --python tests/test_review.py -- /tmp/ar_review
```

已验证（Blender 4.3.2）：
- 后台 e2e：state 15/15，ops 15/15 逐步一致，数据类算子 7/7 复现；transform 在后台按设计降级
- 翻译：e2e 15/15 步验证通过，17 条通道里 16 条用紧凑形式；调用序列 4.2 KB，原始差异 182 KB。GUI 真实录制的 2 步也全部通过
- 复盘测试 37 项全部通过：7 次前后跳转全部与录制哈希一致，标注存取和重新打开正确，原始录制文件未被改动；GUI 中面板和标注弹窗已截图检查
- API 测试 34 项全部通过；检测器在测试 rig 上找到全部 4 个注入问题，没有误报（抬脚迈步不算滑步，突变不重复报成滑步或抖动）
- GUI 模拟输入：transform 拖拽期间不会被切成碎步；`transform.rotate`、`anim.keyframe_insert` 都被真实捕获；GUI 回放 2/2 一致，保真度 1.0
- 性能：120 根骨骼、48 万关键帧，每步快照 + diff 约 170 ms（只在一步结束时执行一次）

## 设计要点 / 踩过的坑

- **步的边界**：Python 拿不到 undo push 的回调，所以用 depsgraph/帧/撤销 handler 加 `wm.operators` 变化把录制器标记为“脏”，安静 0.3 s 后提交一步。拖拽期间别的 modal 会吞掉事件，事件监听器“饿死”时就不提交，避免把一次拖拽切碎（4.3 没有 `Window.modal_operators`）
- **从 Python 调用的算子不会进 `wm.operators`**，只有界面里触发的才会。所以 N 面板直接改值这类操作，只能靠 L3 状态差异捕获
- **写回关键帧必须先写 F-Curve 属性、再 `foreach_set`**：设置 `extrapolation`/`mute` 会触发自动手柄重算，覆盖掉精确的手柄坐标
- **关键帧的左右手柄选择要单独录**：有些算子（如 `graph.smooth`）会看手柄的选择状态，不录手柄选择回放会分歧
- 后台模式下给 `transform.*` 传 region 覆盖会直接崩溃，所以只在 GUI 回放中执行
- `transform.rotate(value=0.3, X)` 实际得到 -0.3，transform 数字输入的单位是角度。录到的是真实参数，回放没问题，但写 Action API 时要注意这个约定

## 已知限制（下一步）

- 只支持 Blender 4.3（旧版 `action.fcurves` API）；4.4+ 的分层 Action 不支持
- 检测器阈值是按测试 rig 定的默认值，还没在真实角色上标定
- 未跟踪：drivers、F-Curve 修改器参数（只记录了类型）、NLA、约束、骨骼集合、网格/雕刻
- F9 调整过的算子（`adjusted`）和撤销/重做步在 ops 模式下用 state 回放
- 选择类算子在回放时跳过，由 `restore_context` 直接恢复选择
- 尚未实现：中层 API（如按 I 插帧的 9 个 `set_keys` 应合并为一句 `key_pose`；`smooth_keys` 等）、规范化轨迹（去掉撤销、合并微调）、MCP 服务、评分器、合成瑕疵生成
