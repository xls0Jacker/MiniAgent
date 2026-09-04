from __future__ import annotations

import asyncio
import os
import random
import time as _time
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TMessage
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from autocode.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from autocode.client import (
    AuthenticationError,
    LLMClient,
    LLMError,
    create_client,
    resolve_context_window,
)
from autocode.commands import (
    CommandContext,
    CommandRegistry,
    complete,
    parse_command,
)
from autocode.commands.completion import CompletionPopup
from autocode.commands.handlers import register_all_commands
from autocode.config import MCPServerConfig, ProviderConfig
from autocode.hooks import HookContext, HookEngine, load_hooks
from autocode.conversation import ConversationManager, Message
from autocode.mcp import MCPManager
from autocode.memory import (
    MemoryManager,
    Session,
    SessionManager,
    find_relevant_memories,
    generate_session_summary,
    load_instructions,
    make_compact_boundary,
    render_reminder,
)
from autocode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from autocode.skills.executor import SkillExecutor
from autocode.skills.loader import SkillLoader
from autocode.commands.handlers.skill_register import register_skill_commands
from rich.text import Text as RichText
from textual.theme import Theme
from autocode.cache import FileCache
from autocode.tools import ToolRegistry, create_default_registry, register_demo_tools
from autocode.tools.ask_user import AskUserEvent, AskUserTool
from autocode.tools.impl.tool_search import ToolSearchTool
from autocode.tools.load_skill import LoadSkill

import re

MAX_TRUNCATED_LINES = 20
MAX_AT_REF_BYTES = 10240

_AT_REF_RE = re.compile(r"@([\w./_\-]+(?:\.[\w]+)*)")

_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".autocode", "build", ".gradle"}


def scan_files_for_at(prefix: str, work_dir: str, limit: int = 10) -> list[str]:
    matches: list[str] = []
    base = os.path.join(work_dir, os.path.dirname(prefix)) if "/" in prefix else work_dir
    name_prefix = os.path.basename(prefix).lower()
    if not os.path.isdir(base):
        return matches
    try:
        for entry in sorted(os.listdir(base)):
            if entry in _SKIP_DIRS or entry.startswith("."):
                continue
            if entry.lower().startswith(name_prefix):
                rel = os.path.join(os.path.dirname(prefix), entry) if "/" in prefix else entry
                if os.path.isdir(os.path.join(base, entry)):
                    rel += "/"
                matches.append(rel)
                if len(matches) >= limit:
                    break
    except OSError:
        pass
    return matches


def expand_at_refs(text: str, work_dir: str) -> str:
    def _replace(m: re.Match) -> str:
        rel_path = m.group(1)
        full_path = os.path.join(work_dir, rel_path)
        if not os.path.isfile(full_path):
            return m.group(0)
        try:
            content = open(full_path, encoding="utf-8", errors="replace").read(MAX_AT_REF_BYTES)
            return f"[File: {rel_path}]\n```\n{content}\n```"
        except Exception:
            return m.group(0)
    return _AT_REF_RE.sub(_replace, text)


