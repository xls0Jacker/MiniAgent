from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from pydantic import ValidationError

from autocode.client import LLMClient
from autocode.context import (
    CompactBoundary,
    CompactCircuitBreaker,
    CompactEvent,
    ContentReplacementRecord,
    ContentReplacementState,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    create_replacement_state,
    ensure_session_dir,
    load_replacement_records,
    reconstruct_replacement_state,
)
from autocode.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from autocode.conversation import ThinkingBlock as ConvThinkingBlock
from autocode.memory.auto_memory import MemoryManager
from autocode.permissions import (
    Decision,
    PermissionChecker,
    PermissionMode,
)
from autocode.hooks import HookContext, HookEngine, ToolRejectedError
from autocode.hooks.engine import HookNotification
from autocode.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from autocode.tools import ToolRegistry
from autocode.tools.base import (
    MAX_OUTPUT_CHARS,
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
    ToolResult,
)

log = logging.getLogger(__name__)

# 记忆抽取节律：每跑 N 轮结束（无工具调用）触发一次记忆沉淀
MEMORY_EXTRACTION_INTERVAL = 5
# 输出 token 上限的「天花板」：max_tokens 截断后先升到这一档再试一次
MAX_TOKENS_CEILING = 64000
# 输出截断后的最大恢复次数，超出即放弃（护栏）
MAX_OUTPUT_TOKENS_RECOVERIES = 3


# ---------------------------------------------------------------------------
# AgentEvent 事件类型
# ---------------------------------------------------------------------------

@dataclass
class StreamText:
    text: str


@dataclass
class ThinkingText:
    text: str


@dataclass
class RetryEvent:
    reason: str
    wait: float = 0.0


@dataclass
class ToolUseEvent:
    tool_name: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    tool_id: str
    tool_name: str
    output: str
    is_error: bool
    elapsed: float


@dataclass
class TurnComplete:
    turn: int


@dataclass
class LoopComplete:
    total_turns: int


@dataclass
class UsageEvent:
    input_tokens: int
    output_tokens: int


@dataclass
class ErrorEvent:
    message: str


@dataclass
class CompactNotification:
    """通知 UI：本轮迭代发生了自动上下文压缩。"""

    before_tokens: int
    message: str
    # 结构化 boundary（摘要 + 原文保留尾部），UI/session 层用它持久化 compact_boundary 记录。
    # 失败路径下为 None。
    boundary: "CompactBoundary | None" = None


@dataclass
class HookEvent:
    """hook 产生的通知事件（hook_id + 触发事件 + 输出），UI 层逐条展示。"""

    hook_id: str
    event: str
    output: str
    success: bool


class PermissionResponse(Enum):
    """用户对一次权限请求的答复。"""

    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"   # 记住这次放行，写进本地权限规则


@dataclass
class PermissionRequest:
    tool_name: str
    description: str
    future: asyncio.Future[PermissionResponse]
    # 触发权限请求的工具调用参数（如 WriteFile 的 content / EditFile 的 old/new_string），
    # 供 UI 在确认框内渲染 diff 预览。可能为 None（老代码构造点未传）。
    arguments: dict[str, Any] | None = None


AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | UsageEvent
    | ErrorEvent
    | PermissionRequest
    | CompactNotification
    | HookEvent
)
# ↑ Agent.run() 对外 yield 的全部事件类型的并集（UI 据此渲染不同卡片）


# ---------------------------------------------------------------------------
# LLM 响应收集器
# ---------------------------------------------------------------------------

@dataclass
class ThinkingBlock:
    thinking: str
    signature: str


@dataclass
class LLMResponse:
    """一轮 LLM 回复的聚合结果：主循环靠它判断要不要调工具、怎么收尾。"""

    text: str = ""                                    # 累积正文
    tool_calls: list[ToolCallComplete] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    stop_reason: str = ""                             # max_tokens / end_turn / tool_use…
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0                               # cache 命中 token 数（计费便宜）
    cache_creation: int = 0


class StreamCollector:
    """把底层 StreamEvent 流同时「转发给 UI」与「聚合进 LLMResponse」。

    UI 要逐字流式（边收边渲染），主循环要完整 response（判断下一步），
    这一个 collector 同时承担两职，避免同一份事件流被解析两遍。
    """

    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(
        self, stream: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[AgentEvent]:
        """消费事件流，边累加边转成 UI 用的 AgentEvent 逐条 yield。

        输入: stream——客户端产出的底层 StreamEvent 流。
        作用: TextDelta/ToolCallComplete/StreamEnd 等累进 self.response；
              Thinking 增量直通转发（UI 要即时看到，不进 response 正文）。
        输出: 异步 AgentEvent 流。
        """
        async for event in stream:
            if isinstance(event, TextDelta):
                self.response.text += event.text   # 累加正文，供主循环判断
                yield StreamText(text=event.text)  # 转 UI 事件，边收边渲染
            elif isinstance(event, ThinkingDelta):
                yield ThinkingText(text=event.text)   # thinking 走独立通道，不进正文
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(
                    ThinkingBlock(thinking=event.thinking, signature=event.signature)
                )
            elif isinstance(event, ToolCallStart):
                pass   # start 只用于流内状态；聚合所需信息在 Complete 里
            elif isinstance(event, ToolCallDelta):
                pass   # 参数分片不回填 response，UI 也无需逐片展示
            elif isinstance(event, ToolCallComplete):
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
            elif isinstance(event, StreamEnd):
                # 终态 usage 落进 response，供 token 统计/压缩判断
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation


# ---------------------------------------------------------------------------
# tool 批量执行
# ---------------------------------------------------------------------------

@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]


