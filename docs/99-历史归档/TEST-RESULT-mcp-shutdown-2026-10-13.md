# Windows MCP 退出清理测试记录

日期：2026-10-13  
结论：**本次退出清理专项测试通过。DeepSeek 第二阶段完整验收尚未完成。**

## 范围与环境

- Windows，项目 `D:\LLM Learning\BearAgent\BearCode`，conda 环境 `BearAgent`。
- 验证交互退出时 MCP 子进程、管道及后台任务的回收；关联[问题记录第 5 项](known-issues-and-fix-plan.md#5-windows-交互模式退出时出现管道清理异常)。
- 用户仅授权此次临时真实 API 测试。程序内部使用现有配置；助手没有查看或输出实际 `.env`、密钥或完整思考正文。测试已结束，不继续使用此授权。

## 针对性离线检查

| 检查范围 | 通过数量 |
| --- | ---: |
| 新增 MCP 关闭与交互退出测试 | 9 |
| 原 MCP 请求超时回归 | 1 |
| DeepSeek 配置回归 | 7 |
| DeepSeek 思考模式回归 | 23 |
| 压缩开关回归 | 5 |
| 合计 | **45** |

新增检查包括重复关闭、等候管道和任务结束、连接失败与取消、部分连接关闭失败、正常 exit、EOF、两次 Ctrl+C 及后台收尾失败。真实本地 Windows Python 父子进程测试确认两者均退出，管道已关闭，没有清理错误。语法检查及 `git diff --check` 通过。

## 临时真实接口测试

启动参数如下，测试输入由标准输入传入：

```powershell
python -X utf8 -B -m agents.main --model deepseek-v4-pro --api-base https://api.deepseek.com --thinking --reasoning-effort low --max-output-tokens 2048 --max-cost 0.10 --max-turns 1 --dont-ask
```

先发送简短提示，要求只回复 `shutdown test OK`，不调用工具、不读写文件、不保存记忆或创建 Skill；之后输入 `exit`。程序自行建立 MCP 连接，不实际调用 MCP 工具。

| 检查项 | 实际结果 |
| --- | --- |
| 真实模型回复 | `shutdown test OK` |
| 会话配置 | `deepseek-v4-pro`，thinking 开启，effort 为 low |
| 工具调用 | 0 次 |
| Playwright | 连接成功，发现 25 个工具 |
| Context7 | 连接失败，界面原因为空；失败连接清理完成 |
| 进程退出 | 退出码 0，无测试超时 |
| 管道报错 | 未出现 `I/O operation on closed pipe` |
| 错误日志 | stderr 为空，0 字节 |
| 进程回收 | 按测试 Python PID 29248 追踪父子关系；退出后追踪进程无残留 |
| 用量与费用 | 累计输入 9586、输出 5 tokens；峰价估算 0.0097 USD，以账单为准 |
| 会话保存 | 新测试会话 `30b4d087` 已保存，没有恢复原会话 |

原始日志文件名为 `mcp-shutdown-2026-10-13-65bddfad.stdout.txt`、`mcp-shutdown-2026-10-13-65bddfad.stderr.txt`；已在测试完成后按用户授权清理，上述结果保留为正式记录。

首次自动化启动因重定向输出的 GBK 编码无法打印提示符，在请求前退出；重试仅启用 Python UTF-8 模式，不为此修改项目代码。成功测试的简单回复包含空字符串思考字段，不将其作为产生非空思考正文的证据。

## 结论与未覆盖项

本次真实对话、Playwright 已连接后的正常退出、Context7 连接失败后的清理均符合专项通过条件。Context7 空原因连接失败仍是独立待观察问题；本次未验证 Context7 成功连接后的退出、真实 Ctrl+C 或 EOF。离线检查已覆盖相应退出分支，但不能替代这些真实补测。

DeepSeek 第二阶段的普通子 Agent、fork Skill、关闭思考回归及中断恢复仍待真实验收，不据此将整个阶段标为完成。`--max-cost` 不是账单硬上限。

测试生成的新会话当时成为全局最新会话。用户随后授权清理本次临时文件，已于 2026-10-13 删除 5 个临时输入/日志文件及确认仅含测试内容的 `30b4d087.json`；原有会话未删除，`--resume` 不再恢复该测试会话。本文保留为正式测试记录，位于被 `.gitignore` 忽略的 `docs/`，未提交 Git。
