[English](README.md) | **简体中文**

# AutoCode MiniAgent — 终端 Coding Agent Harness

一个跑在终端里的 Coding Agent：你输入一句话，它（ReAct 循环地）判断「直接回答」还是
「调用工具」，自己动手查文件、改代码、跑命令、算数，直到给出答案。

Harness 直接对接真实 LLM API。`autocode/` 约 **12.5k 行**，`tests/` 约 **6.1k 行**测试。

特性一览：

- **Agent 主循环** —— ReAct 交替：思考 →（可选）调工具 → 看结果 → 再思考，直到回答
- **三协议 LLM 客户端** —— anthropic / openai / openai-compat，统一流式事件接口
- **工具注册机制** —— 名称 + 描述 + pydantic 参数 Schema，LLM 自主决策调用
- **权限分层（Layer 0–5）** —— 逐层下落、命中即返回：Plan 例外 · 只读命令白名单 ·
  危险命令黑名单 · 路径沙箱 · 规则引擎 · 模式矩阵 · HITL 人工确认
- **上下文压缩** —— 双层（tool-result 预算 + LLM 摘要），长对话可持续
- **会话管理** —— 多窗口独立、JSONL 持久化、断点续聊
- **长程记忆** —— 自动抽取 + 按当前问题召回，以 system-reminder 注入
- **生命周期 Hook** —— 15 个事件点，可配置为通知型或拦截型
- **MCP 接入** —— 外部 server 的工具桥接成本地工具，上层无感
- **终端界面**（Textual）—— 流式打字机、工具卡片、权限弹窗、会话切换

---

## 架构图

仓库带 **11 张可交互架构图**：1 张系统总览 + 10 张模块机制图。
**在线浏览全部图 → <https://xls0Jacker.github.io/MiniAgent/>**

每张图都是**自包含单文件 HTML**——HTML/CSS/JS/SVG 全在里面，零外部依赖。
点任意节点会打开「语义护照」面板（节点信息 + 源码出处），挂 `文件:行号` 的**真实源码证据**，
点一下就跳到 GitHub 上那一行。全图共 **353 条源码引用**，每条都对应源码里的真实位置。

![AutoCode MiniAgent 系统架构总览](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/system-architecture/preview.png)

**总览图**：12 节点、13 关系。主链路 `用户 → AutoCodeApp → Agent 主循环 → LLMClient → LLM API`；
上侧挂 `System Prompt 组装`、`双层压缩`，下侧挂 `权限分层`、`ToolRegistry → MCP Servers`、
`HookEngine`、`Session / Memory`。

