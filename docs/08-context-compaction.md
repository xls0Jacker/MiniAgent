# 模块解读 08：上下文压缩与 Token 管理

> 本文拆解 `autocode/context/manager.py`
> 与 `conversation.py` 的 token 估算，讲清「上下文太长时系统怎么自救」：
> 双 Layer 压缩（Layer 1 tool result 预算、Layer 2 auto-compact 摘要）、预算阈值、
> 熔断器等。
> 全文基于本项目 `autocode/` 包（模块解读）。

## 1. 模块职责

LLM 上下文窗口有限，而 Agent 每轮对话（尤其是调了很多工具）会快速增长。上下文管理
的目标：**在接近窗口上限前，用可接受的信息损失换回可用空间**，且过程对用户透明
（TUI 收到 `CompactNotification` 告知「上下文已压缩」）。

本项目的压缩是**两层**的：

| 层 | 触发时机 | 手段 |
|----|---------|------|
| Layer 1（`apply_tool_result_budget`） | 每次给 LLM 发请求前 | 把过长的 tool result 截断 / 持久化 / 裁剪陈旧结果 |
| Layer 2（`auto_compact`） | 每轮迭代开头 | 用 LLM 把旧前缀摘要成一段，尾部原文保留 |

Layer 1 频繁、便宜、无损可控；Layer 2 重、有损但能大幅回退。两者配合让长对话「无限
继续」（system prompt 里也写了 "unlimited context through automatic summarization"）。

## 2. 核心数据结构

### 内容替换状态（`manager.py`）

```python
class ContentReplacementState:
    replacements: dict[str, str]    # tool_use_id → 替换后的内容
    seen_ids: set[str]              # 已见过的结果 id

class ContentReplacementRecord:
    tool_use_id: str
    replacement: str
```

Layer 1 是**增量、可恢复**的：每个 tool result 被替换成预览/截断后，记录写进 session 目录
（`append_replacement_records`），重开会话能通过 `load_replacement_records` 重建状态——
保证「压缩决定」跨会话一致。

### 熔断器（`CompactCircuitBreaker`）

```python
class CompactCircuitBreaker:
    def record_failure / record_success / is_open
```

连续失败 3 次摘要生成 → `is_open()` 为真 → auto-compact 放弃并提示手动处理，防止
压缩本身反复烧 token 却救不回来。

## 3. 关键流程

### token 估算（`conversation.py`）

```python
def _message_chars(m) -> int
def estimate_tokens(messages) -> int   # 基于字符的粗估
```

`current_tokens()` 以「上次真实 API 用量（计费锚点）+ 锚点后新增消息的字符估算」为当前
体量——用真实计费做锚，冷启动退化为全量字符估算。

### Layer 1：tool result 预算（`apply_tool_result_budget`）

纯函数式的「Design B」：**不改原 conversation，返回新的**。三 Pass 逐条决策：

```
对每个带 tool_results 的消息：
  已决策的（replacements / seen_ids / PERSISTED_TAG）→ 沿用旧决策
  fresh 的结果走：
    Pass 1 单条超限 → persist_tool_result() 落盘，
                      内容替换成 "已持久化到 <file>，内容过长" 的预览
    Pass 2 聚合超限 → 累计超预算的最旧结果同样降级
    Pass 3 陈旧裁剪 → 太老的结果裁剪
  新决策写入 state（replacements/seen_ids）+ 返回 ContentReplacementRecord
```

这样**每轮发给 LLM 的消息体稳定**，不会被一两个 10 万字符的 tool result 撑爆。

### Layer 2：auto-compact 摘要（`auto_compact`）

```
1. 阈值 = compute_compact_threshold(context_window)
   （窗口的某百分比；到点才触发）
2. current = conversation.current_tokens()
   if current < threshold and not manual: return None     # 还没到点
3. if breaker.is_open(): return "压缩已熔断…"
4. keep_start = _compute_keep_start_index(history)  # 决定保留多少尾部原文
   to_summarize = history[:keep_start]    # 只摘要这部分
   keep_tail    = history[keep_start:]    # 尾部原文保留，模型看到近期真实对话
5. if keep_start <= 0 or 前缀小到不值得: return None    # 退化为不压缩
6. 把 to_summarize 喂给 LLM 生成结构化摘要
   （重试 3 次；prompt 太长错误 → 按轮次分组丢掉最早 1/5）
7. 重建：摘要 + 尾部原文 + recovery 附件
   → CompactEvent(before_tokens, boundary) 让上层持久化 boundary 记录
```