def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    """把一批工具调用切成「并发 / 串行」交替的批次。

    输入: tool_calls——模型本轮请求的所有工具调用；registry——查并发安全标记。
    作用: 连续且并发安全（is_concurrency_safe 且启用）的调用并入同一并发批，
          否则另起一个串行批（串行批内逐个执行，防写类工具互踩）。
    输出: list[ToolBatch]——按原顺序排列的批次序列。
    """
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        safe = tool is not None and tool.is_concurrency_safe and registry.is_enabled(tc.tool_name)

        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)   # 接进上一个并发批
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


# ---------------------------------------------------------------------------
# streaming 执行器 — 在 LLM streaming 期间启动 tool 执行
# ---------------------------------------------------------------------------

@dataclass
class _ToolExecResult:
    tool_id: str
    tool_name: str
    result: ToolResult
    elapsed: float
    is_unknown: bool


class StreamingExecutor:
    """在 LLM streaming 期间并发跑已收到的工具调用（边收边执行）。

    先到先提交的任务并行执行；collect_results 按提交顺序（而非完成顺序）
    回收结果，保证 tool 结果回到历史的次序稳定。
    """

    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[_ToolExecResult]]] = []
        self._order = 0   # 提交序号：让结果按发起顺序而非完成顺序归位

    def submit(
        self,
        coro: Any,
    ) -> None:
        """把一个可执行协程丢进事件循环后台执行。"""
        task = asyncio.create_task(coro)
        self._tasks.append((self._order, task))
        self._order += 1

    async def collect_results(self) -> list[_ToolExecResult]:
        """等待所有已提交任务，按提交顺序返回结果（异常折叠为错误结果）。"""
        if not self._tasks:
            return []
        tasks = [t for _, t in sorted(self._tasks, key=lambda x: x[0])]
        results = await asyncio.gather(*tasks, return_exceptions=True)   # 不让单个工具炸掉整批
        out: list[_ToolExecResult] = []
        for r in results:
            if isinstance(r, Exception):
                # 协程内部没接住的异常 → 转成一条错误 ToolResult
                out.append(_ToolExecResult(
                    tool_id="",
                    tool_name="",
                    result=ToolResult(output=f"Tool execution error: {r}", is_error=True),
                    elapsed=0.0,
                    is_unknown=False,
                ))
            else:
                out.append(r)
        return out


