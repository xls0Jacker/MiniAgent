"""权限确认框 diff 预览（Pygments 语法高亮 + Claude Code 风格整行背景）测试。

覆盖：
- _guess_language：按扩展名猜语言，猜不到降级 text
- _highlight_line：单行语法高亮返回 Text 对象，含 [ / ] 不崩溃
- _build_diff_preview：WriteFile 全量预览 / EditFile 真 diff / 截断 / 缺参降级
- 背景色：新增行绿底（color(22)）、删除行红底（color(52)），token 色叠加
"""
from __future__ import annotations

import tempfile
import os

import pytest

from autocode.permission_dialog import (
    _build_diff_preview,
    _guess_language,
    _highlight_line,
    _MAX_PREVIEW_LINES,
)


# ---------------------------------------------------------------------------
# _guess_language
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, expected",
    [
        ("main.py", "python"),
        ("app.js", "javascript"),
        ("style.css", "css"),
        ("index.html", "html"),
        ("data.json", "json"),
        ("no_ext", "text"),
        ("", "text"),
    ],
)
def test_guess_language(path: str, expected: str) -> None:
    lang = _guess_language(path)
    if expected == "text":
        assert lang == "text"
    else:
        assert lang != "text"


# ---------------------------------------------------------------------------
# _highlight_line
# ---------------------------------------------------------------------------

def test_highlight_line_returns_text_object() -> None:
    from rich.text import Text
    t = _highlight_line("def foo(x):\n", "python")
    assert isinstance(t, Text)


def test_highlight_line_plain_text_no_lexer() -> None:
    from rich.text import Text
    t = _highlight_line("just some text", "text")
    assert isinstance(t, Text)
    assert t.plain == "just some text"


def test_highlight_line_with_brackets_no_crash() -> None:
    """含 [ ] 的代码不能触发 markup tag 错乱崩溃（历史 bug）。"""
    from rich.text import Text
    code = "data = [x for x in range(10)]"
    t = _highlight_line(code, "python")
    assert isinstance(t, Text)
    assert t.plain == code


def test_highlight_line_empty() -> None:
    from rich.text import Text
    t = _highlight_line("", "python")
    assert isinstance(t, Text)
    assert t.plain == ""


# ---------------------------------------------------------------------------
# _build_diff_preview — WriteFile
# ---------------------------------------------------------------------------

def test_writefile_preview_has_green_background() -> None:
    preview = _build_diff_preview(
        "WriteFile",
        {"file_path": "foo.py", "content": "def foo(x):\n    return x\n"},
    )
    assert preview, "WriteFile 应有预览"
    row = preview[0]
    # 行首是 sigil(+ ) + 行号 gutter + 代码
    assert row.plain.lstrip().startswith("+ 1 def foo(x):")
    # 整行绿底：任一 span 带 on color(22)
    assert any("on color(22)" in str(s.style) for s in row.spans), "新增行缺绿色背景"


def test_writefile_preview_token_color_preserved() -> None:
    """代码保留原本语法高亮前景色，整行背景叠加在其上方。"""
    preview = _build_diff_preview(
        "WriteFile",
        {"file_path": "foo.py", "content": "def foo(x):\n"},
    )
    row = preview[0]
    styles = [str(s.style) for s in row.spans]
    # 整行背景必须有
    assert any("on color(22)" in s for s in styles), "缺整行背景"
    # 代码区应有 Pygments 前景 token 色（如 def 的蓝色）
    fg_styles = [s for s in styles if "on color" not in s and "color(" in s]
    assert fg_styles, f"应有 Pygments 前景色: {styles}"


def test_writefile_preview_truncates_long_content() -> None:
    content = "\n".join(f"line {i}" for i in range(200))
    preview = _build_diff_preview("WriteFile", {"file_path": "big.txt", "content": content})
    assert len(preview) == _MAX_PREVIEW_LINES + 1  # 60 行 + 省略行


def test_writefile_preview_missing_content_returns_empty() -> None:
    assert _build_diff_preview("WriteFile", {}) == []
    assert _build_diff_preview("WriteFile", {"content": 123}) == []


# ---------------------------------------------------------------------------
# _build_diff_preview — EditFile
# ---------------------------------------------------------------------------

