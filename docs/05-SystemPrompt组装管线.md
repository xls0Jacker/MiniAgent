# 模块解读 05：System Prompt 组装管线

> 本文拆解 `autocode/prompts.py` 的
> `PromptBuilder`：如何把「身份、做事准则、工具用法、语气、环境、记忆、Hook 通知」
> 这些松散的片段按优先级拼成一个最终 system prompt。
> 全文基于本项目 `autocode/` 包（模块解读，供自研参考）。

## 1. 模块职责

一份好的 system prompt 决定了 Agent 的「行为下限」。本项目不把它写成一段死文本，
而是拆成**有优先级的分段**，动态组装：

- 静态段（身份、做事准则、工具用法、语气…）是代码里的常量；
- 动态段（工作目录、日期、长期记忆、Hook 输出、deferred 工具提示）运行期注入；
- 主循环里还有**更动态**的：plan mode reminder、tool-result budget 等作为
  system reminder 临时塞进对话。

`PromptBuilder` 只解决「分段 + 排序 + 拼接」；每段内容由各调用方准备。

## 2. 核心数据结构

### PromptSection + PromptBuilder（`prompts.py`）

```python
@dataclass
class PromptSection:
    name: str
    priority: int        # 越小越靠前
    content: str

class PromptBuilder:
    def add(self, section: PromptSection) -> PromptBuilder: ...
    def build(self) -> str:
        self._sections.sort(key=lambda s: s.priority)
        parts = [s.content.strip() for s in self._sections if s.content.strip()]
        return "\n\n".join(parts)
```

### 分段优先级表

| 优先级 | 段名 | 内容 |
|--------|------|------|
| 0 | Identity | 我是谁、安全红线（不注入命令/不瞎编 URL） |
| 10 | System | 工具被拒别重试、识别 prompt injection、hook 反馈当用户 |
| 20 | DoingTasks | 软件工程任务的行为准则（先读再改、不做投机抽象、忠于验证结果） |
| 30 | ExecutingActions | 危险/不可逆动作要谨慎、破坏性操作先问 |
| 40 | UsingTools | 优先专用工具而非 Bash、可并行就并行、deferred 工具用 ToolSearch |
| 50 | ToneStyle | 简洁、无 emoji、引用用 file:line、工具调用前不加冒号 |
| 60 | TextOutput | 用户的可见文本纪律：动手前一句话、不逐字解说内心戏、收尾总结一两句 |
| 70 | Environment | 工作目录 / 平台 / 日期（`environment_section()` 动态生成） |

## 3. 关键流程

### build_system_prompt / build_environment_context（`prompts.py`）

```python
def build_system_prompt(hook_prompts=None) -> str:
    b = PromptBuilder()
    b.add(IDENTITY_SECTION).add(SYSTEM_SECTION).add(DOING_TASKS_SECTION) \
     .add(EXECUTING_ACTIONS_SECTION).add(USING_TOOLS_SECTION) \
     .add(TONE_STYLE_SECTION).add(TEXT_OUTPUT_SECTION)
    if hook_prompts:                       # Hook 提供的动态提示段
        for hp in hook_prompts: b.add(PromptSection(name=hp.name, priority=95, content=hp.content))
    return b.build()

def build_environment_context(work_dir, ...) -> str:
    # 把工作区文件结构/活跃技能/记忆清单整理成环境描述，
    # 作为 conversation.inject_environment 的内容进入对话
```

主循环里（见 04 章）再叠加：

```python
system = build_system_prompt(hook_prompts=hook_prompts)
# 之后按需 add_system_reminder(...)：
#   - plan mode reminder     每轮：你在方案模式，别改代码
#   - deferred tools 提示     有延迟工具时：用 ToolSearch 加载
#   - hook notifications     钩子产出的通知
```

这些 reminder 不是拼进 system 字符串，而是作为**一条 system-role 消息**加进对话历史，
从而让压缩逻辑也能处理它们（见 08 章）。

### 记忆/环境在「对话层」而非「system 字符串」注入

注意区分两层：

1. **system 字符串**（`PromptBuilder`）：身份、静态准则——基本每轮固定；
2. **对话历史里的 system 消息**：环境、长期记忆、plan reminder、hook 输出——
   随轮次变化，且在压缩时作为一个整体被摘要。

`conversation.inject_environment(env_context)` 与 `inject_long_term_memory(instructions, mem)`
负责把第 2 层塞到消息序列合适的位置（详见 09 记忆章关于「召回时机与放置方式」）。

## 4. 要点小结

1. **为什么把 system prompt 拆成分段 + 优先级，而不是整段常量？**
   动态段（环境、记忆、hook）必须随时插入且不影响静态段。若整段常量，每加一个动态
   信息都要重新拼接整串、易出错。优先级排序保证「先身份、后行为、再环境」的稳定次序。

2. **用 system **消息**而非拼进 system 字符串，有什么好处？**
   plan reminder、hook 输出这类「会变」的内容若拼进 system 字符串，压缩时不好单独
   处理。作为 system-role 消息进对话历史后，它们和其它消息一样参与 token 计数、
   auto-compact 的裁剪与摘要——统一治理。

3. **Environment 段里放日期/平台为什么重要？**
   模型不知道今天几号、在什么系统上跑。给日期避免它编造时间，给平台（`platform.system()`）
   让它生成的命令/路径符合当前 OS。

4. **这些英文行为准则段的来源？**
   对应 Claude Code 的公开系统提示工程实践（识别注入、先读再改、忠于验证结果、
   破坏性操作先确认等）。本项目学习用改写，作为自研 Agent 的「行为下限」。

5. **plan mode 的提醒为什么必须每轮重发？**
   模型对 system prompt 的遵从会随上下文漂移。plan mode 是「不许改代码」的软约束，
   每轮迭代开头重发一次 reminder（含当前是否已有 plan 文件、迭代次数），能把「只出
   方案」的意图牢牢钉在模型近期记忆里。

## 5. 测试绑定

**对应的测试文件：**
- `tests/test_agent.py` —— prompt 组装相关用例：`test_system_prompt_normal`（普通段拼接）、
  `test_system_prompt_plan`（plan 段）、`test_plan_mode_sparse_reminder`（稀疏提醒）、
  `test_environment_context`（环境上下文构造）、`test_plan_mode` / `test_plan_mode_denied_tool_returns_error`
- `tests/test_context.py` —— 压缩消息的构造（`TestBuildCompactMessages`）

**怎么验证本模块：**
- 单条用例：`uv run python -m pytest tests/test_agent.py::test_system_prompt_normal tests/test_agent.py::test_environment_context -q`
- 整文件（含 prompt 相关）：`uv run python -m pytest tests/test_agent.py -q`（存量失败见下）

**需要知道：**
- `docs/测试说明.md` 是全部测试的入口与总表，可反查任意模块。
- `test_agent.py` 中 prompt 相关用例全部通过；`test_multi_step_autonomous` 与
  `test_message_splicing` 为**存量差异，不修**，与本小节无关。
- 每段 system prompt 的「实际措辞」对模型行为的长期影响不在 mock 测试范围，需真实 LLM
  在 TUI 中手动体验。
