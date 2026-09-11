from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from autocode.config import ProviderConfig
from autocode.conversation import ConversationManager
from autocode.serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_openai_input,
)
from autocode.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)


# 限制自动拉取模型元数据的超时时间，防止慢响应或挂起的
# /v1/models 端点拖延启动。超时后降级为 None（即"未知"），
# 由下一层 context window 解析逻辑接管。
ANTHROPIC_MODEL_FETCH_TIMEOUT = 3.0


_EPHEMERAL = {"type": "ephemeral"}


def _mark_last_user_tail_for_cache(messages: list[dict[str, Any]]) -> None:
    """给最后一条 user 消息的最后一个 block 附加 cache_control。

    会原地修改 `messages`。Anthropic 会缓存到（且包含）这个 block 为止的前缀；
    后续请求只要前缀逐字节相同，缓存命中的 token 只需支付 10% 的费用。
    仅适用于 Anthropic 协议的消息。
    """
    if not messages:
        return
    # 从后往前找到最后一条 user 角色消息；assistant 尾部不能锚定 cache。
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            # 把字符串 content 升级为 block 形式，以便附加 cache_control。
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": _EPHEMERAL,
            }]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = _EPHEMERAL
        return


def _mark_last_tool_for_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回一个浅拷贝的 tools 列表，并在最后一个 tool 上标记 cache_control。

    tool schema 在多轮对话之间是稳定的，因此标记列表尾部即可缓存整个 tool block。
    我们不直接修改调用方传入的列表，因为这些 tool schema 往往是注册表里的
    模块级单例。
    """
    if not tools:
        return tools
    marked = list(tools)
    last = dict(marked[-1])
    last["cache_control"] = _EPHEMERAL
    marked[-1] = last
    return marked


class LLMError(Exception):
    """所有 LLM 客户端错误的基类（协议无关，供上层统一捕获）。"""
    pass


class AuthenticationError(LLMError):
    """API key 缺失或无效。"""
    pass


class RateLimitError(LLMError):
    """被限流；retry_after 若已知则为建议等待秒数。"""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after   # None = 未知，上层按默认策略退避


class NetworkError(LLMError):
    """连接/网络层故障（含 TLS、DNS、超时）。"""
    pass


class LLMClient(ABC):
    """所有厂商客户端的统一抽象：Agent 主循环只认这一个接口。"""

    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """把整段对话流式请求出去，逐条产出协议无关的 StreamEvent。

        输入: conversation——对话历史；system——系统提示；tools——工具 schema。
        作用: 屏蔽三厂商差异（请求格式 / 流事件命名 / usage 归因），转成统一的
              TextDelta / ToolCall* / StreamEnd 事件流。
        输出: 异步事件流；协议错误映射为 LLMError 子类抛出。
        """
        yield TextDelta("")

    def set_max_output_tokens(self, tokens: int) -> None:
        """调整最大输出 token 预算（如主循环输出截断后的升级/降级）。"""
        pass


def _supports_adaptive_thinking(model: str) -> bool:
    """判断模型是否支持「自适应 thinking」（预算=0 由模型自定）。

    输入: model——完整模型名。
    作用: 早期 Opus/Sonnet 版本需要显式 budget_tokens；新一代（版本号 ≥ 6）
          支持传 0 表示自适应，不传就退化为旧式预算。
    输出: bool——是否新一代支持自适应。
    """
    for family in ("claude-opus-4-", "claude-sonnet-4-"):
        if model.startswith(family):
            rest = model[len(family):]
            if rest and rest[0].isdigit() and int(rest[0]) >= 6:
                return True
    return False


class AnthropicClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        """构造 Anthropic 客户端；key 缺失/无效直接抛 AuthenticationError。

        输入: config——该 provider 的配置。
        作用: 预取模型名 / thinking 开关 / 输出预算，用 AsyncAnthropic 建连接。
        输出: 无（副作用：初始化 self._client 连接句柄）。
        """
        self.model = config.model
        self.thinking = config.thinking
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            # 启动即失败好过发请求后再被 401：把认证错误前移到构造期
            raise AuthenticationError(
                "Anthropic API key not found. "
                "Set it in .autocode/config.yaml or via ANTHROPIC_API_KEY env var."
            )
        self._client = AsyncAnthropic(api_key=api_key, base_url=config.base_url)

    def set_max_output_tokens(self, tokens: int) -> None:
        """供主循环在输出截断时调高预算。"""
        self.max_output_tokens = tokens

    async def fetch_model_context_window(self) -> int | None:
        """向 Anthropic 兼容的 /v1/models/{model} 端点查询模型的
        max_input_tokens（context window 解析的第 2 层）。

        采用尽力而为策略：遇到任何错误——非 anthropic 端点、网络故障、
        超时、字段缺失——都返回 ``None`` 而非抛出异常，以便调用方降级到
        下一层。它的阻塞时间不会超过 ANTHROPIC_MODEL_FETCH_TIMEOUT，也不会
        向外传播异常，因此在启动时调用是安全的。
        """
        try:
            info = await self._client.models.retrieve(
                self.model, timeout=ANTHROPIC_MODEL_FETCH_TIMEOUT
            )
            window = getattr(info, "max_input_tokens", None)
            if isinstance(window, int) and window > 0:
                return window
            return None
        except Exception:
            return None

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Anthropic Messages API 实现：把流事件翻译成统一 StreamEvent。

        输入: conversation / system / tools——同上接口约定。
        作用: 打上 prompt-cache 断点 → 按需开启 thinking → 消费 content_block_*
              流事件，thinking 块累积、tool 参数块结束时 JSON 解析成 dict。
        输出: 异步事件流；出错时按类型映射成 LLMError 子类。
        """
        import anthropic as _anthropic

        # 先经序列化层把内部历史转成 Anthropic messages 格式
        messages = build_anthropic_messages(conversation.get_messages())

        # 在最长稳定前缀上标记 prompt cache 断点：system、tools
        # 以及最后一条 user 消息的尾部。Anthropic 会缓存到每个断点，
        # 并在下次请求时按字节比对——context.manager 中的
        # ContentReplacementState 保证断点之后的 tool_result 内容保持稳定。
        _mark_last_user_tail_for_cache(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": messages,
        }
        # system 也要锚一个 cache 断点——它是前缀中最稳定的部分
        if system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = _mark_last_tool_for_cache(tools)

        if self.thinking:
            if _supports_adaptive_thinking(self.model):
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 0}
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(self.max_output_tokens - 1, 1024),
                }

        # 跨 content block 的流式累积状态：tool 参数/thinking 都靠这些变量归并
        current_tool_name = ""
        current_tool_id = ""
        json_accum = ""
        in_thinking = False
        thinking_accum = ""
        thinking_signature = ""

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            # 进入 thinking 块：先复位累积器，供后续 delta 填充
                            in_thinking = True
                            thinking_accum = ""
                            thinking_signature = ""
                        elif block.type == "tool_use":
                            # 新工具调用开始：记下 name/id，参数靠后续 delta 累积
                            current_tool_name = block.name
                            current_tool_id = block.id
                            json_accum = ""
                            yield ToolCallStart(
                                tool_name=current_tool_name,
                                tool_id=current_tool_id,
                            )
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(text=delta.text)
                        elif delta.type == "thinking_delta":
                            thinking_accum += delta.thinking   # 累积，块结束才整体上报
                            yield ThinkingDelta(text=delta.thinking)
                        elif delta.type == "signature_delta":
                            thinking_signature = delta.signature   # 签名只取最后一块
                        elif delta.type == "input_json_delta":
                            json_accum += delta.partial_json
                            yield ToolCallDelta(text=delta.partial_json)
                    elif event.type == "content_block_stop":
                        if in_thinking:   # thinking 块结束 → 收束上报签名
                            yield ThinkingComplete(
                                thinking=thinking_accum,
                                signature=thinking_signature,
                            )
                            in_thinking = False   # 复位，防下一个块误判仍在 thinking
                        if current_tool_name:   # tool 块结束 → 整段参数 JSON 解析
                            try:
                                args = json.loads(json_accum) if json_accum else {}
                            except json.JSONDecodeError:
                                args = {}   # 流偶尔截断成非法 JSON，兜底给空参数
                            yield ToolCallComplete(
                                tool_id=current_tool_id,
                                tool_name=current_tool_name,
                                arguments=args,
                            )
                            # 一次 tool 调用处理完，清空跨块状态
                            current_tool_name = ""
                            current_tool_id = ""
                            json_accum = ""
                    elif event.type == "message_stop":
                        pass

                # 流结束：取终态消息拿真实 usage（含 cache 命中计数）
                final = await stream.get_final_message()
                usage = final.usage
                yield StreamEnd(
                    stop_reason=final.stop_reason or "end_turn",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_creation=getattr(
                        usage, "cache_creation_input_tokens", 0
                    ) or 0,
                )

        except _anthropic.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _anthropic.RateLimitError as e:
            retry = e.response.headers.get("retry-after") if e.response else None
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=float(retry) if retry else None,
            ) from e
        except _anthropic.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _anthropic.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e


class OpenAIClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        """构造面向 OpenAI Responses API（/responses）的客户端。

        输入: config——该 provider 的配置。
        作用: 预取模型名 / 输出预算，建 AsyncOpenAI 连接；key 缺失即抛认证错。
        输出: 无（副作用：初始化 self._client）。
        """
        self.model = config.model
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "OpenAI API key not found. "
                "Set it in .autocode/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=config.base_url)

    def set_max_output_tokens(self, tokens: int) -> None:
        """供主循环在输出截断时调高预算。"""
        self.max_output_tokens = tokens

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """OpenAI Responses API 实现：把官方流事件翻译成统一 StreamEvent。

        输入: conversation / system / tools——同上接口约定。
        作用: 走 /responses 端点的 function_call 增量事件，按 name 首次出现触发
              ToolCallStart、累积参数、结束时解析成 ToolCallComplete。
        输出: 异步事件流；出错时按类型映射成 LLMError 子类。
        """
        import openai as _openai

        # 内部历史 → Responses API 的 input 结构
        input_messages = build_openai_input(conversation.get_messages())

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_messages,
            "stream": True,
        }
        # Responses API 的 system 提示走顶层 instructions 字段
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = tools   # 已是 Responses 风格 schema，无需转换

        current_tool_name = ""
        current_call_id = ""
        json_accum = ""

        try:
            response_stream = await self._client.responses.create(**kwargs)
            async for event in response_stream:
                if event.type == "response.output_text.delta":
                    yield TextDelta(text=event.delta)
                elif event.type == "response.function_call_arguments.delta":
                    # 首个参数 delta 才带 name/call_id——用它触发 ToolCallStart
                    if not current_tool_name:
                        current_tool_name = getattr(event, "name", "") or ""
                        current_call_id = getattr(event, "call_id", "") or ""
                        if current_tool_name:
                            yield ToolCallStart(
                                tool_name=current_tool_name,
                                tool_id=current_call_id,
                            )
                    json_accum += event.delta
                    yield ToolCallDelta(text=event.delta)
                elif event.type == "response.function_call_arguments.done":
                    # 参数下完：把累积 JSON 解析成 dict 收尾该次调用
                    if not current_tool_name:
                        current_tool_name = getattr(event, "name", "") or ""
                        current_call_id = getattr(event, "call_id", "") or ""
                    try:
                        args = json.loads(json_accum) if json_accum else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield ToolCallComplete(
                        tool_id=current_call_id,
                        tool_name=current_tool_name,
                        arguments=args,
                    )
                    # 复位，准备接下一次 function_call
                    current_tool_name = ""
                    current_call_id = ""
                    json_accum = ""
                elif event.type == "response.output_item.added":
                    # 部分 provider 先广播整个 item 再下发 delta，这里补一次 start
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        current_tool_name = getattr(item, "name", "")
                        current_call_id = getattr(item, "call_id", "")
                        json_accum = ""
                        yield ToolCallStart(
                            tool_name=current_tool_name,
                            tool_id=current_call_id,
                        )
                elif event.type == "response.completed":
                    resp = getattr(event, "response", None)
                    usage = getattr(resp, "usage", None) if resp else None
                    # Responses API 通过 input_tokens_details.cached_tokens
                    # 暴露 cache 命中数，没有 creation 计数。注意这里的
                    # input_tokens *包含*了缓存 token，所以需要减去它们，
                    # 保持 input + cache_read 可加性，与 Anthropic 对齐。
                    details = getattr(usage, "input_tokens_details", None)
                    cache_read = getattr(details, "cached_tokens", 0) or 0
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    yield StreamEnd(
                        stop_reason="end_turn",
                        input_tokens=max(input_tokens - cache_read, 0),
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                        cache_read=cache_read,
                        cache_creation=0,   # Responses 不报 cache creation
                    )

        except _openai.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=float(retry) if retry else None,
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e


