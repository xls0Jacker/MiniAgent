# 模块解读 02：LLM 客户端与流式响应

> 本文拆解 `autocode/client.py` + `autocode/agent.py` 中
> 的 `StreamCollector`，讲清「一个请求如何变成 UI 上一行行字」的完整链路。
> 全文基于本项目 `autocode/` 包（模块解读，供自研参考）。

## 1. 模块职责

`client.py` 是「网络边界」：负责把对话历史序列化成某厂商 HTTP 请求、发起流式请求、
再把字节流解析成**统一的事件对象**吐给上层。它隔离了三件事：

1. **协议差异** —— anthropic / openai / openai-compat 三种消息格式不同；
2. **流式形态差异** —— 各家 SSE 事件名、token 计数字段不同；
3. **错误差异** —— 认证失败、限流、网络错误统一映射成本地异常类型。

`agent.py` 里的 `StreamCollector` 是「消费方」：`async for` 事件流，一边转发给 UI，
一边聚合成一个完整的 `LLMResponse`（文本 + 工具调用 + token 用量）。

## 2. 核心数据结构

### 流式事件（`autocode/tools/base.py`）

LLM 流是按「增量」到达的，client 把每种增量解析成一个 dataclass：

```python
TextDelta(text)             # 一段正文
ThinkingDelta / ThinkingComplete   # 思考块增量与收尾
ToolCallStart(tool_name, tool_id)
ToolCallDelta(text)         # 工具参数正在流式累积
ToolCallComplete(tool_id, tool_name, arguments)   # 参数拼完，dict 化
StreamEnd(stop_reason, input_tokens, output_tokens,
          cache_read, cache_creation)             # 一轮收尾
```

`StreamEvent = TextDelta | ThinkingDelta | ... | StreamEnd` 是联合类型。UI 只管
`isinstance` 分支，不需要知道 HTTP 层发生了什么。

### 聚合结果（`autocode/agent.py`）

```python
class LLMResponse:
    text: str                 # 累积正文
    tool_calls: list[ToolCallComplete]
    thinking_blocks: list[ThinkingBlock]
    stop_reason: str
    input_tokens / output_tokens / cache_read / cache_creation: int
```

主循环判断「该回复还是该调工具」只看 `response.tool_calls` 是否为空。

### 异常类型（`autocode/client.py`）

```python
class LLMError(Exception): ...
class AuthenticationError(LLMError): ...   # 401：key 无效/缺失
class RateLimitError(LLMError): ...        # 429：触发限流
class NetworkError(LLMError): ...          # 网络不可达
```

## 3. 关键流程

### 三协议如何归一（`create_client`）

```python
def create_client(config: ProviderConfig) -> LLMClient:
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    elif config.protocol == "openai":
        return OpenAIClient(config)
    elif config.protocol == "openai-compat":
        return OpenAICompatClient(config)
```

三者各自实现 `stream()`：内部把 `ConversationManager` 里的消息按各自 schema 序列化，
读 HTTP SSE，遇不同事件类型 yield 对应的 `StreamEvent`。串行 buffer 客户端
（如某些兼容服务不支持流式工具参数增量）则等收到 `StreamEnd` 前的整段再拼。

### StreamCollector：转发 + 聚合（`agent.py`）

```python
async def consume(self, stream) -> AsyncIterator[AgentEvent]:
    async for event in stream:
        if isinstance(event, TextDelta):
            self.response.text += event.text
            yield StreamText(text=event.text)          # 逐字转发
        elif isinstance(event, ThinkingComplete):
            self.response.thinking_blocks.append(...)
        elif isinstance(event, ToolCallComplete):
            self.response.tool_calls.append(event)     # 累积工具调用
            yield ToolUseEvent(...)                    # 通知 UI：模型想调工具
        elif isinstance(event, StreamEnd):
            self.response.stop_reason = event.stop_reason   # 收尾填 token 用量
            self.response.input_tokens = event.input_tokens
```

一个 `async for` 就完成了「给 UI 的实时事件」和「给主循环的完整结果」双输出。

### context window 的两级解析（`resolve_context_window`）

1. 配置显式写了 `context_window` → 直接用（最高优先级）；
2. 否则对 anthropic 协议尝试 `GET {base_url}/v1/models/{model}` 拉取真实的
   `max_input_tokens`，成功后缓存到 config；
3. 拉不到 → 降级内置映射表 / 默认值。

关键设计：**第 2 层完全尽力而为，绝不抛异常**，因此启动时调用是安全的。

## 4. 要点小结

1. **为什么把流式事件设计成「增量」而非「整段」？**
   用户要的是「看到 AI 一个字一个字打出来」的体验。增量事件让 UI 能边收边渲染；
   完整信息（工具参数、token）在收尾事件里补齐，主循环不受影响。

2. **`StreamCollector` 一鱼两吃是怎么做到的？**
   在同一个 `async for` 循环里，每收一个事件就 `yield` 给 UI 一次（转发），同时写进
   `self.response`（聚合）。UI 和主循环各取所需，不需要两遍解析。

3. **prompt cache 的 token 怎么算？**
   Anthropic 把缓存前缀拆成 cache_read（命中）与 cache_creation（写入），input_tokens
   已排除这两块；OpenAI 只暴露 cache_read。`StreamEnd` 带上三个数字，便于统计真实成本。

4. **协议抽象失败时怎么兜底？**
   每个协议实现都要正确解析各自的流；遇到兼容服务不支持的能力（如不带思考块的流），
   client 退化为「缓冲到结尾再整段吐」——上层无感。

5. **`resolve_context_window` 为何用两/三级降级而非硬查？**
   避免「查不到模型元数据就让程序崩溃」。配置显式值 > 拉取真实值 > 内置映射/默认，
   每降一级都保证 Agent 仍可运行，只是估算精度下降。
