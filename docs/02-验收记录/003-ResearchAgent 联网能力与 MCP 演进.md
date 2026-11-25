# 003 ResearchAgent 联网能力与 MCP 演进验收记录

2026-11-25 · WSL Ubuntu · P0 和 P1 已验收，P1 搜索服务商实网调用待补证

[对应技术方案（03）](<../01-技术方案改造/03-ResearchAgent 联网能力与 MCP 演进技术方案文档.md>)

## 分阶段验收

P0、P1 和 P2.1 的执行条件、实测数据及验证范围见下文。

| 阶段 | 验收条件 | 状态 |
| --- | --- | --- |
| P0 基础建设与基线 | 在 WSL/Linux 验证 Playwright 启动、连接与清理超时、错误透传和 JSONL Trace。运行 MCP 回归测试，并保留同一组基线任务的耗时、工具调用次数及 Token 用量。 | 已验收，2026-11-25 |
| P1 联网能力与角色隔离 | 常规查询通过内置搜索和抓取完成；交互页面可委派给仅挂载白名单浏览器工具的 `explore`。验证重复只读查询命中缓存，并核对缓存与 Trace 的实现位置。 | WSL 通过；搜索服务商仅完成离线接口测试 |
| P2 离线评测与对照 | 验证断网且零 API 消耗时的回放结果。用相同任务对比 P0 与 P1 的成功率、P50/P95 时延、平均工具调用次数和上下文 Token 用量。 | P2.1 录制通过，后续子阶段待实施 |
| P3 Web 接入与会话回收 | 验证多标签页并发及异常关闭后的闲置进程回收，检查前端能否追溯主、子 Agent 调用链。Streamable HTTP 按高并发部署需求单独记录。 | 待验收 |

## 实际验收记录

### P0 · 2026-11-25

环境为 WSL Ubuntu 24.04.1、Conda `researchAgent`、Python 3.11.18、MCP Python SDK 2.3.0、AnyIO 4.16.0、Linux Node.js 26.13.1。代码基底为 `8702527` 加本次工作区改动。Playwright MCP 固定为 0.0.86，本轮预先安装浏览器并预热 npm 包缓存。WSL 原先缺少 `libnspr4`、`libnss3`、`libasound2t64`，联调通过临时解包和 `LD_LIBRARY_PATH` 提供这些库；常规安装方式见根 README。

| 验证类型 | 操作 | 实际结果 |
| --- | --- | --- |
| WSL 离线，MCP 专项 | `python -s -B -m unittest discover -s tests -p 'test_mcp*.py' -v` | 34 项，31 通过，3 项 Windows 专用用例跳过。覆盖连接超时清理、软警告和硬截止、stderr 输出、服务名校验、远端错误标志和调用超时后的会话复用。 |
| WSL 离线，完整回归 | `python -s -B -m unittest discover -s tests -v` | 98 项，95 通过，同上 3 项跳过。双协议测试分别核对工具返回、统计与 Trace，包含成功返回正文提及 timeout/error、失败正文没有错误关键词两种情况。 |
| 外部 MCP 联调 | 通过仓库默认配置执行 `McpManager.load_and_connect()`，调用 `mcp__playwright__browser_navigate` 打开 `https://example.org/`，最后执行 `disconnect_all()`。 | Context7 发现 2 个工具，Playwright 发现 25 个工具。浏览器返回成功并读到标题 Example Domain。 |
| 真实模型调用 | 在两个独立主 Agent 会话中运行下面的固定页面任务，调用配置的 DeepSeek API。 | 最终答复与预期编号逐字匹配，2/2 成功。原始计量及逐调用 Trace 见 [P0 基线 JSON](003-P0基线.json)。 |

#### P1 改造前的任务基线

两个小页面均由本地 HTTP 服务提供，样本内容、交互及正确答案固定。使用 `deepseek-v4-pro`，每个任务限 8 轮工具调用、单次输出上限 1,500 Token，关闭记忆预取、折叠和在线技能演化。耗时从 `run_once()` 开始到返回，包含首次 MCP 初始化和模型推理，结束后单独关闭服务。Token 是模型报告的各轮累计值，辅助调用用量两例均为零。

| 任务 | 预期及实测最终答复 | 端到端耗时 | 实际工具调用 | 输入 Token | 输出 Token |
| --- | --- | ---: | --- | ---: | ---: |
| 从静态观测记录查找样本编号 | NL-4827 | 10.279 秒 | 2 次，导航、快照 | 32,626 | 105 |
| 点击 Show result 后读取动态结果 | SW-6319 | 13.585 秒 | 4 次，导航、快照、点击、快照 | 55,186 | 211 |

