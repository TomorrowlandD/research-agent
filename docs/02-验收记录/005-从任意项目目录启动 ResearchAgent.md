# 005 从任意项目目录启动 ResearchAgent

2026-11-27，在 WSL Ubuntu、Python 3.11.18 下验收。安装测试使用临时环境和源码树外的临时项目。历史测试使用临时用户目录，测试任务、文件标记和会话均为合成数据。

## 第一次提交 · 安装和启动

- 在提取的干净源码上运行 `python -s -B -m unittest discover -s tests -v`。共 161 项，158 项通过，3 项 Windows 专用测试跳过。
- 在两套继承已安装依赖的临时环境分别运行 `python -m pip install --no-index --no-deps --no-build-isolation <源码目录>` 及其 `-e` 版本。移除 `PYTHONPATH`，从临时项目运行安装的 `researchagent`，均能进入对话并正常退出。两种安装均包含 Web 静态资源，缺少任务的 `-p` 均以状态码 2 退出。
- 通过已配置的 DeepSeek 接口实测普通安装的交互首轮。目标项目的 `CLAUDE.md` 指定标记 `HARBOUR-417`；批准写入后，目标项目中 `receipt.txt` 内容与标记相符。追问后，同一会话末条回复包含该标记。可编辑安装运行 `researchagent -p` 完成另一条合成任务后以 0 退出。
- 测试覆盖环境、项目和全局配置的别名优先级，以及旧模块入口的位置参数行为。

## 第二次提交 · 继续和选择历史会话

- 使用两个临时项目、OpenAI 与 Anthropic 两种协议构造快照。验证列表按更新时间倒序，只包含相应项目与协议的历史；非法编号可以重试，空列表、取消、参数冲突和旧版快照显式恢复均有覆盖。
- 在第一次验收留下的两条真实会话上运行 `researchagent -c -p "…"`，比较前后快照，只有最近会话被更新。运行 `researchagent -r` 选中另一条历史并追问，模型恢复了先前写入文件的标记；随后以 `--resume <ID> -p "…"` 完成单次任务。三次调用均正常退出，会话 ID 集合保持一致。
- 在干净源码快照、仓库根目录分别运行 `python -s -B -m unittest discover -s tests -v`。前者 163 项，160 项通过；后者包含工作树中已有的 REPL 测试，共 171 项，168 项通过。两次各跳过 3 项 Windows 专用的 MCP 进程清理测试。
- 在开发机现有 `researchAgent` 环境以可编辑模式安装，从源码树外移除 `PYTHONPATH` 后执行 `researchagent --help`，确认安装的入口提供两阶段命令。

真实模型验收使用 DeepSeek 已配置接口。双协议隔离通过本地测试验证。原生 Windows 终端、Windows 进程清理与外部 MCP 服务未在此次 WSL 验收中实测。