def _make_temp_py(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_editfile_preview_has_add_and_del_backgrounds() -> None:
    path = _make_temp_py("a = 1\nb = 2\n")
    try:
        preview = _build_diff_preview(
            "EditFile",
            {"file_path": path, "old_string": "a = 1", "new_string": "a = 10"},
        )
        assert preview, "EditFile 应有预览"
        add_rows = [r for r in preview if r.plain.lstrip().startswith("+")]
        del_rows = [r for r in preview if r.plain.lstrip().startswith("-")]
        assert add_rows and del_rows, "应同时有新增和删除行"
        assert any("on color(22)" in str(s.style) for s in add_rows[0].spans), "新增行缺绿底"
        assert any("on color(52)" in str(s.style) for s in del_rows[0].spans), "删除行缺红底"
    finally:
        os.unlink(path)


def test_editfile_preview_missing_file_returns_empty() -> None:
    assert _build_diff_preview(
        "EditFile", {"file_path": "/no/such/file.py", "old_string": "a", "new_string": "b"}
    ) == []


def test_editfile_preview_truncates() -> None:
    """EditFile 大改动（几十行）也应截断，不刷屏。"""
    big = "\n".join(f"line {i} = {i}" for i in range(80))
    path = _make_temp_py(big + "\n")
    try:
        preview = _build_diff_preview(
            "EditFile",
            {"file_path": path, "old_string": "line 0 = 0", "new_string": "line 0 = 999"},
        )
        assert len(preview) <= _MAX_PREVIEW_LINES + 2  # 60 行 + 省略/hunk 行
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------

def test_unknown_tool_no_preview() -> None:
    assert _build_diff_preview("Bash", {"command": "ls"}) == []


def test_none_arguments_no_preview() -> None:
    assert _build_diff_preview("WriteFile", None) == []


def test_empty_arguments_no_preview() -> None:
    assert _build_diff_preview("WriteFile", {}) == []


# ---------------------------------------------------------------------------
# 词级 diff（参考 Claude Code diffWordsWithSpace）
# ---------------------------------------------------------------------------

def test_word_diff_marks_changed_word_only() -> None:
    """相邻 add/remove 配对：只有变化词加深背景，公共词保持普通背景。"""
    from autocode.permission_dialog import (
        _diff_line, _STYLE_ADD, _STYLE_ADD_WORD, _STYLE_DEL, _STYLE_DEL_WORD,
    )
    old = "function oldName(param) {"
    new = "function newName(param) {"

    add_row = _diff_line("+ ", new, "text", _STYLE_ADD, _STYLE_ADD_WORD,
                         old=old, new=new, is_add=True)
    word_spans = [
        add_row.plain[s.start:s.end]
        for s in add_row.spans if "on color(28)" in str(s.style)
    ]
    # newName 整词深绿，公共 function/(param) { 不被标
    assert "newName" in word_spans, "变化词 newName 应有深绿背景"
    assert all(w not in ("function", "param", "{") for w in word_spans), "公共词不应加深"

    del_row = _diff_line("- ", old, "text", _STYLE_DEL, _STYLE_DEL_WORD,
                         old=old, new=new, is_add=False)
    word_spans = [
        del_row.plain[s.start:s.end]
        for s in del_row.spans if "on color(88)" in str(s.style)
    ]
    assert "oldName" in word_spans, "变化词 oldName 应有深红背景"


def test_word_diff_falls_back_when_ratio_too_high() -> None:
    """改动比例超阈值（0.4）时降级为纯整行背景，不加深任何词。"""
    from autocode.permission_dialog import (
        _diff_line, _STYLE_ADD, _STYLE_ADD_WORD,
    )
    old = "completely different line"
    new = "totally unrelated content"
    row = _diff_line("+ ", new, "text", _STYLE_ADD, _STYLE_ADD_WORD,
                     old=old, new=new, is_add=True)
    word_spans = [s for s in row.spans if "on color(28)" in str(s.style)]
    assert not word_spans, "高变化比例不应有词级加深"
    # 但整行绿底仍在
    assert any("on color(22)" in str(s.style) for s in row.spans), "整行绿底应保留"


def test_word_diff_handles_space_and_punct() -> None:
    """空白/标点 token 不破坏词级 diff（diffWordsWithSpace 语义）。"""
    from autocode.permission_dialog import _word_diff_ranges
    old = "a = 1"
    new = "a = 10"
    old_ranges, new_ranges = _word_diff_ranges(old, new)
    # 变化部分：'1' → '10'
    assert new_ranges, "应有新增区间"
    assert old_ranges, "应有删除区间"
    assert old[old_ranges[0][0]:old_ranges[0][1]] == "1"
    assert new[new_ranges[0][0]:new_ranges[0][1]] == "10"
