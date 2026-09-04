# AutoCode MiniAgent

"""max_iterations 主循环护栏测试。

Agent 每次都被模型要求调用一个工具（永不给出最终回答），
验证达到 max_iterations 后 emit ErrorEvent、主循环终止，
且工具实际执行次数不超过 max_iterations + 1（首轮超限判定前会先执行第 1 个工具）。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from autocode.agent import Agent, ErrorEvent, LoopComplete, ToolResultEvent
from autocode.client import LLMClient
from autocode.conversation import ConversationManager
from autocode.tools import create_default_registry, register_demo_tools
from autocode.tools.base import StreamEnd, TextDelta, ToolCallComplete


class FakeLLMClient(LLMClient):
    """永远返回单个 Calculator 工具调用的客户端（不会自然结束循环）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator:
        self.calls += 1
        yield TextDelta("calculating...")
        yield ToolCallComplete(
            tool_id=f"t{self.calls}",
            tool_name="Calculator",
            arguments={"expression": "1+1"},
        )
        yield StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=5)


@pytest.mark.asyncio
async def test_max_iterations_bounds_tool_loop() -> None:
    """永不收敛时，达到 max_iterations 后报错并停止，工具调用次数受限。"""
    client = FakeLLMClient()
    registry = create_default_registry()
    register_demo_tools(registry)  # Fake 调用 Calculator，需注册 demo 工具
    agent = Agent(
        client, registry, "anthropic",
        work_dir=".", max_iterations=3,
    )
    conv = ConversationManager()
    conv.add_user_message("一直算，别停")

    error_events: list[ErrorEvent] = []
    result_count = 0
    loop_complete = 0

    async for e in agent.run(conv):
        if isinstance(e, ErrorEvent):
            error_events.append(e)
        elif isinstance(e, ToolResultEvent):
            result_count += 1
        elif isinstance(e, LoopComplete):
            loop_complete += 1

    # 护栏触发：报"达到最大迭代次数"
    assert len(error_events) == 1
    assert "maximum iterations" in error_events[0].message

    # 工具调用（=LLM 被调用次数）不超过 max_iterations，主循环随后退出
    assert client.calls <= 3
    assert result_count <= 3  # 每个模型回合最多执行一次 Calculator

    # LoopComplete 只在正常结束（非超限）时 emit，超限路径不产生
    assert loop_complete == 0


@pytest.mark.asyncio
async def test_max_iterations_one_turn_only() -> None:
    """max_iterations=1 时只允许一轮工具调用即被掐停。"""
    client = FakeLLMClient()
    registry = create_default_registry()
    register_demo_tools(registry)  # Fake 调用 Calculator，需注册 demo 工具
    agent = Agent(
        client, registry, "anthropic",
        work_dir=".", max_iterations=1,
    )
    conv = ConversationManager()
    conv.add_user_message("算一下")

    errors = 0
    async for e in agent.run(conv):
        if isinstance(e, ErrorEvent):
            errors += 1

    assert errors == 1
    assert client.calls == 1  # 第 2 轮在迭代计数超限处直接拦截，不再请求 LLM
