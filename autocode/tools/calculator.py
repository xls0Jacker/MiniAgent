from __future__ import annotations

import ast
import operator
from typing import Any

from pydantic import BaseModel, Field

from autocode.tools.base import Tool, ToolResult

# 白名单：只允许四则运算 + 取模 + 幂 + 括号 + 一元正负号。
# 用 AST 而非 eval/exec，从根上杜绝任意代码执行。
_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_MAX_EXPR_LEN = 1000


class Params(BaseModel):
    expression: str = Field(
        description="A mathematical expression to evaluate, e.g. '3.5*128+2'. "
        "Supports + - * / // % ** parentheses and unary signs. Numbers only."
    )


class Calculator(Tool):
    name = "Calculator"
    description = (
        "Evaluate a mathematical expression safely and return the numeric result. "
        "Use for arithmetic like '3.5 * 128 equals what?' or 'what is (2+3)*4?'."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:
        expr = (params.expression or "").strip()
        if not expr:
            return ToolResult(output="Error: empty expression", is_error=True)
        if len(expr) > _MAX_EXPR_LEN:
            return ToolResult(
                output=f"Error: expression too long (>{_MAX_EXPR_LEN} chars)",
                is_error=True,
            )
        try:
            tree = ast.parse(expr, mode="eval")
            result = self._eval_node(tree.body)
        except ZeroDivisionError:
            return ToolResult(output="Error: division by zero", is_error=True)
        except Exception as e:  # 非法语法 / 越权节点等统一走错误返回
            return ToolResult(output=f"Error: {e}", is_error=True)
        return ToolResult(output=format(result))

    def _eval_node(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self._eval_node(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("only numeric literals are allowed")
            return node.value
        if isinstance(node, ast.BinOp):
            op = _BIN_OPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported operator")
            return op(self._eval_node(node.left), self._eval_node(node.right))
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.UAdd):
                return +self._eval_node(node.operand)
            if isinstance(node.op, ast.USub):
                return -self._eval_node(node.operand)
            raise ValueError("unsupported unary operator")
        raise ValueError("only arithmetic expressions are allowed")


def format(result: Any) -> str:
    """数字转文本：整数不带小数点，大浮点数不丢精度到太多位。"""
    if isinstance(result, float) and result.is_integer():
        return str(int(result))
    return repr(result)