class ChatInput(TextArea):
    BINDINGS = [
        Binding("enter", "submit", "Submit", priority=True),
        Binding("shift+enter", "newline", "Newline", priority=True),
        Binding("ctrl+j", "newline", "Newline", priority=True),
        Binding("tab", "complete", "Complete", priority=True),
        Binding("escape", "dismiss_popup", "Dismiss", priority=True),
        Binding("up", "nav_up", "Navigate up", priority=True),
        Binding("down", "nav_down", "Navigate down", priority=True),
    ]

    class Submitted(TMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class TabComplete(TMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cursor_blink = False
        self._history: list[str] = []
        self._history_index: int = -1
        self._history_draft: str = ""
        self._history_file: Path | None = None

    def load_history(self, work_dir: str) -> None:
        self._history_file = Path(work_dir) / ".autocode" / "history"
        if self._history_file.exists():
            try:
                lines = self._history_file.read_text(encoding="utf-8").splitlines()
                self._history = [l for l in lines if l.strip()]
            except Exception:
                pass

    def _persist_entry(self, text: str) -> None:
        if self._history_file is None:
            return
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _popup(self) -> CompletionPopup | None:
        try:
            return self.app.query_one(CompletionPopup)
        except Exception:
            return None

    def action_submit(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            popup.hide()
            if selected:
                self._history.append(selected)
                self._persist_entry(selected)
                self._history_index = -1
                self._history_draft = ""
                self.post_message(self.Submitted(selected))
                self.clear()
                return
        text = self.text.strip()
        if text:
            self._history.append(text)
            self._persist_entry(text)
            self._history_index = -1
            self._history_draft = ""
            self.post_message(self.Submitted(text))
            self.clear()

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            if selected:
                popup.hide()
                self.clear()
                self.insert(selected + " ")
            return
        text = self.text.strip()
        if text.startswith("/"):
            self.post_message(self.TabComplete(text))
        else:
            self.insert("\t")

    def action_dismiss_popup(self) -> None:
        popup = self._popup()
        if popup is not None:
            popup.hide()

    def action_nav_up(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            popup.move_up()
            return
        if not self._history:
            return
        if self._history_index == -1:
            self._history_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self.clear()
        self.insert(self._history[self._history_index])

    def action_nav_down(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            popup.move_down()
            return
        if self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.clear()
            self.insert(self._history[self._history_index])
        else:
            self._history_index = -1
            self.clear()
            self.insert(self._history_draft)

    class AtFileRequest(TMessage):
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    class SlashMenuUpdate(TMessage):
        def __init__(self, prefix: str | None) -> None:
            super().__init__()
            self.prefix = prefix

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/"):
            prefix = text[1:]
            if " " not in prefix and "\n" not in prefix:
                self.post_message(self.SlashMenuUpdate(prefix))
            else:
                self.post_message(self.SlashMenuUpdate(None))
        else:
            self.post_message(self.SlashMenuUpdate(None))

        at_idx = text.rfind("@")
        if at_idx < 0:
            return
        after = text[at_idx + 1:]
        if " " in after or "\n" in after:
            return
        if after:
            self.post_message(self.AtFileRequest(after))


COLLAPSIBLE_TOOLS = {"ReadFile", "Glob", "Grep", "ToolSearch"}


def _tool_title(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "ReadFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Read {path}" if path else "Read"
    if tool_name == "WriteFile":
        path = os.path.basename(arguments.get("file_path", ""))
        content = arguments.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"Write {path} ({lines} lines)" if path else "Write"
    if tool_name == "EditFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Edit {path}" if path else "Edit"
    if tool_name == "Bash":
        cmd = arguments.get("command", "")
        short = cmd[:50] + "…" if len(cmd) > 50 else cmd
        return f"Bash: {short}" if short else "Bash"
    if tool_name == "Glob":
        return f"Glob: {arguments.get('pattern', '')}"
    if tool_name == "Grep":
        return f"Grep: {arguments.get('pattern', '')}"
    return tool_name


def _format_detail(tool_name: str, arguments: dict[str, Any], output: str) -> str:
    parts: list[str] = []

    if tool_name == "Bash":
        parts.append(f"  IN   {arguments.get('command', '')}")
        parts.append("")
        for line in output.splitlines():
            parts.append(f"  OUT  {line}")
    elif tool_name in ("ReadFile", "WriteFile", "EditFile"):
        parts.append(f"  {arguments.get('file_path', '')}")
        parts.append("")
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")
    else:
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")

    return "\n".join(parts)


class ToolCallBlock(Static, can_focus=True):

    def __init__(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tool_name = tool_name
        self._arguments = arguments
        self._title = _tool_title(tool_name, arguments)
        self._full_output = ""
        self._is_error = False
        self._elapsed = 0.0
        self._collapsed = True
        self._loading = True
        self._render_loading()

    def _render_loading(self) -> None:
        self.update(f"  ● {self._title} …")
        self.add_class("tool-block-loading")

    def set_result(self, output: str, is_error: bool, elapsed: float) -> None:
        self._full_output = output
        self._is_error = is_error
        self._elapsed = elapsed
        self._loading = False
        self._collapsed = True
        self.remove_class("tool-block-loading")
        if is_error:
            self.add_class("tool-block-error")
        self._render_collapsed()

    def _render_collapsed(self) -> None:
        if self._is_error:
            self.update(f"  ✗ {self._title} ({self._elapsed:.1f}s)")
        else:
            self.update(f"  ✓ {self._title} ({self._elapsed:.1f}s)")

    def _render_expanded(self) -> None:
        if self._is_error:
            header = f"  ✗ {self._title} ({self._elapsed:.1f}s)"
        else:
            header = f"  ✓ {self._title} ({self._elapsed:.1f}s)"
        detail = _format_detail(self.tool_name, self._arguments, self._full_output)
        self.update(f"{header}\n{detail}")

    def on_click(self) -> None:
        if self._loading:
            return
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._render_collapsed()
        else:
            self._render_expanded()


_MODE_CYCLE = [
    PermissionMode.DEFAULT,
    PermissionMode.ACCEPT_EDITS,
    PermissionMode.PLAN,
    PermissionMode.BYPASS,
]

# 状态栏模式标签的前景色。Rich markup 层不解析 `$token`（Textual 8），
# 因此这里直接给具体色值，并与 _AUTOCODE_THEME 语义色保持同源：
#   DEFAULT      -> muted 灰（继承 $text-muted 观感）
#   ACCEPT_EDITS -> success 绿
#   PLAN         -> planMode sage 绿（Claude Code rgb(72,150,140)）
#   BYPASS       -> 醒目红（Claude Code error 亮红）
_MODE_COLORS = {
    PermissionMode.DEFAULT: "#A8A29A",
    PermissionMode.ACCEPT_EDITS: "#4EBA65",
    PermissionMode.PLAN: "#48968C",
    PermissionMode.BYPASS: "#FF6B80",
}

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _to_past_tense(verb: str) -> str:
    """把现在进行时动词转换为过去式。"""
    if verb.endswith("ing"):
        stem = verb[:-3]
        if stem.endswith("e"):
            return stem + "d"
        if stem and stem[-1] in "atutitet":
            return stem + "ed"
        return stem + "ed"
    return verb + "ed"


THINKING_VERBS = [
    "Accomplishing", "Architecting", "Baking", "Beboppin'", "Befuddling",
    "Bloviating", "Boogieing", "Boondoggling", "Bootstrapping", "Brewing",
    "Calculating", "Canoodling", "Caramelizing", "Cascading", "Cerebrating",
    "Choreographing", "Churning", "Coalescing", "Cogitating", "Combobulating",
    "Composing", "Computing", "Concocting", "Considering", "Contemplating",
    "Cooking", "Crafting", "Creating", "Crunching", "Crystallizing",
    "Cultivating", "Deciphering", "Deliberating", "Dilly-dallying",
    "Discombobulating", "Doodling", "Elucidating", "Enchanting", "Envisioning",
    "Fermenting", "Finagling", "Flambéing", "Flibbertigibbeting", "Flummoxing",
    "Forging", "Frolicking", "Gallivanting", "Garnishing", "Generating",
    "Germinating", "Grooving", "Harmonizing", "Hatching", "Honking",
    "Hullaballooing", "Ideating", "Imagining", "Improvising", "Incubating",
    "Inferring", "Infusing", "Kneading", "Lollygagging", "Manifesting",
    "Marinating", "Meandering", "Metamorphosing", "Mewing", "Moonwalking",
    "Moseying", "Mulling", "Musing", "Noodling", "Orbiting",
    "Orchestrating", "Percolating", "Philosophising", "Pondering",
    "Pontificating", "Pouncing", "Purring", "Puzzling", "Razzle-dazzling",
    "Ruminating", "Scampering", "Simmering", "Sketching", "Spelunking",
    "Spinning", "Sprouting", "Synthesizing", "Thinking", "Tinkering",
    "Transfiguring", "Transmuting", "Undulating", "Unfurling", "Unravelling",
    "Vibing", "Wandering", "Whisking", "Working", "Wrangling", "Zigzagging",
]  # 共 105 个动词，与 Go 版 internal/tui/verbs.go 完全一致


class ToolGroupSummary(Static, can_focus=True):


    def __init__(self, count: int, total_elapsed: float, **kwargs: Any) -> None:
        label = f"● Done ({count} tool uses · {total_elapsed:.1f}s)  (ctrl+o to expand)"
        super().__init__(label, **kwargs)
        self._count = count
        self._total = total_elapsed
        self._expanded = False

    def _refresh_display(self) -> None:
        if self._expanded:
            self.update(f"▼ Done ({self._count} tool uses · {self._total:.1f}s)")
        else:
            self.update(
                f"● Done ({self._count} tool uses · {self._total:.1f}s)"
                "  (ctrl+o to expand)"
            )

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh_display()


    def on_click(self) -> None:
        self.toggle()


_AUTOCODE_THEME = Theme(
    name="autocode",
    primary="#D77757",
    accent="#D77757",
    secondary="#B8A99A",
    foreground="#F2EEE8",
    background="#141312",
    surface="#24211E",
    panel="#2D2924",
    success="#4EBA65",
    warning="#FFC107",
    error="#FF6B80",
    boost="#4D433A",
    dark=True,
)


class AutoCodeApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "AutoCode"
    INLINE_PADDING = 0
    # 注意：这里不能用 `theme = "autocode"` 类属性覆盖——它会遮蔽 App 继承的
    # Reactive 描述符，导致 _watch_theme 永不触发、CSS 变量停留在 textual-dark，
    # 所有 $token 都解析成默认主题色。主题须在 on_mount 里 register + 实例赋值。
    BINDINGS = [
        Binding("ctrl+c", "handle_ctrl_c", "Quit", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("shift+tab", "cycle_mode", "Cycle mode", priority=True),
        Binding("ctrl+o", "toggle_tool_blocks", "Toggle tools", priority=True),
    ]


    def __init__(
        self,
        providers: list[ProviderConfig],
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        mcp_servers: list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        driver_class: type | None = None,
        max_iterations: int = 50,
    ) -> None:
        super().__init__(driver_class=driver_class)
        self.providers = providers
        self._max_iterations = max_iterations
        self._initial_permission_mode = permission_mode
        self._mcp_server_configs = mcp_servers or []
        self.hook_engine = hook_engine
        self.file_cache = FileCache()
        self.client: LLMClient | None = None
        self.conversation = ConversationManager()
        self.registry: ToolRegistry = create_default_registry(file_cache=self.file_cache)
        # 笔试题演示工具（Calculator/Search/Weather/Todo）。Todo 按 session 隔离，
        # 需要拿到实例，在会话创建/切换时注入当前 session id。
        self._todo_tool = register_demo_tools(
            self.registry, storage_dir=Path(".autocode") / "todos"
        )
        self.agent: Agent | None = None
        self.mcp_manager: MCPManager | None = None
        self._mcp_init_task: asyncio.Task[None] | None = None
        self._selected_provider: ProviderConfig | None = None
        self._streaming = False
        self._thinking_start: float = 0.0
        self._thinking_verb: str = ""
        self._spinner_idx: int = 0
        self._spinner_timer = None
        self._spinner_label: Static | None = None
        self._mcp_server_info: str = ""
        self._agent_task: asyncio.Task[None] | None = None
        self.session_manager: SessionManager | None = None
        self.session: Session | None = None
        self.memory_manager: MemoryManager | None = None
        self._instructions_content: str = ""
        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)
        self.skill_loader: SkillLoader | None = None
        self.skill_executor: SkillExecutor | None = None
        self._load_skill_tool: LoadSkill | None = None
        self._current_streaming_label: Static | None = None
        self._current_ai_row: Vertical | None = None
        self._current_accumulated_text: str = ""
        self._mcp_instructions: str = ""
        self._mcp_instructions_ok: bool = False
        self._mcp_connecting: bool = False

    @staticmethod
    def _make_banner(model: str = "", work_dir: str = "") -> RichText:
        # AutoCode 品牌条：左侧终端窗 logo（暗示"终端里的 AI 编程助手"），
        # 右侧 AutoCode 名 + model/工作目录。品牌橙 #D77757 / 暖灰 #A8A29A
        # 对齐 _AUTOCODE_THEME 语义色。
        t = RichText()
        t.append("┌───────┐   ", style="#D77757")
        t.append("AutoCode v0.1.0\n", style="bold #F2EEE8")
        t.append("│  >_   │   ", style="#D77757")
        t.append(f"{model}\n" if model else "\n", style="#A8A29A")
        t.append("└───────┘   ", style="#D77757")
        t.append(work_dir, style="#A8A29A")
        return t

    def compose(self) -> ComposeResult:
        if len(self.providers) > 1:
            with Vertical(id="provider-select"):
                yield Static("Select a Provider", id="select-label")
                yield OptionList(
                    *[
                        Option(f"{p.name}  [{p.model}]", id=p.name)
                        for p in self.providers
                    ],
                    id="provider-list",
                )
        with VerticalScroll(id="chat-area"):
            yield Static(self._make_banner(), id="title-bar")
            yield Vertical(id="chat-messages")
        with Vertical(id="input-area"):
            yield ChatInput(id="chat-input")
            with Horizontal(id="status-bar"):
                yield Static("  default", id="mode-label")
                yield Static("", id="model-label")
            yield CompletionPopup()

    def _mount_chat_widget(self, widget: Widget) -> None:
        """把 widget mount 到聊天区消息列表底部。"""
        chat_messages = self.query_one("#chat-messages", Vertical)
        chat_messages.mount(widget)

    async def _amount_chat_widget(self, widget: Widget) -> None:
        """异步版本：把 widget mount 到聊天区消息列表底部。"""
        chat_messages = self.query_one("#chat-messages", Vertical)
        await chat_messages.mount(widget)

    def on_mount(self) -> None:
        self.register_theme(_AUTOCODE_THEME)
        self.theme = "autocode"
        if len(self.providers) == 1:
            self._select_provider(self.providers[0])
        else:
            self.query_one("#chat-area").display = False
            self.query_one("#input-area").display = False

    def _select_provider(self, provider: ProviderConfig) -> None:
        self._selected_provider = provider
        try:
            self.client = create_client(provider)
        except AuthenticationError as e:
            self._show_error(str(e))
            return

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
            mode=self._initial_permission_mode,
        )

        self._instructions_content = load_instructions(work_dir)
        self.memory_manager = MemoryManager(work_dir)
        self.session_manager = SessionManager(work_dir)
        self.session_manager.cleanup()
        self.session = self.session_manager.create()
        self._todo_tool.set_session(self.session.session_id)

        from autocode.filehistory import FileHistory
        self.file_history = FileHistory(work_dir, self.session.session_id)
        for tool in self.registry.list_tools():
            if hasattr(tool, "file_history"):
                tool.file_history = self.file_history

        load_skill_tool = LoadSkill()
        self.registry.register(load_skill_tool)
        self._load_skill_tool = load_skill_tool

        self.registry.register(
            ToolSearchTool(self.registry, protocol=provider.protocol)
        )
        self.registry.register(AskUserTool())

        from autocode.tools.exit_plan_mode import ExitPlanModeTool
        self._exit_plan_tool = ExitPlanModeTool()
        self.registry.register(self._exit_plan_tool)

        self.agent = Agent(
            client=self.client,
            registry=self.registry,
            protocol=provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=self._instructions_content,
            memory_manager=self.memory_manager,
            hook_engine=self.hook_engine,
            max_iterations=self._max_iterations,
        )
        self.agent.file_history = self.file_history
        self.agent.session_id = self.session.session_id

        self._exit_plan_tool._is_plan_mode = lambda: self.agent.plan_mode
        self._exit_plan_tool._plan_exists = lambda: self.agent._get_plan_path().exists()

        # Layer 2: 在后台异步拉取模型的 context window，不阻塞启动流程。
        # agent 已经有一个同步解析的窗口值（来自配置 / 映射表 / 默认值）；
        # 如果异步拉取成功，就原地升级为更准确的值。
        self.run_worker(
            self._resolve_context_window(provider), exclusive=False
        )

        self.skill_loader = SkillLoader(work_dir)
        self.skill_loader.load_all()

        load_skill_tool.set_loader(self.skill_loader)
        load_skill_tool.set_agent(self.agent)

        self.skill_executor = SkillExecutor(
            agent=self.agent,
            client=self.client,
            protocol=provider.protocol,
        )

        catalog = self.skill_loader.get_catalog()
        if catalog:
            lines = [
                "You can use the following Skills:",
                "",
            ]
            for name, desc in catalog:
                lines.append(f"- {name}: {desc}")
            lines.append("")
            lines.append(
                "If the user's request matches a Skill, call LoadSkill to activate it."
            )
            self.agent.set_skill_catalog("\n".join(lines))

        register_skill_commands(
            self.command_registry, self.skill_loader, self.skill_executor
        )

        if self.hook_engine:
            asyncio.ensure_future(
                self.hook_engine.run_hooks(
                    "startup", HookContext(event_name="startup")
                )
            )

        if self._mcp_server_configs:
            self._mcp_init_task = asyncio.create_task(self._init_mcp())

        self.query_one("#model-label", Static).update(provider.model)
        work_dir = os.getcwd()
        self.query_one("#title-bar", Static).update(
            self._make_banner(provider.model, work_dir)
        )
        self._update_mode_label()

        select = self.query("#provider-select")
        if select:
            select.first().display = False
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.placeholder = "Send a message..."
        chat_input.load_history(work_dir)
        chat_input.focus()

    async def _resolve_context_window(self, provider: ProviderConfig) -> None:
        """Layer 2 后台 worker：异步拉取模型的 context window，
        拉到就原地升级 agent 的窗口值。

        尽力而为 — resolve_context_window 不会抛异常；如果拉不到，
        agent 继续使用同步解析得到的窗口值。
        """
        await resolve_context_window(provider)
        if self.agent is not None:
            self.agent.context_window = provider.get_context_window()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "provider-list":
            provider = self.providers[event.option_index]
            self._select_provider(provider)

    # -----------------------------------------------------------------
    # UIController 协议实现
    # -----------------------------------------------------------------

    def add_system_message(self, text: str) -> None:
        self._show_system_message(text)

    def send_user_message(self, text: str) -> None:
        if self._streaming or self.agent is None:
            return
        self._agent_task = asyncio.create_task(self._send_message(text))

    def set_plan_mode(self, enabled: bool) -> None:
        if self.agent is None:
            return
        if enabled:
            self._pre_plan_mode = self.agent.permission_mode
            self.agent.set_permission_mode(PermissionMode.PLAN)
        else:
            restore = getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT)
            self.agent.set_permission_mode(restore)
        self._update_mode_label()

    def get_token_count(self) -> tuple[int, int]:
        if self.agent:
            return self.agent.total_input_tokens, self.agent.total_output_tokens
        return 0, 0

    def refresh_status(self) -> None:
        self._update_mode_label()

    # -----------------------------------------------------------------
    # 命令分发
    # -----------------------------------------------------------------


    def _build_command_context(self, args: str) -> CommandContext:
        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,
            config={
                "registry": self.command_registry,
                "set_session": self._set_session,
                "set_conversation": self._set_conversation,
                "clear_chat": self._clear_chat,
                "render_restored": self._render_restored_messages,
                "skill_loader": self.skill_loader,
                "skill_executor": self.skill_executor,
            },
        )

    def _set_session(self, session: Session) -> None:
        self.session = session
        if self.agent:
            self.agent.session_id = session.session_id
        self._todo_tool.set_session(session.session_id)

    def _persist_compact_boundary(self, notification: CompactNotification) -> None:
        """Layer-2 compact 后写入 compact_boundary 记录。

        将摘要 + 原样保留的尾部内联到一条记录中，resume 时只需这一条
        就能重建压缩后的状态。之前已写入磁盘的原始前缀不会被重放。
        没有活跃 session 或 compact 未产出 boundary 时直接跳过。
        """
        if not self.session or notification.boundary is None:
            return
        record = make_compact_boundary(
            notification.boundary.summary,
            notification.boundary.keep,
        )
        self.session.append_record(record)

    def _set_conversation(self, conv: ConversationManager) -> None:
        self.conversation = conv

    def _clear_chat(self) -> None:
        chat_messages = self.query_one("#chat-messages", Vertical)
        chat_messages.remove_children()

    async def _dispatch_command(self, text: str) -> None:
        name, args, is_command = parse_command(text)

        if not is_command:
            if self._streaming or self.agent is None:
                return
            self._agent_task = asyncio.create_task(self._send_message(text))
            return

        if name == "":
            commands = self.command_registry.list_commands()
            lines = ["可用命令："]
            for cmd in commands:
                aliases_str = ", ".join(f"/{a}" for a in cmd.aliases)
                name_part = f"/{cmd.name}"
                if aliases_str:
                    name_part += f", {aliases_str}"
                lines.append(f"  {name_part:<24} {cmd.description}")
            self._show_system_message("\n".join(lines))
            return

        cmd = self.command_registry.find(name)
        if cmd is None:
            self._show_system_message(f"未知命令：/{name}，输入 /help 查看可用命令")
            return

        if not args and cmd.arg_prompt:
            self._show_system_message(cmd.arg_prompt)
            return

        ctx = self._build_command_context(args)
        try:
            await cmd.handler(ctx)
        except Exception as e:
            self._show_error(f"命令执行失败: {e}")

    # -----------------------------------------------------------------
    # 输入处理
    # -----------------------------------------------------------------

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text.strip()
        if self._streaming and not text.startswith("/"):
            if self._agent_task and not self._agent_task.done():
                self._agent_task.cancel()
                try:
                    await self._agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._finish_streaming()
            self._show_system_message("(response interrupted)")
        await self._dispatch_command(text)

    def on_chat_input_tab_complete(self, event: ChatInput.TabComplete) -> None:
        matches = complete(self.command_registry, event.text)
        if not matches:
            return
        popup = self.query_one(CompletionPopup)
        if len(matches) == 1:
            input_widget = self.query_one("#chat-input", ChatInput)
            input_widget.clear()
            input_widget.insert(matches[0][1] + " ")
        else:
            popup.show_pairs(matches)

    def on_chat_input_slash_menu_update(self, event: ChatInput.SlashMenuUpdate) -> None:
        popup = self.query_one(CompletionPopup)
        if event.prefix is None:
            popup.hide()
            return
        matches = complete(self.command_registry, event.prefix)
        if not matches:
            popup.hide()
            return
        popup.show_pairs(matches)

    def on_chat_input_at_file_request(self, event: ChatInput.AtFileRequest) -> None:
        work_dir = self.agent.work_dir if self.agent else os.getcwd()
        matches = scan_files_for_at(event.prefix, work_dir)
        if matches:
            popup = self.query_one(CompletionPopup)
            popup.show([f"@{m}" for m in matches])

    def on_completion_popup_selected(self, event: CompletionPopup.Selected) -> None:
        input_widget = self.query_one("#chat-input", ChatInput)
        selected = event.value
        text = input_widget.text
        if selected.startswith("@"):
            at_idx = text.rfind("@")
            if at_idx >= 0:
                input_widget.clear()
                input_widget.insert(text[:at_idx] + selected + " ")
                input_widget.focus()
                return
        input_widget.clear()
        input_widget.insert(selected + " ")
        input_widget.focus()

    def action_cycle_mode(self) -> None:
        if self.agent is None:
            return
        current = self.agent.permission_mode
        try:
            idx = _MODE_CYCLE.index(current)
        except ValueError:
            idx = 0
        next_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
        self.agent.set_permission_mode(next_mode)
        self._update_mode_label()

    def action_toggle_tool_blocks(self) -> None:
        for block in self.query(ToolCallBlock):
            if block._loading:
                continue
            block._collapsed = not block._collapsed
            if block._collapsed:
                block._render_collapsed()
            else:
                block._render_expanded()

        for summary in self.query(ToolGroupSummary):
            was_expanded = summary._expanded
            summary.toggle()
            parent = summary.parent
            if parent:
                for child in parent.children:
                    if isinstance(child, ToolCallBlock) and child.tool_name in COLLAPSIBLE_TOOLS:
                        child.display = summary._expanded

    def action_cancel(self) -> None:
        popup = self.query_one(CompletionPopup)
        if popup.is_visible:
            popup.hide()
            self.query_one("#chat-input", ChatInput).focus()
            return
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()

    async def _prefetch_relevant_memories(self, query: str) -> str:
        """Run the recall selector as a side-query with an 8s timeout.

        Creates a fresh LLM client so the selector's system prompt is
        independent of the main conversation's system prompt. Returns the
        rendered system-reminder body, or "" on any failure / timeout.
        """
        if self.memory_manager is None or self._selected_provider is None:
            return ""

        provider = self._selected_provider
        user_dir = self.memory_manager.user_mem_dir
        project_dir = self.memory_manager.project_mem_dir

        async def selector(system_prompt: str, user_message: str) -> str:
            from autocode.tools.base import StreamEnd, TextDelta

            side_client = create_client(provider)
            mini_conv = ConversationManager()
            mini_conv.history = [Message(role="user", content=user_message)]
            collected = ""
            async for event in side_client.stream(mini_conv, system=system_prompt):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
            return collected

        try:
            results = await asyncio.wait_for(
                find_relevant_memories(
                    query=query,
                    user_mem_dir=user_dir,
                    project_mem_dir=project_dir,
                    recent_tools=None,
                    already_surfaced=None,
                    selector=selector,
                ),
                timeout=8.0,
            )
            return render_reminder(results)
        except (asyncio.TimeoutError, Exception):
            return ""

    async def _send_message(self, text: str, is_notification: bool = False) -> None:
        assert self.agent is not None

        if self._mcp_init_task and not self._mcp_init_task.done():
            self._show_system_message("Waiting for MCP servers to connect...")
            await self._mcp_init_task

        self._streaming = True
        chat = self.query_one("#chat-area", VerticalScroll)
        input_widget = self.query_one("#chat-input", ChatInput)

        if text and "@" in text:
            text = expand_at_refs(text, self.agent.work_dir)

        # Start memory recall prefetch before UI work.
        prefetch_task = asyncio.create_task(
            self._prefetch_relevant_memories(text)
        ) if text else None

        if text:
            user_row = Vertical(classes="user-row")
            await self._amount_chat_widget(user_row)
            from rich.text import Text as RichText
            user_rich = RichText()
            user_rich.append("❯ ", style="bold color(80)")
            user_rich.append(text, style="bold color(255)")
            user_bubble = Static(user_rich, classes="message user-message")
            await user_row.mount(user_bubble)
            self._scroll_chat_end()

            self.conversation.add_user_message(text)
            if self.session:
                self.session.append(Message(role="user", content=text))

        if self._mcp_instructions and not self._mcp_instructions_ok:
            self.conversation.add_system_reminder(self._mcp_instructions)
            self._mcp_instructions_ok = True

        # Collect prefetched recall with 3s timeout, inject as system-reminder.
        if prefetch_task is not None:
            try:
                reminder = await asyncio.wait_for(prefetch_task, timeout=3.0)
                if reminder:
                    self.conversation.add_system_reminder(reminder)
            except (asyncio.TimeoutError, Exception):
                pass

        history_cursor = len(self.conversation.history)

        # 准备 AI 回复区域
        ai_row = Vertical(classes="ai-row")
        await self._amount_chat_widget(ai_row)
        streaming_label = Static("", classes="message ai-message")
        await ai_row.mount(streaming_label)

        accumulated_text = ""
        tool_blocks: dict[str, ToolCallBlock] = {}

        # 在聊天区底部启动持续旋转的加载动画
        self._thinking_start = _time.monotonic()
        self._thinking_verb = random.choice(THINKING_VERBS)
        self._spinner_idx = 0
        self._spinner_label = Static(
            f"  {SPINNER_FRAMES[0]} {self._thinking_verb}…",
            id="spinner-live",
        )
        await self._amount_chat_widget(self._spinner_label)

        self._scroll_chat_end()
        self._start_spinner()

        await asyncio.sleep(0)

        try:
            async for event in self.agent.run(self.conversation):
                if isinstance(event, ThinkingText):
                    self._scroll_chat_end()

                elif isinstance(event, StreamText):
                    if streaming_label is not None and not accumulated_text:
                        await streaming_label.remove()
                        streaming_label = Static("", classes="message ai-message")
                        await ai_row.mount(streaming_label)
                    accumulated_text += event.text
                    from rich.text import Text as RichText
                    t = RichText()
                    t.append("● ", style="bold #D77757")
                    t.append(accumulated_text)
                    streaming_label.update(t)
                    self._scroll_chat_end()

                elif isinstance(event, RetryEvent):
                    self._show_system_message(f"↻ Retrying: {event.reason}")

                elif isinstance(event, ToolUseEvent):
                    if accumulated_text:
                        if streaming_label is not None:
                            await streaming_label.remove()
                        from rich.text import Text as RichText
                        prefix = Static(RichText("●  ", style="bold #D77757"), classes="message")
                        await ai_row.mount(prefix)
                        md = Markdown(accumulated_text, classes="message ai-message")
                        await ai_row.mount(md)
                        streaming_label = None
                        accumulated_text = ""
                    elif streaming_label is not None:
                        await streaming_label.remove()
                        streaming_label = None

                    block = ToolCallBlock(
                        event.tool_name, event.arguments, classes="tool-block"
                    )
                    await ai_row.mount(block)
                    tool_blocks[event.tool_id] = block
                    self._scroll_chat_end()

                elif isinstance(event, PermissionRequest):
                    await self._handle_permission_request(event)

                elif isinstance(event, ToolResultEvent):
                    block = tool_blocks.get(event.tool_id)
                    if block:
                        block.set_result(event.output, event.is_error, event.elapsed)
                    self._scroll_chat_end()

                    ask_tool = self.registry.get("AskUserQuestion")
                    if ask_tool and isinstance(ask_tool, AskUserTool) and ask_tool._pending_event:
                        await self._handle_askuser(ask_tool._pending_event)

                elif isinstance(event, TurnComplete):
                    if self.session:
                        for msg in self.conversation.history[history_cursor:]:
                            self.session.append(msg)
                        history_cursor = len(self.conversation.history)

                    collapsible = [
                        (tid, blk) for tid, blk in tool_blocks.items()
                        if isinstance(blk, ToolCallBlock)
                        and blk.tool_name in COLLAPSIBLE_TOOLS
                        and not blk._loading
                    ]
                    if len(collapsible) >= 2:
                        total_elapsed = sum(b._elapsed for _, b in collapsible)
                        summary = ToolGroupSummary(
                            len(collapsible), total_elapsed,
                            classes="tool-block tool-group-summary",
                        )
                        for _, blk in collapsible:
                            blk.display = False
                        await ai_row.mount(summary)

                    tool_blocks.clear()
                    ai_row = Vertical(classes="ai-row")
                    await self._amount_chat_widget(ai_row)
                    streaming_label = Static("", classes="message ai-message")
                    await ai_row.mount(streaming_label)
                    accumulated_text = ""
                    self._scroll_chat_end()

                elif isinstance(event, UsageEvent):
                    pass  # token 展示已移除

                elif isinstance(event, HookEvent):
                    status = "✓" if event.success else "✗"
                    self._show_system_message(
                        f"Hook [{event.hook_id}] {status} {event.output}"
                    )

                elif isinstance(event, CompactNotification):
                    self._show_system_message(event.message)
                    # auto_compact 已重写 conversation.history（摘要 +
                    # boundary + 保留尾部）。先持久化 boundary 记录，然后
                    # 将游标推进到重建后的历史末尾，这样 TurnComplete/LoopComplete
                    # 刷盘时只追加 boundary 之后的新消息，不会把已压缩的
                    # 前缀作为普通记录重复写入。
                    self._persist_compact_boundary(event)
                    history_cursor = len(self.conversation.history)

                elif isinstance(event, ErrorEvent):
                    self._show_error(event.message)

                elif isinstance(event, LoopComplete):
                    total_time = _time.monotonic() - self._thinking_start
                    done_label = Static(
                        f"✻ {_to_past_tense(self._thinking_verb)} for {total_time:.1f}s",
                        classes="message thinking-done",
                    )
                    await ai_row.mount(done_label)
                    if self.session:
                        for msg in self.conversation.history[history_cursor:]:
                            self.session.append(msg)
                        history_cursor = len(self.conversation.history)
                        self.session.meta.total_tokens = (
                            self.agent.total_input_tokens
                            + self.agent.total_output_tokens
                        )
                        asyncio.ensure_future(
                            self._update_session_summary()
                        )
                    if self.agent.plan_mode:
                        asyncio.ensure_future(
                            self._show_plan_approval()
                        )

            # 收尾：渲染剩余的累积文本
            if accumulated_text and streaming_label is not None:
                await streaming_label.remove()
                md = Markdown(accumulated_text, classes="message ai-message")
                await ai_row.mount(md)
            elif streaming_label is not None:
                await streaming_label.remove()

            self._scroll_chat_end()

        except asyncio.CancelledError:
            if accumulated_text:
                if streaming_label is not None:
                    await streaming_label.remove()
                md = Markdown(
                    accumulated_text + "\n\n*[cancelled]*",
                    classes="message ai-message",
                )
                await ai_row.mount(md)
            self._show_system_message("Operation cancelled")
        except LLMError as e:
            self._show_error(str(e))
        finally:
            self._finish_streaming()
            input_widget.focus()

    async def _show_plan_approval(self) -> None:
        from autocode.plan_dialog import InlinePlanWidget

        widget = InlinePlanWidget()
        await self._amount_chat_widget(widget)
        self._scroll_chat_end()
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_plan_widget_responded(
        self, event: "InlinePlanWidget.Responded"
    ) -> None:
        from autocode.plan_dialog import InlinePlanWidget, PlanChoice

        try:
            self.query_one("#plan-inline", InlinePlanWidget).remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

        if self.agent is None:
            return

        choice = event.choice
        feedback = event.feedback
        plan_path = self.agent._get_plan_path()
        plan_content = ""
        if plan_path.exists():
            try:
                plan_content = plan_path.read_text(encoding="utf-8")
            except Exception:
                pass

        pre = getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT)
        if choice == PlanChoice.YOLO:
            self.agent.set_permission_mode(PermissionMode.BYPASS)
            self._update_mode_label()
            if plan_content:
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        elif choice == PlanChoice.MANUAL:
            self.agent.set_permission_mode(pre)
            self._update_mode_label()
            if plan_content:
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        elif choice == PlanChoice.FEEDBACK:
            if feedback:
                self.send_user_message(feedback)
            else:
                self._show_system_message("Type your feedback and send.")

    async def _handle_askuser(self, event: AskUserEvent) -> None:
        from autocode.askuser_dialog import InlineAskUserWidget

        widget = InlineAskUserWidget(event.questions)
        self._pending_askuser_event = event
        await self._amount_chat_widget(widget)
        self._scroll_chat_end()
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_ask_user_widget_responded(
        self, event: "InlineAskUserWidget.Responded"
    ) -> None:
        from autocode.askuser_dialog import InlineAskUserWidget

        req = getattr(self, "_pending_askuser_event", None)
        if req is not None and not req.future.done():
            req.future.set_result(event.answers if event.answers else {})
            self._pending_askuser_event = None
        try:
            self.query_one("#askuser-inline", InlineAskUserWidget).remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

    def _start_spinner(self) -> None:
        """启动 braille spinner 动画（每帧 80ms）。"""
        if self._spinner_timer is not None:
            return
        self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    def _stop_spinner(self) -> None:
        """停止 spinner 动画。"""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _finish_streaming(self) -> None:
        """清理所有 streaming 状态（取消或完成时调用）。"""
        self._streaming = False
        self._stop_spinner()
        self._agent_task = None
        if self._spinner_label is not None:
            self._spinner_label.remove()
            self._spinner_label = None

    def _tick_spinner(self) -> None:
        """推进持久 spinner 标签上的动画帧。"""
        self._spinner_idx += 1
        frame = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        elapsed = _time.monotonic() - self._thinking_start
        if self._spinner_label is not None:
            self._spinner_label.update(
                f"  {frame} {self._thinking_verb}…  ({elapsed:.0f}s)"
            )
            if self._spinner_idx % 5 == 0:
                self._scroll_chat_end()

    def _scroll_chat_end(self) -> None:
        """仅在用户已停在聊天底部时滚动到底（否则保持当前浏览位置）。

        用户在权限确认 / 流式输出期间上滚查看历史时，不该被新内容拉回底部。
        用 ``is_vertical_scroll_end`` 守卫：只在视口本来就在底部时才跟随。
        """
        try:
            chat = self.query_one("#chat-area", VerticalScroll)
            if chat.is_vertical_scroll_end:
                self.call_after_refresh(chat.scroll_end, animate=False)
        except Exception:
            pass

    async def _handle_permission_request(self, request: PermissionRequest) -> None:
        from autocode.permission_dialog import InlinePermissionWidget

        widget = InlinePermissionWidget(
            request.tool_name,
            request.description,
            arguments=request.arguments,
        )
        self._pending_perm_request = request
        await self._amount_chat_widget(widget)
        self._scroll_chat_end()
        # 权限提示弹窗期间禁用输入框
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_permission_widget_responded(
        self, event: "InlinePermissionWidget.Responded"
    ) -> None:
        from autocode.permission_dialog import InlinePermissionWidget

        req = getattr(self, "_pending_perm_request", None)
        if req is not None:
            # 竞态守卫：用户可能在弹窗响应前按 Esc / ctrl+c 取消 agent，
            # agent 端 `await future` 被打断时 asyncio 会 cancel 该 future。
            # 对已取消/已完成的 future 调 set_result 会抛 InvalidStateError，
            # 故仅当 future 仍存活时才写回结果（与 AskUser handler 一致）。
            if not req.future.done():
                req.future.set_result(event.response)
            self._pending_perm_request = None
        # 从聊天区移除权限弹窗组件
        try:
            widget = self.query_one("#perm-inline", InlinePermissionWidget)
            widget.remove()
        except Exception:
            pass
        # 弹窗移除后滚动到底部，防止留下空白（widget 占用的空间消失后，
        # VerticalScroll 的 scroll offset 可能仍停在旧位置）。
        try:
            chat = self.query_one("#chat-area", VerticalScroll)
            self.call_after_refresh(chat.scroll_end, animate=False)
        except Exception:
            pass
        # 重新启用输入框
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

    # -----------------------------------------------------------------
    # 恢复 session 的消息渲染
    # -----------------------------------------------------------------

    async def _render_restored_messages(self, messages: list[Message]) -> None:
        chat_messages = self.query_one("#chat-messages", Vertical)
        chat_messages.remove_children()

        for msg in messages:
            if msg.tool_results or not msg.content:
                continue
            if msg.role == "user":
                row = Vertical(classes="user-row")
                await self._amount_chat_widget(row)
                user_rich = RichText()
                user_rich.append("❯ ", style="bold color(80)")
                user_rich.append(msg.content, style="bold color(255)")
                bubble = Static(user_rich, classes="message user-message")
                await row.mount(bubble)
            elif msg.role == "assistant":
                row = Vertical(classes="ai-row")
                await self._amount_chat_widget(row)
                md = Markdown(msg.content, classes="message ai-message")
                await row.mount(md)

        self._scroll_chat_end()

    # -----------------------------------------------------------------
    # Session 摘要（异步后台生成）
    # -----------------------------------------------------------------

    async def _update_session_summary(self) -> None:
        if not self.session or not self.client or not self.agent:
            return
        try:
            summary = await generate_session_summary(
                self.client, self.conversation, self.agent.protocol
            )
            if summary:
                self.session.meta.summary = summary
                self.session.meta.save(
                    self.session._sessions_dir / f"{self.session.session_id}.meta"
                )
        except Exception:
            pass

    # -----------------------------------------------------------------
    # MCP
    # -----------------------------------------------------------------

    async def _init_mcp(self) -> None:
        self._mcp_connecting = True
        self._update_mode_label()
        manager = MCPManager()
        manager.load_configs(self._mcp_server_configs)
        tools_before = len(self.registry.list_tools())
        errors = await manager.register_all_tools(self.registry)
        self.mcp_manager = manager
        self._mcp_connecting = False
        self._update_mode_label()
        for err in errors:
            self._show_system_message(f"MCP warning: {err}")
        tools_after = len(self.registry.list_tools())
        mcp_tools = tools_after - tools_before
        server_count = len(manager._clients)
        if server_count > 0:
            self._mcp_server_info = (
                f"Connected to {server_count} MCP server(s), {mcp_tools} tools registered"
            )
        if server_count > 0 and mcp_tools > 0:
            parts = []
            for cfg in self._mcp_server_configs:
                srv_name = cfg.name if hasattr(cfg, 'name') else str(cfg)
                tool_names = [
                    t.name for t in self.registry.list_tools()
                    if t.name.startswith(f"mcp__{srv_name}__")
                ]
                section = f"## {srv_name}\n"
                if tool_names:
                    section += "Available tools: " + ", ".join(tool_names)
                parts.append(section)
            self._mcp_instructions = (
                "# MCP Server Instructions\n\n"
                "The following MCP servers are connected. "
                "Use their tools when the user asks.\n\n"
                + "\n\n".join(parts)
            )

    async def _shutdown_mcp(self) -> None:
        if self._mcp_init_task is not None:
            self._mcp_init_task.cancel()
            try:
                await self._mcp_init_task
            except (asyncio.CancelledError, Exception):
                pass
            self._mcp_init_task = None
        if self.mcp_manager is not None:
            await self.mcp_manager.shutdown()
            self.mcp_manager = None

    # -----------------------------------------------------------------
    # 退出
    # -----------------------------------------------------------------

    async def action_handle_ctrl_c(self) -> None:
        if self._streaming:
            if self._agent_task and not self._agent_task.done():
                self._agent_task.cancel()
            self._show_system_message("(response interrupted)")
            self._finish_streaming()
            try:
                inp = self.query_one("#chat-input", ChatInput)
                inp.disabled = False
                inp.focus()
            except Exception:
                pass
            return

        if getattr(self, "_exit_requested", False):
            self.exit()
            return
        self._exit_requested = True

        async def _cleanup() -> None:
            tasks: list[asyncio.Task] = []

            if self.agent and self.agent.memory_manager:
                tasks.append(asyncio.create_task(
                    self.agent._extract_memories(self.conversation)
                ))
            if self.hook_engine:
                tasks.append(asyncio.create_task(
                    self.hook_engine.run_hooks(
                        "shutdown", HookContext(event_name="shutdown")
                    )
                ))
            tasks.append(asyncio.create_task(self._shutdown_mcp()))

            if tasks:
                await asyncio.wait(tasks, timeout=3.0)
                for t in tasks:
                    if not t.done():
                        t.cancel()

            if self.session:
                self.session.close()

        try:
            await _cleanup()
        except Exception:
            pass
        self.exit()

    def _show_error(self, text: str) -> None:
        error_widget = Static(f"✖ {text}", classes="message error-message")
        self._mount_chat_widget(error_widget)
        self._scroll_chat_end()

    def _show_system_message(self, text: str) -> None:
        msg = Static(f"  {text}", classes="message system-message")
        self._mount_chat_widget(msg)
        self._scroll_chat_end()

    _MODE_DISPLAY = {
        PermissionMode.DEFAULT: "default",
        PermissionMode.ACCEPT_EDITS: "accept-edits",
        PermissionMode.PLAN: "plan",
        PermissionMode.BYPASS: "YOLO",
    }

    def _update_mode_label(self) -> None:
        if self.agent:
            perm = self.agent.permission_mode
            display = self._MODE_DISPLAY.get(perm, perm.value)
            color = _MODE_COLORS.get(perm, "dim")
            label = self.query_one("#mode-label", Static)
            if perm == PermissionMode.DEFAULT:
                label.update(f"[{color}]{display}[/{color}]")
            else:
                label.update(f"[{color}]{display}[/{color}]  (shift+tab to cycle)")
        try:
            model_label = self.query_one("#model-label", Static)
            model_text = self._selected_provider.model if self._selected_provider else ""
            if self._mcp_connecting:
                model_label.update(f"[yellow]MCP connecting…[/yellow]  {model_text}")
            else:
                model_label.update(model_text)
        except Exception:
            pass

    def _update_token_label(self, input_tokens: int, output_tokens: int) -> None:
        pass  # token 标签已从 UI 中移除
