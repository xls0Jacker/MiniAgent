"""TUI 主题渲染冒烟测试（feat/tui-theme）。

覆盖：
- _AUTOCODE_THEME 扩展为 Claude Code 品牌橙暗色系 11 基色，能注册生效。
- 空 providers 启动 App 不崩，关键 widget（chat-area/chat-input/title-bar）存在。
- CSS 加载无报错。
- _MODE_COLORS 四种模式都是合法可渲染的色值（hex），mode label markup 不抛错。
- 权限弹窗响应 handler 对已取消 future 有守卫，不抛 InvalidStateError。
"""
from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static

from autocode.app import AutoCodeApp, _AUTOCODE_THEME, _MODE_COLORS
from autocode.agent import PermissionMode, PermissionRequest


def _make_app() -> AutoCodeApp:
    return AutoCodeApp(providers=[])


# ---------------------------------------------------------------------------
# 主题定义
# ---------------------------------------------------------------------------

def test_theme_has_full_semantic_palette() -> None:
    """主题补齐 Textual 11 基色，brand 橙替换原紫色 #875FFF。"""
    assert _AUTOCODE_THEME.primary == "#D77757", "primary 应为 Claude 品牌橙"
    assert _AUTOCODE_THEME.accent == "#D77757", "accent 应为 Claude 品牌橙"
    # 不再是紫色主调
    assert "#875FFF" not in (_AUTOCODE_THEME.primary or "").upper()
    assert _AUTOCODE_THEME.success is not None
    assert _AUTOCODE_THEME.error is not None
    assert _AUTOCODE_THEME.warning is not None
    assert _AUTOCODE_THEME.surface is not None
    assert _AUTOCODE_THEME.panel is not None
    assert _AUTOCODE_THEME.foreground is not None
    assert _AUTOCODE_THEME.background is not None
    assert _AUTOCODE_THEME.dark is True


def test_surface_panel_are_layered() -> None:
    """surface / panel 不再同值，背景有层次。"""
    assert _AUTOCODE_THEME.surface != _AUTOCODE_THEME.panel
    assert _AUTOCODE_THEME.surface != _AUTOCODE_THEME.background


# ---------------------------------------------------------------------------
# _MODE_COLORS
# ---------------------------------------------------------------------------

def test_mode_colors_are_valid_hex() -> None:
    """状态栏四模式颜色改为可渲染 hex，不再是 dim/green/yellow/red 具名色。"""
    for mode, color in _MODE_COLORS.items():
        assert color.startswith("#"), f"{mode} 颜色应为 hex: {color}"
        assert len(color) == 7, f"{mode} hex 长度异常: {color}"
    # plan 模式用 Claude Code sage 绿，不是旧 yellow
    assert _MODE_COLORS[PermissionMode.PLAN] != "yellow"
    assert _MODE_COLORS[PermissionMode.BYPASS] != "red"


def test_mode_colors_keys_match_cycle() -> None:
    """_MODE_COLORS 覆盖状态栏可切换的全部四种模式。"""
    expected = {
        PermissionMode.DEFAULT,
        PermissionMode.ACCEPT_EDITS,
        PermissionMode.PLAN,
        PermissionMode.BYPASS,
    }
    assert set(_MODE_COLORS) == expected


# ---------------------------------------------------------------------------
# Pilot 冒烟
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_app_starts_and_registers_theme() -> None:
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "autocode", "默认主题应为 autocode"
        # 主题变量真正注入 CSS（若 theme 被类属性遮蔽成普通 str，Reactive watcher
        # 不触发，这里仍会退回 textual-dark 的 #1E1E1E）
        assert app.get_css_variables().get("surface") == "#24211E", \
            "CSS $surface 应解析到 autocode 主题色而非 textual-dark 默认"
        assert app.get_css_variables().get("background") == "#141312"
        # 关键 widget 存在
        assert app.query_one("#chat-area") is not None
        assert app.query_one("#chat-input") is not None
        assert app.query_one("#title-bar") is not None
        assert app.query_one("#status-bar") is not None


@pytest.mark.asyncio
async def test_mode_label_updates_all_modes_without_markup_error() -> None:
    """四种模式下 _update_mode_label 用 hex 渲染 mode label，不抛 markup 错误。"""
    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        # 给一个假 agent 权限模式，触发 mode label 更新
        for mode in _MODE_COLORS:
            app.agent = type("FakeAgent", (), {"permission_mode": mode})()  # type: ignore[attr-defined]
            # _update_mode_label 会 query #mode-label 并 update markup
            app._update_mode_label()
            await pilot.pause()
            label = app.query_one("#mode-label", Static)
            # 内容非空且已渲染（update 无异常即通过）
            assert label.render() is not None


@pytest.mark.asyncio
async def test_permission_responded_cancelled_future_no_crash() -> None:
    """权限弹窗挂着时 agent 被 Esc 取消（future 随之 cancel），
    迟到的 Responded 不应再 set_result，否则抛 InvalidStateError。"""
    from autocode.agent import PermissionResponse
    from autocode.permission_dialog import InlinePermissionWidget

    app = _make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        # 构造一个已被 cancel 的 pending request（模拟用户 Esc 取消 agent）
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionResponse] = loop.create_future()
        future.cancel()
        app._pending_perm_request = PermissionRequest(
            tool_name="WriteFile",
            description="cancel 测试",
            future=future,
        )
        # 模拟迟到的 Responded（widget 已因取消被移除，这里直接构造事件对象）
        event = InlinePermissionWidget.Responded(PermissionResponse.DENY)
        # 不抛即通过：守卫应跳过对已 cancel future 的 set_result
        app.on_inline_permission_widget_responded(event)
        assert app._pending_perm_request is None, "处理后应清空 pending 状态"

