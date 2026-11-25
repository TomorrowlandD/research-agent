# ResearchAgent 联网能力与 MCP 演进技术方案文档

## 1. 背景、目标与落地原则

当前 `ResearchAgent` 已基于官方 `mcp==2.3.0` 跑通了 `@playwright/mcp` 的本地 `stdio` 接入、双模型协议工具适配与大结果落盘机制，但联网能力仍处于单机原型阶段：所有检索均依赖重量级浏览器、25 个 Playwright 工具全量注入主 Agent 上下文、Windows 路径硬编码阻碍 Linux 部署，且缺乏结构化 Trace 与离线评测（Eval）复现能力。

本方案面向 **代码落地与 AI 辅助开发（如 Codex / Claude Code）** 编写，遵循四项硬约束：

1. **复用已有优良基座，禁止推倒重来：** 保留现有的官方 SDK 封装、单服务 `asyncio.Task` 上下文托管、命名空间路由与 30 KiB 大结果落盘机制。
2. **能力分层而非固定工作流：** 拆分 `web_search`、`web_fetch`、`Browser MCP` 三层能力，通过 Prompt 与工具描述软引导模型自主选择，不硬编码逐级调用流水线。
3. **控制重构爆炸半径：** 在现有 `_execute_tool_call()` 入口收口结构化结果与轻量治理中间件，不强行合并 OpenAI/Anthropic 双主循环的并发调度与审批逻辑。
4. **先度量，再优化：** 在 P0 优先补齐极简 JSONL Trace 埋点，为 P1 联网重构提供改造前后的量化对照基线。

## 2. 现状基线与复用边界（执行 Agent 硬约束）

在修改代码前，必须严格区分**保留复用区**与**改造补漏区**：

### 2.1 必须保留的现有实现（禁止重写）

+ **官方 SDK 与任务级生命周期托管（`agents/mcp_client.py:41, 125-186`）：** 沿用 `mcp==2.3.0` 与“每个 MCP 服务由独立 `asyncio.Task` 持有 `stdio_client` 和 `ClientSession` 上下文”的设计。在 AnyIO CancelScope 约束下，该设计比盲目引入 `AsyncExitStack` 更安全，且已有 28 项通过的 WSL 单元测试保护。
+ **命名空间与双协议适配（`agents/mcp_client.py:291-305`, `agents/agent.py:130, 1783, 2089`）：** 沿用 `mcp__<server>__<tool>` 命名防冲突机制、内部共享格式 `{name, description, input_schema}` 以及 OpenAI/Anthropic 出口转换逻辑。
+ **大结果落盘机制（`agents/agent.py:1281, 1738, 2036`）：** 沿用 `Agent._persist_large_result()`（超过 30 KiB 自动写入 `~/.research/tool-results/` 并向模型返回文件路径与预览）。不要在工具层新增与之冲突的暴力丢弃截断逻辑。
+ **双循环各自的并发与审批机制（`agents/agent.py:1702, 1960-1987`）：** 保留 Anthropic 串行执行与 OpenAI 校验审批后按并发安全名单分批执行的现有框架。

### 2.2 需要修补与扩展的现状短板

+ **子 Agent 写死跳过 MCP（`agents/agent.py:514`）：** 目前仅主 Agent 初始化 MCP，需改为**按角色声明挂载**，使 `explore` 子 Agent 能独立加载 Playwright MCP。
+ **结构化错误状态断层（`agents/mcp_client.py:174, 204`, `agents/agent.py:665, 1733-1751`）：** MCP `result.is_error=True` 被转为异常字符串后，Anthropic 的 `tool_result.is_error` 未正确置位，运行时成败统计被迫依赖文本关键字匹配。
+ **启动与清理超时死角（`agents/mcp_client.py:145, 218, 256`）：** `conn.connect()` 缺少外层超时；`_await_cleanup()` 在 15 秒告警后无第二道硬截止时间；子进程 `stderr` 被重定向至 `os.devnull`，导致 Linux 排错困难。
+ **跨平台与版本漂移（`.mcp.json:9-13`）：** 硬编码 `D:\nodejs\npx.cmd`、未锁定 `@playwright/mcp` 版本、未显式开启 `--headless`。
+ **联网能力缺失与治理空白：** 缺少内置 `web_search` 与 `web_fetch`；缺少只读结果 TTL 缓存、结构化 JSONL Trace 及离线评测 `Record/Replay`。

## 3. 目标系统架构

