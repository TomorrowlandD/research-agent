from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agents.tool_result import ToolExecutionResult
from unittest.mock import AsyncMock, patch

from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ListToolsResult, Tool, REQUEST_TIMEOUT
from pydantic import ValidationError

from agents.mcp_client import McpConnection, McpManager
from fixtures.mcp_test_support import AsyncAuditMixin, eventually, fixture


class McpConfigTests(unittest.TestCase):
    def test_config_precedence_invalid_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, project = Path(tmp) / "home", Path(tmp) / "project"
            for directory in (home / ".research", project / ".research"):
                directory.mkdir(parents=True)
            (home / ".research/settings.json").write_text(json.dumps({"mcpServers": {
                "same": {"command": "global"}, "global": {"command": "g"}}}))
            (project / ".research/settings.json").write_text(json.dumps({
                "same": {"command": "project"}, "project": {"command": "p"}, "invalid": {}}))
            config = project / ".mcp.json"
            config.write_text(json.dumps({"mcpServers": {"same": {"command": "local", "args": ["中文"]}}}))
            with patch("agents.mcp_client.Path.home", return_value=home), patch("agents.mcp_client.Path.cwd", return_value=project):
                manager = McpManager()
                self.assertEqual(manager._load_configs(), {"same": {"command": "local", "args": ["中文"]},
                                                          "global": {"command": "g"}, "project": {"command": "p"}})
                config.write_text("{broken")
                self.assertEqual(manager._load_configs()["same"]["command"], "project")
                config.unlink()
                self.assertEqual(manager._load_configs()["same"]["command"], "project")

    def test_ambiguous_service_name_is_rejected_without_losing_valid_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".mcp.json"
            path.write_text(json.dumps({"mcpServers": {"a__b": {"command": "x"}, "ok": {"command": "y"}}}))
            merged = {}
            with patch("agents.mcp_client.print_error") as report:
                McpManager()._merge_config_file(path, merged)
            self.assertEqual(merged, {"ok": {"command": "y"}})
            report.assert_called_once()

    def test_definitions_defaults_and_empty(self):
        manager = McpManager()
        self.assertEqual(manager.get_tool_definitions(), [])
        manager._tools = [{"name": "a__b", "serverName": "demo", "description": None, "inputSchema": {}}]
        self.assertEqual(manager.get_tool_definitions(), [{"name": "mcp__demo__a__b",
                         "description": "MCP tool a__b from demo", "input_schema": {"type": "object", "properties": {}}}])
        self.assertTrue(manager.is_mcp_tool("mcp__demo__a__b"))
        self.assertFalse(manager.is_mcp_tool("echo"))


