# AutoCode MiniAgent 系统架构图

一张可交互的系统架构图：**12 个节点、13 条关系**，每个节点都挂着真实源码的文件与行号。

主链路是 `用户 → AutoCodeApp → Agent 主循环 → LLMClient → LLM API`；Agent 上方挂 `System Prompt 组装`、`双层压缩`，下方挂 `权限分层`、`ToolRegistry → MCP Servers`、`HookEngine`、`Session / Memory`。

---

## 怎么看

**已经在网上发布了**，直接用浏览器打开即可，无需任何本地操作：

**<https://xls0Jacker.github.io/MiniAgent/system-architecture/system-architecture.html>**

下面三种是**本地查看**的办法（离线、或想拿一份副本）。

`system-architecture.html` 是**自包含单文件**——HTML、CSS、JavaScript、SVG 全在里面，没有任何外部资源引用（已验证：0 条外部链接）。不需要装依赖、不需要构建，一个文件就是全部。用现代浏览器打开即可（Chrome / Edge / Firefox / Safari，需支持 `ResizeObserver` 与 `color-mix`）。

所以问题只剩一个：**怎么让浏览器拿到这个文件**。下面按情形选一条，命令都以 Linux 终端为准。

### A. 机器有图形界面

```bash
xdg-open system-architecture.html
```

### B. SSH 到远程服务器（无图形界面）

在远程起一个静态服务，把端口转发到本地，用**本地浏览器**看：

```bash
# 终端 1 —— 远程：在图纸目录起服务
cd /path/to/MiniAgent/docs/system-architecture
python3 -m http.server 8000 --bind 127.0.0.1

# 终端 2 —— 本地：把远程端口转发出来
ssh -N -L 8000:localhost:8000 <user>@<host>
```

然后本地浏览器打开 **<http://localhost:8000/system-architecture/system-architecture.html>**（路径全是 ASCII，可以直接敲）。

`--bind 127.0.0.1` 是刻意的：服务只监听回环地址、不对外网暴露，唯一的入口就是那条 SSH 转发通道。看完 Ctrl-C 停掉即可。

### C. 只想拿一份本地副本

```bash
scp <user>@<host>:/path/to/MiniAgent/docs/system-architecture/system-architecture.html .
```

因为是单文件，拷这一个就够——不需要连 `.json` 和证据文件一起拿。整个目录一起拷的话用 `scp -r`。

### 终端里能直接看图吗？

**不能。** 这是一张交互式 HTML/SVG 图，靠 JavaScript 渲染并响应点击、缩放、搜索；`w3m` / `lynx` 这类终端浏览器不支持 SVG 和脚本，打开只会是一片空白或乱码。

终端在这条链路上只负责**把文件送到浏览器**：要么起服务转发端口（B），要么拷到本地（C）。别指望在终端里把它渲染出来。

> macOS 用 `open system-architecture.html`，Windows 用 `start system-architecture.html`，其余同理。

### 图上有什么

| | 节点 |
|---|---|
| **主链路** | 用户 · AutoCodeApp · Agent 主循环 · LLMClient · LLM API |
| **上侧支** | System Prompt 组装 · 双层压缩 |
| **下侧支** | 权限分层 · ToolRegistry · MCP Servers · HookEngine · Session / Memory |

页面左下角有**图例**，按语义类型分类：前端 1 · 后端 5 · 数据库 1 · 安全 1 · 消息总线 1 · 外部系统 3。

顶部工具栏的 **4 个引导视图**把图切成四段讲解章节，直接对应可以分段讲的故事：

| 编号 | 章节 | 讲什么 |
|---|---|---|
| 01 | 主链路 | 用户输入进 TUI，Agent 每轮调 LLM、执行工具、回填结果 |
| 02 | 护栏与压缩 | 轮次上限、未知工具熔断、双层上下文压缩、权限分层 |
| 03 | 工具与 MCP | 工具调用先过权限闸，再进注册表；MCP 工具以同一接口接入 |
| 04 | 持久化与钩子 | 每轮落盘 JSONL，长程记忆召回；生命周期钩子可拦截工具 |

点 **播放故事** 会按章节自动走一遍，适合直接演示。

### 交互速查

**鼠标** — 点节点打开「语义护照」（节点元数据 + 已验证来源）；滚轮缩放，拖动平移。

**键盘**：

| 键 | 作用 |
|---|---|
| `/` | 查找节点 |
| `?` | 打开 / 关闭图表指南 |
| `+` 或 `=` | 放大 |
| `-` | 缩小 |
| `0` | 重置视图 |
| `T` | 切换深色 / 浅色主题 |
| `S` | 循环切换视觉风格 |
| `E` | 导出菜单 |
| `F` | 进入演示模式（`Escape` 退出） |
| `L` | 语义透镜（按类型统计与比较） |
| `M` | 语义雷达（带实时视口的全局地图） |
| `R` | 追踪有向路径 |
| `[` `]` | 上一个 / 下一个引导章节 |
| `Escape` | 关闭当前浮层 |

