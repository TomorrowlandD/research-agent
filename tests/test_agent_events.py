import asyncio
from contextlib import chdir
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.main import main
from agents.session import load_session
from agents.tool_result import ToolExecutionResult


def response(protocol, calls=()):
    if protocol == "openai":
        message = {"role": "assistant", "content": "answer", "reasoning_content": "original reasoning"}
        if calls:
            message["tool_calls"] = [
                {"id": key, "type": "function", "function": {"name": "read_file", "arguments": json.dumps(args)}}
                for key, args in calls
            ]
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}
    blocks = [SimpleNamespace(type="tool_use", name="read_file", id=key, input=args) for key, args in calls]
    return SimpleNamespace(content=blocks or [SimpleNamespace(type="text", text="answer")],
                           stop_reason="tool_use" if calls else "end_turn",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=1))


class AgentEventTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.sessions = self.root / "sessions"
        self.enterContext(chdir(self.root))
        self.enterContext(patch("agents.session.SESSION_DIR", self.sessions))
        self.enterContext(patch("pathlib.Path.home", return_value=self.root))
        self.enterContext(patch("agents.agent.print_tool_result"))
        self.enterContext(patch("agents.agent.start_spinner"))
        self.enterContext(patch("agents.agent.stop_spinner"))

    def agent(self, protocol):
        return Agent(custom_system_prompt="system", api_key="test", enable_mcp=False, enable_memory_prefetch=False,
                     enable_online_evolution=False, api_base="http://example.test/v1" if protocol == "openai" else None)

    def rows(self, session_id):
        path = self.sessions / f"{session_id}.events.jsonl"
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_tool_results_microcompact_fold_and_cli_resume(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = self.agent(protocol)
                big = "start\n" + "xyz" * 30000 + "\nEND-OF-ARTIFACT"
                calls = [(str(i), {"file_path": str(i)}) for i in range(5)]
                calls.append(("invalid", {}))

                async def read(name, args, state):
                    return big if args["file_path"] == "0" else "original small result " + args["file_path"]

                summary = {"episode_memory": {"current_progress": "saved"},
                           "working_memory": {"next_actions": ["continue"]}, "tool_memory": {}}
                with (patch.object(agent, f"_call_{protocol}_stream", side_effect=[
                        response(protocol, calls), response(protocol), response(protocol)]),
                      patch.object(agent, "_augment_user_message_with_skill_context", return_value=("injected", None)),
                      patch("agents.agent.execute_tool", side_effect=read),
                      patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock, return_value=summary)):
                    asyncio.run(agent.chat("first raw input"))
                    first_trace = agent.trace_id
                    agent.last_api_call_time = 1
                    asyncio.run(agent.chat("second raw input"))
                    asyncio.run(agent.compact())
                events = self.rows(agent.session_id)
                self.assertEqual([r["payload"]["content"] for r in events if r["event_type"] == "user_message"],
                                 ["first raw input", "second raw input"])
                results = [r for r in events if r["event_type"] == "tool_result"]
                self.assertEqual(len(results), 6)
                payloads = {r["payload"]["tool_call_id"]: r["payload"] for r in results}
                self.assertTrue(payloads["invalid"]["is_error"])
                self.assertEqual(payloads["1"]["content"], "original small result 1")
                path = Path(payloads["0"]["artifact_path"])
                self.assertEqual(path.read_text(), big)
                self.assertIn("start", payloads["0"]["content_preview"])
                self.assertNotIn("END-OF-ARTIFACT", json.dumps(events))
                self.assertTrue(all(r["trace_id"] == first_trace for r in results))
                traces = (self.root / ".research" / "traces" / f"{first_trace}.jsonl").read_text().splitlines()
                self.assertTrue(all(json.loads(line)["session_id"] == agent.session_id for line in traces))
                assistant = [r["payload"]["message"] for r in events if r["event_type"] == "assistant_message"]
                self.assertEqual(len(assistant), 3)
                if protocol == "openai":
                    self.assertEqual(assistant[0]["reasoning_content"], "original reasoning")
                    self.assertEqual(assistant[0]["tool_calls"][-1]["function"]["arguments"], "{}")
                else:
                    self.assertEqual(assistant[0]["content"][-1]["input"], {})
                micro, = [r for r in events if r["event_type"] == "micro_compact"]
                self.assertTrue(micro["payload"]["message_indices_by_stage"]["idle"])
                self.assertGreater(micro["seq"], max(r["seq"] for r in results))
                self.assertEqual(events[-1]["event_type"], "context_fold")
                self.assertEqual(events[-1]["payload"]["memory"], summary)
                self.assertEqual(events[-1]["payload"]["trigger"], "manual")
                snapshot = load_session(agent.session_id)
                self.assertNotIn("original small result", json.dumps(snapshot))
                path = self.sessions / f"{agent.session_id}.events.jsonl"
                prefix = path.read_bytes()

                async def continue_chat(resumed):
                    self.assertEqual(resumed.session_id, agent.session_id)
                    resumed.enable_memory_prefetch = resumed.enable_mcp = resumed.enable_online_evolution = False
                    with patch.object(resumed, f"_call_{protocol}_stream", return_value=response(protocol)):
                        await resumed.chat("continue from snapshot")

                with (patch("sys.argv", ["researchagent", "--resume", agent.session_id]),
                      patch("agents.main._load_env_file"), patch("agents.agent.build_system_prompt", return_value="system"),
                      patch("agents.main._resolve_api_config", return_value=("http://example.test", "test", protocol == "openai")),
                      patch("agents.main.run_repl", side_effect=continue_chat)):
                    main()
                resumed_events = self.rows(agent.session_id)
                self.assertTrue(path.read_bytes().startswith(prefix))
                self.assertEqual([r["event_type"] for r in resumed_events[len(events):]],
                                 ["session_resumed", "user_message", "assistant_message"])
                self.assertEqual([r["seq"] for r in resumed_events], list(range(1, len(resumed_events) + 1)))
                self.assertNotEqual(resumed_events[-1]["trace_id"], first_trace)
                self.assertEqual(resumed_events[-1]["trace_id"], resumed_events[-2]["trace_id"])

    def test_cancelled_parallel_batch_logs_each_receipt_once(self):
        agent = self.agent("openai")
        calls = [("1", {"file_path": "complete"}), ("2", {"file_path": "cancel"})]

        async def run(name, args):
            if args["file_path"] == "cancel":
                await asyncio.sleep(0)
                raise asyncio.CancelledError()
            return ToolExecutionResult("completed")

        with (patch.object(agent, "_call_openai_stream", return_value=response("openai", calls)),
              patch.object(agent, "_execute_tool_call", side_effect=run)):
            asyncio.run(agent.chat("read both"))
        results = [r["payload"] for r in self.rows(agent.session_id) if r["event_type"] == "tool_result"]
        self.assertEqual([(r["tool_call_id"], r["is_error"]) for r in results], [("1", False), ("2", True)])

    def test_write_failure_does_not_abort_turn(self):
        agent = self.agent("openai")
        with (patch.object(agent, "_call_openai_stream", return_value=response("openai")),
              patch("agents.event_log._lock_for_append", side_effect=OSError("disk unavailable")),
              self.assertLogs(level="WARNING") as logs):
            asyncio.run(agent.chat("keep going"))
        self.assertIn("Cannot append", "\n".join(logs.output))
        self.assertEqual(load_session(agent.session_id)["openaiMessages"][-1]["content"], "answer")