**核心思想：摘要前缀，保尾原文**。摘要是有损的，所以把越近的内容越少压缩——模型对
「最近发生了什么」需要原文，而对「开头聊过什么」只需一份梗概。

### compact boundary 跨会话恢复（`make_compact_boundary`）

压缩完成后，把「摘要 + 原样保留的尾部」内联成一条 `SessionRecord` 写进 session JSONL。
重开会话时只需重建这一条，就能回到压缩后的状态，而**不重放**那些已被摘要的原始前缀。

## 4. 要点小结

1. **为什么要「摘要前缀、保留尾部原文」而不是整体摘要？**
   摘要是有损的。近期对话决定模型下一步动作，必须给原文；开场几轮对话离当前目标远，
   一份高质量梗概足够。`_compute_keep_start_index` 找的就是这个「新旧分界」。

2. **Layer 1 和 Layer 2 为什么一个便宜一个贵？**
   Layer 1 不调 LLM：截断/落盘/裁剪 tool result 是纯字符串操作，每轮都跑得起。
   Layer 2 要额外花一次 LLM 调用做摘要，不能每轮做，只能到阈值才触发。先便宜后贵，
   把贵操作推迟到真正需要时。

3. **tool result 被替换成「已持久化」预览，模型会困惑吗？**
   预览文案写明「完整内容在文件 <path>，如需可用 ReadFile 读取」。模型真需要细节时
   自己会去读文件——把「存细节」与「喂窗口」分离，正是这个设计的巧妙处。

4. **token 估算不精确，怎么保证不超窗口？**
   用真实 API 计费做锚点（`current_tokens`），估算只作用于锚点后的新增部分；且压缩
   阈值留了安全余量（不是 100% 窗口才触发）。真超出时 model 报错，摘要生成侧也有
   重试丢轮次的降级。多层兜底而非赌估算精确。

5. **熔断器解决什么问题？**
   如果摘要本身反复失败（比如 prompt 太长），自动压缩会反复烧 token 而毫无产出。
   连续 3 次失败即打开熔断，让 auto-compact 停止尝试、提示用户手动 /compact——
   把「自动修复」升级为「人工介入」，避免死循环成本。

6. **压缩与 session 持久化怎么衔接？**
   `CompactNotification.boundary`（摘要+尾部）作为一条 SessionRecord 持久化；重启后
   `parse_compact_boundary` 还原。这保证压缩不是一次性内存操作，而是对话历史可被
   断点续聊的一部分（配合 09 章 session）。

## 5. 测试绑定

**对应的测试文件：**
- `tests/test_context.py` —— Layer 1 工具结果预算（`TestApplyToolResultBudget`：
  单条/聚合超限落盘、未超不动）、auto-compact 决策与阈值（`TestShouldAutoCompact`、
  `TestComputeCompactThreshold`）、摘要提取、熔断器（`TestCompactCircuitBreaker`）、
  Layer 2 保尾压缩端到端（`TestAutoCompactKeepRecent`）、token 估算与计费锚点
  （`TestEstimateTokens` / `TestUsageAnchor`）
- `tests/test_context_window.py` —— 窗口解析/映射（决定压缩阈值）
- `tests/test_recovery.py` —— recovery 附件（压缩后重建上下文的来源）
- `tests/test_replacement_state.py` —— 内容替换状态（Layer 1 的可恢复状态机）
- `tests/test_memory.py` —— compact boundary 跨会话恢复（`TestCompactBoundaryRoundTrip`）、
  session JSONL 持久化 / 恢复（`TestSession` / `TestSessionResume`）
- `tests/test_max_iterations.py` —— 压缩/护栏与主循环的集成

**怎么验证本模块：**
- 整文件（全绿）：`uv run python -m pytest tests/test_recovery.py tests/test_replacement_state.py -q`
- 单条用例：`uv run python -m pytest tests/test_context.py::TestCompactCircuitBreaker -q`
- 压缩核心用例：`uv run python -m pytest "tests/test_context.py::TestApplyToolResultBudget" "tests/test_context.py::TestAutoCompactKeepRecent" "tests/test_context.py::TestUsageAnchor" -q`

**需要知道：**
- `docs/testing.md` 是全部测试的入口与总表，可反查任意模块。
- `test_context.py` 中 `TestBuildCompactMessages::test_basic_structure` 与 `test_memory.py`
  收集级旧失败为**存量差异，不修**，已在测试说明标注；本模块绑定只看上述绿色用例。
- `auto_compact` 的「真实触发时机」依赖 token 估算，mock 只验证到阈值/结构层，真实长
  对话的压缩边界需在 TUI 手动观察。