# ---------------------------------------------------------------------------
# Agent 主循环
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        work_dir: str = ".",
        max_iterations: int = 50,
        permission_checker: PermissionChecker | None = None,
        context_window: int = 200_000,
        instructions_content: str = "",
        memory_manager: MemoryManager | None = None,
        hook_engine: HookEngine | None = None,
    ) -> None:
        """构造 Agent：绑定客户端 / 工具注册表 / 权限 / 压缩等子系统。

        输入: client(LLM)、registry(工具)、protocol、work_dir；可选权限检查器、
              context_window、指令、记忆管理、hook 引擎。
        作用: 备齐一轮对话所需的全部依赖与运行状态（session 目录、压缩熔断器、
              恢复快照等）。
        输出: 无（副作用：初始化实例状态）。
        """
        self.client = client
        self.registry = registry
        self.protocol = protocol   # 工具 schema 按协议生成
        self.work_dir = work_dir
        self.max_iterations = max_iterations   # 主循环轮次护栏
        self.permission_checker = permission_checker
        self.permission_mode: PermissionMode = (
            permission_checker.mode if permission_checker else PermissionMode.DEFAULT
        )
        self.context_window = context_window
        self.session_dir = ensure_session_dir(work_dir)   # .autocode/sessions/
        self.compact_breaker = CompactCircuitBreaker()    # 熔断：防反复压缩空转
        self.replacement_state: ContentReplacementState = create_replacement_state()
        # 保存重建工作上下文所需的快照，在 Layer 2 压缩对话后使用：
        # 最近的文件读取和 skill 调用。每次 ReadFile / skill 调用时记录，
        # auto_compact 触发阈值时消费。
        self.recovery_state: RecoveryState = RecoveryState()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.instructions_content = instructions_content
        self.memory_manager = memory_manager
        self.hook_engine = hook_engine
        self._loop_count = 0              # 无工具轮计数，决定何时抽取记忆
        self._extracting = False          # 防并发触发记忆抽取
        self.session_id: str = ""
        self.active_skills: dict[str, str] = {}
        self._skill_catalog: str = ""
        self.agent_id: str = uuid.uuid4().hex[:12]   # 日志里区分不同 agent 实例
        self.file_history: Any = None     # 由 TUI 注入，用来做会话文件快照

    @property
    def _transcript_path(self) -> str:
        """本会话的 jsonl 记录文件路径（未开会话则为空串）。"""
        if self.session_id:
            return str(Path(self.work_dir) / ".autocode" / "sessions" / f"{self.session_id}.jsonl")
        return ""

    @property
    def plan_mode(self) -> bool:
        """当前是否处于 plan 权限档（只读，不写文件只产出计划）。"""
        return self.permission_mode == PermissionMode.PLAN

    _plan_path_cache: Path | None = None

    def _get_plan_path(self) -> Path:
        """取当前 plan 输出文件路径；首次调用会生成一个随机名并缓存。

        输入: 无（读取 self.work_dir 与缓存）。
        作用: 每次会话首次进入 plan 档时造一个 `<形容词>-<名词>-<时间>` 文件名，
              落在 .autocode/plans/ 下；plan 文件路径还会通知给权限检查器。
        输出: Path——本会话 plan 文件的绝对路径。
        """
        if self._plan_path_cache is not None:
            return self._plan_path_cache
        import random
        import datetime
        _ADJECTIVES = ["bold", "bright", "calm", "cool", "deep", "fair", "fast", "fine",
                       "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
                       "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid"]
        _NOUNS = ["sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
                  "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
                  "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore"]
        plans_dir = Path(self.work_dir) / ".autocode" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%m%d-%H%M")
        slug = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{ts}"
        self._plan_path_cache = plans_dir / f"{slug}.md"   # 缓存：同会话内文件名稳定
        return self._plan_path_cache

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """切换权限档位（同时同步给权限检查器）。"""
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    def activate_skill(self, name: str, prompt_body: str) -> None:
        """登记一个已激活的 skill（其正文会拼进环境上下文）。"""
        self.active_skills[name] = prompt_body

    def clear_active_skills(self) -> None:
        """清空已激活 skill，结束一轮 skill 注入。"""
        self.active_skills.clear()

    def set_skill_catalog(self, catalog: str) -> None:
        """写入可用的 skill 目录清单（供模型按需发现 skill）。"""
        self._skill_catalog = catalog

    def _build_hook_context(self, event: str, **kwargs: str | dict) -> HookContext:
        """把分散的 hook 参数归一成 HookContext 传给 hook 引擎。"""
        return HookContext(
            event_name=event,
            tool_name=str(kwargs.get("tool_name", "")),
            tool_args=kwargs.get("tool_args", {}),
            file_path=str(kwargs.get("file_path", "")),
            message=str(kwargs.get("message", "")),
            error=str(kwargs.get("error", "")),
        )

    def _infer_file_path(self, args: dict) -> str:
        """从工具参数里取目标文件路径（兼容 file_path / path 两种键）。"""
        return str(args.get("file_path", args.get("path", "")))

    def _drain_hook_events(self) -> list[HookEvent]:
        """把 hook 引擎积压的通知转成 HookEvent 列表（供本轮 yield 出去）。"""
        if not self.hook_engine:
            return []
        return [
            HookEvent(
                hook_id=n.hook_id,
                event=n.event,
                output=n.output,
                success=n.success,
            )
            for n in self.hook_engine.drain_notifications()
        ]

    async def run(self, conversation: ConversationManager) -> AsyncIterator[AgentEvent]:
        """Agent 主循环：驱动一次完整对话直到自然结束或触发护栏。

        输入: conversation——当前对话（环境/长程记忆已注入的）。
        作用: 每一轮「模型调用 → 若调工具则执行并写回历史 → 再问」，直到模型直接
              回答（LoopComplete）或撞上 max_iterations / 连续未知工具等护栏。
        输出: 异步 AgentEvent 流；调用方（UI / 测试）消费事件即可渲染全流程。
        """
        self._current_conversation = conversation
        env_context = build_environment_context(
            self.work_dir, self.active_skills, self._skill_catalog
        )
        conversation.inject_environment(env_context)

        # 长程记忆 / 指令注入：让模型"记得"跨会话沉淀的要点
        memory_content = self.memory_manager.load() if self.memory_manager else ""
        conversation.inject_long_term_memory(self.instructions_content, memory_content)

        if self.hook_engine:
            ctx = self._build_hook_context("session_start")
            await self.hook_engine.run_hooks("session_start", ctx)
            for he in self._drain_hook_events():
                yield he   # 把 session_start 通知透传给 UI

        iteration = 0
        consecutive_unknown = 0       # 连续「未知工具」告警计数（另一道护栏）
        max_tokens_escalated = False  # 是否已把输出上限提到天花板
        output_recoveries = 0         # 输出截断后的恢复次数

        while True:
            iteration += 1

            if iteration > self.max_iterations:
                # 护栏：达到最大轮次即终止，防「模型永不结束」死循环烧 token
                yield ErrorEvent(
                    message=f"Agent reached maximum iterations ({self.max_iterations})"
                )
                break

            if self.hook_engine:
                ctx = self._build_hook_context("turn_start")
                await self.hook_engine.run_hooks("turn_start", ctx)
                for he in self._drain_hook_events():
                    yield he

            # Layer 2: 接近 context window 上限时自动 compact（操作原始对话）
            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self.registry.get_all_schemas(self.protocol),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                yield CompactNotification(
                    before_tokens=compact_result.before_tokens,
                    message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）",
                    boundary=compact_result.boundary,
                )
                # 压缩会清掉历史里的环境/记忆注入，压缩后要重新注入
                conversation.inject_environment(env_context)
                mem = self.memory_manager.load() if self.memory_manager else ""
                conversation.inject_long_term_memory(
                    self.instructions_content, mem
                )
            elif isinstance(compact_result, str):
                yield ErrorEvent(message=compact_result)   # 压缩失败 → 报错但不中断

            if self.hook_engine:
                ctx = self._build_hook_context("pre_send")
                await self.hook_engine.run_hooks("pre_send", ctx)
                for he in self._drain_hook_events():
                    yield he

            hook_prompts = (
                self.hook_engine.get_prompt_messages() if self.hook_engine else None
            )
            system = build_system_prompt(hook_prompts=hook_prompts)

            if self.plan_mode:
                plan_path = str(self._get_plan_path())
                if self.permission_checker:
                    self.permission_checker.plan_file_path = plan_path   # 放行写计划文件
                plan_exists = self._get_plan_path().exists()
                plan_reminder = build_plan_mode_reminder(
                    plan_path, plan_exists, iteration
                )
                conversation.add_system_reminder(plan_reminder)   # 提醒模型把结论写进 plan

            if self.hook_engine:
                for note in self.hook_engine.drain_notifications():
                    conversation.add_system_reminder(
                        f"Hook [{note.hook_id}] {note.event}: {note.output}"
                    )

            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names:
                # 延迟加载工具不预载 schema——提醒模型用 ToolSearch 按需选中加载
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            tools = self.registry.get_all_schemas(self.protocol)

            # Layer 1: 在 LLM 调用前应用 tool-result budget，确保 api_conv 反映
            # 本轮迭代中所有已发生的写入（system reminders、hook 通知等）。
            # 原始 conversation 不会被修改；替换决策保存在 self.replacement_state 中。
            api_conv, _new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if _new_records:
                append_replacement_records(self.session_dir, _new_records)   # 替换记录落盘

            collector = StreamCollector()
            llm_stream = self.client.stream(api_conv, system=system, tools=tools)
            async for event in collector.consume(llm_stream):
                yield event   # 流式事件边收边透传给 UI

            response = collector.response   # 聚合结果：判断下一步的唯一依据

            if self.hook_engine:
                ctx = self._build_hook_context("post_receive", message=response.text)
                await self.hook_engine.run_hooks("post_receive", ctx)
                for he in self._drain_hook_events():
                    yield he

            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens
            yield UsageEvent(
                input_tokens=self.total_input_tokens,   # 累计用量，UI 展示实时跑分
                output_tokens=self.total_output_tokens,
            )

            conv_thinking = [
                ConvThinkingBlock(thinking=tb.thinking, signature=tb.signature)
                for tb in response.thinking_blocks
            ]

            if response.stop_reason == "max_tokens":
                # 输出被截断：先把上限升到天花板重试一次，还截断再逐步恢复
                if not max_tokens_escalated:
                    self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                    max_tokens_escalated = True
                    if response.text:
                        conversation.add_assistant_message(
                            response.text, thinking_blocks=conv_thinking
                        )
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content. Pick up mid-thought if needed."
                        )
                    yield RetryEvent(reason="max_tokens escalation")
                    continue   # 下一轮用更高的输出上限继续
                elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
                    output_recoveries += 1
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking
                    )
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces."
                    )
                    yield RetryEvent(
                        reason=f"max_tokens recovery {output_recoveries}/{MAX_OUTPUT_TOKENS_RECOVERIES}"
                    )
                    continue
            else:
                output_recoveries = 0   # 非截断轮复位恢复计数

            if not response.tool_calls:
                # 模型直接回答：落历史、抽记忆、跑 hook，收尾本轮循环
                conversation.add_assistant_message(
                    response.text, thinking_blocks=conv_thinking
                )
                self._loop_count += 1
                if (
                    self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0
                    and self.memory_manager
                ):
                    # 每 N 轮结束抽一次长程记忆（后台任务，不阻塞收尾）
                    asyncio.ensure_future(self._extract_memories(conversation))
                if self.hook_engine:
                    ctx = self._build_hook_context("turn_end")
                    await self.hook_engine.run_hooks("turn_end", ctx)
                    ctx = self._build_hook_context("session_end")
                    await self.hook_engine.run_hooks("session_end", ctx)
                    for he in self._drain_hook_events():
                        yield he
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                yield LoopComplete(total_turns=iteration)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(
                response.text, tool_uses, thinking_blocks=conv_thinking
            )
            # 在 assistant 回复加入历史后锚定实际用量：基线（input + cache + output）
            # 覆盖到当前位置，因此下一轮迭代顶部的 auto-compact 检查只需对
            # 接下来追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            tool_results: list[ToolResultBlock] = []
            batches = partition_tool_calls(response.tool_calls, self.registry)

            for batch in batches:
                if batch.concurrent and len(batch.calls) > 1:
                    # 并发批：整批并行执行，逐条回收结果
                    batch_results = await self._execute_batch_parallel(batch.calls)
                    for br in batch_results:
                        if br.is_unknown:
                            consecutive_unknown += 1
                        else:
                            consecutive_unknown = 0
                        content = self._maybe_persist_or_truncate(
                            br.tool_id, br.result.output
                        )
                        tool_results.append(
                            ToolResultBlock(
                                tool_use_id=br.tool_id,
                                content=content,
                                is_error=br.result.is_error,
                            )
                        )
                        yield ToolResultEvent(
                            tool_id=br.tool_id,
                            tool_name=br.tool_name,
                            output=br.result.output,
                            is_error=br.result.is_error,
                            elapsed=br.elapsed,
                        )
                else:
                    # 串行批：逐个执行（含权限询问），结果写回后再处理下一个
                    for tc in batch.calls:
                        result: ToolResult | None = None
                        elapsed = 0.0
                        is_unknown = False

                        if self.hook_engine:
                            file_path = self._infer_file_path(tc.arguments)
                            hook_ctx = self._build_hook_context(
                                "pre_tool_use",
                                tool_name=tc.tool_name,
                                tool_args=tc.arguments,
                                file_path=file_path,
                            )
                            # 拦截型 hook：返回非 None 表示拒绝执行该工具
                            rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
                            for he in self._drain_hook_events():
                                yield he
                            if rejection is not None:
                                result = ToolResult(
                                    output=f"Hook rejected: {rejection.reason}",
                                    is_error=True,
                                )
                                content = self._maybe_persist_or_truncate(
                                    tc.tool_id, result.output
                                )
                                tool_results.append(
                                    ToolResultBlock(
                                        tool_use_id=tc.tool_id,
                                        content=content,
                                        is_error=True,
                                    )
                                )
                                yield ToolResultEvent(
                                    tool_id=tc.tool_id,
                                    tool_name=tc.tool_name,
                                    output=result.output,
                                    is_error=True,
                                    elapsed=0.0,
                                )
                                continue   # 被拒的工具不进入执行

                        async for item in self._execute_tool(tc):
                            if isinstance(item, PermissionRequest):
                                yield item   # 权限询问透传给 UI 等待用户
                            else:
                                result, elapsed, is_unknown = item

                        if result is None:
                            result = ToolResult(output="Error: no result from tool", is_error=True)

                        if is_unknown:
                            consecutive_unknown += 1
                        else:
                            consecutive_unknown = 0

                        if self.hook_engine:
                            file_path = self._infer_file_path(tc.arguments)
                            hook_ctx = self._build_hook_context(
                                "post_tool_use",
                                tool_name=tc.tool_name,
                                tool_args=tc.arguments,
                                file_path=file_path,
                            )
                            # 通知型 hook：不拦截，只记录副作用
                            await self.hook_engine.run_hooks("post_tool_use", hook_ctx)
                            for he in self._drain_hook_events():
                                yield he

                        content = self._maybe_persist_or_truncate(
                            tc.tool_id, result.output
                        )
                        tool_results.append(
                            ToolResultBlock(
                                tool_use_id=tc.tool_id,
                                content=content,
                                is_error=result.is_error,
                            )
                        )
                        yield ToolResultEvent(
                            tool_id=tc.tool_id,
                            tool_name=tc.tool_name,
                            output=result.output,
                            is_error=result.is_error,
                            elapsed=elapsed,
                        )

            if consecutive_unknown >= 3:
                # 护栏：模型连问 3 次不存在的工具 → 判定走偏，终止
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            exit_plan_called = any(
                tc.tool_name == "ExitPlanMode" for tc in response.tool_calls
            )
            conversation.add_tool_results_message(tool_results)   # 结果写回历史，供下一轮
            if exit_plan_called:
                # plan 档：模型调 ExitPlanMode 表示计划完成 → 本轮即整个会话结束
                yield TurnComplete(turn=iteration)
                yield LoopComplete(total_turns=iteration)
                break

            if self.hook_engine:
                ctx = self._build_hook_context("turn_end")
                await self.hook_engine.run_hooks("turn_end", ctx)
                for he in self._drain_hook_events():
                    yield he
            yield TurnComplete(turn=iteration)   # 一轮结束，下一轮迭代继续


    def _build_permission_description(self, tc: ToolCallComplete) -> str:
        """为权限确认框生成一段可读描述（优先显示命令/文件路径，而非整包参数）。"""
        if tc.tool_name == "Bash":
            return tc.arguments.get("command", tc.tool_name)
        if tc.tool_name in ("ReadFile", "WriteFile", "EditFile"):
            return tc.arguments.get("file_path", tc.tool_name)
        return str(tc.arguments)

    async def _execute_single_tool_direct(
        self, tc: ToolCallComplete
    ) -> _ToolExecResult:
        """并发批用的直通执行路径：不含权限询问（并发前已判过档），失败折叠成错误结果。

        输入: tc——一次工具调用。
        作用: 查注册表 → 参数校验 → execute；未知/禁用/校验失败/异常都转成错误 ToolResult。
        输出: _ToolExecResult（含耗时、是否未知工具）。
        """
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()

        if tool is None:
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: unknown tool '{tc.tool_name}'", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=True,   # 未知工具：供主循环累计 unknown 护栏
            )

        if not self.registry.is_enabled(tc.tool_name):
            return _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: tool '{tc.tool_name}' is disabled", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )

        try:
            params = tool.params_model.model_validate(tc.arguments)   # pydantic 校验参数
            result = await tool.execute(params)
        except ValidationError as e:
            result = ToolResult(output=f"Parameter validation error: {e}", is_error=True)
        except Exception as e:
            result = ToolResult(output=f"Tool execution error: {e}", is_error=True)

        self._snapshot_for_recovery(tc, result)   # ReadFile 结果进恢复快照

        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=result,
            elapsed=time.monotonic() - start,
            is_unknown=False,
        )


    async def _execute_batch_parallel(
        self, calls: list[ToolCallComplete]
    ) -> list[_ToolExecResult]:
        """并行执行一整批并发安全工具的调用。"""
        tasks = [self._execute_single_tool_direct(tc) for tc in calls]
        return list(await asyncio.gather(*tasks))

    async def _execute_tool(
        self, tc: ToolCallComplete
    ) -> AsyncIterator[tuple[ToolResult, float, bool]]:
        """执行单个工具调用，可能穿插一次权限询问。

        输入: tc——一次工具调用。
        作用: 依次走 未知/禁用/权限 三道闸：需要 ask 时先 yield 权限请求并 await 用户
              答复（ALLOW_ALWAYS 会写一条本地放行规则）；通过后校验参数并 execute。
        输出: 异步产出 (ToolResult, 耗时秒, 是否未知工具) 元组。
        """
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()
        is_unknown = False

        if tool is None:
            result = ToolResult(
                output=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            )
            is_unknown = True   # 未知工具要单独标记，供主循环 unknown 护栏
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        if not self.registry.is_enabled(tc.tool_name):
            result = ToolResult(
                output=f"Error: tool '{tc.tool_name}' is disabled in current mode",
                is_error=True,
            )
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        # 权限检查：deny 直接拒绝；ask 抛给 UI 等用户裁夺
        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)

            if decision.effect == "deny":
                result = ToolResult(
                    output=f"Permission denied: {decision.reason}",
                    is_error=True,
                )
                elapsed = time.monotonic() - start
                yield result, elapsed, is_unknown
                return

            if decision.effect == "ask":
                loop = asyncio.get_running_loop()
                future: asyncio.Future[PermissionResponse] = loop.create_future()
                desc = self._build_permission_description(tc)
                # 向调用方 yield 权限请求事件，由调用方处理
                yield PermissionRequest(
                    tool_name=tc.tool_name,
                    description=desc,
                    arguments=tc.arguments,
                    future=future,
                )
                response = await future   # 挂起直到 UI/用户 fill 这个 future

                if response == PermissionResponse.DENY:
                    result = ToolResult(
                        output="Permission denied: 用户拒绝了此操作",
                        is_error=True,
                    )
                    elapsed = time.monotonic() - start
                    yield result, elapsed, is_unknown
                    return

                if response == PermissionResponse.ALLOW_ALWAYS:
                    # 记住放行：把「这条命令/文件」固化成一条本地规则
                    from autocode.permissions.rules import Rule, extract_content
                    content = extract_content(tc.tool_name, tc.arguments)
                    pattern = f"{content[:60]}*" if len(content) > 60 else f"{content}*"
                    rule = Rule(tool_name=tc.tool_name, pattern=pattern, effect="allow")
                    self.permission_checker.rule_engine.append_local_rule(rule)

        try:
            params = tool.params_model.model_validate(tc.arguments)
            result = await tool.execute(params)
        except ValidationError as e:
            result = ToolResult(
                output=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(
                output=f"Tool execution error: {e}", is_error=True
            )

        self._snapshot_for_recovery(tc, result)

        elapsed = time.monotonic() - start
        yield result, elapsed, is_unknown

    def _snapshot_for_recovery(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> None:
        """捕获 ReadFile 刚交给模型的内容，以便 Layer 2 压缩对话后
        auto_compact 能重新附加这些数据。每次 ReadFile 多一次磁盘读取，
        比从 tool 输出中反向解析行号要划算。
        """
        if result.is_error or tc.tool_name != "ReadFile":
            return
        path = tc.arguments.get("file_path") if isinstance(tc.arguments, dict) else None
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return
        self.recovery_state.record_file_read(path, content)

    async def _extract_memories(
        self, conversation: ConversationManager
    ) -> None:
        """从对话中抽取长程记忆；幂等防并发。

        输入: conversation——当前对话。
        作用: 周期性地让模型把值得记住的要点沉淀进 memory_manager。
        输出: 无（副作用：写记忆存储；失败仅记 debug 日志不打断）。
        """
        if self._extracting or not self.memory_manager:
            return   # 正在抽取或没配记忆 → 跳过
        self._extracting = True
        try:
            await self.memory_manager.extract(
                self.client, conversation, self.protocol
            )
        except Exception as e:
            log.debug("Memory extraction failed: %s", e)   # 记忆失败不该炸掉主流程
        finally:
            self._extracting = False

    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactNotification | ErrorEvent:
        """用户手动触发的一次上下文压缩（对应 UI 的 compact 命令）。

        输入: conversation——要压缩的对话。
        作用: 走与自动压缩相同的 auto_compact；成功返回带 boundary 的通知。
        输出: CompactNotification（成功）或 ErrorEvent（失败/没到压缩条件）。
        """
        # auto_compact 会用摘要替换 conversation.history，所有 tool-result 内容
        # （原始或已替换的）都将被丢弃。这里跳过 apply_tool_result_budget —
        # 它在主循环中的唯一目的是为 LLM 调用生成 api_conv，而本路径不需要
        # 发起看到替换结果的 LLM 调用（auto_compact 内部的摘要调用操作的是原始对话）。
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self.registry.get_all_schemas(self.protocol),
            transcript_path=self._transcript_path,
        )
        if isinstance(result, CompactEvent):
            # 压缩丢掉了环境/记忆注入，成功后要重新注入
            env_context = build_environment_context(
                self.work_dir, self.active_skills, self._skill_catalog
            )
            conversation.inject_environment(env_context)
            memory_content = self.memory_manager.load() if self.memory_manager else ""
            conversation.inject_long_term_memory(
                self.instructions_content, memory_content
            )
            return CompactNotification(
                before_tokens=result.before_tokens,
                message=f"上下文已压缩（压缩前 {result.before_tokens:,} tokens）",
                boundary=result.boundary,
            )
        return ErrorEvent(message=result or "压缩失败：对话历史为空或未达到压缩条件")

    async def run_to_completion(
        self, task: str, conversation: ConversationManager | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """非交互跑完一个任务：内部循环直到模型直接回答，返回最终文本。

        输入: task——用户问题；conversation——可选的既有对话（缺省新建并注入环境）；
              event_callback——可选回调，逐事件通知（usage / stream_text / tool_use）。
        作用: 与 run() 同构但无事件流/yield：把「每轮 → 执行工具 → 写回历史」收紧在
              方法内，权限 ask 档在非交互下自动放行或拒绝。
        输出: str——模型最后一段正文（没有则为空串）。
        """
        if conversation is None:
            conversation = ConversationManager()

            env_context = build_environment_context(
                self.work_dir, self.active_skills, self._skill_catalog
            )
            conversation.inject_environment(env_context)

            if self.instructions_content:
                memory_content = self.memory_manager.load() if self.memory_manager else ""
                conversation.inject_long_term_memory(
                    self.instructions_content, memory_content
                )

        if task:
            conversation.add_user_message(task)   # 用户提问进历史

        hook_prompts = (
            self.hook_engine.get_prompt_messages() if self.hook_engine else None
        )
        system = build_system_prompt(hook_prompts=hook_prompts)

        tools = self.registry.get_all_schemas(self.protocol)

        log.info(
            "[run_to_completion] agent=%s tools=%d names=%s",
            self.agent_id,
            len(tools),
            [t["name"] for t in tools][:10],
        )

        last_text = ""   # 累积「最后一次模型正文」作为返回值

        for iteration in range(1, self.max_iterations + 1):   # 上限即护栏
            if self.hook_engine:
                ctx = self._build_hook_context("turn_start")
                await self.hook_engine.run_hooks("turn_start", ctx)

            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self.registry.get_all_schemas(self.protocol),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                conversation.inject_environment(env_context)   # 压缩后重注环境

            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            # 与 run() 同：发请求前把 tool-result 预算应用到副本上
            api_conv, _new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if _new_records:
                append_replacement_records(self.session_dir, _new_records)

            collector = StreamCollector()
            llm_stream = self.client.stream(api_conv, system=system, tools=tools)
            async for _event in collector.consume(llm_stream):
                pass   # 非交互：事件不需要展示，只在 collector 里聚合

            response = collector.response
            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens

            if event_callback:
                event_callback({
                    "type": "usage",
                    "usage": {
                        "inputTokens": self.total_input_tokens,
                        "outputTokens": self.total_output_tokens,
                    },
                })

            if response.text:
                last_text = response.text   # 每次有正文都刷新「最后正文」
                if event_callback:
                    event_callback({
                        "type": "stream_text",
                        "text": response.text,
                    })

            log.info(
                "[run_to_completion] agent=%s iter=%d tool_calls=%d text_len=%d stop=%s",
                self.agent_id, iteration, len(response.tool_calls),
                len(response.text), response.stop_reason,
            )

            if not response.tool_calls:
                # 模型直接回答 → 落历史、可选快照，循环结束
                conversation.add_assistant_message(response.text)
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(response.text, tool_uses)
            # assistant 回复已在历史中，锚定实际用量；下一轮迭代只需对
            # 下方追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            tool_results: list[ToolResultBlock] = []
            for tc in response.tool_calls:
                if event_callback:
                    event_callback({
                        "type": "tool_use",
                        "toolName": tc.tool_name,
                        "args": tc.arguments,
                    })
                result = await self._execute_tool_noninteractive(tc)
                content = self._maybe_persist_or_truncate(tc.tool_id, result.output)
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tc.tool_id,
                        content=content,
                        is_error=result.is_error,
                    )
                )

            conversation.add_tool_results_message(tool_results)

            if self.hook_engine:
                ctx = self._build_hook_context("turn_end")
                await self.hook_engine.run_hooks("turn_end", ctx)

        return last_text   # 可能为空串：模型从头到尾没产出正文

    async def _execute_tool_noninteractive(
        self, tc: ToolCallComplete
    ) -> ToolResult:
        """非交互路径的工具执行：不可弹权限框，按档位自动放行或拒绝。

        输入: tc——一次工具调用。
        作用: 未知/禁用/被 hook 拒绝都直接返回错误；权限 ask 档在 dontAsk 下放行、
              否则拒绝（避免无人值守时卡死）。
        输出: ToolResult——执行结果或失败说明。
        """
        tool = self.registry.get(tc.tool_name)

        if tool is None:
            return ToolResult(
                output=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            )

        if not self.registry.is_enabled(tc.tool_name):
            return ToolResult(
                output=f"Error: tool '{tc.tool_name}' is disabled",
                is_error=True,
            )

        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "pre_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                file_path=file_path,
            )
            rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
            if rejection is not None:
                return ToolResult(
                    output=f"Hook rejected: {rejection.reason}",
                    is_error=True,
                )

        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            if decision.effect == "deny":
                return ToolResult(
                    output=f"Permission denied: {decision.reason}",
                    is_error=True,
                )
            if decision.effect == "ask":
                # 无人可问：dontAsk 自动放行，其它档位只能拒绝
                if self.permission_mode == PermissionMode.DONT_ASK:
                    pass  # 自动批准
                else:
                    return ToolResult(
                        output="Permission denied: non-interactive agent cannot prompt user",
                        is_error=True,
                    )

        try:
            params = tool.params_model.model_validate(tc.arguments)
            result = await tool.execute(params)
        except ValidationError as e:
            result = ToolResult(
                output=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(
                output=f"Tool execution error: {e}", is_error=True
            )

        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "post_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                file_path=file_path,
            )
            await self.hook_engine.run_hooks("post_tool_use", hook_ctx)

        return result

    def _maybe_persist_or_truncate(self, tool_use_id: str, text: str) -> str:
        """决定 tool 结果写回历史时的形态：超长落盘给预览，中长截断，否则原样。

        输入: tool_use_id——工具调用 id（作落盘文件名依据）；text——工具输出。
        作用: 防单条工具结果撑爆上下文——超过单条上限存到文件只留预览，
              次一档直接截断并标注。
        输出: str——放进历史里的最终文本。
        """
        from autocode.context.manager import (
            SINGLE_RESULT_CHAR_LIMIT,
            make_persisted_preview,
            persist_tool_result,
        )

        if len(text) > SINGLE_RESULT_CHAR_LIMIT:
            fp = persist_tool_result(tool_use_id, text, self.session_dir)   # 全量落盘
            return make_persisted_preview(text, fp)   # 历史里只放「预览 + 文件指针」
        if len(text) > MAX_OUTPUT_CHARS:
            return text[:MAX_OUTPUT_CHARS] + "\n… (output truncated)"
        return text   # 足够短：原样保留，省去模型再去读文件
