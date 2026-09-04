from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from autocode.agent import PermissionResponse


_PERM_OPTIONS = [
    ("Yes", PermissionResponse.ALLOW),
    ("Yes, and don't ask again for this pattern", PermissionResponse.ALLOW_ALWAYS),
    ("No", PermissionResponse.DENY),
]

# diff 预览最多显示的原始行数，避免超长文件刷屏
_MAX_PREVIEW_LINES = 60

# 参考 Claude Code（StructuredDiff/Fallback.tsx + dark 主题）的 diff 配色：
# 整行背景用暗色，词级高亮用更亮的同色系，代码本身保持终端默认前景色。
_STYLE_ADD = "on color(22)"        # 新增整行：暗绿底（diffAdded  rgb(34,92,43)）
_STYLE_DEL = "on color(52)"        # 删除整行：暗红底（diffRemoved rgb(122,41,54)）
_STYLE_ADD_WORD = "on color(28)"   # 新增词：更深绿底（diffAddedWord  rgb(56,166,96)）
_STYLE_DEL_WORD = "on color(88)"   # 删除词：更深红底（diffRemovedWord rgb(179,89,107)）

# 词级 diff 的改动比例阈值：超过即降级为整行渲染（与 Claude Code CHANGE_THRESHOLD=0.4 一致）
_CHANGE_THRESHOLD = 0.4


def _guess_language(file_path: str) -> str:
    """根据文件扩展名猜测语言，用于语法高亮。猜不到返回 text（无高亮）。"""
    try:
        from pygments.lexers import get_lexer_for_filename

        lexer = get_lexer_for_filename(file_path)
        return lexer.name.lower()
    except Exception:
        return "text"


def _highlight_line(line: str, language: str) -> Text:
    """对单行代码做语法高亮，返回 Rich Text 对象。

    用 Pygments 逐行高亮，返回 Text 对象（span 样式），避免 markup 字符串
    在含 ``[`` / ``]`` 时 tag 错乱崩溃的问题。语言为 text 时返回纯文本。
    返回的 Text 只含前景 token 色，背景由调用方叠加（词级 diff / 整行背景）。
    """
    if not line:
        return Text("")
    if language == "text":
        return Text(line)
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_by_name
        from pygments.formatters import Terminal256Formatter

        lexer = get_lexer_by_name(language)
        ansi = highlight(line, lexer, Terminal256Formatter())
        text = Text.from_ansi(ansi)
        text.rstrip()
        return text
    except Exception:
        return Text(line)


_WORD_RE = re.compile(r"\w+|[^\w\s]|\s+")


def _word_diff_ranges(
    old: str, new: str
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """对 old/new 两行做词级 diff（对应 Claude Code 的 diffWordsWithSpace）。

    先把两行按 token 切分（词、标点、空白各自成 token），再对 token
    序列做 diff，返回 (old 变化区间, new 变化区间)。整个变化词（而非单个
    字符）会被标记，与 diffWords 的整词语义一致。
    """
    def _tokenize(s: str) -> list[tuple[str, int, int]]:
        return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(s)]

    old_toks = _tokenize(old)
    new_toks = _tokenize(new)
    matcher = difflib.SequenceMatcher(
        None,
        [t[0] for t in old_toks],
        [t[0] for t in new_toks],
        autojunk=False,
    )
    old_ranges: list[tuple[int, int]] = []
    new_ranges: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for k in range(i1, i2):
            old_ranges.append((old_toks[k][1], old_toks[k][2]))
        for k in range(j1, j2):
            new_ranges.append((new_toks[k][1], new_toks[k][2]))
    return old_ranges, new_ranges


