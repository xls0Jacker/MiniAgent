"""非交互模式（`autocode -p`）的终端输出美化。

用 Rich 渲染 markdown（含代码块语法高亮），替代裸 print。
依据 tui-design skill 的指导：Rich 适合 "print and exit" 的一次性 CLI 输出。

设计要点：
- 不破坏 `run_to_completion` 返回的原始文本，只改变最终呈现。
- 空文本 / 非 markdown 内容也能优雅降级。
- 保持 stdout 干净（诊断走 stderr，便于脚本解析）。
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown

# 一次创建，复用；默认探测终端宽度，非 TTY 时自动关色
_console = Console()


def print_result(text: str) -> None:
    """把 `autocode -p` 的最终结果渲染到 stdout。

    - 空结果：什么都不打（避免空 markdown 产生空行噪音）。
    - 有内容：按 markdown 渲染，代码块自动语法高亮（Pygments，默认 monokai）。
    """
    if not text:
        return
    # Markdown 渲染本身足够健壮；即使内容是普通文本，也会被当作一段正文输出。
    # 原 print 带 flush=True，这里显式 flush 保持相同的实时输出语义。
    _console.print(Markdown(text))
    _console.file.flush()
