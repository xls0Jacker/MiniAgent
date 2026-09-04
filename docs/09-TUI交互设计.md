# 模块解读 09：TUI 交互设计（自研 Textual 界面）

> 本文拆解 `autocode/app.py`
> （Textual 应用）+ 各 dialog 模块，讲清 TUI 的布局、Agent 事件如何驱动界面刷新、
> 权限弹窗 / 会话管理 / 记忆召回的交互闭环。
> 全文基于本项目 `autocode/` 包（模块解读，供自研参考）。

## 1. 模块职责

`AutoCodeApp(Textual.App)` 是 TUI 主应用。它把 Agent 主循环产出的**事件流**翻译成
**屏幕变化**：模型正文一行行滚出、工具调用变成可点开/折叠的卡片、需要批准时弹出
对话框。它不包含 Agent 逻辑——只是 Agent 事件最忠实的「放映员 + 遥控器」。

配套模块：

| 文件 | 职责 |
|------|------|
| `app.py` | 主应用：compose 布局、事件绑定、命令分发、会话/权限/记忆编排 |
| `permission_dialog.py` | 工具调用的批准/拒绝弹窗（HITL） |
| `plan_dialog.py` | Plan Mode 的方案确认对话框 |
| `askuser_dialog.py` | 模型主动向用户提问的对话框 |
| `session_dialog.py` | 会话列表切换界面 |
| `styles.tcss` | Textual CSS：主题、布局、消息气泡样式 |

## 2. 界面布局（compose）

```
┌──────────────────────────────────────────────┐
│  ┌ #chat-area (VerticalScroll)              │
│  │  #title-bar      ← AutoCode banner       │
│  │  #chat-messages  ← 用户/模型气泡 + 工具卡片│
│  │     .user-row / .ai-row / ToolCallBlock   │
│  └───────────────────────────────────────────│
│  ┌ #input-area                              │
│  │  #chat-input (ChatInput)  ← 支持 /命令补全│
│  │  #status-bar: mode-label · model-label    │
│  │  CompletionPopup                          │
│  └───────────────────────────────────────────│
│  多 provider 时首屏 #provider-select 选模型   │
└──────────────────────────────────────────────┘
```

- 单 provider：直接进对话；多 provider：先弹出 `#provider-select` 列表选模型。
- 输入框 `ChatInput` 是自定义 TextArea，支持 `/` 斜杠菜单、`@文件` 展开、
  Tab 补全、上/下翻命令历史（`load_history` / `_persist_entry`）。

## 3. Agent 事件与界面的绑定

这是 TUI 的核心循环（`_send_message`）：

```
用户回车 → action_submit → _send_message(text)
  1. 若含 @xxx → expand_at_refs 展开成文件内容引用
  2. 异步启动记忆召回 prefetch（_prefetch_relevant_memories，8s 超时侧查询）
  3. 把用户消息渲染成气泡，写进 conversation + session JSONL
  4. 启动 spinner（“思考中…”）
  5. async for event in agent.run(conversation):
       StreamText      → 追加到 AI 气泡（打字机效果）
       ToolUseEvent    → 挂一个 ToolCallBlock 卡片（loading → 结果）
       ToolResultEvent → 卡片 set_result（折叠/展开切换）
       PermissionRequest → 弹权限对话框，等回调注入 PermissionResponse
       AskUserEvent    → 弹 askuser 对话框
       CompactNotification → 顶栏提示“上下文已压缩”
       ThinkingText/ThinkingComplete → 思考块渲染
       ErrorEvent      → 红色错误气泡
  6. 停 spinner，刷新 status（token/模式/model），持久化 session
```

**关键点：事件 → 界面是一一对应的 `async for` 推送**。Textual 的事件循环里，
Agent 的 `yield` 事件在 UI 线程被消费，天然串行、天然流式，无需额外状态机。

### ToolCallBlock（工具卡片）

每个工具调用渲染成一张可聚焦卡片：
- loading 态：`(转圈) 正在调用 Calculator…`；
- 完成态：`set_result(output, is_error, elapsed)`，展示「✓ 结果 / ✗ 错误」+ 耗时；
- 点击在折叠 / 展开间切换（`_render_collapsed` / `_render_expanded`）。
同批并发的工具会聚成一个 `ToolGroupSummary`（计数 + 总耗时），点开看明细。

## 4. 权限弹窗与 Plan 确认（HITL）

```
Agent yield PermissionRequest
  → app._handle_permission_request 弹出 InlinePermissionWidget
      展示 工具名 + 参数预览 + 拦截原因
      按钮：Approve（仅此一次）/ Always Allow（规则）/ Deny
  → 用户选择 → widget 回调 → 构造 PermissionResponse
  → 注入回 Agent 正在 await 的那个点，工具继续/终止
```

Plan Mode 走 `_show_plan_approval`：模型产出的方案文件（`.autocode/plans/…`）由用户
在 `plan_dialog` 里审阅批准后才进入执行模式。

## 5. 会话管理界面与记忆召回