def _diff_line(
    mark: str,
    line: str,
    language: str,
    bg_style: str,
    word_style: str,
    old: str | None = None,
    new: str | None = None,
    is_add: bool = True,
    line_no: int | None = None,
    gutter_width: int = 0,
    width: int = 0,
) -> Text:
    """生成一行带整行背景色的 diff 行（Claude Code 风格）。

    代码保留原本语法高亮配色（Pygments），只叠整行背景 + 变化词加深背景。

    - mark：行首 ``+`` / ``-`` 标记，白色前景叠加在背景上。
    - line：该行去除标记后的代码。
    - language：Pygments 语言名，传给 ``_highlight_line`` 做语法高亮。
    - bg_style：整行背景色（``on color(22)`` / ``on color(52)``）。
    - word_style：词级 diff 时变化词叠加深背景（``on color(28)`` / ``on color(88)``）。
    - old / new：相邻 add/remove 配对行的原/新内容。传了才做词级 diff：
      当前行相对配对行**实际变化**的部分用更深背景（对应 Claude Code 的
      diffAddedWord / diffRemovedWord），公共段保持整行背景。
    - line_no / gutter_width：行号 gutter。line_no 右对齐到 gutter_width，
      与 Claude Code 的 ``lineNumStr = 行号.padStart(maxWidth) + ' '`` 一致。
    - width：容器宽度。大于 0 时在代码尾部 pad 空格，让背景横贯到行尾。
    """
    row = Text("  ", style="")
    row.append(mark, style=f"white {bg_style}")

    # 行号 gutter：右对齐到 gutter_width（gutter_width > 0 时启用）
    if gutter_width > 0:
        if line_no is not None:
            num = f"{line_no:>{gutter_width}} "
        else:
            num = " " * (gutter_width + 1)
        row.append(num, style=bg_style)

    # 代码保留原本语法高亮前景色，只叠背景
    code = _highlight_line(line, language)
    # 整行叠背景（先于词级，保证视觉上是连续色条）
    code.stylize(bg_style)

    # 词级 diff：仅当传入配对行且整行改动比例不超阈值时启用
    if old is not None and new is not None:
        old_ranges, new_ranges = _word_diff_ranges(old, new)
        # 与 Claude Code 一致：只统计当前渲染侧的变化字符数
        # （add 行看 new 侧 added 段，remove 行看 old 侧 removed 段）
        ranges = new_ranges if is_add else old_ranges
        changed = sum(end - start for start, end in ranges)
        total = len(old) + len(new)
        if total > 0 and changed / total <= _CHANGE_THRESHOLD:
            for start, end in ranges:
                code.stylize(word_style, start, end)

    row.append_text(code)

    # 把背景横贯到整行宽度：pad 空格直到 container width
    if width > 0:
        pad_len = width - row.cell_len
        if pad_len > 0:
            row.append(" " * pad_len, style=bg_style)

    return row


