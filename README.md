**English** | [简体中文](README.zh-CN.md)

# AutoCode MiniAgent — A Terminal Coding Agent Harness

A coding agent that lives in your terminal. You type a sentence; it decides — through a ReAct
loop — whether to answer directly or call a tool, then goes off to read files, edit code, run
commands, and do arithmetic until it has an answer.

The harness drives real LLM APIs directly. `autocode/` is roughly **12.5k lines**, backed by
**6.1k lines** of tests.

What's inside:

- **An agent loop** — ReAct alternation: think → (optionally) call a tool → read the
  result → think again, until the model answers without reaching for another tool
- **Three-protocol LLM client** — anthropic / openai / openai-compat behind one unified
  streaming event interface
- **Tool registry** — name + description + pydantic parameter schema; the model decides what to call
- **Layered permissions (Layer 0–5)** — sequential fall-through, first verdict wins: plan-mode
  exceptions · read-only command allowlist · dangerous command denylist · path sandbox · rule
  engine · mode matrix · human-in-the-loop confirmation
- **Two-layer context compaction** — a tool-result budget plus LLM summarization, so long
  conversations stay viable
- **Session management** — independent windows, JSONL persistence, resume where you left off
- **Long-term memory** — automatic extraction plus recall keyed to the current question,
  injected as a system-reminder
- **Lifecycle hooks** — 15 event points, configurable as notification or interception hooks
- **MCP integration** — tools from external servers bridge in as local tools, transparently to
  every layer above
- **Terminal UI** (Textual) — streaming typewriter output, tool cards, permission dialogs,
  session switching

---

## Architecture Diagrams

The repository ships **11 interactive architecture diagrams**: one system overview plus 10
module detail diagrams. **Browse them online → <https://xls0Jacker.github.io/MiniAgent/>**

Every diagram is a **self-contained single HTML file** — HTML, CSS, JS, and SVG all inline, zero
external dependencies. Click any node to open its "semantic passport" panel — it lists what that
node does and where it lives in the source. Across all diagrams there are **353 source
references**, each one pointing at a real file and line: click it and GitHub opens at that exact
line.

![AutoCode MiniAgent system architecture overview](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/system-architecture/preview.png)

**The overview diagram** — 12 nodes, 13 relationships. The spine runs
`User → AutoCodeApp → Agent loop → LLMClient → LLM API`. `System Prompt assembly` and
`Two-layer compaction` hang above it; `Layered permissions`, `ToolRegistry → MCP Servers`,
`HookEngine`, and `Session / Memory` hang below.