class OpenAICompatClient(LLMClient):
    """面向 OpenAI 兼容 provider 的客户端，使用 Chat Completions API。

    与面向较新的 Responses API（``/responses``）的 ``OpenAIClient`` 不同，
    本客户端使用受广泛支持的 Chat Completions 端点（``/chat/completions``），
    因此能兼容任何暴露 OpenAI 兼容接口的 provider（例如 vLLM、Ollama、
    Together、Azure OpenAI 等）。
    """

    def __init__(self, config: ProviderConfig) -> None:
        """构造兼容客户端的 AsyncOpenAI 连接（指向任意 openai 兼容 base_url）。

        输入: config——该 provider 的配置。
        作用: 预取模型名 / 输出预算；key 缺失即抛认证错。
        输出: 无（副作用：初始化 self._client）。
        """
        self.model = config.model
        self.max_output_tokens = config.get_max_output_tokens()
        api_key = config.resolve_api_key()
        if not api_key:
            raise AuthenticationError(
                "OpenAI-compatible API key not found. "
                "Set it in .autocode/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(api_key=api_key, base_url=config.base_url)

    def set_max_output_tokens(self, tokens: int) -> None:
        """供主循环在输出截断时调高预算。"""
        self.max_output_tokens = tokens

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 tool schema 转换成 Chat Completions 格式。

        tool 注册表为 ``openai`` 系列输出的是 Responses API 风格的 dict::

            {"type": "function", "name": "...", "description": "...",
             "parameters": {...}}

        而 Chat Completions 要求把 name/description/parameters 嵌套在
        ``function`` 键下::

            {"type": "function", "function": {"name": "...",
             "description": "...", "parameters": {...}}}
        """
        converted: list[dict[str, Any]] = []
        for t in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", t.get("input_schema", {})),
                },
            })
        return converted

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """OpenAI Chat Completions 实现：兼容 vLLM/Ollama/本地服务等。

        输入: conversation / system / tools——同上接口约定。
        作用: 走 /chat/completions 的流式增量；tool schema 转成嵌套 function 结构；
              按 chunk.choices[0].delta 分派文本 / 工具调用增量 / 收尾 usage。
        输出: 异步事件流；出错时按类型映射成 LLMError 子类。
        """
        import openai as _openai

        messages = build_chat_completion_messages(conversation.get_messages())

        # 如果有 system 消息则插入到消息列表头部。
        if system:
            messages = [{"role": "system", "content": system}] + messages

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},   # 让末 chunk 携带 usage
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        # 用于累积 streaming tool call 的状态。Chat Completions 流按
        # tool_calls 列表中的位置索引下发 delta，我们按索引跟踪每个进行中的调用。
        active_calls: dict[int, dict[str, str]] = {}  # 索引 -> {id, name, args}

        try:
            response = await self._client.chat.completions.create(**kwargs)
            async for chunk in response:
                if not chunk.choices:
                    # 最后一个 chunk，只包含 usage 数据。
                    if chunk.usage:
                        # 部分兼容 provider 通过 prompt_tokens_details.cached_tokens
                        # 上报 cache 命中数，大多数不上报（cache_read 保持 0）。
                        # prompt_tokens 包含了缓存 token，需要减去以保持
                        # input + cache_read 可加性。没有 provider 上报 creation 计数。
                        details = getattr(
                            chunk.usage, "prompt_tokens_details", None
                        )
                        cache_read = getattr(details, "cached_tokens", 0) or 0
                        prompt_tokens = chunk.usage.prompt_tokens or 0
                        yield StreamEnd(
                            stop_reason="end_turn",
                            input_tokens=max(prompt_tokens - cache_read, 0),
                            output_tokens=chunk.usage.completion_tokens or 0,
                            cache_read=cache_read,
                            cache_creation=0,
                        )
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # --- 文本内容 ---
                if delta and delta.content:
                    yield TextDelta(text=delta.content)

                # --- tool call 增量 ---
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        # 同一索引的多次 delta 归并到同一个进行中的调用
                        if idx not in active_calls:
                            active_calls[idx] = {"id": "", "name": "", "args": ""}
                        call = active_calls[idx]

                        if tc.id:
                            call["id"] = tc.id
                        if tc.function and tc.function.name:
                            call["name"] = tc.function.name
                            # name 首次出现即认为该调用开始
                            yield ToolCallStart(
                                tool_name=call["name"],
                                tool_id=call["id"],
                            )
                        if tc.function and tc.function.arguments:
                            call["args"] += tc.function.arguments
                            yield ToolCallDelta(text=tc.function.arguments)

                # --- 结束原因 ---
                if choice.finish_reason in ("tool_calls", "stop"):
                    if choice.finish_reason == "tool_calls":
                        # 流结束前把每个累积的调用按发起顺序收尾解析
                        for _idx, call in sorted(active_calls.items()):
                            try:
                                args = json.loads(call["args"]) if call["args"] else {}
                            except json.JSONDecodeError:
                                args = {}   # 截断容错，交给调用方按空参处理
                            yield ToolCallComplete(
                                tool_id=call["id"],
                                tool_name=call["name"],
                                arguments=args,
                            )
                        active_calls.clear()

        except _openai.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=float(retry) if retry else None,
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            raise LLMError(f"API error ({e.status_code}): {e.message}") from e


def create_client(config: ProviderConfig) -> LLMClient:
    """按 config.protocol 工厂式选型，返回对应 LLM 客户端实例。

    输入: config——单个 provider 的配置。
    作用: 把协议字符串（anthropic/openai/openai-compat）映射到具体实现类。
    输出: LLMClient 子类实例；协议未知抛 ValueError。
    """
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    elif config.protocol == "openai":
        return OpenAIClient(config)
    elif config.protocol == "openai-compat":
        return OpenAICompatClient(config)
    raise ValueError(f"Unknown protocol: {config.protocol}")   # 正常已被 validator 挡住


async def resolve_context_window(config: ProviderConfig) -> None:
    """context window 解析的第 2 层：对于 anthropic 协议的 provider，
    从 {base_url}/v1/models/{model} 自动拉取一次模型的 max_input_tokens，
    并通过 set_fetched_context_window 缓存到 ``config`` 上，这样后续
    config.get_context_window() 调用就能直接使用、无需再次访问网络。

    完全尽力而为，绝不抛出异常：非 anthropic provider、客户端构造失败
    （例如缺少 API key）、拉取失败或超时，都会让缓存保持不变，从而让
    get_context_window() 降级到内置映射表 / 默认值。在启动时调用是安全的——
    阻塞时间不会超过拉取自身的超时，也不会导致崩溃。
    """
    # 配置中显式指定的 window 在 get_context_window() 中优先级最高，
    # 上次调用已缓存的值也不需要重新拉取——直接跳过网络请求。
    if config.context_window > 0 or config._fetched_context_window > 0:
        return
    if config.protocol != "anthropic":
        return   # 只有 anthropic 协议的 provider 实现了 /v1/models 拉取

    try:
        client = create_client(config)
    except Exception:
        return   # 连客户端都建不起来（如缺 key）——降级到映射表/默认值
    fetch = getattr(client, "fetch_model_context_window", None)
    if fetch is None:
        return   # 该实现不提供拉取能力，直接跳过

    try:
        window = await fetch()
    except Exception:
        window = None   # 拉取失败不阻塞启动，保持缓存为 0 走下一层
    if window:
        config.set_fetched_context_window(window)
