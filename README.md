# AutoCode MiniAgent — 从零实现的最小可用 Agent

一个跑在终端里的最小 Agent：你输入一句话，它（ReAct 循环地）判断「直接回答」还是
「调用工具」，自己动手查文件、跑命令、算数、查知识库，直到给出答案。

> 本项目与 AI 协作开发：核心 Agent Runtime 从零实现，不依赖 langgraph / openhands /
> openclaw 等任何现成 agent 框架，直接对接真实 LLM API。仓库内的 AutoCode MiniAgent
> 为一个命令行 Agent 的完整参考实现，聚焦于基础能力闭环。

特性一览：

- **自研 Agent 主循环** —— ReAct 交替：思考 →（可选）调工具 → 看结果 → 再思考，直到回答
- **三协议 LLM 客户端** —— anthropic / openai / openai-compat，统一流式事件接口
- **工具注册机制** —— 名称 + 描述 + pydantic 参数 Schema，LLM 自主决策调用
- **演示工具** —— Calculator / Search / Weather / Todo（全本地、确定性、离线可测）
- **五层权限防御** —— 模式矩阵 / 危险命令黑名单 / 路径沙箱 / 规则引擎 / HITL 人工确认
- **上下文压缩** —— 双层（tool-result 预算 + LLM 摘要），长对话可持续
- **会话管理** —— 多窗口独立、JSONL 持久化、断点续聊
- **自研 TUI**（Textual）—— 流式打字机、工具卡片、权限弹窗、会话切换

---

## Quick Start

环境：Python ≥ 3.11（实测 3.13），uv 或 pip。

```bash
# 1. 安装依赖
uv sync                 # 或 pip install -e .

# 2. 配置 LLM API
#    复制示例配置，填入你的 api_key / base_url / model
cp config.example.yaml .autocode/config.local.yaml
#    编辑 .autocode/config.local.yaml（已 gitignore，不会误传密钥）

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
    api_key: ${YOUR_ANTHROPIC_KEY}  # ← 填你的 key（此文件不提交 git）

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

### 架构图

```
┌─────────────── TUI (Textual) ───────────────┐
│  聊天区 · 工具卡片 · 权限弹窗 · 会话列表     │
└───────────────┬──────────────────────────────┘
                │ AgentEvent 事件流（yield）
┌───────────────▼──────────────────────────────┐
│              Agent 主循环（ReAct）           │
│                                              │
│  while True:                                 │
│    if iteration > max_iterations: break      │  护栏
│    auto_compact()          ← 上下文太长压缩    │  Layer 2
│    build_system_prompt()   ← 身份/准则/环境    │
│    stream()  ← LLM 客户端（三协议统一流式）     │
│    tool_calls? 否 → 回答，结束                │
│    是 → partition → 权限检查 → 执行 → 回填     │
└──────┬──────────────────────┬───────────────┘
       │                      │
┌──────▼───────┐      ┌───────▼──────────────┐
│ ToolRegistry │      │ PermissionChecker    │
│  ReadFile    │      │  模式矩阵 / 黑名单    │
│  Bash/Grep…  │      │  沙箱 / 规则引擎      │
│  Calculator  │      │  HITL ask            │
│  Search/Wea… │      └──────────────────────┘
│  Todo(会话)  │
└──────────────┘
   ┌──────────────── 横切 ────────────────┐
   │ Session(JSONL) · 压缩(boundary) ·     │
   │ Memory(召回) · Hook · MCP(可选扩展)   │
   └───────────────────────────────────────┘
