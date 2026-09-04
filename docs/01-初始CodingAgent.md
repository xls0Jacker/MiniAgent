# 模块解读 01：初始 Coding Agent

> 本文从 `autocode/` 的实际源码出发，
> 拆解 Agent 的骨架：CLI 入口、配置加载、LLM 客户端抽象，以及一个最小「跑通」的对话流程。
> 全文基于本项目 `autocode/` 包（模块解读，供自研参考）。

## 1. 模块职责

这一模块解决「如何让一个命令行程序具备 Agent 的最小雏形」，对应到本项目代码里是三层：

| 文件 | 职责 |
|------|------|
| `autocode/__main__.py` | CLI 入口：解析参数 → 加载配置 → 选 LLM 协议 → 进入 TUI 或非交互单发 |
| `autocode/config.py` | 配置模型与加载：providers（多厂商）、permission_mode、max_iterations 等 |
| `autocode/validator.py` | 对用户 YAML 配置做结构校验，把「脏配置」挡在启动前 |
| `autocode/client.py` | LLM 客户端抽象：统一 `stream()` 流式接口，屏蔽三厂商差异 |
| `autocode/agent.py` | Agent 主循环：接收输入 → 调模型 → 判断回复或调工具 → 循环或返回 |

第 1 章只做最小闭环：**用户一句话 → 配置读取 → 客户端请求 → 打印回复**。后续章节在这个
骨架上逐步长上工具、权限、压缩、session。

## 2. 核心数据结构

### LLM 客户端的统一接口（`autocode/client.py`）

三种厂商协议都实现同一个抽象基类，这样 Agent 主循环只认一套接口：

```python
class LLMClient(ABC):
    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """把对话历史流式吐成事件序列。"""
```

客户端按 `protocol` 区分三种实现（`create_client(config)` 工厂选择）：

- `AnthropicClient` —— Anthropic Messages API
- `OpenAIClient` —— OpenAI Responses API
- `OpenAICompatClient` —— OpenAI Chat Completions 兼容接口（可接任意 base_url，如本地服务）

### 配置模型（`autocode/config.py`）

```python
class ProviderConfig:
    name: str            # 配置里的别名，如 "deepseek"
    protocol: str        # anthropic | openai | openai-compat
    base_url: str
    model: str
    api_key: str
    context_window: int | None   # 模型窗口；None 时走映射表/异步拉取
```

`AppConfig` 持有 `providers: list[ProviderConfig]`、`permission_mode`、`mcp_servers`、
`hooks`、`max_iterations`（Agent 主循环最大轮次，护栏）。配置来自 `config.yaml`，
本地敏感配置（含 api_key）放 `.autocode/config.local.yaml`，会被 gitignore 屏蔽。

## 3. 关键流程

### 启动路径（`autocode/__main__.py` `main()`）

1. `load_config()` 读 YAML，先经 `validator.validate_config_structure` 结构化校验；
2. `permission_mode` 决定权限默认档位（命令行 `--mode` 可覆盖配置）；
3. 有 `-p "prompt"` 走非交互 `_run_prompt`（单轮跑完打印结果）；否则进 TUI。
4. `_run_prompt` 里：`create_client(provider)` → `create_default_registry()` 注册工具 →
   `Agent(...)` 构造 → `agent.run_to_completion(prompt, conv)`。

### 最小对话循环（`agent.py`）

`Agent.run()` 的主循环骨架（伪代码）：

```
iteration = 0
while True:
    iteration += 1
    if iteration > max_iterations:      # 护栏：防死循环
        yield ErrorEvent("...maximum iterations..."); break
    async for ev in collector.consume(client.stream(conv, system, tools)):
        yield ev                          # 流式转发给 UI
    if not response.tool_calls:           # 模型直接回答 → 结束
        yield TurnComplete(); break
    results = execute_tools(response.tool_calls)   # 执行工具
    conv.add_tool_results(results)        # 结果写回历史，进入下一轮
```

`StreamCollector` 把底层 `StreamEvent`（`TextDelta`/`ToolCallComplete`/`StreamEnd`…）累加
成完整的 `LLMResponse`，同时逐条转成给 UI 的 `AgentEvent`（`StreamText`/`ToolUseEvent`…）。
这样**底层流式细节与上层 UI 展示解耦**：UI 边收边渲染，主循环拿全量结果判断下一步。

## 4. 要点小结

1. **为什么客户端要做协议抽象？**
   三厂商的流式事件、工具调用格式都不同。统一成 `AsyncIterator[StreamEvent]` + 一个
   `LLMResponse` 聚合对象后，Agent 主循环和 TUI 只依赖这一层，换模型零改动主链路。

2. **`config.local.yaml` 为什么要和 `config.yaml` 分开、且 gitignore？**
   本地文件装 api_key 等敏感项，避免密钥进版本库。validator 对合并结果统一校验，
   漏配 key 会在 `create_client` 抛 `AuthenticationError` 被捕获并友好提示。

3. **`max_iterations` 护栏放在主循环哪里、起什么作用？**
   在每轮迭代**最前面**检查 `iteration > max_iterations`，达到即 yield `ErrorEvent`
   并 `break`，防止「模型永不结束、持续调工具」的死循环烧 token。它和
   `consecutive_unknown`（连续未知工具告警）是两道不同护栏。

4. **`-p` 非交互模式与 TUI 模式共用什么？**
   共用整个 Agent 主循环、工具注册、权限检查；差别只在「事件怎么展示」——
   非交互 `print_result` 聚合打印，TUI 把事件渲染成消息气泡 / 工具卡片。

5. **`StreamCollector` 为什么既要 yield 又要累积？**
   UI 需要「逐字流式」体验（边收边渲染），主循环需要「完整 response」判断下一步
   （有没有工具调用）。一个 collector 同时承担转发与聚合两个角色，避免两套解析。

## 5. 测试绑定

**对应的测试文件：**
- `tests/test_context_window.py` —— 模型 context window 的解析/映射/降级（对应本模块
  配置模型与启动路径中的窗口决定逻辑）
- `tests/test_mcp.py` —— 配置里的 MCP servers 加载（`autocode/config.py` 的配置模型）
- `tests/test_agent.py::test_stop_*` —— 最小对话循环的护栏（取消 / 超时 / 连续未知工具）

**怎么验证本模块：**
- 整文件（全绿）：`uv run python -m pytest tests/test_context_window.py -q`
- 单条用例（护栏）：`uv run python -m pytest tests/test_agent.py::test_stop_cancel -q`
- 单类用例（MCP 配置加载）：`uv run python -m pytest "tests/test_mcp.py::TestResolveEnvVars" "tests/test_mcp.py::TestBuildChildEnv" -q`

**需要知道：**
- `docs/测试说明.md` 是全部测试的入口与总表，可反查任意模块。
- `tests/test_mcp.py::TestLoadConfigMCP` 中 4 项为存量差异（validator 行为不一致），已在
  测试说明标注「不修」；本模块绑定只看 `TestResolveEnvVars` / `TestBuildChildEnv` 等配置
  相关绿色用例。
- 真实 LLM 行为（协议选型、`-p` 非交互单发）不在 mock 测试范围，需 TUI 手动验证。