> The image above is a **downscaled preview**. README body width is around 880px, so a 2048px
> source gets scaled down and the node labels stop being readable. Open the live version to pan,
> zoom, and click through the nodes →
> **[system-architecture.html](https://xls0Jacker.github.io/MiniAgent/system-architecture/system-architecture.html)**
> (to run one locally instead, see [how to open it](docs/system-architecture/README.md#怎么看)).
>
> **For English readers:** this is a Chinese-language project. The diagrams, the diagram READMEs
> they link to, and most of the in-code comments and docstrings are written in Chinese
> (`zh-CN`). Code identifiers and this README are in English; [`README.zh-CN.md`](README.zh-CN.md)
> is the Chinese version of this same document.

### The 10 module diagrams

The overview answers "which modules does a single turn pass through". These answer "**how does
this module work on its own**":

| # | Diagram | Module | Core mechanism | Size |
|---|---------|--------|----------------|------|
| 1 | [agent-loop](https://xls0Jacker.github.io/MiniAgent/module-architecture/agent-loop/agent-loop.html) | Agent loop | 8 stages per turn + 3 guardrails + dual-path tool execution | 15 nodes · 16 edges · 28 refs |
| 2 | [prompt-assembly](https://xls0Jacker.github.io/MiniAgent/module-architecture/prompt-assembly/prompt-assembly.html) | System prompt assembly | Two paths: `system` param vs. history messages | 12 nodes · 11 edges · 20 refs |
| 3 | [llm-client](https://xls0Jacker.github.io/MiniAgent/module-architecture/llm-client/llm-client.html) | LLM client | Three-protocol dispatch + event normalization + cache breakpoints | 12 nodes · 11 edges · 28 refs |
| 4 | [tool-execution](https://xls0Jacker.github.io/MiniAgent/module-architecture/tool-execution/tool-execution.html) | Tool registry & execution | Declarative metadata + concurrent/serial split + output gate | 13 nodes · 13 edges · 32 refs |
| 5 | [permissions](https://xls0Jacker.github.io/MiniAgent/module-architecture/permissions/permissions.html) | Layered permissions | Layer 0–5 fall-through, first verdict returns | 12 nodes · 11 edges · 30 refs |
| 6 | [context-compaction](https://xls0Jacker.github.io/MiniAgent/module-architecture/context-compaction/context-compaction.html) | Two-layer compaction | Layer 1 result budget + Layer 2 summary + circuit breaker | 15 nodes · 15 edges · 44 refs |
| 7 | [hooks](https://xls0Jacker.github.io/MiniAgent/module-architecture/hooks/hooks.html) | Hook engine | Notification vs. interception hooks + condition expressions | 14 nodes · 13 edges · 38 refs |
| 8 | [memory-session](https://xls0Jacker.github.io/MiniAgent/module-architecture/memory-session/memory-session.html) | Session & memory | JSONL persist/resume + memory extraction and recall | 18 nodes · 17 edges · 54 refs |
| 9 | [mcp](https://xls0Jacker.github.io/MiniAgent/module-architecture/mcp/mcp.html) | MCP integration | Two transports + tool-wrapper registration + lazy reconnect | 14 nodes · 12 edges · 39 refs |
| 10 | [tui](https://xls0Jacker.github.io/MiniAgent/module-architecture/tui/tui.html) | TUI interaction | A dispatch ladder over 12 AgentEvent types + three suspension points | 14 nodes · 13 edges · 40 refs |

Three representative module diagrams (click through to the interactive version):

| [![Agent loop](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/module-architecture/agent-loop/preview.png)](https://xls0Jacker.github.io/MiniAgent/module-architecture/agent-loop/agent-loop.html) | [![Layered permissions](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/module-architecture/permissions/preview.png)](https://xls0Jacker.github.io/MiniAgent/module-architecture/permissions/permissions.html) | [![Two-layer compaction](https://raw.githubusercontent.com/xls0Jacker/MiniAgent/main/docs/module-architecture/context-compaction/preview.png)](https://xls0Jacker.github.io/MiniAgent/module-architecture/context-compaction/context-compaction.html) |
|---|----|----|
| **Agent loop** — 8 stages per turn | **Permissions** — Layer 0–5 fall-through | **Compaction** — budget + summary |

The diagrams are generated by [archify](https://github.com/tt-a1i/archify) from `.json` specs —
edit the spec to change copy or add nodes. Regeneration and validation commands live in the
[module diagrams README](docs/module-architecture/README.md) (Chinese).

---

## Quick Start

Requirements: Python ≥ 3.11 (tested on 3.13), with uv or pip.

```bash
# 1. Install dependencies
uv sync                 # or: pip install -e .

# 2. Configure the LLM API
#    Copy the example config and fill in your api_key / base_url / model
cp config.example.yaml .autocode/config.local.yaml
#    Edit .autocode/config.local.yaml (kept out of the repository — your key stays local)

# 3. Launch the TUI
uv run autocode

# 4. Or send a single non-interactive prompt
uv run autocode -p "What is 3.5*128+2?"
```

### Configuration example (`.autocode/config.local.yaml`)

Pick one protocol — `protocol` decides which API shape is used:

```yaml
permission_mode: default     # default | acceptEdits | plan | bypassPermissions | dontAsk
max_iterations: 50           # agent loop iteration ceiling (guardrail)

providers:
  - name: anthropic            # Option 1: Anthropic official
    protocol: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-5
    api_key: ${YOUR_ANTHROPIC_KEY}  # ← your key (this file is never uploaded)

  # - name: my_openai_compat   # Option 2: any OpenAI-compatible endpoint (deepseek / local, etc.)
  #   protocol: openai-compat
  #   base_url: https://api.deepseek.com/v1
  #   model: deepseek-chat
  #   api_key: sk-...
```

> Multiple providers all show up in the startup menu — pick one in the TUI. The `--mode` flag
> overrides the permission mode from the command line.

---

## TUI Cheat Sheet

| Action | What it does |
|--------|--------------|
| Type and press Enter | Send a message to the agent |
| `/` | Slash-command menu (`/session`, `/compact`, `/mode`, …) |
| `@path/to/file` | Reference file contents in your input |
| Tab | Completion |
| ↑/↓ | Input history |
| Click a tool card | Collapse / expand tool-call details |
| Ctrl+O | Collapse / expand all tool cards |
| Tab / Shift+Tab | Cycle permission modes |
| Esc / Ctrl+C | Cancel the current streamed reply / quit |

Sessions are independent (each persisted as JSONL). `/session` lists, switches, and restores
past sessions.

---

## System Design

Four layers: **TUI** (the Textual app) → **Agent** (loop + event stream) → **capabilities**
(LLM client, tool registry, permissions, compaction, memory, hooks) → **protocols** (three LLM
APIs plus MCP). Below, each module links to its own diagram.

### The agent loop ([diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/agent-loop/agent-loop.html))

One input drives the loop to a final answer: **take input → decide to answer or call tools →
(if tools) → write results back → continue or return**. The termination condition is the model
producing a final answer with **no further tool calls**; two guardrails back that up —
`max_iterations` (default 50) and "3 consecutive unknown tools" — so a non-converging model
can't quietly burn your budget.

The stage order within a turn is fixed: iteration guardrail → `turn_start` hook → Layer 2
compaction → `pre_send` hook → assemble the system prompt → inject reminders → Layer 1 budget →
stream the request. Tool execution splits two ways: concurrency-safe tools batch into a
**parallel** run; everything else goes through a **serial** path (which includes hook
interception and the permission check).

### Tool system ([diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/tool-execution/tool-execution.html))

Each tool is `name` + `description` + a pydantic `Params` model. That one pydantic model does
triple duty: `get_schema()` feeds the model's decision, `execute()` receives strongly-typed
arguments, and invalid arguments fail automatically.

Concurrency is decided by the tool's own declared `is_concurrency_safe` flag — **not** inferred
from a read/write category. The concurrent batch takes a direct path; only the serial batch
passes through hooks and the permission gate. Oversized results get truncated or persisted by
the context layer.

### System prompt assembly ([diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/prompt-assembly/prompt-assembly.html))

The prompt the model receives **does not come from one place**: the `system` parameter carries
only 8 fixed sections (identity, conduct, tool usage, tone, environment…), while everything that
**changes turn to turn** — environment snapshot, long-term memory, plan-mode reminders — is
injected into the **history messages**. Compaction wipes those injections, which is why the code
re-injects them after every compaction.

### Permissions ([diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/permissions/permissions.html))

The single gate every tool call passes before the loop executes it: each `ToolCall` goes through
`check()` and comes back allow / deny / ask. The decision is a **Layer 0–5 fall-through** — any
layer that commits a verdict returns immediately, so **the layer order *is* the priority order**:

| Layer | Verdict |
|-------|---------|
| 0 | Plan-mode exception (4 read-only tools, plus the plan file itself) |
| 1 | Safe read-only commands auto-allowed (prefix allowlist, no pipes or redirects) |
| 1b | Dangerous command denylist (8 regexes; a hit means deny) |
| 2 | Path sandbox (file tools reaching outside the root → deny) |
| 3 | Rule engine (user → project → local tiers; within a tier, the later rule wins) |
| 4 | Permission-mode matrix fallback (6 modes × 3 tool categories) |
| 5 | Nothing has committed yet → ask; suspend and hand it to the human (HITL) |

The rule engine sits *before* the mode matrix: a hit in `permissions.yaml` returns before the
fallback matrix is ever consulted.

### Context compaction, two layers ([diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/context-compaction/context-compaction.html))

- **Layer 1 (every turn, no LLM call)**: before each request, oversized tool results are
  truncated, persisted, or snipped — three passes: a single result over the threshold gets
  persisted, a total over the threshold persists largest-first, and results older than 10 turns
  get snipped down.
- **Layer 2 (threshold-triggered, calls the LLM)**: as the window fills, the **old prefix is
  summarized into one block while the tail stays verbatim**. The result is persisted as a
  compact-boundary, so a restart can resume. A circuit breaker stops retrying after repeated
  failures.

Both layers share one persistence function and one session directory — a single mechanism with
two entry points.

### Memory and sessions ([diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/memory-session/memory-session.html))

Two channels, one immediate and one resident:

1. **Per-turn recall (keyed to the user's current question)**
   - *When*: on each user message, a background side-query fires in parallel (its own LLM client
     and mini-conversation, 8s timeout, failures silently skipped — it never blocks the main flow).
   - *What*: `find_relevant_memories(query, …)` scores the user-level and project-level memory
     files and picks the most relevant entries.
   - *Where*: `render_reminder(hits)` assembles a single `<system-reminder>`, injected
     **right after the user message that just arrived and before the AI reply**, so it rides
     along with that turn's request.

2. **Startup resident injection (project instructions + long-term memory)**
   - *When*: once, at session start (after the Agent is constructed).
   - *Where*: `inject_long_term_memory(instructions, memories)` bundles the AUTOCODE.md
     instructions and resident memories into one system-reminder and inserts it at the
     **very front of the history (index 0/1)**; an `ltm_injected` flag guarantees it happens once.

In one line: **resident memory sets the floor (front of history), immediate recall sticks to the
current question (after this turn's message), and both enter the context as system-reminders
rather than being scattered through the prose.**

Persistence works the other way around from reading: one message is **split into several
records** on the way out, and reassembled on resume. Because compaction boundaries inline their
summaries, the original pre-compaction prefix never has to be replayed.

### LLM client ([diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/llm-client/llm-client.html))

The three protocols have completely different API shapes (Messages / Responses / Chat
Completions), but all of them are translated into the **same 7 `StreamEvent` types** — the
differences are entirely contained inside `client.py`, and everything above only knows that one
event vocabulary. Cache breakpoints are placed in three spots: the system prompt, the tail of
the tool list, and the last user message.

### Hooks and MCP ([hooks diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/hooks/hooks.html) · [MCP diagram](https://xls0Jacker.github.io/MiniAgent/module-architecture/mcp/mcp.html))

**Hooks** come in two kinds: notification hooks (they run and don't interfere with the main
flow) and interception hooks (only `pre_tool_use`, which can reject a tool call). Conditions are
matched with expressions (`==` `!=` `=~` `~=`; `&&` and `||` cannot be mixed).

**MCP** wraps tools exposed by a remote server into local tools — the wrapper class hardcodes the
category and concurrency flag, loads the parameter schema lazily, and registers into the same
`ToolRegistry`, so the agent loop, the permission system, and tool search **all need zero changes**.

---

## Demo Tools & Example Prompts

Just ask the agent in the TUI — it picks the tool itself:

| Tool | Example prompt | Notes |
|------|----------------|-------|
| Calculator | "What is 3.5×128+2?" | AST-allowlist evaluation, so no `eval` injection |
| Search | "What is an agent loop? Look it up" | Local knowledge-base mock; deterministic, testable offline |
| Weather | "What's the weather in Beijing?" | Deterministic pseudo-data from a hash of the city name |
| Todo | "Note a todo: write the README" / "List my todos" | Stored per session (window) |

Beyond these there is a set of **coding tools** (`ReadFile` / `WriteFile` / `EditFile` / `Bash` /
`Glob` / `Grep`), registered by `create_default_registry`.

> Weather goes through a built-in tool (a local mock) rather than MCP — the point being
> demonstrated is the "tool registration → autonomous model invocation" mechanism itself, without
> dragging in a network or service dependency. MCP support is kept in the codebase
> (`autocode/mcp/`) as an open-ended extension channel.

---

## Tests

```bash
uv run python -m pytest tests/ -q
```

Every test is offline, deterministic, and needs no real LLM. Highlights:

- `tests/test_demo_tools.py` — behavior of the 4 demo tools, schema completeness, Todo session isolation
- `tests/test_max_iterations.py` — the agent loop's iteration ceiling
- `tests/test_permissions.py` — permission layers, path sandbox, dangerous commands, rule engine, end-to-end verdicts
- `tests/test_serialization.py` — normalizing streaming events across all three protocols

See the [test documentation](docs/testing.md) (Chinese) for the full mapping.

---

## Documentation

### Module write-ups (9)

| # | Document | Module |
|---|----------|--------|
| 01 | [The initial coding agent](docs/01-initial-coding-agent.md) | Entry point / config / minimal loop |
| 02 | [LLM client and streaming](docs/02-llm-client-streaming.md) | client.py / StreamCollector |
| 03 | [Tool registry and execution framework](docs/03-tool-registry.md) | tools/ · registry |
| 04 | [Agent loop and event stream](docs/04-agent-loop-events.md) | agent.py ReAct loop |
| 05 | [System prompt assembly pipeline](docs/05-system-prompt.md) | prompts.py |
| 06 | [Permission system](docs/06-permissions.md) | permissions/ layered defense |
| 07 | [MCP integration](docs/07-mcp.md) | mcp/ open extension channel |
| 08 | [Context compaction and token management](docs/08-context-compaction.md) | context/ two-layer compaction |
| 09 | [TUI interaction design](docs/09-tui-design.md) | app.py Textual interface |

Plus the [test documentation](docs/testing.md). All of these are in Chinese.

### Architecture diagrams (11)

- [System overview](https://xls0Jacker.github.io/MiniAgent/system-architecture/system-architecture.html) — 12 nodes, which
  modules one turn passes through ([how to view](docs/system-architecture/README.md#怎么看))
- [10 module diagrams](docs/module-architecture/README.md) — how each module works internally;
  see [the table above](#the-10-module-diagrams)

---

## Project Layout

```
MiniAgent/
├── autocode/                 # Main package (harness + TUI)
│   ├── __main__.py           # CLI entry point (TUI / -p non-interactive)
│   ├── agent.py              # Agent loop + event stream
│   ├── client.py             # Three-protocol LLM client
│   ├── prompts.py            # System prompt assembly pipeline
│   ├── app.py                # Textual TUI application
│   ├── conversation.py       # Conversation history + token estimation + injection
│   ├── tools/                # Tool base class / registry / coding tools / demo tools
│   ├── permissions/          # Layered permission decisions
│   ├── context/              # Two-layer context compaction
│   ├── memory/               # session / auto_memory / recall
│   ├── hooks/                # Lifecycle hooks
│   ├── mcp/                  # MCP protocol (open extension channel)
│   └── ...
├── tests/                    # Tests
├── docs/                     # Module write-ups / architecture diagrams / test docs
├── config.example.yaml       # Example config
├── pyproject.toml
└── .autocode/config.local.yaml   # Local config (kept out of the repository)
```