class McpAdapterTests(AsyncAuditMixin, unittest.IsolatedAsyncioTestCase):
    async def test_no_configuration_starts_no_connection(self):
        manager = McpManager()
        with patch.object(manager, "_load_configs", return_value={}), patch("agents.mcp_client.McpConnection") as factory:
            await manager.load_and_connect()
            await manager.load_and_connect()
            factory.assert_not_called()
        await manager.disconnect_all()

    async def test_connect_timeout_releases_owner_before_return(self):
        exited = asyncio.Event()

        @asynccontextmanager
        async def stalled_stdio(*args, **kwargs):
            try:
                await asyncio.Event().wait()
                yield
            finally:
                exited.set()

        import sys
        conn = McpConnection("stalled", sys.executable)
        with patch("agents.mcp_client.stdio_client", stalled_stdio), \
             patch("agents.mcp_client.MCP_CONNECT_TIMEOUT_SECONDS", .03):
            with self.assertRaisesRegex(TimeoutError, "stalled.*connect timed out"):
                await asyncio.wait_for(conn.connect(), 1)
        self.assertTrue(exited.is_set())
        self.assertTrue(conn._owner_task.done())

    async def test_hard_cleanup_deadline_does_not_cancel_sdk_owner(self):
        from agents.mcp_client import _await_cleanup
        future = asyncio.get_running_loop().create_future()
        with patch("agents.mcp_client.MCP_CLOSE_TIMEOUT_SECONDS", .01), \
             patch("agents.mcp_client.MCP_CLEANUP_DEADLINE_SECONDS", .04), \
             patch("agents.mcp_client.print_error") as report:
            with self.assertRaisesRegex(TimeoutError, "hard deadline"):
                await asyncio.wait_for(_await_cleanup(future), 1)
        self.assertEqual(report.call_count, 2)
        self.assertFalse(future.done())
        future.set_result(None)

    async def test_schema_mapping_is_exact_and_empty_supported(self):
        schema = {"type": "object", "properties": {"q": {"type": "string", "enum": ["中", "x"]}},
                  "required": ["q"], "additionalProperties": False}
        session = SimpleNamespace(list_tools=AsyncMock(return_value=ListToolsResult(tools=[
            Tool(name="find", input_schema=schema)])))
        conn = McpConnection("demo", "unused")
        with patch.object(conn, "_get_session", return_value=session):
            self.assertEqual(await conn.list_tools(), [{"name": "find", "description": "", "inputSchema": schema, "serverName": "demo"}])
            session.list_tools.return_value = ListToolsResult(tools=[])
            self.assertEqual(await conn.list_tools(), [])

    async def test_route_preserves_arguments_and_double_underscores(self):
        conn = SimpleNamespace(call_tool=AsyncMock(return_value=ToolExecutionResult("ok")))
        manager = McpManager()
        manager._connections["demo"] = conn
        args = {"nested": {"x": 1}}
        self.assertEqual(await manager.call_tool("mcp__demo__a__b", args), ToolExecutionResult("ok"))
        conn.call_tool.assert_awaited_once_with("a__b", args)
        with self.assertRaises(ValueError):
            await manager.call_tool("invalid", {})
        with self.assertRaisesRegex(RuntimeError, "missing.*not connected"):
            await manager.call_tool("mcp__missing__x", {})

    async def test_protocol_errors_timeout_validation_and_cancel_mapping(self):
        try:
            CallToolResult.model_validate({"content": [{"type": "text"}]})
        except ValidationError as exc:
            validation = exc
        cases = [(MCPError(-32603, "protocol failure"), RuntimeError, "demo.*ping.*-32603.*protocol failure"),
                 (MCPError(REQUEST_TIMEOUT, "deadline"), TimeoutError, "demo.*ping.*deadline"),
                 (validation, RuntimeError, "demo.*ping.*ValidationError")]
        conn = McpConnection("demo", "unused")
        session = SimpleNamespace(call_tool=AsyncMock())
        with patch.object(conn, "_get_session", return_value=session):
            for error, cls, message in cases:
                with self.subTest(error=type(error).__name__):
                    session.call_tool.side_effect = error
                    with self.assertRaisesRegex(cls, message) as caught:
                        await conn.call_tool("ping", {})
                    self.assertIs(caught.exception.__cause__, error)
            session.call_tool.side_effect = None
            session.call_tool.return_value = {"content": []}
            with self.assertRaisesRegex(RuntimeError, "demo.*ping.*Unexpected tool result"):
                await conn.call_tool("ping", {})
            session.call_tool.side_effect = asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                await conn.call_tool("ping", {})


