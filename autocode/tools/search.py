from __future__ import annotations

from pydantic import BaseModel, Field

from autocode.tools.base import Tool, ToolResult

# 笔试题允许 search 为 mock（真实联网搜索需额外 API key，超出必要范围）。
# 本实现为内置知识库子串匹配：确定性、离线可测，作为工具注册机制的演示载体。
_KB: list[dict] = [
    {"title": "ReAct: Reasoning and Acting in Language Models", "snippet": "让 LLM 在'思考'与'行动(调工具)'间交替，直到得出答案的经典范式。", "url": "https://example.com/react"},
    {"title": "Function Calling 工具系统", "snippet": "Agent 通过工具 Schema 让 LLM 自主决策调用外部函数，返回结构化结果。", "url": "https://example.com/function-calling"},
    {"title": "Agent Loop 主循环", "snippet": "接收输入→判断回复或调工具→执行工具→根据结果决定继续循环或返回。", "url": "https://example.com/agent-loop"},
    {"title": "上下文管理与 Token 预算", "snippet": "接近 context window 上限时用摘要压缩历史，保留关键信息继续对话。", "url": "https://example.com/context-window"},
    {"title": "System Prompt 组装", "snippet": "把身份、工具用法、环境、记忆等分段按优先级拼成最终系统提示词。", "url": "https://example.com/system-prompt"},
    {"title": "MCP 协议开放工具生态", "snippet": "Model Context Protocol 让 Agent 通过统一协议接入任意外部工具服务。", "url": "https://example.com/mcp"},
    {"title": "记忆系统跨会话召回", "snippet": "把用户长期偏好写入记忆文件，新会话启动时按相关性注入系统提示词。", "url": "https://example.com/memory"},
    {"title": "Permission 权限拦截", "snippet": "工具执行前经过权限检查，高危操作请求用户确认，形成安全刹车。", "url": "https://example.com/permissions"},
    {"title": "Python 列表推导式", "snippet": "Python 用一行表达式对可迭代对象做映射与过滤的惯用写法。", "url": "https://example.com/python-listcomp"},
    {"title": "async/await 异步编程", "snippet": "Python 通过事件循环让 IO 密集任务并发执行，asyncio 是其标准库。", "url": "https://example.com/python-asyncio"},
]

_TOP_N = 3


class Params(BaseModel):
    query: str = Field(
        description="Search keywords, e.g. 'agent loop' or 'python async'."
    )


class Search(Tool):
    name = "Search"
    description = (
        "Search a local knowledge base (mock engine, no network) and return the "
        "top matching entries with a short snippet and url. Use to look up "
        "concepts about agents, tools, and programming."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:
        query = (params.query or "").strip().lower()
        if not query:
            return ToolResult(output="Error: empty query", is_error=True)

        hits = [
            e for e in _KB
            if query in e["title"].lower() or query in e["snippet"].lower()
        ]
        if not hits:
            return ToolResult(
                output=f"No results found for '{params.query}'. "
                "Try keywords like 'agent', 'tool', 'memory', 'python'."
            )

        lines = [f"Top {len(hits)} result(s) for '{params.query}':", ""]
        for i, e in enumerate(hits[:_TOP_N], 1):
            lines.append(f"{i}. {e['title']}")
            lines.append(f"   {e['snippet']}")
            lines.append(f"   URL: {e['url']}")
            lines.append("")
        return ToolResult(output="\n".join(lines).rstrip())
