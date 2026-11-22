# DeepSeek V4 Pro 第二阶段真实接口验收

日期：2026-10-12  
状态：代码已实现，48 项离线与相关回归测试通过；本文件由用户执行，真实思考接口尚未验收。

关联：[适配计划](PLAN-deepseek-v4-pro.md)、[工作记录](known-issues-and-fix-plan.md)。

## 1. 配置与新会话

由用户自行维护实际 `.env` 中的密钥，测试过程中不读取、不展示或记录密钥。保持 `MODEL=deepseek-v4-pro`、`API=https://api.deepseek.com`；辅助思考推荐关闭。

激活 `BearAgent`，在 BearCode 根目录启动全新会话，不使用 `--resume`：

```powershell
python -m agents.main --model deepseek-v4-pro --api-base https://api.deepseek.com --thinking --reasoning-effort low
```

该命令覆盖环境中的主思考开关与强度。默认最大输出 65536 token，包含思考和最终回答，不代表每次实际生成这么多。首次验收不需要 `max` 强度。费用是实际 API 使用费用，项目显示仍为估算。

通过条件：正常启动；真实请求正常回复。界面的 `DeepSeek thinking...` 是状态提示，不能单凭提示证明协议正确。

## 2. 连续对话与工具调用

依次发送并等待回复：

```text
本次正在验收 DeepSeek 思考模式，测试代号“熊猫862”。请记住代号并简短回复。
```

```text
刚才的代号是什么？请先用 read_file 读取 docs/PLAN-deepseek-v4-pro.md，再用 grep_search 在 path="agents/deepseek.py" 中搜索 BEAR_DEEPSEEK_THINKING，结合实际结果解释主请求与辅助请求的思考开关。依次调用工具，不要读取 .env 或任何密钥文件，不要修改文件，不要调用 MCP 或子 Agent。
```

```text
再用 grep_search 在 path="agents/agent.py" 搜索 reasoning_content，说明为何普通回答轮次也要保留这个字段。不要读取 .env，不要修改文件。
```

通过条件：记忆正确，工具实际调用并正确返回；多轮及多次工具回传后仍正常回复，没有 HTTP 400、思考字段缺失或工具消息配对错误。第二阶段将配置解析集中到 `agents/deepseek.py`，不要继续用旧路径判断环境变量不存在。

## 3. 保存与恢复

输入 `exit`，然后恢复刚才的思考会话：

```powershell
python -m agents.main --model deepseek-v4-pro --api-base https://api.deepseek.com --thinking --reasoning-effort low --resume
```

发送：

```text
之前的测试代号是什么？再搜索 agents/deepseek.py 中的 BEAR_DEEPSEEK_THINKING。不要读取 .env，不要修改文件。
```

通过条件：恢复成功，记忆和工具工作正常，没有因恢复丢失思考字段而出现协议错误。应在同一项目、紧接前次会话恢复；原项目 `--resume` 选择全局最近会话，测试期间不要穿插其他项目的会话。

## 4. 主动压缩与继续执行

有至少两轮对话后输入 `/compact`，再发送：

```text
测试代号是什么？再搜索 agents/deepseek.py 中的 MAX_OUTPUT_TOKENS 并说明输出预算。不要读取 .env，不要修改文件。
```

通过条件：摘要保留代号与任务，压缩后继续调用工具并回复。检查本次 `.bear/sessions/<会话ID>.folded-memory.latest.json`，确认是相关结构化摘要；若 task_description 为 `Previous conversation was compacted without structured JSON.`，说明使用失败兜底，不能算辅助接口成功。辅助请求默认明确关闭思考。

压缩后至少再完成一轮对话，然后 `exit`。可在 PowerShell 用下面的只读检查确认本地保存的字段；不打印思考正文、密钥或原始请求：

