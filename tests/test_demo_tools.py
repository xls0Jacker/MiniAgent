# AutoCode MiniAgent

"""演示工具（Calculator/Search/Weather/Todo）的单元测试。

覆盖 Calculator / Search / Weather / Todo 四个内置演示工具的行为，
以及 register_demo_tools 的注册完整性。全部为本地 mock，无网络依赖。
"""
from __future__ import annotations

import pytest

from autocode.tools import create_default_registry, register_demo_tools
from autocode.tools.calculator import Params as CalcParams
from autocode.tools.search import Params as SearchParams
from autocode.tools.todo import Params as TodoParams
from autocode.tools.weather import Params as WeatherParams

DEMO_NAMES = {"Calculator", "Search", "Weather", "Todo"}


def _demo_tool(name: str):
    """建一个注册了全部演示工具的 registry，并返回指定工具。"""
    registry = create_default_registry()
    register_demo_tools(registry)
    return registry.get(name)


# ---------------------------------------------------------------------------
# 注册完整性
# ---------------------------------------------------------------------------

def test_register_demo_tools_registers_all_four() -> None:
    """register_demo_tools 之后 4 个演示工具均可从注册表 get 到。"""
    registry = create_default_registry()
    todo = register_demo_tools(registry)
    for name in DEMO_NAMES:
        assert registry.get(name) is not None, f"{name} 未注册"
    assert registry.get("Todo") is todo  # 返回同一实例，便于 session 注入


def test_demo_tools_schemas_are_well_formed() -> None:
    """每个演示工具 get_schema() 都含 name/description/input_schema.properties。"""
    registry = create_default_registry()
    register_demo_tools(registry)
    for name in DEMO_NAMES:
        schema = registry.get(name).get_schema()
        assert schema["name"] == name
        assert schema["description"]
        props = schema["input_schema"]["properties"]
        assert isinstance(props, dict) and props


# ---------------------------------------------------------------------------
# Calculator：AST 白名单安全求值
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_calculator_evaluates_arithmetic() -> None:
    tool = _demo_tool("Calculator")
    r = await tool.execute(CalcParams(expression="3.5*128+2"))
    assert not r.is_error
    assert r.output == "450"  # 整数结果不带小数点


@pytest.mark.asyncio
async def test_calculator_parens_and_pow() -> None:
    tool = _demo_tool("Calculator")
    r = await tool.execute(CalcParams(expression="(2+3)**2"))
    assert not r.is_error
    assert r.output == "25"


@pytest.mark.asyncio
async def test_calculator_rejects_code_injection() -> None:
    tool = _demo_tool("Calculator")
    r = await tool.execute(CalcParams(expression="__import__('os').system('echo hi')"))
    assert r.is_error


@pytest.mark.asyncio
async def test_calculator_division_by_zero_is_error() -> None:
    tool = _demo_tool("Calculator")
    r = await tool.execute(CalcParams(expression="1/0"))
    assert r.is_error
    assert "zero" in r.output


@pytest.mark.asyncio
async def test_calculator_oversized_expression_is_error() -> None:
    tool = _demo_tool("Calculator")
    r = await tool.execute(CalcParams(expression="1+" * 500 + "1"))
    assert r.is_error  # 超 _MAX_EXPR_LEN


# ---------------------------------------------------------------------------
# Search：本地知识库子串匹配（确定性 mock）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_returns_matching_entries() -> None:
    tool = _demo_tool("Search")
    r = await tool.execute(SearchParams(query="agent loop"))
    assert not r.is_error
    assert "ReAct" in r.output or "Agent Loop" in r.output


@pytest.mark.asyncio
async def test_search_no_hit_returns_hint() -> None:
    tool = _demo_tool("Search")
    r = await tool.execute(SearchParams(query="zzz_no_such_kw"))
    assert not r.is_error  # 无命中是提示，不是错误
    assert "No results found" in r.output


# ---------------------------------------------------------------------------
# Weather：确定性伪数据 + 未知城市报错
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_weather_deterministic_per_city() -> None:
    tool = _demo_tool("Weather")
    r1 = await tool.execute(WeatherParams(city="北京"))
    r2 = await tool.execute(WeatherParams(city="北京"))
    assert not r1.is_error
    assert r1.output == r2.output  # 同一城市稳定返回同一组值
    assert "北京" in r1.output


@pytest.mark.asyncio
async def test_weather_unsupported_city_is_error() -> None:
    tool = _demo_tool("Weather")
    r = await tool.execute(WeatherParams(city="不存在市"))
    assert r.is_error
    assert "Supported cities" in r.output


# ---------------------------------------------------------------------------
# Todo：add/list/done/remove 全流程 + 跨 session 隔离
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_todo_add_list_done_remove(tmp_path) -> None:
    registry = create_default_registry()
    todo = register_demo_tools(registry, storage_dir=tmp_path)
    todo.set_session("win-A")

    r = await todo.execute(TodoParams(action="add", content="写 spec"))
    assert not r.is_error and "#1" in r.output

    await todo.execute(TodoParams(action="add", content="写 plan"))

    listed = await todo.execute(TodoParams(action="list"))
    assert "写 spec" in listed.output and "写 plan" in listed.output
    assert "[ ]" in listed.output

    done = await todo.execute(TodoParams(action="done", item_id=1))
    assert not done.is_error
    listed = await todo.execute(TodoParams(action="list"))
    assert "[x]" in listed.output

    removed = await todo.execute(TodoParams(action="remove", item_id=1))
    assert not removed.is_error
    listed = await todo.execute(TodoParams(action="list"))
    assert "写 spec" not in listed.output


@pytest.mark.asyncio
async def test_todo_sessions_are_isolated(tmp_path) -> None:
    """窗口 A 与窗口 B 的待办互不可见；切回 A 仍能读到自己的列表。"""
    registry = create_default_registry()
    todo = register_demo_tools(registry, storage_dir=tmp_path)

    todo.set_session("win-A")
    await todo.execute(TodoParams(action="add", content="A 的任务"))

    todo.set_session("win-B")
    b_list = await todo.execute(TodoParams(action="list"))
    assert "待办列表为空" in b_list.output  # B 看不到 A 的

    todo.set_session("win-A")
    a_list = await todo.execute(TodoParams(action="list"))
    assert "A 的任务" in a_list.output  # 切回 A 数据还在
    assert "win-A" in a_list.output


@pytest.mark.asyncio
async def test_todo_bad_action_params(tmp_path) -> None:
    registry = create_default_registry()
    todo = register_demo_tools(registry, storage_dir=tmp_path)
    todo.set_session("win-A")
    await todo.execute(TodoParams(action="add", content="任务"))

    # done/remove 引用不存在的 id → 错误返回
    bad_done = await todo.execute(TodoParams(action="done", item_id=999))
    assert bad_done.is_error
    # add 缺 content → 错误返回
    empty_add = await todo.execute(TodoParams(action="add"))
    assert empty_add.is_error
