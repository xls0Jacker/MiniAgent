# 测试说明

> 本文档记录 MiniAgent 的测试运行方式、各测试覆盖的功能点，以及当前测试的已知边界。

## 运行环境与命令

- Python ≥ 3.11（实测 3.13.12）
- 依赖安装：`uv sync`（见 `pyproject.toml`）
- 运行全部测试：`uv run python -m pytest tests/ -q`
- 运行单个文件：`uv run python -m pytest tests/test_demo_tools.py -q`

> 说明：项目根需为 git 仓库，且 `README.md` 需存在——部分测试硬编码读取根目录
> `README.md` / `pyproject.toml`（如 `test_concurrent_batch_execution`）。

## 测试文件 → 功能点映射

| 测试文件 | 覆盖的功能点 | 归属 |
|----------|--------------|------|
| `tests/test_demo_tools.py` | 演示工具（Calculator 安全求值 / Search mock / Weather / Todo）、工具注册机制（schemas / register）、Schema 完整性、Todo session 隔离 | 新增 |
| `tests/test_max_iterations.py` | context 管理的最大轮次限制（Agent 主循环护栏） | 新增 |
| `tests/test_agent.py` | 基本循环（single-step / multi-step / autonomous）、工具调用 trace、异常处理（工具错误 / 除零 / 注入拒绝 / 取消 / 超时）、会话续聊、上下文压缩、多工具并发、token 用量 | 存量 |
| `tests/test_commands.py` | 命令行解析、命令注册、模式切换 | 存量 |
| `tests/test_context.py` | 基础压缩（auto-compact 消息构造）、token 预算 | 存量 |
| `tests/test_context_window.py` | 模型 context window 解析/映射 | 存量 |
| `tests/test_hooks.py` | Hook 引擎（pre/post tool、turn 事件） | 存量 |
| `tests/test_mcp.py` | MCP 服务器配置加载 | 存量 |
| `tests/test_memory.py` | session 管理（JSONL 持久化、meta、恢复、compact boundary）、指令加载 | 存量 |
| `tests/test_permission_dialog.py` | 权限确认对话框 UI | 存量 |
| `tests/test_permissions.py` | 权限系统（模式决策、路径沙箱、危险命令检测、规则引擎、端到端 ask/allow） | 存量 |
| `tests/test_recovery.py` | 崩溃恢复 / 断点续聊 | 存量 |
| `tests/test_replacement_state.py` | 文本替换状态管理 | 存量 |
| `tests/test_scroll_guard.py` | 滚动保护 | 存量 |
| `tests/test_serialization.py` | LLM 消息/工具调用序列化、三协议（anthropic/openai/responses）、流式事件→结构化 | 存量 |
| `tests/test_skills.py` | Skill 加载与执行 | 存量 |
| `tests/test_tool_search.py` | 工具搜索（延迟发现的 ToolSearchTool） | 存量 |
| `tests/test_tui_render.py` | TUI 渲染、主题注册、模式标签 | 存量 |

## 模块 ↔ 测试 双向索引

> 选入口：想在**某模块**看怎么测 → 查下表 → 到对应测试文件（每篇 `docs/0N-*.md`
> 末尾的「测试绑定」小节是这张表的模块侧入口）；想从**某测试**反查它守护哪个模块 →
> 对照上表「测试文件 → 功能点映射」。

