from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from autocode.cli_output import print_result
from autocode.config import ConfigError, load_config
from autocode.hooks import HookConfigError, HookEngine, load_hooks
from autocode.permissions import PermissionMode


def main() -> None:
    """解析命令行并启动 AutoCode：非交互单发，或拉起 TUI 会话。

    输入: 无（读取 sys.argv，以及当前目录下的 config.yaml）。
    作用: 备好日志目录 → 解析 --mode/-p → 加载配置与 hooks →
          给了 -p 就走非交互 _run_prompt，否则进入 AutoCodeApp TUI。
    输出: 无（进程内打印 / 启动 UI；配置或 hook 出错时向 stderr 报错并以退出码 1 结束）。
    """
    # 先确保 .autocode/ 目录存在，否则下面写 debug.log 会因目录不存在而崩溃
    Path(".autocode").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=".autocode/debug.log",
        filemode="w",
    )

    parser = argparse.ArgumentParser(prog="autocode", description="AutoCode AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # 命令行 --mode 优先；未传时回退到配置文件里的档位
    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        asyncio.run(_run_prompt(config, permission_mode, hook_engine, args.p))
        return

    # 局部导入：TUI 依赖较重，非交互路径（上面已 return）不必加载它们
    from autocode.app import AutoCodeApp
    from autocode.driver import NoAltScreenDriver

    app = AutoCodeApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        driver_class=NoAltScreenDriver,
        max_iterations=config.max_iterations,
    )
    app.run()


async def _run_prompt(config, permission_mode, hook_engine, prompt: str) -> None:
    """非交互单发：跑完一个 prompt，把最终结果文本打印到 stdout。

    输入: config（已加载配置）、permission_mode、hook_engine、prompt（用户问题）。
    作用: 组装一次完整 Agent 链路（客户端 / 权限检查 / 工具注册 / 指令），
          用 run_to_completion 一路跑到模型直接回答。
    输出: 无（副作用：把结果文本打印到 stdout）。
    """
    from autocode.agent import Agent
    from autocode.client import create_client, resolve_context_window
    from autocode.conversation import ConversationManager
    from autocode.memory.instructions import load_instructions
    from autocode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        RuleEngine,
    )
    from autocode.tools import create_default_registry, register_demo_tools
    from autocode.tools.impl.tool_search import ToolSearchTool

    provider = config.providers[0]   # 非交互模式固定用配置里的第一个 provider
    client = create_client(provider)
    # 第 2 层：尽力从 provider 自动拉取模型的 context window（缓存在 provider 上）。
    # 不会抛异常或阻塞启动；失败则退化到映射表。
    await resolve_context_window(provider)
    work_dir = os.getcwd()
    home = Path.home()

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(
            user_rules_path=home / ".autocode" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".autocode" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".autocode" / "permissions.local.yaml",
        ),
        mode=permission_mode,
    )

    instructions = load_instructions(work_dir)
    registry = create_default_registry()
    # 延迟发现的兜底搜索工具：schema 不预载，靠 ToolSearchTool 按需搜索加载
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))
    register_demo_tools(registry)

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=instructions,
        hook_engine=hook_engine,
        max_iterations=config.max_iterations,
    )

    conv = ConversationManager()
    last_result = await agent.run_to_completion(prompt, conv)
    print_result(last_result)


if __name__ == "__main__":
    main()