```powershell
@'
from agents.session import get_latest_session_id, load_session
session_id = get_latest_session_id()
data = load_session(session_id)
assert data and data['metadata']['model'] == 'deepseek-v4-pro'
assert data['metadata']['thinking'] is True
assistants = [m for m in data['openaiMessages'] if m.get('role') == 'assistant']
assert assistants and all(isinstance(m.get('reasoning_content'), str) for m in assistants)
print('Session:', session_id)
print('Assistant messages:', len(assistants))
print('Reasoning characters:', sum(len(m['reasoning_content']) for m in assistants))
print('Saved reasoning fields: OK')
'@ | python -B -
```

通过条件：检查通过，而且思考字符数大于 0。压缩会整体移除旧消息，所以这里检查的是仍保留的 assistant 消息；此前多轮字段回传的正确性由连续调用和离线捕获参数测试共同验证。

## 5. 子 Agent 和 fork Skill

新建或恢复思考会话，发送：

```text
请调用 explore 子 Agent，搜索 agents/deepseek.py 中的思考配置，返回主请求与辅助请求开关的区别，再由你总结。不要读取 .env，不要修改文件，不要调用 MCP。
```

通过条件：子 Agent 正常启动、调用工具、结束，主 Agent 正常接续；子 Agent 的思考文本不会混入它交给主 Agent 的最终回答。

若需真实验收 fork Skill，可由用户在本地创建 `.bear/skills/deepseek-phase2-probe/SKILL.md`（或使用已有的 `context: fork` Skill）。该测试文件内容：

```markdown
---
name: deepseek-phase2-probe
description: DeepSeek phase 2 read-only fork check
context: fork
allowed-tools: read_file, grep_search
---
Search agents/deepseek.py for BEAR_DEEPSEEK_THINKING and explain the main/side thinking settings.
Do not read .env or secrets. Do not edit files, run shell commands, or use MCP.
```

然后在 BearCode 中输入 `/deepseek-phase2-probe`。通过条件：出现 skill-fork 的启动和结束，工具及主模型后续回复正常。该样例属于用户本地测试材料，不需要删除既有 Skill。

## 6. 关闭模式与旧历史

- 用 `--no-thinking` 启动新会话，重复两轮对话和内置工具任务，确认第一阶段关闭模式仍可用。
- 用关闭模式完成一轮并退出后，直接使用 `--thinking --resume` 应提示旧 assistant 缺少 `reasoning_content`，不向接口提交伪造字段。
- 用 `--no-thinking --resume` 恢复该会话并执行 `/compact`；手动压缩会立即保存。退出后以 `--thinking --resume` 恢复摘要，继续工具任务应可用。压缩前仍需满足足够消息数量。

## 7. 中断、输出异常与费用

- 截断、过滤、无效工具 JSON、流中断和旧历史的保护已用离线模拟验证。可选真实中断测试：回复过程中按一次 Ctrl+C，等待终止后再发一个简单问题，确认没有残缺 tool_calls 导致后续错误。
- 若发生输出截断，应看到明确错误且不执行该次工具；可提高 `--max-output-tokens` 或降低思考强度后重试。不要靠重复读取大文件测试截断，因为分段读取仍是独立待改进项。
- `/cost` 显示主请求、辅助请求及子 Agent 的累计用量、缓存与估算费用。默认峰价保守估算，可明确设置 `BEAR_DEEPSEEK_PRICE_PERIOD=off_peak` 使用谷价；不自动推断节假日或实际计费时段。
- `--max-cost` 使用返回用量事后检查，单次请求、并行辅助任务或未返回 usage 的中断请求可能超过或无法准确计入预算，不能作为服务商硬限额。

## 验收记录

记录日期、实际模型和接口、有效主/辅助开关、强度、上述各项结果及必要的脱敏错误信息。不要记录密钥或分享完整思考正文。

第二阶段只有在开启/关闭模式、多轮工具、恢复、压缩及子 Agent 的真实验收完成后才能标为“真实接口验收通过”；离线测试通过不等于该结论。