> 上面是**缩略预览**：README 正文宽度约 880px，2048px 的原图会被缩小，节点小字读不清。
> 看细节请打开在线版，可缩放、可点节点 →
> **[system-architecture.html](https://xls0Jacker.github.io/MiniAgent/system-architecture/system-architecture.html)**
> （想在本地跑一份，见[总览图说明](docs/system-architecture/README.md#怎么看)）。

### 10 张模块机制图

总览图回答「一轮对话经过哪些模块」，这些图回答「**这个模块自己怎么工作**」：

| # | 图 | 模块 | 核心机制 | 规模 |
|---|----|------|---------|------|
| 1 | [agent-loop](https://xls0Jacker.github.io/MiniAgent/module-architecture/agent-loop/agent-loop.html) | Agent 主循环 | 每轮 8 阶段 + 3 道护栏 + 工具双路执行 | 15 节点 · 16 边 · 28 引用 |
| 2 | [prompt-assembly](https://xls0Jacker.github.io/MiniAgent/module-architecture/prompt-assembly/prompt-assembly.html) | System Prompt 组装 | 双路径合成：system 参数 vs 历史消息 | 12 节点 · 11 边 · 20 引用 |
| 3 | [llm-client](https://xls0Jacker.github.io/MiniAgent/module-architecture/llm-client/llm-client.html) | LLM 客户端 | 三协议分发 + 事件归一 + 缓存断点 | 12 节点 · 11 边 · 28 引用 |
| 4 | [tool-execution](https://xls0Jacker.github.io/MiniAgent/module-architecture/tool-execution/tool-execution.html) | 工具注册与执行 | 声明式元数据 + 并发/串行双路 + 输出闸 | 13 节点 · 13 边 · 32 引用 |
| 5 | [permissions](https://xls0Jacker.github.io/MiniAgent/module-architecture/permissions/permissions.html) | 权限分层 | Layer 0–5 逐层下落，命中即返回 | 12 节点 · 11 边 · 30 引用 |
| 6 | [context-compaction](https://xls0Jacker.github.io/MiniAgent/module-architecture/context-compaction/context-compaction.html) | 双层压缩 | Layer 1 结果预算 + Layer 2 摘要 + 熔断 | 15 节点 · 15 边 · 44 引用 |
| 7 | [hooks](https://xls0Jacker.github.io/MiniAgent/module-architecture/hooks/hooks.html) | Hook 引擎 | 通知型 vs 拦截型 + 条件表达式 | 14 节点 · 13 边 · 38 引用 |
| 8 | [memory-session](https://xls0Jacker.github.io/MiniAgent/module-architecture/memory-session/memory-session.html) | Session 与记忆 | JSONL 落盘/恢复 + 记忆抽取与召回 | 18 节点 · 17 边 · 54 引用 |
| 9 | [mcp](https://xls0Jacker.github.io/MiniAgent/module-architecture/mcp/mcp.html) | MCP 接入 | 两条传输 + 工具包装注册 + 懒重连 | 14 节点 · 12 边 · 39 引用 |
| 10 | [tui](https://xls0Jacker.github.io/MiniAgent/module-architecture/tui/tui.html) | TUI 交互 | 12 种 AgentEvent 的分发梯子 + 三处挂起 | 14 节点 · 13 边 · 40 引用 |

三张有代表性的模块机制图（点击开交互版）：

| [![Agent 主循环](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/module-architecture/agent-loop/preview.png)](https://xls0Jacker.github.io/MiniAgent/module-architecture/agent-loop/agent-loop.html) | [![权限分层](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/module-architecture/permissions/preview.png)](https://xls0Jacker.github.io/MiniAgent/module-architecture/permissions/permissions.html) | [![双层压缩](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/module-architecture/context-compaction/preview.png)](https://xls0Jacker.github.io/MiniAgent/module-architecture/context-compaction/context-compaction.html) |
|---|----|----|
| **Agent 主循环** —— 每轮 8 阶段 | **权限分层** —— Layer 0–5 下落 | **双层压缩** —— 预算 + 摘要 |

图由 [archify](https://github.com/tt-a1i/archify) 从 `.json` 规格生成，改文案/加节点改规格即可；
重新生成与校验命令见[模块图 README](docs/module-architecture/README.md)。

---

## Quick Start

环境：Python ≥ 3.11（实测 3.13），uv 或 pip。

```bash
# 1. 安装依赖
uv sync                 # 或 pip install -e .

# 2. 配置 LLM API
#    复制示例配置，填入你的 api_key / base_url / model
cp config.example.yaml .autocode/config.local.yaml
#    编辑 .autocode/config.local.yaml（不进仓库，密钥只留在本机）

# 3. 启动 TUI
uv run autocode

# 4. 或非交互单发
uv run autocode -p "3.5*128+2 等于多少？"
```

### 配置示例（`.autocode/config.local.yaml`）

任选一种协议，`protocol` 决定走哪套 API：

```yaml
permission_mode: default     # default | acceptEdits | plan | bypassPermissions | dontAsk
max_iterations: 50           # Agent 主循环最大轮次（护栏）

providers:
  - name: anthropic            # 方式一：Anthropic 官方
    protocol: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-5
    api_key: ${YOUR_ANTHROPIC_KEY}  # ← 填你的 key（此文件不会上传）

  # - name: my_openai_compat   # 方式二：任意 OpenAI 兼容服务（deepseek/本地等）
  #   protocol: openai-compat
  #   base_url: https://api.deepseek.com/v1
  #   model: deepseek-chat
  #   api_key: sk-...
```

> 多 provider 会都在启动菜单里出现，TUI 里选一个即可。`--mode` 命令行可覆盖权限模式。

---

## TUI 操作速览

| 操作 | 说明 |
|------|------|
| 直接输入回车 | 发送消息给 Agent |
| `/` | 斜杠命令菜单（`/session`、`/compact`、`/mode` 等） |
| `@文件路径` | 在输入里引用文件内容 |
| Tab | 补全 |
| ↑/↓ | 翻输入历史 |
| 点击工具卡片 | 折叠 / 展开工具调用详情 |
| Ctrl+O | 全部工具卡片折叠 / 展开 |
| Tab / Shift+Tab | 循环切换权限模式 |
| Esc / Ctrl+C | 取消当前流式回复 / 退出 |

会话彼此独立（JSONL 落盘）。`/session` 可查看、切换、恢复历史会话。

---

## 系统设计

整体分四层：**TUI 层**（Textual 应用）→ **Agent 层**（主循环 + 事件流）→
**能力层**（LLM 客户端 / 工具注册表 / 权限 / 压缩 / 记忆 / Hook）→
**协议层**（三家 LLM API + MCP）。下面按模块展开，每节对应一张该模块的机制图。

### Agent 主循环（[机制图](https://xls0Jacker.github.io/MiniAgent/module-architecture/agent-loop/agent-loop.html)）

一次输入 → 循环推进到最终回答：**接收输入 → 判断回复或调工具 →（调工具）→ 结果回填 →
继续或返回**。终止判据是模型给出**不再调用工具**的最终回答；`max_iterations`（默认 50）
与「连续 3 次未知工具」双护栏兜底，防止模型不收敛、白白烧掉 token 预算。

每轮的阶段顺序是固定的：轮次护栏 → `turn_start` hook → Layer 2 压缩 → `pre_send` hook →
组装 system prompt → 注入提醒 → Layer 1 预算 → 流式请求。工具执行分两路：并发安全的工具
聚成同批**并行**执行，其余逐个走**串行**路径（含 hook 拦截与权限确认）。

### 工具系统（[机制图](https://xls0Jacker.github.io/MiniAgent/module-architecture/tool-execution/tool-execution.html)）

每个工具 = `name` + `description` + pydantic `Params`。一份 pydantic 模型同时满足三处用途：
`get_schema()` 喂给 LLM 决策、`execute()` 拿强类型参数、非法参数自动报错。

是否并发由**工具自己声明的 `is_concurrency_safe`** 决定，不按「读/写」类别推断——
并发批走直通路径，串行批才过 hook 与权限闸。超长结果由上下文层截断或落盘。

### System Prompt 组装（[机制图](https://xls0Jacker.github.io/MiniAgent/module-architecture/prompt-assembly/prompt-assembly.html)）

发给模型的提示词**不在同一个地方**：`system` 参数只装固定 8 段（身份 / 准则 / 工具用法 /
语气 / 环境…），而**每轮变化的内容**——环境快照、长程记忆、plan 提醒——全部注入在**历史消息**里。
压缩会清掉这些注入，所以压缩后代码会重新注入一次。

### 权限系统（[机制图](https://xls0Jacker.github.io/MiniAgent/module-architecture/permissions/permissions.html)）

主循环执行工具前的**唯一关卡**：每个 `ToolCall` 都要过 `check()` 得到一个
allow / deny / ask。判定是 **Layer 0–5 顺序下落**，任何一层表态就立刻返回，**层序本身就是优先级**：

| Layer | 判定 |
|-------|------|
| 0 | Plan 模式例外（4 个只读工具白名单 + plan 文件豁免） |
| 1 | 安全的只读命令自动放行（前缀白名单，且不含管道/重定向） |
| 1b | 危险命令黑名单（8 条正则，命中即 deny） |
| 2 | 路径沙箱（文件类工具越界即 deny） |
| 3 | 规则引擎（user → project → local 三层，同层后者覆盖） |
| 4 | 权限模式矩阵兜底（6 模式 × 3 工具类别） |
| 5 | 以上都没表态 → ask，挂起交人（HITL） |

规则引擎排在模式矩阵之前：`permissions.yaml` 命中即返回，根本走不到兜底矩阵。

### 上下文压缩（双层）（[机制图](https://xls0Jacker.github.io/MiniAgent/module-architecture/context-compaction/context-compaction.html)）

- **Layer 1（每轮，无 LLM）**：请求前把过长的 tool result 截断 / 持久化 / 裁剪过期条目，
  三趟处理——单条超阈值落盘、总量超阈值按大小先落盘、超过 10 轮的过期结果裁剪；
- **Layer 2（阈值触发，调 LLM）**：接近窗口上限时把**旧前缀摘要成一段、尾部原文保留**，
  压缩结果以 compact-boundary 落盘，重启可续。带熔断器，连续失败即停手。

两层共用同一个落盘函数与同一个 session 目录——一套机制的两个入口。

### 记忆与会话（[机制图](https://xls0Jacker.github.io/MiniAgent/module-architecture/memory-session/memory-session.html)）

两类通道，一次「即时」、一次「常驻」：

1. **每轮即时召回（按用户当前问题）**
   - 时机：用户发出新消息时，后台并行发起一次**侧查询**（独立 LLM client + 独立
     mini-conversation，8s 超时，失败静默跳过，不阻塞主流程）。
   - 召回：`find_relevant_memories(query, …)` 对用户级/项目级记忆文件打分，挑最相关几条。
   - 放置：`render_reminder(命中)` 拼成一条 `<system-reminder>`，
     **注入位置 = 刚加入的用户消息之后、AI 回复之前**，随当轮请求进模型。

2. **启动常驻注入（项目指令 + 长期记忆）**
   - 时机：会话启动（Agent 构造后）一次性注入。
   - 放置：`inject_long_term_memory(instructions, memories)` 把 AUTOCODE.md 指令 +
     常驻记忆包成一个 system-reminder，**插在对话历史最前（index 0/1）**，
     `ltm_injected` 标记保证只注入一次。

一句话：**常驻记忆放在历史最前，即时召回紧贴当轮消息，二者都以 system-reminder
形式进入上下文，而不是散落在正文里**。

落盘是「一条消息拆成多条记录」，恢复是反过来拼；压缩边界内联摘要，所以压缩前的
原始前缀不必回放。

### LLM 客户端（[机制图](https://xls0Jacker.github.io/MiniAgent/module-architecture/llm-client/llm-client.html)）

三家协议的 API 形态完全不同（Messages / Responses / Chat Completions），但都被翻译成
**同一组 7 种 `StreamEvent`**——差异全部收敛在 `client.py` 内，上层只认这一套事件。
缓存断点打在 system / tools 尾 / 最后一条 user 消息三处。

### Hook 与 MCP（[Hook 图](https://xls0Jacker.github.io/MiniAgent/module-architecture/hooks/hooks.html) · [MCP 图](https://xls0Jacker.github.io/MiniAgent/module-architecture/mcp/mcp.html)）

**Hook** 分两类：通知型（跑完不干涉主流程）与拦截型（只有 `pre_tool_use`，可拒绝工具调用）。
条件用表达式匹配（`==` `!=` `=~` `~=`，`&&` 与 `||` 不可混用）。

**MCP** 把远端 server 暴露的工具包装成本地工具——包装类写死类别与并发标记，延迟加载参数
Schema，注册进同一个 `ToolRegistry`，所以主循环、权限、工具搜索**统统不用改一行**。

---

## 演示工具与示例提问

在 TUI 里直接问 Agent（它会自己决定调哪个工具）：

| 工具 | 示例提问 | 说明 |
|------|---------|------|
| Calculator | 「3.5×128+2 等于多少？」 | AST 白名单安全求值，杜绝 eval 注入 |
| Search | 「agent loop 是什么？给我查一下」 | 本地知识库 mock，确定性、离线可测 |
| Weather | 「北京现在天气怎么样？」 | 城市名哈希生成确定性伪数据 |
| Todo | 「记一条待办：写 README」「列出我的待办」 | 按会话（窗口）隔离存储 |

除演示工具外，还有一套**编码工具**（`ReadFile` / `WriteFile` / `EditFile` / `Bash` /
`Glob` / `Grep`），由 `create_default_registry` 注册。

> Weather 走内置 Tool（本地 mock）而非 MCP——演示重点在「工具注册 → LLM 自主调用」
> 机制本身，避免引入网络/服务依赖。MCP 接入能力保留在代码中（`autocode/mcp/`）
> 作开放式扩展。

---

## 测试

```bash
uv run python -m pytest tests/ -q
```

测试全部离线、确定性、无需真实 LLM。重点覆盖：

- `tests/test_demo_tools.py` —— 4 个演示工具行为 + Schema 完整性 + Todo 会话隔离
- `tests/test_max_iterations.py` —— 主循环最大轮次护栏
- `tests/test_permissions.py` —— 权限分层、路径沙箱、危险命令、规则引擎、端到端判定
- `tests/test_serialization.py` —— 三协议流式事件归一

详见 [测试说明](docs/testing.md)。

---

## 文档

### 模块解读（9 篇）

| # | 文档 | 对应模块 |
|---|------|---------|
| 01 | [初始 Coding Agent](docs/01-initial-coding-agent.md) | 入口 / 配置 / 最小闭环 |
| 02 | [LLM 客户端与流式响应](docs/02-llm-client-streaming.md) | client.py / StreamCollector |
| 03 | [工具注册与执行框架](docs/03-tool-registry.md) | tools/ · registry |
| 04 | [Agent 主循环与事件流](docs/04-agent-loop-events.md) | agent.py ReAct loop |
| 05 | [System Prompt 组装管线](docs/05-system-prompt.md) | prompts.py |
| 06 | [权限系统](docs/06-permissions.md) | permissions/ 分层防御 |
| 07 | [MCP 协议接入](docs/07-mcp.md) | mcp/ 开放式扩展 |
| 08 | [上下文压缩与 Token 管理](docs/08-context-compaction.md) | context/ 双层压缩 |
| 09 | [TUI 交互设计](docs/09-tui-design.md) | app.py Textual 界面 |

另有 [测试说明](docs/testing.md)。

### 架构图（11 张）

- [系统总览图](https://xls0Jacker.github.io/MiniAgent/system-architecture/system-architecture.html) —— 12 节点，一轮对话经过哪些模块
  （[怎么看](docs/system-architecture/README.md#怎么看)）
- [10 张模块机制图](docs/module-architecture/README.md) —— 每个模块自己怎么工作，见[上方表格](#10-张模块机制图)

---

## 目录结构

```
MiniAgent/
├── autocode/                 # 主包（Harness + TUI）
│   ├── __main__.py           # CLI 入口（TUI / -p 非交互）
│   ├── agent.py              # Agent 主循环 + 事件流
│   ├── client.py             # 三协议 LLM 客户端
│   ├── prompts.py            # System Prompt 组装管线
│   ├── app.py                # Textual TUI 主应用
│   ├── conversation.py       # 对话历史 + token 估算 + 注入
│   ├── tools/                # Tool 基类 / registry / 编码工具 / 演示工具
│   ├── permissions/          # 权限分层判定
│   ├── context/              # 双层上下文压缩
│   ├── memory/               # session / auto_memory / recall
│   ├── hooks/                # 生命周期钩子
│   ├── mcp/                  # MCP 协议（开放式扩展）
│   └── ...
├── tests/                    # 测试
├── docs/                     # 模块解读 / 架构图 / 测试说明
├── config.example.yaml       # 配置样例
├── pyproject.toml
└── .autocode/config.local.yaml   # 本地配置（不进仓库）
```
