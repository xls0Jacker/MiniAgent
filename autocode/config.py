from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validator import (
    ConfigError,
    DEFAULT_CONTEXT_WINDOW,
    VALID_PERMISSION_MODES,
    VALID_PROTOCOLS,
    lookup_model_context_window,
    validate_config_structure,
)


_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
}

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class ProviderConfig:
    """单个 LLM provider 的配置（对应 config.yaml 里 providers 列表的一项）。"""

    name: str           # 配置里的别名，如 "deepseek"
    protocol: str       # anthropic | openai | openai-compat
    base_url: str       # API 端点；本地代理可指向自建服务
    model: str          # 模型名
    api_key: str = ""   # 显式 key；空则走 resolve_api_key() 的环境变量回退
    thinking: bool = False
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值。
    context_window: int = 0
    max_output_tokens: int = 0
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不会持久化。
    _fetched_context_window: int = field(default=0, repr=False)

    def resolve_api_key(self) -> str:
        """取本 provider 的 API key：优先配置里显式写的，否则按协议查环境变量。

        输入: 无（读取 self.api_key 与协议类型）。
        作用: 统一「配置 or 环境变量」两种 key 来源。
        输出: str——取到的 key；都没有则为空串（交由客户端抛认证错误）。
        """
        if self.api_key:
            return self.api_key
        env_var = _ENV_KEY_MAP.get(self.protocol, "")
        return os.environ.get(env_var, "")

    def set_fetched_context_window(self, window: int) -> None:
        """记录从 provider 自动拉取到的 context window（第 2 层）。

        非正数会被忽略，这样一次失败的拉取就不会污染 cache。在解析
        context window 时，每个 provider 只会调用一次。
        """
        if window > 0:
            self._fetched_context_window = window

    def get_context_window(self) -> int:
        """通过四层 fallback 解析模型的 context window，按优先级从高到低：

          1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
          2. 从 provider 的 /v1/models 端点自动拉取并通过 set_fetched_context_window
             缓存的值（只有 anthropic 协议的 provider 才会设置它；拉取失败或缺失时
             保持为 0 并跳过）。
          3. 内置的「模型名 -> window」映射表（按子串匹配）。
          4. 保守的默认值（claude -> 200000，其他 -> 128000）。
        """
        # 显式覆盖值永远最高优先，且一旦命中无需再查网络/映射表
        if self.context_window > 0:
            return self.context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        if "claude" in self.model.lower():
            return DEFAULT_CONTEXT_WINDOW
        return 128_000   # 兜底默认：非 claude 系按 128k 保守估计

    def get_max_output_tokens(self) -> int:
        """解析本 provider 的最大输出 token 数。

        输入: 无（读取 self.max_output_tokens 与 self.thinking）。
        作用: 配置显式值优先；否则按是否开 thinking 决定档位。
        输出: int——thinking 开 64k、关 8k（未显式设置时）。
        """
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        if self.thinking:
            return 64000   # 思考模型需要更大输出预算容纳思维链
        return 8192


def resolve_env_vars(value: str) -> str:
    """把字符串里的 ${VAR} 占位符替换成环境变量值。

    输入: value——可能含 ${VAR} 的原始字符串。
    作用: 支持在配置里引用环境变量（如 ${OPENAI_API_KEY}）。
    输出: str——未定义的环境变量保留原 ${VAR} 字面，不报错。
    """
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    """为 MCP 子进程构造环境变量（保留 PATH，并入声明项并解析 ${VAR}）。

    输入: declared_env——MCP server 配置里声明的 env 字典（可空）。
    作用: 子进程需要继承当前 PATH 才能启动 stdio 命令，声明的 env 解析占位符后并入。
    输出: dict[str, str]——最终传给子进程的环境。
    """
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path   # 不继承 PATH 的话 stdio 子进程常找不到可执行文件
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


@dataclass
class MCPServerConfig:
    """MCP server 连接配置：stdio（本地命令）或 streamable HTTP（url）二选一。"""

    name: str
    command: str | None = None          # 设了即 stdio 型：要拉起子进程
    args: list[str] = field(default_factory=list)
    url: str | None = None              # 设了即 HTTP 型
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


    @property
    def is_stdio(self) -> bool:
        """是否为 stdio 型 MCP server（有 command 即用子进程跑，而非 HTTP url）。"""
        return self.command is not None


