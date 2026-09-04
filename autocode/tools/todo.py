from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from autocode.tools.base import Tool, ToolResult


class Params(BaseModel):
    action: Literal["add", "list", "done", "remove"] = Field(
        description="add a todo item, list all items, mark an item done, or remove an item"
    )
    content: str = Field(
        default="", description="Todo text (required for action='add')"
    )
    item_id: int = Field(
        default=0, description="Item id (required for action='done'/'remove')"
    )


class Todo(Tool):
    """按 session 隔离的待办工具。

    每个 session 拥有独立的 JSON 文件（``<storage_dir>/<session_id>.json``），
    窗口 A 与窗口 B 记的待办互不可见；切换/重开会话后仍能读到自己的待办。
    """

    name = "Todo"
    description = (
        "Manage a per-session todo list: add/list/done/remove items. "
        "Todo items are stored per conversation session, so each window keeps "
        "its own independent list."
    )
    params_model = Params
    category = "write"
    is_concurrency_safe = True

    def __init__(self, storage_dir: Path | None = None, session_id: str = "default") -> None:
        self._storage_dir = storage_dir or Path(".autocode") / "todos"
        self._session_id = session_id

    def set_session(self, session_id: str) -> None:
        """切换当前 session（由 App 在创建/切换会话时调用）。"""
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def _path(self) -> Path:
        return self._storage_dir / f"{self._session_id}.json"

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items: list[dict]) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._path().write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def execute(self, params: Params) -> ToolResult:
        action = params.action
        if action == "list":
            return ToolResult(output=self._render(self._load()))

        items = self._load()

        if action == "add":
            content = (params.content or "").strip()
            if not content:
                return ToolResult(
                    output="Error: 'content' is required for action='add'",
                    is_error=True,
                )
            item = {
                "id": (max((i["id"] for i in items), default=0) + 1),
                "content": content,
                "done": False,
                "created_at": time.time(),
            }
            items.append(item)
            self._save(items)
            return ToolResult(
                output=f"Added todo #{item['id']}: {content}\n"
                f"现在共有 {len(items)} 条待办（session: {self._session_id}）"
            )

        if not items:
            return ToolResult(output="待办列表为空。")

        item = next((i for i in items if i["id"] == params.item_id), None)
        if item is None:
            return ToolResult(
                output=f"Error: no todo with id={params.item_id} "
                f"(current ids: {', '.join(str(i['id']) for i in items) or 'none'})",
                is_error=True,
            )

        if action == "done":
            item["done"] = True
            self._save(items)
            return ToolResult(output=f"Marked todo #{item['id']} as done: {item['content']}")

        # action == "remove"
        items = [i for i in items if i["id"] != params.item_id]
        self._save(items)
        return ToolResult(output=f"Removed todo #{params.item_id}")

    def _render(self, items: list[dict]) -> str:
        if not items:
            return f"待办列表为空（session: {self._session_id}）。"
        lines = [f"待办列表（session: {self._session_id}）:", ""]
        for i in items:
            mark = "[x]" if i["done"] else "[ ]"
            lines.append(f"{i['id']}. {mark} {i['content']}")
        return "\n".join(lines)
