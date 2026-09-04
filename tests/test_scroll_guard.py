"""聊天滚动守卫测试：权限确认 / 流式输出期间用户上滚查看历史不被拉回底部。

``AutoCodeApp._scroll_chat_end()`` 只在视口本就停在底部时才跟随新内容，
这样用户在权限确认框弹出期间上滚看历史，不会被新内容 / spinner 拽回底部。
"""
from __future__ import annotations

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from autocode.app import AutoCodeApp


def _make_app() -> AutoCodeApp:
    """空 providers 启动 AutoCodeApp（不触发真实 agent）。

    空 providers 时 chat-area 默认隐藏，测试里手动 display=True 以布局。
    """
    return AutoCodeApp(providers=[])


async def _visible_chat(app: AutoCodeApp, pilot) -> VerticalScroll:
    """让 chat-area 可见并挂大量内容，返回可滚动的 chat。"""
    chat = app.query_one("#chat-area", VerticalScroll)
    chat.display = True
    await pilot.pause()
    for i in range(100):
        await chat.mount(Static(f"line {i}"))
    await pilot.pause(0.1)
    assert chat.max_scroll_y > 0, "需要有可滚动内容"
    return chat


@pytest.mark.asyncio
async def test_scroll_guard_does_not_yank_when_scrolled_up() -> None:
    """用户上滚后，_scroll_chat_end 不应把视口拉回底部。"""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = await _visible_chat(app, pilot)
        chat.scroll_end(animate=False)
        await pilot.pause()
        assert chat.is_vertical_scroll_end

        # 用户上滚 10 行
        chat.scroll_to(y=10, animate=False)
        await pilot.pause()
        assert not chat.is_vertical_scroll_end

        # 守卫：当前不在底部，不应滚动
        app._scroll_chat_end()
        await pilot.pause()
        assert chat.scroll_offset.y == 10, "上滚位置应被保留，不被拉回底部"


@pytest.mark.asyncio
async def test_scroll_guard_follows_when_at_bottom() -> None:
    """用户在底部时，_scroll_chat_end 应跟随滚动到底（正常跟随新内容）。"""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = await _visible_chat(app, pilot)
        chat.scroll_end(animate=False)
        await pilot.pause()
        assert chat.is_vertical_scroll_end

        # 守卫：在底部时应能滚动（跟随新内容）
        app._scroll_chat_end()
        await pilot.pause()
        assert chat.scroll_offset.y == chat.max_scroll_y, "在底部时应跟随到底"
