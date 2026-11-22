# 工具执行、权限与 MCP

**核心结论。** 内置工具和 MCP 工具都在模型侧声明参数结构，运行时先校验参数与权限，再按名称分派。主 Agent 首次对话时发现 MCP 服务，将其工具注册为 `mcp__服务名__工具名`；执行时由 MCP 客户端路由到对应 stdio 服务。内置文件工具还会在执行时检查文件状态。[参数校验与权限入口](../../agents/agent.py#L1959) · [注册](../../agents/agent.py#L515) · [分派](../../agents/agent.py#L1317) · [MCP 路由](../../agents/mcp_client.py#L309)

| 关键判断 | 实际行为 |
| --- | --- |
| 谁决定允许或拒绝 | `bypassPermissions` 直接放行；其他模式先处理显式拒绝，再执行 Plan Mode 限制，然后才处理显式允许和模式默认值。Plan Mode 拦截 Shell、MCP 和普通文件编辑，允许编辑指定计划文件。[权限决策](../../agents/tools.py#L673) |
| 审批能否复用 | 默认模式且无匹配规则时，Shell 和文件写改每次确认；MCP 的确认结果按提示文本缓存在当前 Agent，同一 MCP 工具后续可复用批准。危险命令匹配函数未见接入审批路径。[确认缓存](../../agents/agent.py#L2173) · [危险命令匹配](../../agents/tools.py#L596) |
| 文件获批后能否写入 | 新文件可以直接创建。改写已有文件要求当前 Agent 先读过，且磁盘 mtime 与读取记录一致；发生变化需重读。此检查覆盖内置写改工具。[文件状态检查](../../agents/tools.py#L751) |

**运行验证 · 2026-11-22。** WSL Ubuntu、Python 3.11.18，权限、工具参数、MCP 客户端与退出四组 unittest 共 43 项：40 项通过，3 项 Windows 专属用例跳过。本地 stdio 测试服务覆盖路由、超时、取消与清理。另用临时文件验证了未读先写被拒、外部修改后写入被拒、重读后写入成功。[权限](../../tests/test_permissions.py) · [参数](../../tests/test_tool_arguments.py) · [MCP 客户端](../../tests/test_mcp_client.py) · [退出清理](../../tests/test_mcp_shutdown.py)

**Web 接入评估。** 参数校验、单次权限判断和工具分派可复用。权限规则缓存位于进程全局，文件读取记录与批准缓存位于 Agent 实例。共享服务需要显式绑定项目规则和会话身份，避免不同项目沿用同一份规则。[规则加载与缓存](../../agents/tools.py#L618)