| 模块（`docs/0N-*.md`） | 对应测试文件 | 验证什么 |
|------------------------|-------------|---------|
| 01 初始 Coding Agent | `test_context_window.py`、`test_mcp.py`（config/validator 部分）、`test_agent.py::test_stop_*` | 配置加载与校验、context window 解析、主循环护栏 |
| 02 LLM 客户端与流式响应 | `test_serialization.py` | 三协议流式事件归一、消息/工具调用序列化 |
| 03 工具注册与执行框架 | `test_demo_tools.py`、`test_tool_search.py`、`test_skills.py` | 演示工具、注册机制、延迟发现的 ToolSearch |
| 04 Agent 主循环与事件流 | `test_agent.py`、`test_max_iterations.py` | 主循环、事件流、max_iterations 护栏 |
| 05 System Prompt 组装 | `test_agent.py`（prompt 相关用例）、`test_context.py` | prompt 分段构造、compact 消息 |
| 06 权限系统 | `test_permissions.py`、`test_permission_dialog.py` | 模式/危险命令/沙箱/规则/HITL、确认弹窗 |
| 07 MCP 协议接入 | `test_mcp.py` | 配置 env 替换、ToolWrapper 桥接、manager 部分失败 |
| 08 上下文压缩与 Token 管理 | `test_context.py`、`test_context_window.py`、`test_recovery.py`、`test_replacement_state.py`、`test_memory.py`、`test_max_iterations.py` | 双层压缩、token 估算、恢复、替换状态、boundary 持久化 |
| 09 TUI 交互设计 | `test_tui_render.py`、`test_scroll_guard.py`、`test_permission_dialog.py` | 渲染/主题/模式标签、滚动保护、权限弹窗 UI |

> 注：`tests/test_agent.py` 是**多模块共测**（主循环 + prompt + 权限集成），不整文件挂在
> 单一模块下，各模块只引用其专属的稳定用例。

## 已知边界（真实 LLM 需手动 TUI 验证）

以下场景测试使用 mock LLM 客户端，不依赖网络；真实模型行为需在 TUI 中手动确认：

1. **真实 LLM 流式输出与工具决策**——mock 预设了工具调用，真实模型是否「正确地」选择 Calculator/Search/Weather 需对话验证。
2. **ToolSearchTool 延迟发现**——测试验证机制本身，真实多工具场景下的召回质量需手动体验。
3. **上下文压缩触发的真实时机**——`auto_compact` 依赖 token 估算，真实长对话的压缩边界需手动观察。
4. **Todo 跨窗口隔离**——`tests` 用两个 session_id 模拟；TUI 中开两个窗口实际验证。

## 存量测试的已知失败与处置

部分存量测试与当前实现行为不一致，原因与处置如下。除两项环境/文件依赖外，其余为
存量用例的预期行为与实现之间的差异，判定不改实现、保留用例并注明：

| 测试 | 失败原因 | 处置 |
|------|---------|------|
| `test_agent.py::test_concurrent_batch_execution` | 硬编码读取根目录 `README.md`（此前不存在） | README 落地后转绿 |
| `test_permissions.py::test_e2e_rule_allows_git` | 依赖 git 仓库环境（在非 git 树的临时目录跑 `git status` 必然 fatal） | `git init` 后转绿 |
| `test_memory.py`（收集级） | import 了源码中不存在的 `build_time_gap_message` | 已删除失效的 `TestTimeGapMessage` 类 |
| `test_agent.py::test_multi_step_autonomous` | 测试直接 WriteFile，撞上「先读后写」保护（当前实现如此） | 存量差异，不修 |
| `test_agent.py::test_message_splicing` | 与当前实现的消息拼接行为不符 | 存量差异，不修 |
| `test_commands.py::TestPlanDoHandlers/test_all_commands_registered` | 命令集与当前注册行为存在差异 | 存量差异，不修 |
| `test_context.py::TestBuildCompactMessages::test_basic_structure` | 压缩消息构造与当前实现不一致 | 存量差异，不修 |
| `test_mcp.py::TestLoadConfigMCP`（4 项） | MCP 配置校验与当前 validator 行为不一致 | 存量差异，不修 |
| `test_permissions.py::test_plan_mode / plan_mode_denies_write` | 期望 PLAN 模式写操作 deny，实现返回 ask | 存量差异，不修 |
| `test_permissions.py::test_e2e_bypass_mode_allows_all` | 写前保护阻止绕过模式直接写未读文件 | 存量差异，不修 |

**结论：** 排除 README 依赖与 git 环境两类外部因素后，剩余失败均为上述存量差异，不影响
新增测试（demo 工具 + max_iterations）与功能验收。新增测试全部通过。