Plaintext

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Host / Agent Runtime                            │
│  ┌────────────────────────────┐      ┌───────────────────────────────┐  │
│  │  Main Agent (高层规划/综合) │ 委派 │  Explore Sub-Agent (深度探索)  │  │
│  │  • 内置: web_search,       │ ───▶ │  • 内置: web_search,          │  │
│  │          web_fetch, 等     │      │          web_fetch            │  │
│  │  • MCP: 默认不挂 Playwright │      │  • MCP: playwright (白名单裁剪)│  │
│  └─────────────┬──────────────┘      └───────────────┬───────────────┘  │
│                └──────────────────┬──────────────────┘                  │
│                                   ▼                                     │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │        统一分派入口 _execute_tool_call() + 轻量治理中间件           │  │
│  │  • 统一返回结构化 ToolExecutionResult(ok, content, is_error, meta) │  │
│  │  • 中间件: [JSONL Trace] [只读 TTL 缓存] [Record/Replay 代理]       │  │
│  │  • 复用已有: _persist_large_result() (>30 KiB 自动落盘+预览)        │  │
│  └───────────────┬───────────────────────────────┬───────────────────┘  │
│                  ▼                               ▼                      │
│  ┌────────────────────────────────┐  ┌───────────────────────────────┐  │
│  │      内置工具层 (agents/tools)  │  │     McpManager (mcp_client)   │  │
│  │  • web_search (Tavily/Serper)  │  │  • 纯协议通信与 Task 托管      │  │
│  │  • web_fetch (httpx+trafilatura│  │  • 角色过滤 + 工具白名单裁剪   │  │
│  │  • 已有本地工具与状态分支       │  │  • connect/cleanup 硬超时兜底  │  │
│  └────────────────────────────────┘  └───────────────┬───────────────┘  │
└──────────────────────────────────────────────────────┼──────────────────┘
                                                       │ JSON-RPC 2.0
                                                       │ (P0-P3初: stdio)
                                                       │ (P3高并发: Streamable HTTP)
                                                       ▼
                                         ┌─────────────────────────────┐
                                         │   @playwright/mcp (锁版本)   │
                                         │   --headless                │
                                         │   --image-responses omit    │
                                         └─────────────────────────────┘
```

## 4. 核心技术选型明细

### 4.1 工具形态边界：内置工具 vs. MCP Server

+ **`web_search` 与 `web_fetch` → 进程内内置工具（Python 异步函数）：** 逻辑仅为 HTTP 请求与文本清洗，进程内调用零序列化开销，最便于直接接入缓存、并发安全名单与 Trace，无需强行套壳 MCP。
+ **浏览器自动化 → 外部 MCP Server（`@playwright/mcp`）：** 依赖 Node.js 与无头浏览器内核，走 MCP 实现跨语言与进程隔离，直接复用微软官方基于 Accessibility Tree（无障碍树快照）的成熟实现。

### 4.2 底层搜索源（`web_search`）与静态正文抓取（`web_fetch`）

#### 底层搜索 API 选型

| **搜索源**        | **底层机制**              | **优势**                                   | **成本参考**                            | **定位**     |
| ----------------- | ------------------------- | ------------------------------------------ | --------------------------------------- | ------------ |
| **Tavily**        | LLM 专用聚合与正文清洗    | 自动去噪，直接返回高信息密度摘要，开箱即用 | 每月 1,000 次免费；超额约 $0.008/次     | **默认主选** |
| **Serper**        | Google SERP 代理与解析    | 中文及长尾技术词召回强、延迟低、单价低     | 注册送 2,500 次；后续 $0.30～$1.00/千次 | **配置备选** |
| **Brave Search**  | 自建独立全网索引          | 隐私合规，提供面向 LLM 的精简接口          | 每月 $5 免费额度（约 1,000 次）         | 可选扩展     |
| **Exa / SearXNG** | 语义检索 / 开源自建元搜索 | 分别适合论文深度语义挖掘 / 零预算私有部署  | 按量 / 仅服务器成本                     | 按需扩展     |

+ **落地方案：** 在内置工具层抽象极简 `SearchProvider` 接口，**主选 Tavily、备选 Serper**（通过环境变量切换）。
+ **硬参数约束：** 默认关闭 `include_raw_content=False`；`max_results` 服务端硬限幅 `[1, 10]`；单条摘要截断 ≤ 600 字符，单次 `web_search` 总输出控制在 **4,000 字符以内**；外层请求超时 **15 秒**。

#### 静态正文抓取选型

+ **最终选型：** **`httpx`（异步请求） + `trafilatura`（本地正文抽取转 Markdown）**。
+ **核心依据：** 零外部 API 费用、无第三方限流、延迟极低。
+ **与现有机制衔接：** 外层超时 **20 秒**；返回正文不强行做低阈值丢弃截断，超过 30 KiB 时直接复用现有 `Agent._persist_large_result()` 落盘并提供预览。当遭遇空正文、明显 JS 占位页或 403/503 状态码时，返回明确提示文本，由模型自主决策是否委派 `explore` 子 Agent 启动浏览器。

### 4.3 浏览器自动化 MCP 规范

+ **最终选型：** 保留微软官方 `@playwright/mcp`，坚持**独立隔离浏览器实例**（严禁配置 `--cdp-endpoint` 接管宿主机 Opera 或导出本地 Cookie，避免风控与服务端不可移植）。
+ **强制启动参数规范：**
  1. **版本锁定：** `.mcp.json` 中严禁使用 `@playwright/mcp@latest`，固定具体语义化版本号（如 `@playwright/mcp@0.0.x`）。
  2. **跨平台命令：** 移除 `D:\nodejs\npx.cmd` 硬编码，改为通用 `"command": "npx"`，在 `mcp_client.py` 中通过 `shutil.which()` 按平台动态解析（Windows 自动匹配 `npx.cmd`，Linux/WSL 匹配 `npx`）。
  3. **无头与去图：** 启动参数显式追加 `--headless` 与 `--image-responses omit`（禁用 Base64 截图回传，防止撑爆上下文；且 `mcp_client.py` 当前本就只提取 `TextContent`）。

### 4.4 MCP 传输层演进策略（`stdio` → Streamable HTTP）

+ **P0–P3 Web MVP 阶段：继续沿用 `stdio` + 懒加载。** 不将传输协议迁移作为 Web 化的前置条件；Web 后端上线时补充**会话级 Idle Timeout（空闲超时回收）**，自动关闭闲置会话的 Playwright 子进程，防止僵尸 Chromium 吃光内存。
+ **P3 高并发阶段（按需演进）：迁移至 Streamable HTTP。** 将 `@playwright/mcp` 拆为独立容器服务（通过 `--port` 启动），在 `mcp_client.py` 中新增基于 `mcp.client.streamable_http` 的连接分支，配置加载器放开 `url` 字段，实现多会话共享浏览器服务池。

### 4.5 角色级工具隔离与白名单裁剪

+ **角色分工：**
  + **Main Agent：** 仅可见内置工具（含 `web_search`、`web_fetch`）与子 Agent 委派工具，**默认不挂载 Playwright MCP**。
  + **`explore` Sub-Agent：** 可见 `web_search`、`web_fetch` 及 Playwright MCP 工具。
+ **工具白名单裁剪：** 在 `McpManager` 暴露工具给模型前增加按服务配置的 `allowed_tools` 过滤机制，从 Playwright 的 25 个工具中仅保留高频核心操作（约 6～8 个：`browser_navigate`、`browser_snapshot`、`browser_click`、`browser_type`、`browser_wait_for`、`browser_navigate_back`、`browser_press_key`、`browser_close`），大幅压缩每轮固定输入 Token 并提升工具选择准确率。

### 4.6 统一执行层与横切治理设计

不推翻现有双主循环，而是采用“结果结构化 + 轻量中间件”方案：

1. **统一结构化返回对象：** 定义 `ToolExecutionResult(ok: bool, content: str, is_error: bool, meta: dict)`。`_execute_tool_call()` 统一返回该对象（不再返回裸字符串或靠抛异常传递失败）。
2. **打通 `is_error` 全链路：**
   + `mcp_client.py` 在 `result.is_error=True` 时返回结构化错误标记，不再仅抛 `RuntimeError`。
   + Anthropic 主循环读取 `ToolExecutionResult.is_error`，准确设置 `tool_result["is_error"] = True`；OpenAI 主循环保留 `role="tool"` 回填文本，同时运行时成败统计直接读取 `result.ok`，**彻底淘汰基于文本关键字（`error`/`timeout`）的脆弱判断**。
3. **只读工具 TTL 缓存：** 仅对具备幂等只读特性的 **`web_search` 与 `web_fetch`** 启用内存/SQLite TTL 缓存（Key 为 `(tool_name, normalized_args)`，TTL 10～60 分钟）。**严禁对 Playwright 交互类工具做结果缓存**，避免页面状态错乱。
4. **极简 JSONL Trace（P0 上线）：** 在统一执行外层自动落盘单行 JSON 记录，包含字段：`timestamp`、`trace_id`、`span_id`、`agent_role`、`tool_name`、`source_type`（`builtin`/`mcp`）、`latency_ms`、`output_chars`、`cache_hit`、`ok`、`error_type`。
5. **Eval Record/Replay 中间件（P2 上线）：** 支持 `live`（正常执行）、`record`（执行并将 `(tool_name, normalized_args) -> ToolExecutionResult` 写入快照文件）、`replay`（离线强制读取快照，未命中直接报错提示）三种模式。

## 5. 结合现有代码库的落地方案（模块级修改指引）

以下指引直接对应现有仓库结构，供实施阶段逐文件核对：

### 5.1 `.mcp.json`（配置层）

+ 将 `command` 从 `D:\nodejs\npx.cmd` 改为 `"npx"`。
+ 将 `@playwright/mcp@latest` 替换为锁定版本号，并追加 `"--headless"`、`"--image-responses"`、`"omit"`。
+ 在服务定义中新增可选字段：`"roles": ["explore"]`（声明允许挂载的 Agent 角色）与 `"allowed_tools": [...]`（工具白名单）。

### 5.2 `agents/mcp_client.py`（纯协议与连接层）

+ **保持职责纯粹：** 严禁在文件内引入结果缓存、Trace 写入或业务截断逻辑。
+ **跨平台命令解析：** 构造 `StdioServerParameters` 前，若 `command` 非绝对路径，使用 `shutil.which(command)` 解析（兼容 Windows `.cmd` 与 Linux/WSL）。
+ **服务名校验：** 加载配置时校验 `server_name` 严禁包含 `__`，规避 `mcp__<server>__<tool>` 反向路由拆段歧义（`mcp_client.py:297-305`）。
+ **堵住超时与日志死角：**
  + 给 `conn.connect()` 补加外层超时（如 20 秒），超时后触发 `close()`。
  + 给 `_await_cleanup()`（`mcp_client.py:218`）在 15 秒告警后增加第二道硬截止时间（如总计 30 秒），超时后强制放弃等待并记录错误，防止进程永久挂起。
  + 将 `stdio_client` 的 `errlog`（`mcp_client.py:145`）从 `os.devnull` 改为指向独立日志文件（如 `~/.research/logs/mcp-<server>.stderr.log`）或环形内存缓冲，确保异常可查。
+ **白名单与结构化返回：** `get_tool_definitions(role=...)` 按 `roles` 与 `allowed_tools` 过滤工具；`call_tool()` 返回结构化结果对象（携带 `is_error`、`text` 与 `error_type`），保留超时后复用会话的既有行为。

### 5.3 `agents/tools.py`（内置工具层）

+ 新增异步内置工具 `web_search`（接 Tavily/Serper）与 `web_fetch`（`httpx` + `trafilatura`），并将二者加入并发安全工具名单（支持 OpenAI 循环并发拉取多个网页或搜索多个词）。
+ 在 `web_fetch` 与浏览器快照文本外层包裹明确的数据边界标记（如 `<external_web_content source="...">...</external_web_content>`），防御网页 Prompt Injection。

### 5.4 `agents/agent.py`（主循环与角色编排层）

+ **解除子 Agent 的 MCP 限制（`agent.py:514`）：** 将“仅主 Agent 加载 MCP”改为按当前 Agent 的 `role`（如 `main` / `explore`）从 `McpManager` 按需懒加载对应角色允许的 MCP 服务与工具。
+ **升级 `_execute_tool_call()`（`agent.py:1317`）：** 保留内部对折叠、计划模式、子 Agent 和 Skills 的既有状态分支；统一返回 `ToolExecutionResult`，在函数外层挂载轻量中间件（Trace 埋点、只读 TTL 缓存、P2 阶段的 Record/Replay）。
+ **适配双主循环（`agent.py:1707-1751, 2029-2049`）：**
  + 两侧循环均从 `ToolExecutionResult` 读取 `content` 与 `is_error`，不再依赖 `try/except` 拼装模糊错误字符串。
  + Anthropic 分支透传 `is_error=result.is_error`；运行统计直接以 `result.ok` 为准。
  + 保持调用 `_persist_large_result()` 的位置不变，确保 `web_fetch` 和 Playwright 快照超 30 KiB 时无缝落盘。
+ **Prompt 软引导：** 在主 Agent 与 `explore` 子 Agent 的 System Prompt 中简明声明三层联网能力边界（查线索优先 `web_search`，已知静态链接直接 `web_fetch`，动态渲染/交互受阻再委派 `explore` 使用 Playwright）。

## 6. 分阶段实施路线图与验收标准（P0–P3）

| **阶段**                   | **核心交付内容**                                             | **验收标准（DoD）**                                          |
| -------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| **P0：基础建设与基线建立** | 1. 跨平台 `npx` 解析、锁定 `@playwright/mcp` 版本、开启 `--headless` 与 `--image-responses omit`。  2. 补齐 `connect()` 超时、`_await_cleanup()` 第二道硬超时、`stderr` 日志落盘、服务名 `__` 校验。  3. 升级 `_execute_tool_call()` 返回 `ToolExecutionResult`，打通 `is_error`，上线极简 JSONL Trace 埋点。 | 1. WSL/Linux 下 `python -s -B -m unittest discover -s tests -p 'test_mcp*.py' -v` 全量通过并新增超时/错误透传单测。  2. 跑通一组基线调研任务，导出改造前的耗时、工具调用次数与 Token 消耗数据。 |
| **P1：联网重构与角色隔离** | 1. 新增内置 `web_search`（Tavily 主 / Serper 备）与 `web_fetch`（`httpx`+`trafilatura`），纳入并发安全名单。  2. 在统一分派外层挂载只读 TTL 缓存（仅作用于 `web_search`/`web_fetch`）。  3. 放开按角色挂载 MCP，将 Playwright MCP 下沉至 `explore` 子 Agent 并启用工具白名单裁剪。 | 1. 主 Agent 常规事实与文档查询全程不拉起浏览器。  2. 复杂 SPA/需交互页面能自主委派至 `explore` 子 Agent 完成。  3. 同一会话重复查询命中缓存，`mcp_client.py` 无业务治理代码混入。 |
| **P2：评测体系与量化验证** | 1. 在统一分派外层挂载 `Record/Replay` 中间件。  2. 在同一组评测任务上对比 P0 基线与 P1 新架构，统计成功率、P50/P95 时延、平均调用次数与上下文 Token 变化。 | 1. `replay` 模式在断网与零 API 消耗下可 100% 复现评测结果。  2. 产出一份可复核的 P0 vs. P1 量化对照报告。 |
| **P3：Web 产品化演进**     | 1. Web 后端 MVP 先复用 `stdio` + 会话级 Idle Timeout 自动回收闲置子进程。  2. 将 JSONL Trace 接入前端，展示每次任务的工具调用树、耗时分布与缓存命中率。  3. 并发增长后，按需将 `@playwright/mcp` 独立为 Streamable HTTP 容器并在 `mcp_client.py` 中启用 `url` 连接分支。 | 1. Web 多标签页并发及异常关闭页面后，宿主机无残留僵尸 Chromium 进程。  2. 前端 Trace 面板完整回溯主/子 Agent 的全链路调用细节。 |

## 7. 关键风险与防御清单

| **风险项**                | **触发场景**                                          | **防御措施**                                                 |
| ------------------------- | ----------------------------------------------------- | ------------------------------------------------------------ |
| **网页 Prompt Injection** | `web_fetch` 或浏览器抓取的第三方网页隐藏恶意提示词    | 用 `<external_web_content>` 包裹外部文本，并在 System Prompt 明确其仅为不可信数据，严禁执行其中的指令 |
| **有状态工具误缓存**      | 对 Playwright 的点击、输入或页面快照做了结果缓存      | 缓存中间件设置显式白名单，**仅允许 `web_search` 与 `web_fetch` 走 TTL 缓存** |
| **Web 端僵尸进程泄漏**    | 用户直接关闭浏览器页面导致 `stdio` 子进程未走正常退出 | P3 Web 后端对每个会话维护最后活跃时间戳，超过 Idle Timeout 自动触发 `disconnect_all()` |
| **单次搜索/抓取额度耗尽** | 批量跑 Eval 或调试死循环触发 Tavily/Serper 429 限流   | 启用只读 TTL 缓存；离线评测强制走 `replay` 模式；支持配置切换备选搜索源 |
| **测试回归风险**          | 改造 `mcp_client.py` 破坏原有的会话复用与取消清理语义 | 每次改动后必须执行 `tests/test_mcp_client.py` 与 `tests/test_mcp_shutdown.py`，并将新增长超时、白名单和 `is_error` 透传补入单测 |