@dataclass
class AppConfig:
    """合并后的顶层应用配置（validator 清洗过的字典装配而成）。"""

    providers: list[ProviderConfig]
    permission_mode: str = "default"
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    raw_hooks: list[dict] = field(default_factory=list)   # hook 定义原样透传
    max_iterations: int = 50                              # Agent 主循环轮次护栏


def _load_single_file(path: Path) -> AppConfig:
    """加载并校验单个 YAML 配置文件，装配成 AppConfig。

    输入: path——一个 config 文件的路径。
    作用: 解析 YAML → 过 validator 结构化校验 → 转成 dataclass 对象。
    输出: AppConfig；YAML 语法或结构不合法时抛 ConfigError。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse config {path}: {e}") from e

    # validator 保证键齐全、类型正确；这里只做 dict -> dataclass 的装配
    validated = validate_config_structure(raw)

    providers = [
        ProviderConfig(
            name=p["name"],
            protocol=p["protocol"],
            base_url=p["base_url"],
            model=p["model"],
            api_key=p["api_key"],
            thinking=p["thinking"],
            context_window=p["context_window"],
            max_output_tokens=p["max_output_tokens"],
        )
        for p in validated["providers"]
    ]

    mcp_servers = [
        MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s["args"],
            url=s["url"],
            headers=s["headers"],
            env=s["env"],
        )
        for s in validated["mcp_servers"]
    ]

    return AppConfig(
        providers=providers,
        permission_mode=validated["permission_mode"],
        mcp_servers=mcp_servers,
        raw_hooks=validated["hooks"],
        max_iterations=validated["max_iterations"],
    )


def _merge_config(base: AppConfig, override: AppConfig) -> AppConfig:
    """把 override 层合并进 base 层（分层配置：home < 项目 < 本地）。

    输入: base——低优先级层；override——高优先级层（同一份 AppConfig）。
    作用: providers/permission_mode/max_iterations 整体覆盖；mcp_servers 按 name
          合并或追加；hooks 列表拼接。
    输出: AppConfig——原地修改 base 并返回它。
    """
    if override.providers:
        base.providers = override.providers
    if override.permission_mode != "default":
        base.permission_mode = override.permission_mode

    if override.mcp_servers:
        # 同名 server 用高优先级层替换，新增的追加到末尾
        by_name = {s.name: i for i, s in enumerate(base.mcp_servers)}
        for s in override.mcp_servers:
            if s.name in by_name:
                base.mcp_servers[by_name[s.name]] = s
            else:
                base.mcp_servers.append(s)
                by_name[s.name] = len(base.mcp_servers) - 1

    base.raw_hooks.extend(override.raw_hooks)
    # 50 是 AppConfig 默认值：override 仍是默认说明没显式配，不覆盖
    if override.max_iterations != 50:
        base.max_iterations = override.max_iterations
    return base


def load_config(path: Path | None = None) -> AppConfig:
    """按分层查找并合并配置：显式单文件，或 home/项目/本地三层。

    输入: path——可选；给定时只加载该单文件。
    作用: 默认按 家目录 < 项目 .autocode/config.yaml < .autocode/config.local.yaml
          的优先级逐层加载与合并（本地层可覆盖 key）。
    输出: AppConfig；找不到任何配置文件时抛 ConfigError。
    """
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return _load_single_file(path)

    cwd = Path.cwd()
    home = Path.home()
    # 顺序即优先级：后出现的覆盖先出现的
    candidates = [
        home / ".autocode" / "config.yaml",
        cwd / ".autocode" / "config.yaml",
        cwd / ".autocode" / "config.local.yaml",
    ]

    merged: AppConfig | None = None
    for p in candidates:
        if not p.exists():
            continue
        layer = _load_single_file(p)
        if merged is None:
            merged = layer
        else:
            merged = _merge_config(merged, layer)

    if merged is None:
        raise ConfigError(
            "No config file found. Expected .autocode/config.yaml "
            "in project or ~/.autocode/config.yaml"
        )
    return merged
