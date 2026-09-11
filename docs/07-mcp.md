# 模块解读 07：MCP 协议 —— 开放式工具生态

> 本文拆解 `autocode/mcp/` 包：
> 如何把任意 MCP server 暴露的工具，无缝桥接成本项目 ToolRegistry 里一个普通 Tool，
> 让 Agent 主循环、权限系统、工具搜索统统不用改。
> 全文基于本项目 `autocode/` 包（模块解读，供自研参考）。

## 1. 模块职责

MCP（Model Context Protocol）是「Agent ↔ 外部工具服务」的统一协议。本项目保留
`autocode/mcp/` 作为**开放式扩展通道**：不把天气、数据库、浏览器等写成内置工具，
而是通过 MCP server 接入，内置工具只留核心编码 + 演示工具（见 03 章）。

Weather 演示工具**特意走内置 Tool 而非 MCP**（本目录的 Weather 用本地哈希
mock）——因为它要演示的是「工具注册 → LLM 自主调用」这一核心机制，走 MCP 会
把演示路径引入网络/服务依赖。MCP 接入能力保留在代码里，作为独立的生态扩展通道。

`autocode/mcp/` 三个文件：

| 文件 | 职责 |
|------|------|
| `client.py` | `MCPClient`：stdio / HTTP 两种传输的连接、list_tools、call_tool |
| `manager.py` | `MCPManager`：管理多个 server 配置，把每个工具注册进本地 registry |
| `tool_wrapper.py` | `MCPToolWrapper`：把远端 tool 包成本地 `Tool` 子类 |

## 2. 核心数据结构

### MCPClient（`client.py`）

```python
class MCPClient:
    def __init__(self, config: MCPServerConfig): ...
    async def connect(self): ...        # stdio 起子进程 / http 建会话
    async def list_tools(self) -> list[types.Tool]: ...
    async def call_tool(self, name, arguments) -> ...: ...
    async def close(self): ...
```

一个 client 对应一个 server。传输类型由配置的 `transport`（stdio / http）决定，
`_connect_stdio` 用 asyncio 子进程跑 server 命令，`_connect_http` 走流式 HTTP。

### MCPToolWrapper（`tool_wrapper.py`）—— 关键桥接

```python
class MCPToolWrapper(Tool):
    def __init__(self, server_name, tool_def, client):
        self.name = f"mcp_{server_name}_{tool_def.name}"   # 唯一命名，防冲突
        self.category = "command"
        self.should_defer = True      # 走延迟发现，不全量暴露
        self.params_model = _build_params_model(tool_def.name, tool_def.inputSchema)
```

**一次漂亮的适配**：MCP 工具描述里的 JSON Schema（`inputSchema`）被动态转成 pydantic
模型（`_build_params_model`），于是 MCP 工具也能享受本地 Tool 同款的
`execute(params: BaseModel)` 强类型入口 + `get_schema()` 自动出 Schema。

## 3. 关键流程

### MCPManager 把远端工具「搬」进 registry（`manager.py`）

```
启动时：
  MCPManager.load_configs(configs)          # 记录各 server 配置
  await manager.register_all_tools(registry) # 对每个 server：
      client = MCPClient(config); await client.connect()
      tools = await client.list_tools()
      for t in tools:
          registry.register(MCPToolWrapper(server, t, client))
```

注册后，这些 `mcp_xxx_yyy` 工具对 Agent 而言就是普通工具：
- 出现在 `registry.get_all_schemas()`（但因为 `should_defer=True`，默认不进每轮 tool 列表，
  需 ToolSearch 按名加载，控制 token——见 03 章延迟发现）；
- 调用时过同一套权限检查（`category="command"` → 默认 ask）；
- `MCPToolWrapper.execute` 内部调 `client.call_tool`，把远端返回的 content list 抽出纯文本
  拼成 `ToolResult`。

### 断线自愈

`execute()` 里先查 `client.is_alive`，断了就尝试 `await client.connect()` 重连，
失败返回 `ToolResult(is_error=True)`——错误走正常回填给模型，模型可换方案或重试。
退出时 `manager.shutdown()` 统一 close 各 client。

## 4. 要点小结

1. **MCP 工具和内置 Tool 在本项目里差别是什么？**
   无差别——都是注册表里的 Tool，主循环/权限/搜索一视同仁。差别只在「实现出处」：
   内置是本进程内 `execute`，MCP 是远端 server 通过协议调用。这就是适配层（wrapper）
   的价值。

2. **为什么要给 MCP 工具命名加 `mcp_{server}_{name}` 前缀？**
   两个 server 可能暴露同名工具（都叫 `search`），直接同名会互相覆盖注册。
   前缀按 server 隔离命名空间，同时保留原始名在 `mcp_tool_name` 供转发。

3. **MCP 工具为何设 `should_defer=True`？**
   一个 server 可能暴露几十上百个工具，全量塞进每轮请求既烧 token 又稀释注意力。
   defer 让它们按需加载（ToolSearch），与内置工具数量多时的策略一致（见 03 章）。

4. **为什么 Weather 演示不走 MCP？**
   这里要演示的核心是「自研 Runtime 的工具注册 → LLM 自主决策调用」。weather 走 MCP 需要
   起一个外部 server（网络/服务依赖），既偏离「最小可用、离线可测」的演示诉求，也让
   他人难以在无网环境复现。故用确定性 mock 的内置 Weather 工具讲注册机制，把 MCP
   作为扩展能力另行补充即可。

5. **MCP 的 JSON Schema 怎么转成本地 pydantic 模型？**
   `_build_params_model` 用 pydantic 的 `create_model` 动态建类：遍历 `inputSchema` 的
   `properties`，把 JSON type 映射到 Python 类型（`_json_type_to_python`），生成一个
   字段模型。动态创建让「不预知工具长什么样」成为可能。

## 5. 测试绑定

**对应的测试文件：** `tests/test_mcp.py`

**怎么验证本模块：**
- 单条用例：`uv run python -m pytest tests/test_mcp.py::TestMCPToolWrapper -q`
  （远端工具 → 本地 Tool 的桥接：`mcp_{server}_{name}` 命名、Schema 复用原 inputSchema）
- 单条用例：`uv run python -m pytest tests/test_mcp.py::TestMCPManagerPartialFailure -q`
  （单 server 失败不拖垮其它 server）
- 配置相关：`tests/test_mcp.py::TestResolveEnvVars`、`TestBuildChildEnv`（env 变量替换/子进程环境）

**需要知道：**
- `docs/testing.md` 是全部测试的入口与总表，可反查任意模块。
- `TestLoadConfigMCP` 中 4 项（stdio / http / 二选一 / 两者皆缺）为**存量差异，不修**
  （validator 校验行为与实现不一致），已在测试说明标注——本模块绑定不指向它们。
- 真实 MCP server 的连接 / 断线自愈 / 远端工具调用不在 mock 测试范围，需接真实 server
  在 TUI 中验证。
