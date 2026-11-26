"""
MCP 客户端模块。

这个文件负责连接基于 stdio 的 MCP Server，完成工具发现，并把 Agent 发起的工具调用
转发到对应的 MCP Server。

实现方式：
- 使用官方 MCP Python SDK v2 管理 stdio 协议、会话与进程清理。
- 每个 MCP Server 都由一个子进程承载。
- 每个 MCP 工具都会被包装成 `mcp__serverName__toolName` 形式，避免和本地工具重名。

配置来源：
- 全局配置：`~/.research/settings.json`
- 项目配置：`.research/settings.json`
- Claude Code 约定配置：`.mcp.json`

配置格式示例：
{
    "mcpServers": {
        "name": {
            "command": "...",
            "args": [...],
            "env": {...}
        }
    }
}
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

from .paths import RESEARCH_DIRNAME, workspace_dir
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Implementation, REQUEST_TIMEOUT, TextContent

from .tool_result import ToolExecutionResult
from .ui import print_error, print_info

MCP_TOOL_TIMEOUT_SECONDS = 60.0
MCP_CONNECT_TIMEOUT_SECONDS = 20.0
MCP_CLOSE_TIMEOUT_SECONDS = 15.0
MCP_CLEANUP_DEADLINE_SECONDS = 30.0


# ─── 单个 MCP 连接：一个 McpConnection 对应一个 MCP Server 子进程 ──────────────────


async def _await_cleanup(future: asyncio.Future, *, started: float | None = None) -> Any:
    """Wait across caller cancellations, bounded by a single monotonic deadline."""
    cancelled = None
    overdue = False
    clock = asyncio.get_running_loop().time
    start = clock() if started is None else started
    while not future.done():
        budget = MCP_CLEANUP_DEADLINE_SECONDS if overdue else min(
            MCP_CLOSE_TIMEOUT_SECONDS, MCP_CLEANUP_DEADLINE_SECONDS
        )
        remaining = start + budget - clock()
        if remaining <= 0:
            if overdue:
                message = f"MCP cleanup hard deadline exceeded ({budget:g} seconds)"
                print_error(message)
                # The SDK owner still owns its context managers and may exit later.
                future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                if cancelled is not None:
                    raise cancelled
                raise TimeoutError(message)
            overdue = True
            print_error(f"MCP cleanup exceeded {MCP_CLOSE_TIMEOUT_SECONDS:g} seconds; still waiting for resource owner")
            continue
        try:
            await asyncio.wait({future}, timeout=remaining)
        except asyncio.CancelledError as exc:
            cancelled = exc
    try:
        result = future.result()
    except BaseException as exc:
        if cancelled is not None:
            print_error(f"MCP cleanup failed during cancellation: {exc}")
            raise cancelled from exc
        raise
    if cancelled is not None:
        raise cancelled
    if overdue:
        raise TimeoutError(f"MCP cleanup exceeded {MCP_CLOSE_TIMEOUT_SECONDS:g} seconds")
    return result


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_error_text(child) for child in exc.exceptions)
    return str(exc) or type(exc).__name__


class McpConnection:
    """One stdio SDK session, entered and exited by its own asyncio task."""

    def __init__(self, server_name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._session: ClientSession | None = None
        self._owner_task: asyncio.Task | None = None
        self._owner_scope: anyio.CancelScope | None = None
        self._ready: asyncio.Future | None = None
        self._stop: asyncio.Event | None = None
        self._closing = False
        self._close_started: float | None = None
        self._was_ready = False
        self._owner_error: BaseException | None = None

    async def connect(self) -> None:
        """Start once; concurrent callers share readiness, not resource ownership."""
        if self._closing:
            raise RuntimeError(f"MCP server '{self.server_name}' is closed")
        if self._owner_task is None:
            self._ready = asyncio.get_running_loop().create_future()
            self._stop = asyncio.Event()
            self._owner_task = asyncio.create_task(self._run_owner(), name=f"mcp-owner:{self.server_name}")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), MCP_CONNECT_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            await self.close()
            raise TimeoutError(f"MCP server '{self.server_name}' connect timed out after "
                               f"{MCP_CONNECT_TIMEOUT_SECONDS:g} seconds") from exc
        except asyncio.CancelledError:
            try:
                await self.close()
            except Exception as exc:
                print_error(f"MCP cleanup failed: {self.server_name}: {exc}")
            raise
        self._get_session()

    async def _run_owner(self) -> None:
        """Never transfer an AnyIO context manager to another task."""
        try:
            if self._closing:
                return
            with anyio.CancelScope() as scope:
                self._owner_scope = scope
                env = {**os.environ, **self.env}
                command = self.command
                if not os.path.isabs(command):
                    command = shutil.which(command, path=env.get("PATH"))
                    if command is None:
                        raise FileNotFoundError(f"Command not found: {self.command}")
                params = StdioServerParameters(command=command, args=self.args, env=env)
                log_dir = Path.home() / RESEARCH_DIRNAME / "logs"
                log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                log_path = log_dir / f"mcp-{quote(self.server_name, safe='')}.stderr.log"
                with log_path.open("a", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=None,
                                                 client_info=Implementation(name="mini-claude", version="1.0.0")) as session:
                            self._session = session
                            self._was_ready = True
                            self._ready.set_result(None)
                            await self._stop.wait()
        except (Exception, asyncio.CancelledError) as exc:
            # Consume background failures here; connect/operations/close expose
            # the stored cause, without an unobserved Task/Future exception.
            self._owner_error = exc
        finally:
            self._session = None
            self._owner_scope = None
            if not self._ready.done():
                self._ready.set_result(None)

    def _get_session(self) -> ClientSession:
        if self._closing or self._session is None or self._owner_task.done():
            reason = _error_text(self._owner_error) if self._owner_error else "not connected or closed"
            raise RuntimeError(f"MCP server '{self.server_name}': {reason}") from self._owner_error
        return self._session

    @asynccontextmanager
    async def _operation(self, operation: str):
        """Translate SDK failures without swallowing caller cancellation."""
        try:
            yield self._get_session()
        except MCPError as exc:
            message = f"MCP server '{self.server_name}' {operation}: MCP error {exc.code}: {exc.message}"
            if exc.code == REQUEST_TIMEOUT:
                raise TimeoutError(message) from exc
            raise RuntimeError(message) from exc
        except TimeoutError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"MCP server '{self.server_name}' {operation}: {type(exc).__name__}: {_error_text(exc)}"
            ) from exc

    async def initialize(self) -> None:
        async with self._operation("initialize") as session:
            await session.initialize()

    async def list_tools(self) -> list[dict]:
        async with self._operation("list_tools") as session:
            result = await session.list_tools()
            return [
                {"name": tool.name, "description": tool.description or "",
                 "inputSchema": tool.input_schema, "serverName": self.server_name}
                for tool in result.tools
            ]

    async def call_tool(self, name: str, args: dict) -> ToolExecutionResult:
        async with self._operation(f"call_tool '{name}'") as session:
            result = await session.call_tool(name, arguments=args)
            if not isinstance(result, CallToolResult):
                raise TypeError(f"Unexpected tool result: {type(result).__name__}")
            text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
            if result.is_error:
                return ToolExecutionResult.failure(text or "Tool reported failure without text", "mcp_tool_error")
            return ToolExecutionResult(text)

    async def close(self) -> None:
        """Signal the owner, then wait for actual completion across cancellation."""
        self._closing = True
        if self._owner_task is None:
            return
        if self._close_started is None:
            self._close_started = asyncio.get_running_loop().time()
        self._stop.set()
        if not self._ready.done() and self._owner_scope is not None:
            # Only cancel the startup scope; never Task.cancel() SDK teardown.
            self._owner_scope.cancel()
        await _await_cleanup(self._owner_task, started=self._close_started)
        if self._was_ready and self._owner_error is not None:
            raise RuntimeError(f"MCP server '{self.server_name}' lifecycle failed: {_error_text(self._owner_error)}") from self._owner_error


# ─── MCP 管理器：统一管理多个 MCP Server 连接和工具路由 ─────────────────────────────


class McpManager:
    """
    管理所有 MCP Server 连接。

    使用方式：
    1. 调用 load_and_connect() 读取配置、连接 Server、发现工具。
    2. 调用 get_tool_definitions() 把 MCP 工具暴露给模型。
    3. 当模型调用 mcp__server__tool 形式的工具时，用 call_tool() 路由到对应 Server。
    """

    def __init__(self):
        # 已连接的 MCP Server。key 是 server name，value 是对应连接对象。
        self._connections: dict[str, McpConnection] = {}
        self._role = "main"
        self._allowed: dict[str, set[str]] = {}
        # 所有 MCP Server 发现出来的工具定义，保持接近 MCP 原始格式。
        self._tools: list[dict] = []
        # 防止重复连接。load_and_connect() 只应真正执行一次。
        self._connected = False

    async def load_and_connect(self, role: str = "main", *, permitted_tools: set[str] | None = None) -> None:
        """读取配置，连接所有配置的 MCP Server，并发现它们提供的工具。"""
        if self._connected:
            return
        self._connected = True
        self._role = role

        # 合并全局、项目和 .mcp.json 配置。后读取的配置会覆盖同名 Server。
        configs = self._load_configs()
        if not configs:
            return

        # 每个 Server 的初始化和工具发现最多等待 15 秒，避免坏配置长期卡住启动。
        timeout = 15.0

        for name, cfg in configs.items():
            roles = cfg.get("roles", ["main"])
            allowed = cfg.get("allowed_tools")
            if any(key in cfg and (not isinstance(cfg[key], list) or
                   not all(isinstance(item, str) for item in cfg[key])) for key in ("roles", "allowed_tools")):
                print_error(f"Invalid roles/allowed_tools for MCP server {name}")
                continue
            if role not in roles:
                continue
            if permitted_tools is not None:
                prefix = f"mcp__{name}__"
                permitted = {tool[len(prefix):] for tool in permitted_tools if tool.startswith(prefix)}
                allowed = permitted if allowed is None else permitted.intersection(allowed)
            if allowed is not None and not allowed:
                continue
            # 根据配置创建一个独立连接对象。
            conn = McpConnection(
                name,
                cfg["command"],
                cfg.get("args"),
                cfg.get("env"),
            )
            try:
                # 连接子进程 -> MCP 初始化握手 -> 查询工具列表。
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                # 只有初始化和工具发现都成功，才登记为可用连接。
                if allowed is not None:
                    server_tools = [tool for tool in server_tools if tool["name"] in allowed]
                    self._allowed[name] = {tool["name"] for tool in server_tools}
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print_info(f"MCP connected: {name} ({len(server_tools)} tools)")
            except asyncio.CancelledError:
                await self._close_failed_connection(conn)
                raise
            except Exception as e:
                # 单个 Server 失败不影响其他 Server。失败连接需要关闭以清理子进程。
                print_error(f"MCP failed to connect: {name}: {e}")
                await self._close_failed_connection(conn)

    async def _close_failed_connection(self, conn: McpConnection) -> None:
        # The connection is not yet registered. Preserve the original failure,
        # but still finish its cleanup if the loading task is cancelled again.
        try:
            await _await_cleanup(asyncio.create_task(conn.close()))
        except Exception as exc:
            print_error(f"MCP cleanup failed: {conn.server_name}: {exc}")

    def get_tool_definitions(self, role: str | None = None) -> list[dict]:
        """返回 Agent/Anthropic 可直接使用的工具定义，并给 MCP 工具名加前缀。"""
        if role is not None and role != self._role:
            return []
        return [
            {
                # 前缀格式：mcp__服务名__工具名。
                # 这样可以避免 MCP 工具和内置工具重名，也方便 call_tool() 反向解析路由。
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                # Anthropic 工具字段叫 input_schema；MCP 原始工具字段一般叫 inputSchema。
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        """判断一个工具名是否是 MCP 工具名。"""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> ToolExecutionResult:
        """把带前缀的 MCP 工具调用路由到正确的 MCP Server。"""
        # 工具名格式为 mcp__serverName__toolName。
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        # 工具名本身也可能包含 "__"，所以第 3 段之后要重新拼回去。
        tool_name = "__".join(parts[2:])  # tool name might contain __
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        if server_name in self._allowed and tool_name not in self._allowed[server_name]:
            raise ValueError(f"Tool is not allowed for {self._role}: {prefixed_name}")
        try:
            return await asyncio.wait_for(
                conn.call_tool(tool_name, args), timeout=MCP_TOOL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"MCP tool '{prefixed_name}' timed out after {MCP_TOOL_TIMEOUT_SECONDS:g} seconds"
            ) from exc

    async def disconnect_all(self) -> None:
        """断开所有 MCP Server 连接，并清空工具缓存。"""
        connections = list(self._connections.values())
        self._connections.clear()
        self._allowed.clear()
        self._tools.clear()
        self._connected = False
        # 并行清理，单个连接出错也不跳过其他连接。
        cleanup = asyncio.gather(*(conn.close() for conn in connections), return_exceptions=True)
        try:
            await _await_cleanup(cleanup)
        finally:
            if cleanup.done() and not cleanup.cancelled():
                for conn, result in zip(connections, cleanup.result()):
                    if isinstance(result, BaseException):
                        print_error(f"MCP cleanup failed: {conn.server_name}: {result}")

    # ─── 配置加载 ──────────────────────────────────────

    def _load_configs(self) -> dict[str, dict]:
        """按优先级加载并合并 MCP Server 配置。"""
        merged: dict[str, dict] = {}

        # 1. 全局配置：~/.research/settings.json
        global_path = Path.home() / RESEARCH_DIRNAME / "settings.json"
        self._merge_config_file(global_path, merged)

        # 2. 当前项目配置：<cwd>/.research/settings.json
        project_path = workspace_dir() / RESEARCH_DIRNAME / "settings.json"
        self._merge_config_file(project_path, merged)

        # 3. Claude Code 约定配置：<cwd>/.mcp.json
        mcp_json_path = workspace_dir() / ".mcp.json"
        self._merge_config_file(mcp_json_path, merged)

        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        """把单个配置文件里的 mcpServers 合并进 target。"""
        if not path.exists():
            return
        try:
            # 明确使用 UTF-8，避免 Windows 默认编码无法读取配置中的中文说明。
            raw = json.loads(path.read_text(encoding="utf-8"))
            # 支持两种格式：
            # 1. {"mcpServers": {"name": {...}}}
            # 2. {"name": {...}}
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                if "__" in name or not name:
                    print_error(f"Invalid MCP server name in {path}: {name!r}")
                    continue
                # 只接收包含 command 的对象；无效配置直接忽略。
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception:
            # 配置文件格式错误时跳过，避免一个坏配置导致整个 Agent 启动失败。
            pass