```

### Agent 主循环

一次输入 → 循环推进到最终回答：**接收输入 → 判断回复或调工具 →（调工具）→ 结果回填 →
继续或返回**。终止判据是模型给出**不再调用工具**的最终回答；`max_iterations`（默认 50）
与「连续未知工具」双护栏兜底，防止模型不收敛烧钱。

### 工具系统

每个工具 = `name` + `description` + pydantic `Params`。pydantic 模型一鱼三吃：
`get_schema()` 喂给 LLM 决策、`execute()` 拿强类型参数、非法参数自动报错。
并发安全的工具自动聚成同批并行执行；超长结果由上下文层截断/落盘。

### 上下文压缩（双层）

- **Layer 1**：每轮请求前，把过长的 tool result 截断/持久化/裁剪陈旧项；
- **Layer 2**：接近窗口上限时，用 LLM 把**旧前缀摘要成一段、尾部原文保留**，
  压缩结果以 compact-boundary 落盘，重启可续。

### Memory 的召回时机与放置方式

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

一句话：**常驻记忆垫底（历史最前），即时召回贴当前问题（当轮消息后），二者都以
system-reminder 形式进入上下文，而非散进正文**。

---

## 演示工具与示例提问

在 TUI 里直接问 Agent（它会自己决定调哪个工具）：

| 工具 | 示例提问 | 说明 |
|------|---------|------|
| Calculator | 「3.5×128+2 等于多少？」 | AST 白名单安全求值，杜绝 eval 注入 |
| Search | 「agent loop 是什么？给我查一下」 | 本地知识库 mock，确定性、离线可测 |
| Weather | 「北京现在天气怎么样？」 | 城市名哈希生成确定性伪数据 |
| Todo | 「记一条待办：写 README」「列出我的待办」 | 按会话（窗口）隔离存储 |

> Weather 走内置 Tool（本地 mock）而非 MCP——演示重点在「工具注册 → LLM 自主调用」
> 机制本身，避免引入网络/服务依赖。MCP 接入能力保留在代码中（`autocode/mcp/`）
> 作开放式扩展。

---

## 测试

```bash
uv run python -m pytest tests/ -q
```

新增测试（离线、确定性、无需真实 LLM）：

- `tests/test_demo_tools.py` —— 4 个演示工具行为 + Schema 完整性 + Todo 会话隔离
- `tests/test_max_iterations.py` —— 主循环最大轮次护栏

详见 `docs/测试说明.md`。

---

## 模块文档（docs/）

9 篇中文文档，按模块解读源码：

| # | 文档 | 对应模块 |
|---|------|---------|
| 01 | [初始 Coding Agent](docs/01-初始CodingAgent.md) | 入口 / 配置 / 最小闭环 |
| 02 | [LLM 客户端与流式响应](docs/02-LLM客户端与流式响应.md) | client.py / StreamCollector |
| 03 | [工具注册与执行框架](docs/03-工具注册与执行框架.md) | tools/ · registry |
| 04 | [Agent 主循环与事件流](docs/04-Agent主循环与事件流.md) | agent.py ReAct loop |
| 05 | [System Prompt 组装管线](docs/05-SystemPrompt组装管线.md) | prompts.py |
| 06 | [权限系统](docs/06-权限系统.md) | permissions/ 五层防御 |
| 07 | [MCP 协议接入](docs/07-MCP协议接入.md) | mcp/ 开放式扩展 |
| 08 | [上下文压缩与 Token 管理](docs/08-上下文压缩与Token管理.md) | context/ 双层压缩 |
| 09 | [TUI 交互设计](docs/09-TUI交互设计.md) | app.py Textual 界面 |

另有 [测试说明](docs/测试说明.md)。

---

## 能力对照

| 能力 | 落地位置 |
|-----------|---------|
| 从零实现、不依赖 agent 框架 | 主循环 / 工具 / 权限 / 压缩 / session 全在 `autocode/` 自研 |
| 基本循环 | `agent.py` Agent.run（ReAct 交替） |
| ≥3 个演示工具 | Calculator / Search / Weather / Todo（`register_demo_tools`） |
| 工具注册机制 | `tools/base.py` Tool + `get_schema()` + ToolRegistry |
| LLM 输出解析 | `client.py` 三协议 → 统一 `StreamEvent` → `StreamCollector` |
| session 管理（多窗口/续聊） | `memory/session.py` Session/SessionManager + TUI `/session` |
| context 管理（轮次限制/压缩） | `max_iterations` + `context/manager.py` 双层压缩 |
| 基本异常处理 | 工具错误回填、护栏 ErrorEvent、LLMError 分类、熔断 |
| 工具调用 trace / 日志 | ToolUseEvent/ToolResultEvent + `.autocode/debug.log` |
| 测试用例 | `tests/`（demo 工具 + max_iterations） |
| 真实 LLM API | 三协议客户端，`config.local.yaml` 配 key |

---

## 目录结构

```
MiniAgent/
├── autocode/                 # 主包（自研 Runtime + TUI）
│   ├── __main__.py           # CLI 入口（TUI / -p 非交互）
│   ├── agent.py              # Agent 主循环 + 事件流
│   ├── client.py             # 三协议 LLM 客户端
│   ├── prompts.py            # System Prompt 组装管线
│   ├── app.py                # Textual TUI 主应用
│   ├── conversation.py       # 对话历史 + token 估算 + 注入
│   ├── tools/                # Tool 基类 / registry / 编码工具 / 演示工具
│   ├── permissions/          # 五层权限防御
│   ├── context/              # 双层上下文压缩
│   ├── memory/               # session / auto_memory / recall
│   ├── mcp/                  # MCP 协议（可选扩展）
│   └── ...
├── tests/                    # 测试
├── docs/                     # 模块解读文档 / 测试说明
├── config.example.yaml       # 配置样例
├── pyproject.toml
└── .autocode/config.local.yaml   # 本地配置（gitignore，勿提交）
```