> 按 `?` 会打开应用内的图表指南，那里列出的快捷键始终以图自身为准。

### 导出

按 `E` 打开导出菜单，可以导出**可编辑矢量图（SVG）**、**位图 / 无损图像（PNG，或直接复制到剪贴板）**，以及**动效（WebM，需浏览器支持 `MediaRecorder`）**。也可以直接用浏览器的打印功能出 PDF。

---

## 源码定位怎么用

这是这张图的主要用途：**从图上任意节点跳到真实代码行**。

1. 点击图上的节点（例如 `Agent 主循环`）。
2. 右侧打开「**语义护照**」面板，底部是「**已验证的源代码证据**」。
3. 每条来源显示 `路径:行号` 和一个「**打开 ↗**」链接，点击跳到 GitHub 对应行。

例如节点 `Agent 主循环` 挂的两条来源是：

```
autocode/agent.py:302   class Agent
autocode/agent.py:416   run() 主循环
```

全图共 **21 条已验证来源引用**，覆盖 12 个节点中的 10 个（`用户` 和 `LLM API` 是外部系统，没有源码可指）。

面板上会标注一句话：*「已按固定修订版本验证本地 Git 证据，未检查远程访问权限。」* 意思是这些行号在生成时就对照过该 commit 的 Git 对象校验通过，但链接本身能不能打开取决于你对 GitHub 的访问——仓库是公开的，正常网络即可。

### 一个必须知道的细节：行号钉在 commit 上

图上的行号指向 commit **`d1d0718`**，**不是**你工作区的当前行号。

这是刻意的：行号锚在 commit 上就不会随工作区改动漂移，图不会因为代码改动而指向错误的位置。代价是——`autocode/agent.py` 和 `autocode/client.py` 在工作区里比该 commit 分别多 140 / 85 行，所以**图上的行号和你本地编辑器里看到的可能对不上**。要看真实代码，请走图上的链接，不要照抄行号去本地定位。

---

## 文件说明

| 文件 | 说明 | 进 git |
|---|---|---|
| `system-architecture.html` | 图本身，自包含单文件 | ✅ |
| `system-architecture.json` | 图的输入规格，改文案/加节点改这个 | ✅ |
| `preview.png` | 缩略预览图（2048×1320 浅色），供仓库根 README 内嵌 | ✅ |
| `.gitignore` | 屏蔽本目录下的浏览器证据文件 | ✅ |
| `system-architecture.visual-check.*.png` | 4 张浏览器截图（1440×900 与 2048×1320，各明暗两套） | ❌ 本地 |
| `system-architecture.visual-check.html` | 截图的 contact sheet | ❌ 本地 |
| `system-architecture.visual-check.json` | `visual-check` 的校验回执 | ❌ 本地 |

带 ❌ 的是**校验产物**而不是项目内容，已在 `.gitignore` 里排除，只留在本地。需要时重跑 `visual-check` 就能重建。

---

## 重新生成与校验

图由 [archify](https://github.com/tt-a1i/archify) 从 `system-architecture.json` 生成。**所有命令都要在仓库根目录执行**，`--repo-root .` 会让校验器按 commit 去读 Git 对象、逐条验证源码引用是否真实存在。

```bash
ARCHIFY=<archify 所在目录>

# 1. 校验：9 项 artifact 检查 + 每条源码引用
node $ARCHIFY/bin/archify.mjs validate architecture \
  docs/system-architecture/system-architecture.json \
  --quality showcase --repo-root . --json

# 2. 交付：校验全过后才写 HTML，回执含 SHA-256 与字节数
node $ARCHIFY/bin/archify.mjs deliver architecture \
  docs/system-architecture/system-architecture.json \
  docs/system-architecture/system-architecture.html \
  --quality showcase --repo-root . --json

# 3. 浏览器证据：在 4 档视口测量，截图写入本目录
node $ARCHIFY/bin/archify.mjs visual-check \
  docs/system-architecture/system-architecture.html --json
```

第 2 步失败时**不会**覆盖已有的 HTML，旧产物会保留——这时候不要接着跑第 3 步，否则量到的是上一版产物。第 3 步需要本机有 Chrome / Chromium；找不到时会返回 `skipped` 而不是失败。

当前状态（`--quality showcase`）：

```
validation : 9/9 checks, 0 errors, 0 warnings
contention : 1440×900 · 1600×1000 · 1920×1080 · 2048×1320 四档均无溢出
readability: 最小投影字号 6.64px ≥ 阈值 6px
```

---

## 不在图里的东西

- **没有时序维度**：这是一张静态的系统架构图，不是时序图。一轮对话的先后顺序在卡片文案里，不在拓扑上。
- **不是部署图**：没有进程、主机、容器边界。全部节点都在同一台机器上的同一个进程里（MCP Servers 除外，它是 stdio 子进程）。
- **不是完整模块清单**：仓库有约 40 个模块，图上只画了主链路经过的 12 个。细粒度模块说明见 `docs/01-09` 那九篇模块解读。
