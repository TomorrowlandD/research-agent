# 002 MCP 的 Linux 适配

待实施。当前项目的 Context7 和 Playwright 服务使用 Windows 本机的 `npx.cmd` 绝对路径。Docker 构建也会复制这份配置。[服务声明](../../.mcp.json) · [容器入口](../../Dockerfile)

目标是让 WSL Ubuntu 和 Linux 容器使用各自环境中的 Node.js 与 npx 启动这两个服务。验收时，在两个环境中分别通过项目 MCP 客户端连接服务，列出各自工具，退出会话后确认子进程已回收。验收记录沿用编号 002。

