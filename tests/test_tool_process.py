import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from agents.agent import Agent
from agents.paths import use_workspace
from agents.session import load_session
from agents.tools import execute_tool
from agents.tool_result import ToolFailure
from test_agent_events import response


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process group and /proc checks")
class ToolProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(use_workspace(self.root))
        self.enterContext(patch("pathlib.Path.home", return_value=self.root))
        self.sessions = self.root / "sessions"
        self.enterContext(patch("agents.session.SESSION_DIR", self.sessions))
        self.enterContext(patch.dict(os.environ, {"RESEARCH_TOOL_MODE": "live"}))
        self.enterContext(patch("agents.agent.start_spinner"))
        self.enterContext(patch("agents.agent.stop_spinner"))
        (self.root / "worker.py").write_text(
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, lambda *_: Path('term').touch())\n"
            "Path('child').write_text(str(os.getpid()))\n"
            "time.sleep(20)\n"
        )
        self.command = f"echo $$ > leader; {shlex.quote(sys.executable)} worker.py & wait"

    async def assert_group_reaped(self):
        leader = int((self.root / "leader").read_text())
        child = int((self.root / "child").read_text())
        self.assertTrue((self.root / "term").is_file())
        with self.assertRaises(ChildProcessError):
            os.waitpid(leader, os.WNOHANG)
        await until(lambda: not Path(f"/proc/{child}").exists())
        with self.assertRaises(ProcessLookupError):
            os.killpg(leader, 0)

    async def test_normal_exit_cleans_background_descendant_with_inherited_output(self):
        cmd = self.command.removesuffix("wait") + "while [ ! -s child ]; do sleep 0.01; done; echo complete"
        self.assertEqual(await execute_tool("run_shell", {"command": cmd, "timeout": 3000}), "complete\n")
        await self.assert_group_reaped()

    async def test_cancel_during_launch_then_again_during_cleanup(self):
        launched = asyncio.Event()
        handoff = asyncio.Event()
        spawn = asyncio.create_subprocess_shell

        async def delayed_launch(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            launched.set()
            await handoff.wait()
            return process

        with patch("agents.tool_process.asyncio.create_subprocess_shell", delayed_launch):
            task = asyncio.create_task(execute_tool("run_shell", {"command": self.command}))
            try:
                await asyncio.wait_for(launched.wait(), 5)
                await until(lambda: (self.root / "child").exists())
                task.cancel()
                handoff.set()
                await until(lambda: (self.root / "term").exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            finally:
                handoff.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.assert_group_reaped()

    async def test_native_grep_and_shell_result_contracts(self):
        (self.root / "sample.txt").write_text("match\n" * 105)
        result = await execute_tool("grep_search", {"pattern": "match", "include": "*.txt"})
        self.assertIn("sample.txt:1:match", result)
        self.assertTrue(result.endswith("... and 5 more matches"))
        self.assertEqual(await execute_tool("grep_search", {"pattern": "absent"}), "No matches found.")
        self.assertEqual(await execute_tool("run_shell", {"command": "true"}), "(no output)")
        with self.assertRaisesRegex(ToolFailure, r"(?s)exit code 7.*Stdout: out.*Stderr: err"):
            await execute_tool("run_shell", {"command": "echo out; echo err >&2; exit 7"})
        result = await execute_tool("run_shell", {"command": "head -c 70000 /dev/zero | tr '\\0' x"})
        self.assertIn("truncated", result)
        self.assertLessEqual(len(result), 50000)

    async def test_native_grep_cancel_reaps_process(self):
        fifo = self.root / "input"
        os.mkfifo(fifo)
        started = asyncio.get_running_loop().create_future()
        spawn = asyncio.create_subprocess_exec

        async def observe(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            started.set_result(process)
            return process

        with patch("agents.tool_process.asyncio.create_subprocess_exec", observe):
            task = asyncio.create_task(execute_tool("grep_search", {"pattern": "data", "path": str(fifo)}))
            try:
                process = await asyncio.wait_for(started, 5)
                self.assertIsNone(process.returncode)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    async def test_grep_timeout_does_not_restart_the_scan(self):
        async def timeout(*args, **kwargs):
            raise TimeoutError

        with (patch("agents.tools.run_tool_process", timeout),
              patch("agents.tools._grep_python") as fallback):
            with self.assertRaisesRegex(TimeoutError, "10000ms"):
                await execute_tool("grep_search", {"pattern": "data"})
            fallback.assert_not_called()

    def agent(self, protocol):
        return Agent(api_base="https://example.invalid" if protocol == "openai" else None,
                     api_key="test", permission_mode="bypassPermissions", custom_system_prompt="test",
                     enable_online_evolution=False, enable_mcp=False, enable_memory_prefetch=False)

    async def test_cancel_during_approval_records_each_call_once(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                approval = asyncio.Event()
                agent = self.agent(protocol)
                agent.permission_mode = "default"

                async def confirm(message):
                    approval.set()
                    await asyncio.Future()

                agent.set_confirm_fn(confirm)
                calls = [("pending", {"command": "echo ok"}), ("later", {"file_path": "probe"})]
                batch = response(protocol, calls)
                if protocol == "openai":
                    batch["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "run_shell"
                else:
                    batch.content[0].name = "run_shell"
                with (patch.object(agent, f"_call_{protocol}_stream", return_value=batch),
                      patch.object(agent, "_execute_tool_call") as execute):
                    turn = asyncio.create_task(agent.chat("request approval"))
                    try:
                        await asyncio.wait_for(approval.wait(), 5)
                    finally:
                        turn.cancel()
                        await asyncio.gather(turn, return_exceptions=True)
                    self.assertTrue(turn.cancelled())
                    execute.assert_not_called()
                results = self.logged_results(agent.session_id)
                self.assertEqual([r["tool_call_id"] for r in results], ["pending", "later"])
                self.assertTrue(all(r["is_error"] for r in results))

    def logged_results(self, session_id):
        path = self.sessions / f"{session_id}.events.jsonl"
        events = map(json.loads, path.read_text().splitlines())
        return [event["payload"] for event in events if event["event_type"] == "tool_result"]

    async def check_turn(self, protocol, action):
        agent = self.agent(protocol)
        (self.root / "probe").write_text("kept")
        for marker in ("leader", "child", "term"):
            (self.root / marker).unlink(missing_ok=True)
        calls = [("before", {"file_path": "probe"}),
                 ("shell", {"command": self.command, "timeout": 1000 if action == "timeout" else 10000}),
                 ("after", {"file_path": "probe"})]
        batch = response(protocol, calls)
        if protocol == "openai":
            batch["choices"][0]["message"]["tool_calls"][1]["function"]["name"] = "run_shell"
        else:
            batch.content[1].name = "run_shell"
        with patch.object(agent, f"_call_{protocol}_stream", side_effect=[batch, response(protocol)]):
            task = asyncio.create_task(agent.chat("run tools"))
            try:
                await until(lambda: (self.root / "child").is_file())
                if action == "cancel":
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                else:
                    if action == "abort":
                        agent.abort()
                    await asyncio.wait_for(task, 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.assert_group_reaped()
        self.assertEqual(agent._aborted, action != "timeout")
        results = self.logged_results(agent.session_id)
        self.assertEqual([r["tool_call_id"] for r in results], ["before", "shell", "after"])
        self.assertEqual([r["is_error"] for r in results], [False, True, action != "timeout"])
        self.assertIn("kept", results[0]["content"])
        if action == "timeout":
            self.assertIn("timed out after 1000ms", results[1]["content"])

        snapshot = load_session(agent.session_id)
        history = snapshot["openaiMessages" if protocol == "openai" else "anthropicMessages"]
        if protocol == "openai":
            receipts = [m for m in history if m["role"] == "tool"]
            ids = [m["tool_call_id"] for m in receipts]
        else:
            receipts = [b for m in history if isinstance(m["content"], list)
                        for b in m["content"] if b["type"] == "tool_result"]
            ids = [b["tool_use_id"] for b in receipts]
        self.assertEqual(ids, ["before", "shell", "after"])
        self.assertEqual([r["content"] for r in receipts], [r["content"] for r in results])
        restored = self.agent(protocol)
        restored.restore_session(snapshot)
        with (patch.object(restored, f"_call_{protocol}_stream", return_value=response(protocol)),
              patch.object(restored, "_execute_tool_call") as execute):
            await restored.chat("continue")
            execute.assert_not_called()

    async def test_shell_timeout_and_stop_preserve_paired_history(self):
        for protocol in ("openai", "anthropic"):
            for action in ("timeout", "abort", "cancel"):
                with self.subTest(protocol=protocol, action=action):
                    await self.check_turn(protocol, action)
