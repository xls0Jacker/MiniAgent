from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from autocode.tools.base import Tool, ToolResult

# 模拟天气数据：真实天气需联网 API。为满足笔试题演示与离线测试，
# 这里按城市名哈希出"确定性"的伪数据——同一城市每次查询结果一致。
_CITIES: dict[str, str] = {
    "北京": "beijing",
    "上海": "shanghai",
    "广州": "guangzhou",
    "深圳": "shenzhen",
    "杭州": "hangzhou",
    "成都": "chengdu",
    "武汉": "wuhan",
    "西安": "xian",
}

_CONDITIONS = ["晴", "多云", "阴", "小雨", "中雨", "阵雨"]


class Params(BaseModel):
    city: str = Field(
        description="City name in Chinese, e.g. '北京', '上海'."
    )


class Weather(Tool):
    name = "Weather"
    description = (
        "Query the current (simulated) weather for a Chinese city and return "
        "temperature, condition and humidity. Data is mocked locally for "
        "demonstration — no real network call."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: Params) -> ToolResult:
        city = (params.city or "").strip()
        key = _CITIES.get(city)
        if key is None:
            supported = "、".join(_CITIES.keys())
            return ToolResult(
                output=f"Error: unsupported city '{city}'. Supported cities: {supported}",
                is_error=True,
            )

        # 用哈希构造确定性伪数据：同一城市稳定返回同一组值。
        digest = hashlib.md5(key.encode("utf-8")).digest()
        temp = 8 + (digest[0] % 27)                      # 8 ~ 34 ℃
        humidity = 35 + (digest[1] % 60)                 # 35% ~ 94%
        condition = _CONDITIONS[digest[2] % len(_CONDITIONS)]
        return ToolResult(
            output=(
                f"【{city}】当前天气（模拟数据）\n"
                f"  天气状况：{condition}\n"
                f"  气温：{temp} ℃\n"
                f"  湿度：{humidity}%\n"
                f"  （mock 数据，非实时）"
            )
        )
