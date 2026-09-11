# 模块机制图

一组**下钻图**：现有 [`docs/runtime-architecture/runtime-architecture.html`](../runtime-architecture/runtime-architecture.html) 是总览图（一轮对话经过哪些模块），这里每个模块各一张，回答**这个模块自己怎么工作**。

每张图都带**真实源码行号**——点任意节点打开面板，能看到 `文件:行号` 并可跳到 GitHub 对应行。

---

## 怎么读

打开方式与总览图完全相同（自包含单文件，见[总览图 README](../runtime-architecture/README.md#怎么看)）。SSH 场景下最省事的一条：

```bash
# 终端 1（远程）
cd /path/to/MiniAgent/docs/module-architecture
python3 -m http.server 8000 --bind 127.0.0.1

# 终端 2（本地）
ssh -N -L 8000:localhost:8000 <user>@<host>
```

本地浏览器打开 <http://localhost:8000/>，根目录列出十个文件夹，点进任意一个再点同名 `.html` 即可：

```
http://localhost:8000/agent-loop/agent-loop.html
http://localhost:8000/prompt-assembly/prompt-assembly.html
http://localhost:8000/tool-execution/tool-execution.html
…
```

（同一文件夹里还有 `.json` 规格与 `*.visual-check.*` 证据边车，后者已 gitignore、只留在本地。）

---

## 图列表（10 张，已全部交付）

| # | 图 | 模块 | 核心机制 | 主要源码 | 规模 |
|---|----|------|---------|---------|------|
| 1 | [agent-loop](agent-loop/agent-loop.html) | Agent 主循环 | 每轮 8 阶段 + 3 道护栏 + 工具双路执行 | `agent.py` `run()` | 15 节点 · 16 边 · 28 引用 |
| 2 | [prompt-assembly](prompt-assembly/prompt-assembly.html) | System Prompt 组装 | 双路径合成：system 参数 vs 历史消息 | `prompts.py` | 12 节点 · 11 边 · 20 引用 |
| 3 | [llm-client](llm-client/llm-client.html) | LLM 客户端 | 三协议分发 + 事件归一 + 缓存断点 | `client.py` | 12 节点 · 11 边 · 28 引用 |
| 4 | [tool-execution](tool-execution/tool-execution.html) | 工具注册与执行 | 声明式元数据 + 并发/串行双路 + 输出闸 | `tools/`, `agent.py` | 13 节点 · 13 边 · 32 引用 |
| 5 | [permissions](permissions/permissions.html) | 权限分层 | Layer 0–5 逐层下落，命中即返回 | `permissions/` | 12 节点 · 11 边 · 30 引用 |
| 6 | [context-compaction](context-compaction/context-compaction.html) | 双层压缩 | Layer 1 结果预算 + Layer 2 摘要 + 熔断 | `context/manager.py` | 15 节点 · 15 边 · 44 引用 |
| 7 | [hooks](hooks/hooks.html) | Hook 引擎 | 通知型 vs 拦截型 + 条件表达式 | `hooks/` | 14 节点 · 13 边 · 38 引用 |
| 8 | [memory-session](memory-session/memory-session.html) | Session 与记忆 | JSONL 落盘/恢复 + 记忆抽取与召回 | `memory/` | 18 节点 · 17 边 · 54 引用 |
| 9 | [mcp](mcp/mcp.html) | MCP 接入 | 两条传输 + 工具包装注册 + 懒重连 | `mcp/` | 14 节点 · 12 边 · 39 引用 |
| 10 | [tui](tui/tui.html) | TUI 交互 | 12 种 AgentEvent 的 elif 梯子 + 三处挂起 | `app.py` | 14 节点 · 13 边 · 40 引用 |

---

## 目录结构

每张图**各占一个文件夹**，图与它的输入规格同放一处：

```
docs/module-architecture/
├── README.md                      # 本文件
├── .gitignore                     # 屏蔽 *.visual-check.* 证据边车
├── label_collisions.py            # 自建碰撞检测器（见下文）
├── agent-loop/
│   ├── agent-loop.html            # 图本身，自包含单文件
│   ├── agent-loop.json            # 图的输入规格，改文案/加节点改这个
│   └── agent-loop.visual-check.*  # 浏览器证据（已 gitignore，只留本地）
├── prompt-assembly/
├── llm-client/
├── tool-execution/
├── permissions/
├── context-compaction/
├── hooks/
├── memory-session/
├── mcp/
└── tui/
```

---

## 十张图各自最值得讲的一点

按面试叙述顺序排，每条都是读源码核实过的机制，不是推测。

| 图 | 一句话看点 |
|----|-----------|
| agent-loop | 工具分两路的依据是**工具自己声明的 `is_concurrency_safe`**，不按读 / 写类别推断；并发批走直通路径、完全不经权限检查器 |
| prompt-assembly | 发给模型的提示词**不在同一个地方**：system 参数只装固定 8 段，环境 / 记忆 / 提醒全部注入在历史消息里 |
| llm-client | 三家协议 API 形态完全不同，但都被翻译成同一组 7 种 `StreamEvent`——差异全部收敛在 `client.py` 内 |
| tool-execution | 输出闸是**入口式一次性定型**：结果写回历史前就决定落盘还是截断，之后不再重算 |
| permissions | 六层是**顺序下落**，任何一层表态就 `return`；层序本身就是优先级 |
| context-compaction | 两条压缩路径共用同一个落盘函数与同一个 session 目录——一套机制的两个入口 |
| hooks | 15 个事件里**实际只发射 10 个**；`reject` 与 `async` 在配置期就限定只能配 `pre_tool_use` |
| memory-session | 落盘是「一条消息拆成多条记录」，恢复是反过来拼；压缩边界内联摘要，所以压缩前的原始前缀不必回放 |
| mcp | 包装类把远端工具伪装成本地工具：写死 `command` / 不可并发 / 延迟加载，上层完全无感 |
| tui | 12 种事件全靠一串 `isinstance`，**没有 default 兜底**——新增事件会立刻暴露成未处理 |

---

## 重新生成与校验

图由 [archify](https://github.com/tt-a1i/archify) 从 `.json` 生成。**命令要在仓库根目录执行**，`--repo-root .` 让校验器按 commit 读取 Git 对象、逐条验证源码引用真实存在。

```bash
cd /path/to/MiniAgent
ARCHIFY=<archify 所在目录>
NAME=agent-loop          # 换成任意一张图的名字

ARCHIFY_CHROME=<chromium 可执行文件> \
ARCHIFY_CHROME_NO_SANDBOX=1 \
  node $ARCHIFY/bin/archify.mjs validate architecture \
  docs/module-architecture/$NAME/$NAME.json \
  --quality showcase --repo-root . --json

# 校验全过后才写 HTML
node $ARCHIFY/bin/archify.mjs deliver architecture \
  docs/module-architecture/$NAME/$NAME.json \
  docs/module-architecture/$NAME/$NAME.html \
  --quality showcase --repo-root . --json

# 浏览器证据（四档视口 × 明暗两主题）
ARCHIFY_CHROME=<chromium 可执行文件> ARCHIFY_CHROME_NO_SANDBOX=1 \
  node $ARCHIFY/bin/archify.mjs visual-check \
  docs/module-architecture/$NAME/$NAME.html --json
```

### 一个 archify 查不出来的问题，用 `label_collisions.py` 补

archify 的 `label-route-clearance` **不检测「标签压标签」**。这个脚本直接从交付后的 HTML 里读渲染期坐标（不是从 JSON 模型推算），所以检出的重叠就是浏览器里真实存在的重叠：

```bash
python3 docs/module-architecture/label_collisions.py docs/module-architecture/*/[a-z]*.html
```

它查三类碰撞：**标签 × 标签**、**标签 × 节点**、**标签 × 别的边**。退出码 0 表示干净。当前十张图全部为 0。

---

## 一个必须知道的细节：行号钉在 commit 上

所有行号指向 commit **`d1d07182cda2695d8bc6b98fd01249a1b93a5911`**，**不是**工作区的当前行号。

这是刻意的：锚在 commit 上行号不会随代码改动漂移。代价是部分文件在工作区已经比该 commit 长，**图里的行号和你本地编辑器看到的不一致**：

| 文件 | commit 行数 | 工作区行数 | 差 |
|------|------------|-----------|-----|
| `autocode/agent.py` | 1206 | 1346 | +140 |
| `autocode/client.py` | 601 | 686 | +85 |
| `autocode/config.py` | 221 | 277 | +56 |

**工作区与 commit 行数一致、可以直接对照行号的文件**：`prompts.py`、`conversation.py`、`context/manager.py`、`permissions/*.py`、`hooks/*.py`、`mcp/*.py`、`memory/*.py`、`tools/*.py`、`app.py`。

要看真实代码请走图上的链接，不要照抄行号去本地定位。

---

## 与总览图的关系

```
runtime-architecture.html        ← 总览：12 节点，一轮对话经过哪些模块
        │
        ├── agent-loop.html          ← 下钻：Agent 主循环节点内部怎么跑
        ├── prompt-assembly.html     ← 下钻：System Prompt 组装节点内部怎么拼
        ├── llm-client.html          ← 下钻：LLM 客户端怎么归一三种协议
        ├── tool-execution.html      ← 下钻：工具怎么注册、分批、执行、回填
        ├── permissions.html         ← 下钻：一次权限判定怎么逐层下落
        ├── context-compaction.html  ← 下钻：两层压缩各自做什么
        ├── hooks.html               ← 下钻：钩子怎么匹配、怎么拦截
        ├── memory-session.html      ← 下钻：会话怎么落盘、记忆怎么抽与召
        ├── mcp.html                 ← 下钻：外部工具怎么接进来
        └── tui.html                 ← 下钻：事件怎么变成屏幕上的东西
```

总览图回答「有哪些模块」，本目录回答「每个模块怎么工作」。面试时可先讲总览图建立全局，再按需下钻。
