from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
import unittest

from agents.tool_result import ToolExecutionResult
from unittest.mock import AsyncMock, Mock, patch

from agents.main import run_repl, run_one_shot
from agents.mcp_client import McpConnection, McpManager
import agents.mcp_client as module
from fixtures.mcp_test_support import AsyncAuditMixin, eventually, fixture


class McpShutdownTests(AsyncAuditMixin, unittest.IsolatedAsyncioTestCase):
    async def test_close_waits_for_pipes_and_tasks_and_is_repeatable(self):
        async with fixture(self) as f:
            await asyncio.create_task(f.conn.connect())
            pending = asyncio.create_task(f.conn.call_tool("delayed_echo", {"text": "pending", "delay_ms": 30000}))
            try:
                await eventually(lambda: f.request("tools/call", "delayed_echo"))
                await asyncio.gather(f.conn.close(), f.conn.close(), f.conn.close())
                await f.conn.close()
                await f.assert_exited(self)
                with self.assertRaises(RuntimeError):
                    await asyncio.wait_for(pending, 5)
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    await f.conn.call_tool("echo", {"text": "closed"})
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_manager_cleans_other_connections_after_one_failure(self):
        manager = McpManager()
        bad = SimpleNamespace(server_name="bad", close=AsyncMock(side_effect=OSError("failed")))
        good = SimpleNamespace(server_name="good", close=AsyncMock())
        manager._connections = {"bad": bad, "good": good}
        manager._tools = [{}]
        manager._connected = True
        with patch("agents.mcp_client.print_error") as error:
            await manager.disconnect_all()
            await manager.disconnect_all()
        good.close.assert_awaited_once()
        bad.close.assert_awaited_once()
        error.assert_called_once()
        self.assertFalse(manager._connected)
        self.assertEqual(manager._connections, {})
        self.assertEqual(manager._tools, [])

    async def test_initialization_failure_closes_unregistered_connection(self):
        await self._failed_initialization(RuntimeError("initialization failed"))

    async def test_initialization_cancellation_closes_unregistered_connection(self):
        await self._failed_initialization(asyncio.CancelledError())

    async def _failed_initialization(self, error):
        manager = McpManager()
        connection = SimpleNamespace(connect=AsyncMock(), initialize=AsyncMock(side_effect=error),
                                     close=AsyncMock())
        with patch.object(manager, "_load_configs", return_value={"demo": {"command": "unused"}}), \
             patch("agents.mcp_client.McpConnection", return_value=connection), \
             patch("agents.mcp_client.print_error"):
            if isinstance(error, asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await manager.load_and_connect()
            else:
                await manager.load_and_connect()
        connection.close.assert_awaited_once()
        self.assertEqual(manager._connections, {})

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup")
    async def test_real_windows_process_tree_and_pipes_are_closed(self):
        async with fixture(self, "parent-stubborn") as f:
            self.assertGreaterEqual(len(f.owned.states()), 2)
            self.assertTrue(all(not row["exited"] for row in f.owned.states()))
            with patch("agents.mcp_client.print_error") as error:
                await f.conn.close()
            error.assert_not_called()
            await f.assert_exited(self)

    @unittest.skipUnless(os.name == "nt", "Windows inherited-pipe cleanup")
    async def test_parent_exits_before_child_holding_pipes(self):
        async with fixture(self, "parent-exits-child-holds-pipes") as f:
            self.assertEqual(await f.conn.call_tool("exit_server", {"delay_ms": 100}), ToolExecutionResult("exiting"))
            await eventually(lambda: any(row["role"] == "parent_pid" and row["exited"] for row in f.owned.states()))
            rows = f.owned.states()
            self.assertTrue(any(row["role"] == "child_pid" and not row["exited"] for row in rows))
            print("MCP_PARENT_EXIT_BEFORE_CLOSE", rows, flush=True)
            await f.conn.close()
            await f.assert_exited(self)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object startup race")
    async def test_early_descendants_before_job_assignment_are_reaped(self):
        # Simulate a scheduling pause after CreateProcess but before assignment.
        # Only a public OS call is delayed; no SDK private fields are modified.
        import win32job
        assign = win32job.AssignProcessToJobObject
        def delayed_assign(job, process):
            time.sleep(.2)
            assign(job, process)
        with patch.object(win32job, "AssignProcessToJobObject", delayed_assign):
            async with fixture(self, "parent-stubborn") as f:
                self.assertGreaterEqual(len(f.owned.states()), 2)
                await f.conn.close()
                await f.assert_exited(self)

    async def test_initialize_and_discovery_failures_and_timeouts_are_isolated(self):
        real_wait_for = asyncio.wait_for
        for mode in ("init-error", "list-error", "init-delay", "list-delay"):
            with self.subTest(mode=mode):
                async with fixture(self, mode, start=False) as bad, fixture(self, start=False) as good:
                    manager = McpManager()
                    calls = []
                    async def shortened(awaitable, timeout):
                        if (timeout == 15.0 and asyncio.iscoroutine(awaitable)
                                and awaitable.cr_code.co_name in ("initialize", "list_tools")):
                            calls.append((awaitable.cr_code.co_name, timeout))
                            timeout = .5
                        return await real_wait_for(awaitable, timeout)
                    original_good_connect = good.conn.connect
                    async def good_connect_and_capture():
                        await original_good_connect()
                        await good.capture()
                    original_connect = bad.conn.connect
                    async def connect_and_capture():
                        await original_connect()
                        await bad.capture()
                    with patch.object(manager, "_load_configs", return_value={"bad": bad.config, "good": good.config}), \
                         patch("agents.mcp_client.McpConnection", side_effect=[bad.conn, good.conn]), \
                         patch.object(bad.conn, "connect", connect_and_capture), \
                         patch.object(good.conn, "connect", good_connect_and_capture), \
                         patch("agents.mcp_client.asyncio.wait_for", shortened), \
                         patch("agents.mcp_client.print_error") as error:
                        try:
                            await manager.load_and_connect()
                            await good.capture()
                            self.assertEqual(list(manager._connections), ["good"])
                            self.assertEqual(await manager.call_tool("mcp__good__echo", {"text": mode}), ToolExecutionResult(mode))
                            self.assertEqual(len(manager.get_tool_definitions()), 8)
                            expected = ["initialize"] + (["list_tools"] if mode.startswith("list") else []) + ["initialize", "list_tools"]
                            self.assertEqual([name for name, timeout in calls], expected)
                            error.assert_called()
                            await bad.assert_exited(self)
                        finally:
                            await manager.disconnect_all()

    async def test_startup_failure_and_close_before_connect(self):
        empty = McpConnection("empty", "unused")
        await empty.close()
        await empty.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await empty.connect()
        missing = McpConnection("missing", "research-mcp-nonexistent-test-command-38117.exe")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            await asyncio.wait_for(missing.connect(), 8)
        await missing.close()
        self.assertTrue(missing._owner_task.done())

    async def test_startup_cancellation_notifies_ready_and_drains_owner(self):
        original = module.stdio_client
        @asynccontextmanager
        async def delayed_enter(*args, **kwargs):
            async with original(*args, **kwargs) as streams:
                await asyncio.Event().wait()
                yield streams
        async with fixture(self, start=False) as f:
            with patch.object(module, "stdio_client", delayed_enter):
                task = asyncio.create_task(f.conn.connect())
                try:
                    await f.capture()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(f.conn._ready.done())
                    await f.assert_exited(self)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_real_initialization_cancellation_cleans_unregistered_process(self):
        async with fixture(self, "init-delay", start=False) as f:
            manager = McpManager()
            with patch.object(manager, "_load_configs", return_value={"demo": f.config}), \
                 patch("agents.mcp_client.McpConnection", return_value=f.conn):
                task = asyncio.create_task(manager.load_and_connect())
                try:
                    await f.capture()
                    await eventually(lambda: f.request("initialize"))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(manager._connections, {})
                    await f.assert_exited(self)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await manager.disconnect_all()

    async def test_unexpected_exit_releases_inflight_request(self):
        async with fixture(self) as f:
            pending = asyncio.create_task(f.conn.call_tool("delayed_echo", {"text": "never", "delay_ms": 30000}))
            try:
                await eventually(lambda: f.request("tools/call", "delayed_echo"))
                await f.conn.call_tool("exit_server", {"delay_ms": 500})
                with self.assertRaises(RuntimeError):
                    await asyncio.wait_for(pending, 8)
                await f.conn.close()
                await f.assert_exited(self)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_close_cancelled_twice_finishes_cleanup_before_propagation(self):
        async with fixture(self, "parent-stubborn") as f:
            task = asyncio.create_task(f.conn.close())
            await eventually(lambda: f.conn._closing)
            task.cancel()
            await asyncio.sleep(.05)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await f.assert_exited(self)

    async def test_manager_cancelled_twice_finishes_all_cleanup(self):
        async with fixture(self, "parent-stubborn") as first, fixture(self) as second:
            manager = McpManager()
            manager._connections = {"first": first.conn, "second": second.conn}
            manager._tools = [{}]
            manager._connected = True
            task = asyncio.create_task(manager.disconnect_all())
            await eventually(lambda: first.conn._closing and second.conn._closing)
            task.cancel()
            await asyncio.sleep(.05)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await first.assert_exited(self)
            await second.assert_exited(self)
            self.assertEqual(manager._connections, {})
            self.assertEqual(manager.get_tool_definitions(), [])
            self.assertFalse(manager._connected)

    async def test_cleanup_budget_exceeded_is_failure_after_resources_exit(self):
        async with fixture(self, "parent-stubborn") as f:
            with patch.object(module, "MCP_CLOSE_TIMEOUT_SECONDS", .01), patch.object(module, "print_error") as error:
                with self.assertRaisesRegex(TimeoutError, "cleanup exceeded"):
                    await f.conn.close()
                error.assert_called_once()
            await f.assert_exited(self)


class ReplShutdownTests(AsyncAuditMixin, unittest.IsolatedAsyncioTestCase):
    def make_agent(self):
        return SimpleNamespace(set_confirm_fn=Mock(), set_plan_approval_fn=Mock(),
                               drain_background_skill_tasks=AsyncMock(),
                               _mcp_manager=SimpleNamespace(disconnect_all=AsyncMock()),
                               _aborted=True, _output_buffer=None)

    async def exercise_repl(self, agent, input_effect):
        with ExitStack() as stack:
            for name in ("print_welcome", "print_user_prompt", "print_goodbye", "print_warning"):
                stack.enter_context(patch("agents.main." + name))
            stack.enter_context(patch("builtins.input", side_effect=input_effect))
            previous = signal.getsignal(signal.SIGINT)
            await run_repl(agent)
            self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    async def test_normal_exit_cleans_mcp(self):
        agent = self.make_agent()
        await self.exercise_repl(agent, ["exit"])
        agent.drain_background_skill_tasks.assert_awaited_once()
        agent._mcp_manager.disconnect_all.assert_awaited_once()

    async def test_eof_cleans_mcp(self):
        agent = self.make_agent()
        await self.exercise_repl(agent, EOFError())
        agent._mcp_manager.disconnect_all.assert_awaited_once()

    async def test_double_ctrl_c_cleans_mcp(self):
        agent = self.make_agent()

        def interrupted_input():
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            handler(signal.SIGINT, None)

        await self.exercise_repl(agent, interrupted_input)
        agent._mcp_manager.disconnect_all.assert_awaited_once()

    async def test_mcp_cleanup_runs_even_when_background_drain_fails(self):
        agent = self.make_agent()
        agent.drain_background_skill_tasks.side_effect = RuntimeError("drain failed")
        with self.assertRaisesRegex(RuntimeError, "drain failed"):
            await self.exercise_repl(agent, ["exit"])
        agent._mcp_manager.disconnect_all.assert_awaited_once()

    async def test_one_shot_chat_failure_cleans_real_mcp(self):
        async with fixture(self) as f:
            manager = McpManager()
            manager._connections["demo"] = f.conn
            agent = self.make_agent()
            agent.permission_mode = "default"
            agent._mcp_manager = manager
            agent.chat = AsyncMock(side_effect=RuntimeError("chat failed"))
            with self.assertRaisesRegex(RuntimeError, "chat failed"):
                await run_one_shot(agent, "local test")
            agent.drain_background_skill_tasks.assert_not_awaited()
            await f.assert_exited(self)

    async def test_one_shot_drain_failure_and_cancel_still_cleanup(self):
        for failure in (RuntimeError("drain failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                agent = self.make_agent()
                agent.permission_mode = "default"
                agent.chat = AsyncMock()
                agent.drain_background_skill_tasks.side_effect = failure
                with self.assertRaises(type(failure)):
                    await run_one_shot(agent, "local test")
                agent._mcp_manager.disconnect_all.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