def _build_diff_preview(
    tool_name: str, arguments: dict[str, Any] | None, width: int = 0
) -> list[Text]:
    """根据工具调用参数构造 diff 预览行（Rich Text 对象列表）。

    返回空列表表示"无预览"（例如 Bash 命令，保持原有描述即可）。
    - WriteFile：展示将写入的完整 content（新建 / 覆盖），代码保留原本配色。
    - EditFile：读原文件 + old/new_string，用 difflib 生成真正 diff，
      新增行 ``+``（绿底）、删除行 ``-``（红底），代码保留原本配色。
    - width：容器宽度，用于把每行背景 pad 到行尾（传 0 则不 pad）。
    任何读文件失败 / 缺参都优雅降级为空列表。
    """
    if not arguments:
        return []
    try:
        if tool_name == "WriteFile":
            content = arguments.get("content", "")
            file_path = arguments.get("file_path", "")
            if not isinstance(content, str):
                return []
            language = _guess_language(file_path)
            lines = content.splitlines()
            if len(lines) > _MAX_PREVIEW_LINES:
                shown = lines[:_MAX_PREVIEW_LINES]
                tail = f"\n  [dim]… {len(lines) - _MAX_PREVIEW_LINES} more lines[/dim]"
            else:
                shown = lines
                tail = ""
            # 整体标注新增（写操作就是新增/覆盖），代码保留原本配色 + 绿底
            max_line = len(shown)
            gutter = len(str(max_line)) if max_line else 0
            preview: list[Text] = []
            for idx, ln in enumerate(shown, start=1):
                preview.append(_diff_line(
                    "+ ", ln, language, _STYLE_ADD, _STYLE_ADD_WORD,
                    line_no=idx, gutter_width=gutter, width=width,
                ))
            if tail:
                preview.append(Text.from_markup(tail))
            return preview

        if tool_name == "EditFile":
            path = Path(arguments.get("file_path", ""))
            old_string = arguments.get("old_string", "")
            new_string = arguments.get("new_string", "")
            if not path.exists() or not isinstance(old_string, str) or not isinstance(new_string, str):
                return []
            original = path.read_text(encoding="utf-8", errors="replace")
            # 用 old_string → new_string 模拟编辑后的结果，与 edit_file.py 逻辑一致
            new_content = original.replace(old_string, new_string, 1)
            diff = difflib.unified_diff(
                original.splitlines(),
                new_content.splitlines(),
                fromfile="before",
                tofile="after",
                lineterm="",
            )
            language = _guess_language(str(path))
            # 收集 diff 行，去掉 +++/--- 文件头，只留 +/-/@/上下文
            lines = [
                ln for ln in diff
                if not ln.startswith("+++") and not ln.startswith("---") and ln
            ]
            # 解析成展示行：含行号 + 配对信息（对齐 Claude Code numberDiffLines）
            shown: list[tuple[str, str, int, str | None, str | None]] = []
            old_line = 1
            i = 0
            while i < len(lines):
                ln = lines[i]
                m = re.search(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
                if m:
                    old_line = int(m.group(1))
                    i += 1
                    continue
                if ln.startswith(" "):
                    # 上下文行：暗背景，行号递增
                    shown.append(("ctx", ln[1:], old_line, None, None))
                    old_line += 1
                    i += 1
                    continue
                if ln.startswith("-") and not ln.startswith("--"):
                    dels: list[str] = []
                    while i < len(lines) and lines[i].startswith("-") and not lines[i].startswith("--"):
                        dels.append(lines[i][1:])
                        i += 1
                    adds: list[str] = []
                    while i < len(lines) and lines[i].startswith("+"):
                        adds.append(lines[i][1:])
                        i += 1
                    pair_count = min(len(dels), len(adds))
                    for k in range(pair_count):
                        # remove/add 配对共享 old_line 行号
                        shown.append(("del", dels[k], old_line, dels[k], adds[k]))
                        shown.append(("add", adds[k], old_line, dels[k], adds[k]))
                    for k in range(pair_count, len(dels)):
                        shown.append(("del", dels[k], old_line, None, None))
                    for k in range(pair_count, len(adds)):
                        old_line += 1
                        shown.append(("add", adds[k], old_line, None, None))
                    old_line += len(dels)
                    continue
                if ln.startswith("+"):
                    shown.append(("add", ln[1:], old_line, None, None))
                    old_line += 1
                    i += 1
                    continue
                i += 1
            # 统一行号 gutter 宽度
            max_line = max((row[2] for row in shown), default=0)
            gutter = len(str(max_line)) if max_line else 0
            preview: list[Text] = []
            for kind, code, line_no, old, new in shown:
                if len(preview) >= _MAX_PREVIEW_LINES:
                    preview.append(Text.from_markup("  [dim]… more lines[/dim]"))
                    break
                if kind == "ctx":
                    row = Text("  ", style="")
                    row.append(" ", style=f"white on color(236)")
                    if gutter:
                        row.append(f"{line_no:>{gutter}} ", style="on color(236)")
                    ctx_text = _highlight_line(code, language)
                    ctx_text.stylize("on color(236)")
                    row.append_text(ctx_text)
                    if width > 0:
                        pad_len = width - row.cell_len
                        if pad_len > 0:
                            row.append(" " * pad_len, style="on color(236)")
                    preview.append(row)
                elif kind == "del":
                    preview.append(_diff_line("- ", code, language, _STYLE_DEL, _STYLE_DEL_WORD,
                                              old=old, new=new, is_add=False,
                                              line_no=line_no, gutter_width=gutter, width=width))
                else:
                    preview.append(_diff_line("+ ", code, language, _STYLE_ADD, _STYLE_ADD_WORD,
                                              old=old, new=new, is_add=True,
                                              line_no=line_no, gutter_width=gutter, width=width))
            return preview
    except Exception:
        return []
    return []


class InlinePermissionWidget(Vertical, can_focus=True):
    """渲染在聊天区域内部的内联权限确认提示。

    与 Go 版 TUI 的权限对话框一致：工具名 + 描述 + 带编号的
    选项，支持方向键导航 + 回车确认。
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "deny", "Deny", priority=True),
    ]

    class Responded(Message):


        def __init__(self, response: PermissionResponse) -> None:
            super().__init__()
            self.response = response

    def __init__(self, tool_name: str, description: str, arguments: dict[str, Any] | None = None, **kwargs) -> None:
        super().__init__(id="perm-inline", **kwargs)
        self._tool_name = tool_name
        self._description = description
        self._arguments = arguments
        self._cursor = 0

    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="perm-content")

    def _content_width(self) -> int:
        """取组件内容区宽度；未挂载时返回 0，diff 行不 pad。"""
        try:
            return self.content_size.width if self.is_mounted else 0
        except Exception:
            return 0

    def on_mount(self) -> None:
        self.focus()
        self._refresh()

    def on_resize(self) -> None:
        self._refresh()

    def _build_content(self) -> Text:
        t = Text()
        t.append(f"\n  {self._tool_name} command\n", style="bold yellow")
        t.append(f"    {self._description}\n")

        # diff 预览：WriteFile/EditFile 展示将发生的改动，其他工具无预览保持原样
        preview = _build_diff_preview(self._tool_name, self._arguments, width=self._content_width())
        if preview:
            t.append("\n  Change preview:\n", style="bold")
            for row in preview:
                t.append_text(row)
                t.append("\n")
            t.append("\n")

        t.append("  This command requires approval\n", style="dim")
        t.append("  Do you want to proceed?\n")

        for i, (label, _resp) in enumerate(_PERM_OPTIONS):
            if i == self._cursor:
                t.append(f" ❯ {i + 1}. {label}\n", style="bold cyan")
            else:
                t.append(f"   {i + 1}. {label}\n", style="dim")

        return t


    def _refresh(self) -> None:
        content = self.query_one("#perm-content", Static)
        content.update(self._build_content())

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()

    def action_cursor_down(self) -> None:
        if self._cursor < len(_PERM_OPTIONS) - 1:
            self._cursor += 1
            self._refresh()

    def action_select(self) -> None:
        _, response = _PERM_OPTIONS[self._cursor]
        self.post_message(self.Responded(response))


    def action_deny(self) -> None:
        self.post_message(self.Responded(PermissionResponse.DENY))