### 会话切换

每个窗口/会话是独立的 JSONL（`Session`/`SessionManager`，见 08 章 boundary 持久化）。
TUI 提供命令（/session、/resume 等）切到 `session_dialog`，选中历史会话后
`_set_session` 把 agent + Todo 工具都切到该 session 的 id（演示工具 Todo 按此隔离）。

### 记忆召回时机与放置方式

用户发出新消息时（`_send_message` 步骤 2），后台并行做一次**记忆召回侧查询**：

```
_prefetch_relevant_memories(text)
  → 独立 LLM client + 独立 mini-conversation（不污染主对话）
  → find_relevant_memories(query, user_mem_dir, project_mem_dir, selector=…)
  → 8s 超时；失败/超时静默返回 ""
  → render_reminder(命中记忆)  → 一段 <system-reminder> 文本

召回结果注入时机/位置：
  await prefetch_task（再等 ≤3s）
  若命中 → conversation.add_system_reminder(reminder)
  → 位置：紧跟刚加入的用户消息之后、AI 回复之前
  → 之后随 agent.run 作为一条 system 消息进模型
```

即「**发问 → 侧召回（最相关几条记忆）→ 作为 system-reminder 塞进当轮请求**」——
召回是**有界的**（不把整个记忆库倒给模型）、**按相关性**的（selector 打分）、
**尽力而为**的（超时不阻塞主流程）。

另有「长期注入」通道：`inject_long_term_memory(instructions, memories)` 把项目
AUTOCODE 指令 + 常驻记忆包成一个 system-reminder，**插在对话最前（index 0/1）**，
只注入一次（`ltm_injected` 标记），与每轮召回的「即时注入」互补。

## 6. 要点小结

1. **为什么 TUI 要用事件流而非轮询？**
   Agent 本身是 `async generator`。Textual 的 UI 线程直接 `async for` 消费，事件一到
   就更新对应 widget，天然流式（打字机）、天然可中断（取消 spinner / 杀掉 task），
   不需要在 UI 和 Agent 间搬一个共享状态再定时刷新。

2. **工具卡片折叠有什么交互价值？**
   一轮对话可能调十几个工具，若全部默认展开会把屏幕撑满。折叠成一行摘要（✓ 工具名 +
   耗时），用户想看细节再点开——信息密度与可读性的平衡。

3. **HITL 的「挂起」是怎么跨过 async 边界的？**
   Agent yield `PermissionRequest` 后停在 await 上；TUI 弹窗、用户点按钮、回调构造
   `PermissionResponse` 并塞回那个 await——UI 与 Agent 通过这个「request/response
   握手」同步，互不阻塞其它渲染。

4. **记忆召回为什么要「独立 client 侧查询」而不是复用主对话？**
   召回 selector 有自己的 system prompt（判定哪些记忆相关），若塞进主对话会污染历史、
   干扰主任务。独立 mini-conversation + 独立 client 隔离副作用，8s 超时兜底。

5. **「每轮召回注入」与「启动时长期注入」有什么区别？**
   长期注入（`inject_long_term_memory`）放对话最前、一次注入，提供跨会话的稳定背景
   （AUTOCODE 指令等）；每轮召回是即时、按用户当前问题挑最相关几条，放当轮消息后。
   一个「常驻基底」，一个「即时弹药」。

6. **Todo 演示工具的 session 隔离在 TUI 怎么体现？**
   app 持有 Todo 实例（`register_demo_tools` 返回值），切会话时 `set_session(id)` 注入；
   底层按 `storage_dir/<session_id>.json` 落盘。所以两个窗口各自记待办互不可见，切回
   原窗口数据还在——正好演示「session 隔离」。

## 7. 测试绑定

**对应的测试文件：**
- `tests/test_tui_render.py` —— 主题/面板/模式色、应用启动注册主题、模式标签渲染、
  permission future 取消不崩溃（Textual 的 Pilot 驱动）
- `tests/test_scroll_guard.py` —— 滚动保护（上翻时不自动拉到最新、在底部才跟随）
- `tests/test_permission_dialog.py` —— 权限确认弹窗 UI（WriteFile/EditFile 预览、语法
  高亮、word diff）——同时覆盖第 4 节 HITL 弹窗的展示层

**怎么验证本模块：**
- 整文件：`uv run python -m pytest tests/test_tui_render.py tests/test_scroll_guard.py tests/test_permission_dialog.py -q`
- 单条用例：`uv run python -m pytest tests/test_tui_render.py::test_app_starts_and_registers_theme -q`

**需要知道：**
- `docs/测试说明.md` 是全部测试的入口与总表，可反查任意模块。
- 这三个文件全部通过（无存量失败）。
- TUI 的核心是**真实交互**：打字机流式、工具卡片点击折叠、权限弹窗点按钮、会话切换、
  记忆召回时机——mock 只能验证到「事件→widget 不崩」，完整闭环需在终端手动跑
  `uv run autocode`（或 `uv run python -m autocode`）体验。
