# 模块解读 04：Agent 主循环与事件流

> 本文拆解 `autocode/agent.py` 的 ReAct 主循环：
> 接收输入 → 判断「回复 or 调工具」→ 执行工具 → 结果回填 → 继续循环或返回，
> 以及这套循环如何用一套事件流把状态喂给 TUI。
> 全文基于本项目 `autocode/` 包（模块解读，供自研参考）。

## 1. 模块职责

`Agent` 是整套系统的「心脏」。它自身不做网络请求、不画界面，只做一件事：
**把一次用户输入，通过「模型判断 + 工具执行」的循环，推进到模型给出最终回答为止。**

为了不把「主循环逻辑」和「展示方式」焊死，`Agent` 不直接返回最终文本，而是
`yield` 一串事件（`AgentEvent`）：正文增量、工具调用开始/结束、权限请求、压缩通知、
错误……TUI 和非交互模式都是这些事件的**消费者**，可以各自决定怎么渲染。

## 2. 核心数据结构

### 事件类型（`autocode/agent.py`）

```python
StreamText(text)             # 模型正文增量
ThinkingText(text)           # 思考块增量
ToolUseEvent(tool_name, tool_id, arguments)     # 模型决定调工具
ToolResultEvent(tool_id, tool_name, output, is_error, elapsed)  # 工具执行完
TurnComplete                 # 一轮（1 次模型调用）结束
LoopComplete(total_turns)    # 主循环整体正常结束
UsageEvent                   # token 用量累计
ErrorEvent(message)          # 主循环级错误（超轮次护栏等）
CompactNotification          # 触发上下文压缩
HookEvent                    # 钩子触发（供 UI 展示）
PermissionRequest            # 需要用户批准（见 06 章）
```

### LLMResponse（一次模型调用的聚合结果）

```python
class LLMResponse:
    text: str
    tool_calls: list[ToolCallComplete]     # 空 → 直接回答；非空 → 执行工具
    stop_reason: str
    input_tokens / output_tokens: int
```

`tool_calls` 非空是驱动循环「不结束」的唯一信号。

### Agent 构造要点（`Agent.__init__`）

```python
Agent(
    client, registry, protocol,
    work_dir, permission_checker, context_window,
    instructions_content, memory_manager, hook_engine,
    max_iterations=50,          # 主循环护栏（配置可透传）
)
```

`permission_checker` / `hook_engine` 都可为空（非交互批处理时可以不带）。

## 3. 关键流程

### ReAct 主循环（`Agent.run()`，简化）

```
iteration = 0
while True:
    iteration += 1
    if iteration > max_iterations:            # ① 护栏
        yield ErrorEvent("Agent reached maximum iterations"); break

    compact_result = await auto_compact(...)   # ② Layer2 接近窗口自动压缩
    if compacted: yield CompactNotification(...); conversation.inject_environment(...)

    system = build_system_prompt(...)          # ③ 组装系统提示
    tools  = registry.get_all_schemas(protocol)  # ④ 当前可见工具

    collector = StreamCollector()
    async for ev in collector.consume(client.stream(conv, system, tools)):
        yield ev                                # ⑤ 流式事件转发给 UI

    response = collector.response
    if not response.tool_calls:                 # ⑥ 模型直接回答
        yield TurnComplete(); break

    for batch in partition_tool_calls(response.tool_calls, registry):
        ... 执行工具 / 处理权限 / 回填历史 ...
    # ⑦ 带工具结果的对话进入下一轮 while
yield LoopComplete(total_turns=iteration)
```

### 几个关键子系统在主循环里的落点

1. **护栏**：`max_iterations` 每轮开头查（防死循环烧钱）；`consecutive_unknown` 连续
   未知工具 ≥3 即终止（防模型胡编工具名）。
2. **双层压缩**：Layer 2 `auto_compact`（对话太长时摘要压缩）；Layer 1 工具结果预算
   （`apply_tool_result_budget`，把过长 tool result 截断/持久化，见 08 章）。
3. **环境注入**：`conversation.inject_environment(env_context)`、`inject_long_term_memory`
   把「工作目录环境 + 长期记忆」作为 system 段塞进历史（见 05、09 记忆章）。
4. **plan mode**：`conversation.add_system_reminder(plan_reminder)` 每轮提醒模型「当前
   在出方案模式，别急着改代码」。
5. **延迟工具提示**：有 deferred 工具时在消息里塞一句「可用 ToolSearch 按名加载」。

### 工具结果如何回填（保证 API 约束）

```
assistant: 正文 + tool_use(id=t1, Calculator{expression:"1+1"})
→ 执行 Calculator → ToolResultEvent(t1, "2")
→ 追加 tool_result(tool_use_id=t1, content="2")
→ 下一轮请求带完整链条，模型看到结果再决定回复还是再调
```

`tool_use_id` 配对是 Anthropic / OpenAI 的硬约束：**每条 assistant 工具调用都必须有
对应 tool_result**，否则 API 报错。

## 4. 要点小结

1. **ReAct「交替」具体指什么？**
   思考（调模型的输出）与行动（调工具并回填结果）交替进行，直到模型给出**没有工具调用**
   的最终回答。`response.tool_calls` 空不空是唯一的终止判据。

2. **为什么要用事件流而不是回调/返回值？**
   主循环一次迭代里既有流式正文又有工具执行、权限请求、压缩通知，种类多且有先后。
   用 `yield` 事件让消费者（TUI）按到达顺序处理，主循环自己不需要知道「怎么展示」，
   天然支持流式渲染、取消、权限挂起。

3. **`max_iterations` 和 `consecutive_unknown` 两道护栏差异？**
   前者管「模型反复调工具但不收敛」（预算上限）；后者管「模型在调一个不存在的工具」
   （幻觉），连续 3 次即停——因为继续给错误提示往往也救不回来。

4. **Plan Mode 是怎么「说服」模型别写代码的？**
   不靠硬禁止，而是每轮在 system 里注入一条 reminder：目标是把任务拆成可执行方案
   （写到 plan 文件），需要用户批准后才进入执行。软约束，但模型遵循良好。

5. **一次工具调用从触发到下一轮，状态存在哪？**
   存在 `conversation`（消息历史）。模型看到的历史里，assistant 的工具调用和 tool_result
   成对出现，因此下一轮它能「看到自己刚做了什么、结果如何」，再决定下一步。

## 5. 测试绑定

**对应的测试文件：**
- `tests/test_agent.py` —— 主循环（单步工具调用、结束回合、超轮次护栏、取消、连续未知
  工具、plan mode 工具被拒、token 用量累计、`partition_tool_calls` 批划分）
- `tests/test_max_iterations.py` —— max_iterations 护栏：限制工具死循环、单轮即止

**怎么验证本模块：**
- 整文件：`uv run python -m pytest tests/test_max_iterations.py -q`
- 单条用例：`uv run python -m pytest tests/test_agent.py::test_single_step_tool_call -q`

**需要知道：**
- `docs/测试说明.md` 是全部测试的入口与总表，可反查任意模块。
- `test_agent.py` 中 `test_multi_step_autonomous`（写前保护）与 `test_message_splicing`
  （消息拼接）为**存量差异，不修**，已在测试说明标注；其余全部通过。
- 主循环与 TUI 的联动（事件如何渲染成气泡/工具卡片）不在本模块绑定内，见 09 章。