在仓库根目录按相同条件重跑，使用新文件名保存对照样本。

```bash
PYTHONPATH=. python -s -B tests/fixtures/web_baseline.py --output /tmp/researchagent-web-comparison.json
```

预跑发现 `run_once()["text"]` 同时收集中间发言和最终答复。基线脚本从会话历史读取最后一条助手消息进行评分，上表使用修正后重新执行的一组样本。每题仅采样一次，适合核对工具路径和后续同题对照；开放式科研检索质量、延迟分位数、Windows 原生及容器实机运行仍待各自阶段或环境验证。[固定任务与计量脚本](../../tests/fixtures/web_baseline.py)

### P1 · 2026-11-25

以 P0 的 WSL Ubuntu 24.04.1 环境、Python 3.11.18、Playwright MCP 0.0.86 和 Chromium 验证。同一组页面和评分脚本、模型、轮数与输出上限沿用 P0。实测时通过 WSL 的 Linux Node.js 运行，Chromium 所需的三个共享库沿用 P0 验收时解包的临时目录。

| 验证路径 | 运行与结果 |
| --- | --- |
| 离线回归 | `python -s -B -m unittest discover -s tests -p 'test_mcp*.py' -v` 运行 34 项，31 通过、3 项 Windows 专用用例跳过。完整命令 `python -s -B -m unittest discover -s tests -v` 运行 105 项，102 通过、同样 3 项跳过。 |
| 搜索和抓取接口 | P1 专项覆盖两家服务商的请求与响应、条数与字符上限、HTTP 429、超时、403/503、空页面和大正文。搜索使用 HTTP MockTransport；本环境未配置两家搜索服务的密钥，未验证真实搜索召回、账号权限和额度。 |
| 角色及缓存 | 测试验证角色过滤发生在建连接前，Playwright 白名单同时约束发现与调用。默认主 Agent 没有浏览器工具。同会话等价请求复用成功结果；过期或失败重试会访问上游。浏览器快照重复执行不命中缓存。 |
| 真实模型与 MCP | 静态样例由主 Agent 使用 `web_fetch` 回答；交互样例由主 Agent 委派 `explore`，子 Agent 在隔离浏览器中导航、读取快照、点击并再次读取快照。原始记录含角色、父子 span、耗时与 Token 计数。[逐次调用数据](003-P1联网验收.json) |

| 任务 | 答案 | 端到端耗时 | 执行工具数（含委派） | 累计输入 / 输出 Token |
| --- | --- | ---: | ---: | ---: |
| 从静态观测目录读取编号 | NL-4827 | 4.665 秒 | 1 | 9,697 / 62 |
| 点击按钮后读取编号 | SW-6319 | 17.097 秒 | 5 | 23,089 / 355 |

两项均为一次采样，用于核对执行路径。运行 `PYTHONPATH=. python -s -B tests/fixtures/web_baseline.py --output /tmp/researchagent-p1.json` 可在已配置模型、浏览器的 WSL 环境中复测。P2 的回放与多样本统计留在下一阶段。

### P2.1 · 单次工具调用录制 · 2026-11-25

WSL Ubuntu，项目 `researchAgent` Python 3.11 环境。默认 `live` 模式不写工具快照。设置 `RESEARCH_TOOL_MODE=record` 后，每次完成的调用写入 `~/.research/snapshots/<trace_id>/<span_id>.json`。每份快照包含工具名、规范化参数和原始结果的 `content`、`is_error`、`meta`。快照文件名可与同一调用的 Trace 对应。

| 验证 | 命令 | 实测结果 |
| --- | --- | --- |
| 录制与默认模式 | `python -s -B -m unittest discover -s tests -p 'test_tool_snapshot.py' -v` | 2 项通过。回环 HTTP 服务提供固定页，两次抓取的 URL 仅片段不同，服务实际接收 1 个请求。两份快照记录相同的规范化 URL，均完整保留超过 30 KiB 的正文，第二份含缓存命中标记。另验证默认模式未创建快照目录，录制模式保留读文件失败的状态及元数据。 |
| 既有联网路径 | `python -s -B -m unittest discover -s tests -p 'test_web_runtime.py' -v` | 7 项通过。覆盖搜索参数限幅、静态抓取、只读缓存、浏览器与角色边界。 |

本次使用临时用户目录，未调用模型或搜索服务。P2.1 只验收录制；回放和性能对照尚未实施。