class McpToolTimeoutTests(AsyncAuditMixin, unittest.IsolatedAsyncioTestCase):
    async def test_text_content_and_tool_errors(self):
        async with fixture(self) as f:
            for case, expected in [("multi", "first\nsecond"), ("mixed", "first\nsecond"), ("empty", ""), ("nontext", "")]:
                with self.subTest(case=case):
                    self.assertEqual(await f.conn.call_tool("content_case", {"case": case}), ToolExecutionResult(expected))
            for args, message in [({"message": "deliberate"}, "deliberate"), ({"empty": True}, "without text")]:
                result = await f.conn.call_tool("tool_error", args)
                self.assertFalse(result.ok)
                self.assertTrue(result.is_error)
                self.assertIn(message, result.content)
                self.assertEqual(result.meta["error_type"], "mcp_tool_error")
            self.assertEqual(await f.conn.call_tool("echo", {"text": "after errors"}), ToolExecutionResult("after errors"))

    async def test_timeout_cleans_pending_request_and_next_call_can_succeed(self):
        # Historical test name retained; assertion uses observable session reuse.
        async with fixture(self, "ignore-cancel") as f:
            manager = McpManager()
            manager._connections["demo"] = f.conn
            session = f.conn._session
            with patch("agents.mcp_client.MCP_TOOL_TIMEOUT_SECONDS", .15):
                with self.assertRaisesRegex(TimeoutError, "mcp__demo__delayed_echo.*timed out after 0.15 seconds"):
                    await manager.call_tool("mcp__demo__delayed_echo", {"text": "LATE", "delay_ms": 500})
            request = f.request("tools/call", "delayed_echo")
            self.assertIsNotNone(request)
            self.assertEqual(await manager.call_tool("mcp__demo__echo", {"text": "before late"}), ToolExecutionResult("before late"))
            await eventually(lambda: any(row["discarded"] for row in f.events()))
            await eventually(lambda: any(row["direction"] == "sent" and row["wire"].get("id") == request["id"] for row in f.events()))
            sent = next(row["wire"] for row in f.events() if row["direction"] == "sent" and row["wire"].get("id") == request["id"])
            self.assertEqual(sent["result"]["content"][0]["text"], "LATE")
            self.assertEqual(await manager.call_tool("mcp__demo__echo", {"text": "after late"}), ToolExecutionResult("after late"))
            self.assertIs(f.conn._session, session)
            print("MCP_LATE_RESPONSE " + json.dumps({"request_id": request["id"], "cancel_discarded": True,
                                                     "late_response_sent": True, "same_session_reused": True}))

    async def test_cancel_propagates_and_same_session_is_reusable(self):
        async with fixture(self) as f:
            manager = McpManager()
            manager._connections["demo"] = f.conn
            session = f.conn._session
            task = asyncio.create_task(manager.call_tool("mcp__demo__delayed_echo", {"text": "cancel", "delay_ms": 30000}))
            try:
                await eventually(lambda: f.request("tools/call", "delayed_echo"))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(await manager.call_tool("mcp__demo__echo", {"text": "reused"}), ToolExecutionResult("reused"))
                self.assertIs(f.conn._session, session)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_environment_inheritance_override_and_stderr_pressure(self):
        with tempfile.TemporaryDirectory() as temp, patch("agents.mcp_client.Path.home", return_value=Path(temp)), \
             patch.dict(os.environ, {"RESEARCH_MCP_TEST_INHERITED": "parent", "RESEARCH_MCP_TEST_OVERRIDE": "original"}):
            async with fixture(self, env={"RESEARCH_MCP_TEST_OVERRIDE": "child"}) as f:
                self.assertEqual(json.loads((await f.conn.call_tool("read_test_env", {})).content),
                                 {"RESEARCH_MCP_TEST_INHERITED": "parent", "RESEARCH_MCP_TEST_OVERRIDE": "child"})
                self.assertEqual(await asyncio.wait_for(f.conn.call_tool("stderr_burst", {"bytes_count": 1048576}), 8), ToolExecutionResult("ok"))
                self.assertEqual(await f.conn.call_tool("echo", {"text": "after stderr"}), ToolExecutionResult("after stderr"))
            log = Path(temp) / ".research" / "logs" / f"mcp-{f.conn.server_name}.stderr.log"
            self.assertGreaterEqual(log.stat().st_size, 1048576)



if __name__ == "__main__":
    unittest.main()
