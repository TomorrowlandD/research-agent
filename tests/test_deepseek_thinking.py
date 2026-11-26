from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from agents.tool_result import ToolExecutionResult
from unittest.mock import AsyncMock, Mock, patch

import openai
try:
    import httpx2 as httpx  # OpenAI SDK 3.x
except ImportError:
    import httpx  # OpenAI SDK 1.x / 2.x

from agents.deepseek import CONTEXT_WINDOW, MAX_OUTPUT_TOKENS, normalize_usage
from agents.main import main, parse_args
from agents.session import load_session
import test_deepseek_config as phase1_tests


def chunk(*, reasoning=None, content=None, calls=None, finish=None, usage=None):
    return NS(usage=usage, choices=[NS(
        delta=NS(reasoning_content=reasoning, content=content, tool_calls=calls), finish_reason=finish,
    )])


def call(*, args, name="read_file", ident="read_1", index=0):
    return NS(index=index, id=ident, function=NS(name=name, arguments=args))


class FakeStream:
    def __init__(self, *events):
        self.events = events
        self.close = AsyncMock()

    def __aiter__(self):
        async def iterate():
            for event in self.events:
                if isinstance(event, BaseException):
                    raise event
                yield event
        return iterate()


def answer(text="OK", reasoning="reasoning", usage=None):
    return FakeStream(chunk(reasoning=reasoning), chunk(content=text, finish="stop", usage=usage))


def side_response(content="[]", *, usage=None, finish="stop"):
    return NS(choices=[NS(message=NS(content=content), finish_reason=finish)], usage=usage)


class DeepSeekThinkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ("start_spinner", "stop_spinner", "print_info", "print_cost",
                     "print_tool_call", "print_tool_result", "print_divider", "print_assistant_text"):
            stack.enter_context(patch("agents.agent." + name))
        stack.enter_context(patch("agents.agent.check_permission", return_value={"action": "allow"}))

    def agent(self, **kwargs):
        return phase1_tests.DeepSeekConfigTests()._agent(**kwargs)

    async def test_enabled_environment_and_cli_precedence(self):
        for mode, override, enabled in (
            ("enabled", None, True), ("disabled", True, True),
            ("enabled", False, False), (None, None, False),
        ):
            with self.subTest(mode=mode, override=override):
                agent = self.agent(mode=mode, thinking=override, reasoning_effort="low")
                create = agent._openai_client.chat.completions.create
                create.return_value = answer()
                await agent._call_openai_stream()
                request = create.call_args.kwargs
                self.assertEqual(request["extra_body"]["thinking"]["type"], "enabled" if enabled else "disabled")
                self.assertEqual(request.get("reasoning_effort"), "low" if enabled else None)
                self.assertEqual(agent.thinking, enabled)

    async def test_effort_levels_and_output_context_limits(self):
        for effort, expected in (("low", 65536), ("high", 65536), ("max", 131072)):
            with self.subTest(effort=effort):
                agent = self.agent(thinking=True, reasoning_effort=effort)
                self.assertEqual(agent.max_output_tokens, expected)
                self.assertEqual(agent.effective_window, CONTEXT_WINDOW - expected - 20000)
                self.assertEqual(agent._openai_request_options["reasoning_effort"], effort)
        agent = self.agent(max_output_tokens=MAX_OUTPUT_TOKENS)
        self.assertEqual(agent.effective_window, CONTEXT_WINDOW - MAX_OUTPUT_TOKENS - 20000)
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            self.agent(max_output_tokens=MAX_OUTPUT_TOKENS + 1)

    async def test_cli_has_explicit_on_off_effort_and_output_options(self):
        with patch("sys.argv", ["researchagent", "--thinking", "--reasoning-effort", "max", "--max-output-tokens", "8000"]):
            args = parse_args()
        self.assertTrue(args.thinking)
        self.assertEqual((args.reasoning_effort, args.max_output_tokens), ("max", 8000))
        with patch("sys.argv", ["researchagent", "--no-thinking"]):
            self.assertFalse(parse_args().thinking)
        with patch("sys.argv", ["researchagent"]):
            self.assertIsNone(parse_args().thinking)

    async def test_main_thinking_does_not_enable_side_requests(self):
        agent = self.agent(thinking=True, reasoning_effort="max")
        create = agent._openai_client.chat.completions.create
        create.return_value = side_response()
        await agent._build_side_query()("system", "task")
        self.assertEqual(create.call_args.kwargs["extra_body"]["thinking"]["type"], "disabled")
        self.assertNotIn("reasoning_effort", create.call_args.kwargs)
        self.assertEqual(create.call_args.kwargs["max_tokens"], 256)

    async def test_independent_side_thinking_and_truncation(self):
        agent = self.agent(thinking=False, side_thinking="enabled", side_reasoning_effort="low")
        create = agent._openai_client.chat.completions.create
        create.return_value = side_response("answer")
        self.assertEqual(await agent._build_side_query(max_tokens=4096)("system", "task"), "answer")
        self.assertEqual(create.call_args.kwargs["reasoning_effort"], "low")
        self.assertEqual(create.call_args.kwargs["extra_body"]["thinking"]["type"], "enabled")
        create.return_value = side_response("partial", finish="length")
        with self.assertRaisesRegex(ValueError, "auxiliary output was truncated"):
            await agent._build_side_query()("system", "task")

    async def test_reasoning_and_function_fragments_are_assembled_and_stream_closed(self):
        agent = self.agent(thinking=True)
        stream = FakeStream(
            chunk(reasoning="think "), chunk(reasoning="fully"),
            chunk(calls=[call(args='{"file_', name="read_")]),
            chunk(calls=[call(args='path":"README.md"}', name="file", ident=None)], finish="tool_calls"),
        )
        agent._openai_client.chat.completions.create.return_value = stream
        result = await agent._call_openai_stream()
        message = result["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "think fully")
        function = message["tool_calls"][0]["function"]
        self.assertEqual(function["name"], "read_file")
        self.assertEqual(json.loads(function["arguments"]), {"file_path": "README.md"})
        stream.close.assert_awaited_once()

    async def test_two_tool_calls_and_followup_return_all_reasoning(self):
        agent = self.agent(thinking=True)
        create = agent._openai_client.chat.completions.create
        create.side_effect = [
            FakeStream(chunk(reasoning="read reason", calls=[call(args='{"file_path":"README.md"}')], finish="tool_calls")),
            FakeStream(chunk(reasoning="search reason", calls=[call(args='{"pattern":"thinking","path":"agents/agent.py"}', name="grep_search", ident="search_1")], finish="tool_calls")),
            answer("done", "answer reason"), answer("follow-up", "follow-up reason"),
        ]
        with patch.object(agent, "_execute_tool_call", new_callable=AsyncMock, return_value=ToolExecutionResult("result")) as execute:
            await agent._chat_openai("read then search")
            await agent._chat_openai("continue")
        self.assertEqual(execute.await_count, 2)
        request = create.call_args_list[-1].kwargs
        assistants = [m for m in request["messages"] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], ["read reason", "search reason", "answer reason"])
        self.assertEqual([m["tool_call_id"] for m in request["messages"] if m["role"] == "tool"], ["read_1", "search_1"])
        self.assertTrue(all(c.kwargs["extra_body"]["thinking"]["type"] == "enabled" for c in create.call_args_list))

    async def test_real_sdk_http_serialization_and_sse_preserve_reasoning_and_cache_usage(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            first = len(requests) == 1
            delta = {"reasoning_content": "wire thought", "content": None if first else "wire answer"}
            if first:
                delta["tool_calls"] = [{"index": 0, "id": "wire_call", "type": "function",
                                        "function": {"name": "read_file", "arguments": '{"file_path":"README.md"}'}}]
            event = {"id": "fake", "object": "chat.completion.chunk", "created": 0, "model": "deepseek-v4-pro",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if first else "stop"}]}
            usage = {"id": "fake", "object": "chat.completion.chunk", "created": 0, "model": "deepseek-v4-pro",
                     "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                                               "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}}
            sse = "data: " + json.dumps(event) + "\n\ndata: " + json.dumps(usage) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
        agent = self.agent(thinking=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = openai.AsyncOpenAI(api_key="test-placeholder", base_url="https://api.deepseek.com", http_client=http_client)
            agent._openai_client = client
            with patch.object(agent, "_execute_tool_call", new_callable=AsyncMock, return_value=ToolExecutionResult("mock result")):
                await agent._chat_openai("read")
                await agent._chat_openai("follow-up")
            await client.close()
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0]["thinking"], {"type": "enabled"})
        self.assertEqual(requests[0]["reasoning_effort"], "high")
        assistants = [m for m in requests[-1]["messages"] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], ["wire thought", "wire thought"])
        self.assertEqual(assistants[0]["tool_calls"][0]["id"], "wire_call")
        self.assertEqual((agent.cache_hit_tokens, agent.cache_miss_tokens), (240, 60))

    async def test_missing_reasoning_is_rejected_before_request_or_tool_execution(self):
        agent = self.agent(thinking=True)
        agent._openai_messages.append({"role": "assistant", "content": "old reply"})
        create = agent._openai_client.chat.completions.create
        with self.assertRaisesRegex(ValueError, "Start a new session"):
            await agent._call_openai_stream()
        create.assert_not_awaited()
        agent._openai_messages.pop()
        create.return_value = answer(reasoning=None)
        with self.assertRaisesRegex(ValueError, "reasoning_content"):
            await agent._chat_openai("new question")
        self.assertFalse(any(m["role"] == "assistant" for m in agent._openai_messages))

    async def test_no_tools_can_continue_without_reasoning_history(self):
        agent = self.agent(thinking=True, custom_tools=[])
        agent._openai_messages.append({"role": "assistant", "content": "reply"})
        create = agent._openai_client.chat.completions.create
        create.return_value = answer(reasoning=None)
        await agent._chat_openai("continue")
        self.assertNotIn("tools", create.call_args.kwargs)

    async def test_session_roundtrip_preserves_thought_and_folded_memory(self):
        agent = self.agent(thinking=True)
        agent._openai_messages.extend([
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": None, "reasoning_content": "tool reasoning",
             "tool_calls": [{"id": "saved_call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "saved_call", "content": "saved result"},
            {"role": "assistant", "content": "answer", "reasoning_content": "original reasoning"},
        ])
        agent._folded_session_memories = [{"session_id": "fake", "episode_memory": {"task_description": "task"}}]
        root = Path(tempfile.mkdtemp(prefix="researchagent-session-", dir=Path(__file__).resolve().parent))
        self.addCleanup(root.rmdir)
        with patch("agents.session.SESSION_DIR", root):
            agent._auto_save()
            saved_path = root / f"{agent.session_id}.json"
            self.addCleanup(saved_path.unlink)
            data = load_session(agent.session_id)
        self.assertIsNotNone(data)
        resumed = self.agent(thinking=True)
        resumed.restore_session(data)
        self.assertEqual(resumed._openai_messages[-1]["reasoning_content"], "original reasoning")
        self.assertEqual(resumed._folded_session_memories, agent._folded_session_memories)
        self.assertTrue(data["metadata"]["thinking"])
        create = resumed._openai_client.chat.completions.create
        create.return_value = answer()
        await resumed._chat_openai("continue")
        history = create.call_args.kwargs["messages"]
        self.assertEqual(history[2]["reasoning_content"], "tool reasoning")
        self.assertEqual(history[3]["tool_call_id"], "saved_call")
        self.assertEqual(history[4]["reasoning_content"], "original reasoning")

    async def test_old_session_rejected_atomically_with_thinking_but_allowed_when_disabled(self):
        old = {"openaiMessages": [{"role": "system", "content": "system"}, {"role": "assistant", "content": "old"}]}
        agent = self.agent(thinking=True)
        before = list(agent._openai_messages)
        with self.assertRaisesRegex(ValueError, "--no-thinking"):
            agent.restore_session(old)
        self.assertEqual(agent._openai_messages, before)
        disabled = self.agent(thinking=False)
        disabled.restore_session(old)
        self.assertEqual(disabled._openai_messages, old["openaiMessages"])

    async def test_manual_compaction_saves_old_history_upgrade_immediately(self):
        agent = self.agent(thinking=False)
        agent._openai_messages.extend([
            {"role": "user", "content": "one"}, {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "two"},
        ])
        agent._openai_client.chat.completions.create.return_value = side_response('{"episode_memory":{"task_description":"old task"}}')
        with patch("agents.agent.save_folded_session_memory"), patch.object(agent, "_auto_save") as save:
            await agent.compact()
            save.assert_called_once()
        enabled = self.agent(thinking=True)
        enabled.restore_session({"openaiMessages": agent._openai_messages})
        self.assertEqual([m["role"] for m in enabled._openai_messages], ["system", "user"])

    async def test_compaction_failure_keeps_fallback_but_is_not_reported_as_model_success(self):
        agent = self.agent(thinking=True)
        agent._openai_client.chat.completions.create.return_value = side_response("partial", finish="length")
        memory = await agent._generate_folded_session_memory("important task transcript")
        self.assertEqual(memory["episode_memory"]["task_description"], "Previous conversation was compacted without structured JSON.")
        self.assertIn("important task transcript", memory["episode_memory"]["current_progress"])

    async def test_plain_answer_truncation_does_not_enter_saved_history(self):
        agent = self.agent(thinking=True)
        agent._openai_client.chat.completions.create.return_value = FakeStream(chunk(reasoning="thought", content="partial", finish="length"))
        with self.assertRaisesRegex(ValueError, "response incomplete"):
            await agent._chat_openai("answer")
        self.assertEqual([m["role"] for m in agent._openai_messages], ["system", "user"])

    async def test_compaction_uses_disabled_side_and_can_continue_with_thinking(self):
        agent = self.agent(thinking=True)
        agent._openai_messages.extend([
            {"role": "user", "content": "remember blue"},
            {"role": "assistant", "content": "OK", "reasoning_content": "thought"},
            {"role": "user", "content": "continue"},
        ])
        memory = {"episode_memory": {"task_description": "remember blue"}, "working_memory": {}, "tool_memory": {}}
        create = agent._openai_client.chat.completions.create
        create.return_value = side_response(json.dumps(memory))
        with patch("agents.agent.save_folded_session_memory"), patch("agents.agent.fallback_folded_memory") as fallback:
            self.assertTrue(await agent._compact_conversation(trigger="manual"))
            fallback.assert_not_called()
        self.assertEqual(create.call_args.kwargs["extra_body"]["thinking"]["type"], "disabled")
        self.assertEqual(agent._openai_messages[-1]["reasoning_content"], "thought")
        create.return_value = answer("continued")
        await agent._chat_openai("continue after compact")
        self.assertEqual(create.call_args.kwargs["extra_body"]["thinking"]["type"], "enabled")

    async def test_cancelled_stream_does_not_save_partial_thought_or_tool(self):
        agent = self.agent(thinking=True, is_sub_agent=True)
        stream = FakeStream(chunk(reasoning="partial", calls=[call(args='{"file_')]), asyncio.CancelledError())
        agent._openai_client.chat.completions.create.return_value = stream
        with patch.object(agent, "_execute_tool_call", new_callable=AsyncMock) as execute:
            result = await agent.run_once("question")
        execute.assert_not_awaited()
        stream.close.assert_awaited_once()
        self.assertTrue(agent._aborted)
        self.assertEqual(result["text"], "")
        self.assertEqual([m["role"] for m in agent._openai_messages], ["system", "user"])

    async def test_truncated_filtered_or_unfinished_tools_never_execute(self):
        for finish, args in (("length", '{"file_'), ("content_filter", '{}'), (None, '{}')):
            with self.subTest(finish=finish, args=args):
                agent = self.agent(thinking=True)
                agent._openai_client.chat.completions.create.return_value = FakeStream(
                    chunk(reasoning="thought", calls=[call(args=args)], finish=finish))
                with patch.object(agent, "_execute_tool_call", new_callable=AsyncMock) as execute:
                    with self.assertRaises(ValueError):
                        await agent._chat_openai("read")
                    execute.assert_not_awaited()
                self.assertFalse(any(m["role"] == "assistant" for m in agent._openai_messages))

    async def test_children_and_skill_forks_inherit_cli_configuration_and_usage(self):
        for fork in (False, True):
            with self.subTest(fork=fork):
                agent = self.agent(thinking=True, reasoning_effort="max", max_output_tokens=4000,
                                   side_thinking="disabled", deepseek_price_period="off_peak")
                children = []
                def spawn(**kwargs):
                    child = self.agent(mode="disabled", **kwargs)
                    children.append(child)
                    usage = NS(prompt_tokens=100, completion_tokens=20, prompt_cache_hit_tokens=80,
                               completion_tokens_details=NS(reasoning_tokens=5))
                    child._openai_client.chat.completions.create.return_value = answer("child answer", "private thought", usage)
                    return child
                with patch("agents.agent.Agent", side_effect=spawn), \
                     patch("agents.agent.get_sub_agent_config", return_value={"system_prompt": "test", "tools": []}), \
                     patch("agents.skills.execute_skill", return_value={"context": "fork", "prompt": "test"}), \
                     patch("agents.agent.print_sub_agent_start"), patch("agents.agent.print_sub_agent_end"):
                    result = (await agent._execute_skill_tool({"skill_name": "fake"}) if fork
                              else await agent._execute_agent_tool({"prompt": "test"}))
                self.assertEqual(result.strip(), "child answer")
                self.assertNotIn("private thought", result)
                child = children[0]
                self.assertTrue(child.thinking)
                self.assertEqual(child._deepseek.effort, "max")
                self.assertEqual(child.max_output_tokens, 4000)
                self.assertEqual(child._deepseek.price_period, "off_peak")
                self.assertEqual(agent.cache_hit_tokens, 80)
                self.assertEqual(agent.reasoning_tokens, 5)
                self.assertAlmostEqual(agent._get_current_cost_usd(), child._get_current_cost_usd())

    async def test_cost_includes_cache_side_and_thinking_without_double_billing(self):
        agent = self.agent(thinking=True, deepseek_price_period="peak")
        usage = NS(prompt_tokens=1000, completion_tokens=200, prompt_cache_hit_tokens=800,
                   completion_tokens_details=NS(reasoning_tokens=150))
        create = agent._openai_client.chat.completions.create
        create.return_value = answer(usage=usage)
        await agent._chat_openai("question")
        create.return_value = side_response(usage=NS(prompt_tokens=100, completion_tokens=10))
        await agent._build_side_query()("system", "task")
        expected = (800 * 0.044 + 300 * 1.32 + 210 * 3.96) / 1_000_000
        self.assertAlmostEqual(agent._get_current_cost_usd(), expected)
        self.assertEqual((agent.cache_hit_tokens, agent.cache_miss_tokens, agent.reasoning_tokens), (800, 300, 150))
        agent._print_cost()
        from agents.agent import print_cost
        self.assertAlmostEqual(print_cost.call_args.kwargs["estimated_cost_usd"], expected)
        normalized = normalize_usage(usage)
        self.assertEqual(normalize_usage(normalized), normalized)
        agent.max_cost_usd = expected / 2
        self.assertTrue(agent._check_budget()["exceeded"])
        create.reset_mock()
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            await agent._build_side_query()("system", "task")
        create.assert_not_awaited()

    async def test_standard_cached_usage_and_child_auxiliary_usage_are_counted(self):
        agent = self.agent()
        agent._record_openai_usage(NS(prompt_tokens=100, completion_tokens=10,
                                     prompt_tokens_details=NS(cached_tokens=60)))
        self.assertEqual((agent.cache_hit_tokens, agent.cache_miss_tokens), (60, 40))
        agent._merge_sub_agent_usage({"side_tokens": {"input": 20, "output": 2},
                                     "deepseek_usage": {"estimated_cost_usd": 0.01, "cache_hit_tokens": 5}})
        self.assertEqual((agent.side_input_tokens, agent.side_output_tokens, agent.cache_hit_tokens), (20, 2, 65))

    async def test_off_peak_estimate_is_half_peak_and_other_model_options_are_unchanged(self):
        peak = self.agent(deepseek_price_period="peak")
        off_peak = self.agent(deepseek_price_period="off_peak")
        usage = NS(prompt_tokens=1000, completion_tokens=100, prompt_cache_hit_tokens=200)
        peak._record_openai_usage(usage)
        off_peak._record_openai_usage(usage)
        self.assertAlmostEqual(peak._get_current_cost_usd(), off_peak._get_current_cost_usd() * 2)
        other = self.agent(model="gpt-4o", api_base="https://api.openai.com/v1")
        self.assertEqual(other._sub_agent_model_options(), {})
        self.assertEqual(other._openai_request_options, {})
        with self.assertRaisesRegex(ValueError, "official HTTPS"):
            self.agent(api_base="https://gateway.example.test", reasoning_effort="high")


class DeepSeekCliIntegrationTests(unittest.TestCase):
    def test_cli_passes_configuration_and_complete_saved_session_without_loading_env_file(self):
        session = {"metadata": {"project_hash": "project", "protocol": "openai"},
                   "openaiMessages": [], "foldedSessionMemories": [{"session_id": "fake"}]}
        fake = NS(restore_session=Mock())
        with patch.dict(os.environ, {"APIKEY": "test-placeholder", "API": "https://api.deepseek.com"}, clear=True), \
             patch("sys.argv", ["researchagent", "--model", "deepseek-v4-pro", "--thinking", "--reasoning-effort", "low", "--max-output-tokens", "8000", "--resume"]), \
             patch("agents.main._load_env_file"), patch("agents.main.Agent", return_value=fake) as factory, \
             patch("agents.main.get_latest_session_id", return_value="fake"), \
             patch("agents.main.load_session", return_value=session), \
             patch("agents.main.run_repl", new_callable=AsyncMock) as repl:
            main()
        self.assertEqual(factory.call_args.kwargs["thinking"], True)
        self.assertEqual(factory.call_args.kwargs["reasoning_effort"], "low")
        self.assertEqual(factory.call_args.kwargs["max_output_tokens"], 8000)
        fake.restore_session.assert_called_once_with(session)
        repl.assert_awaited_once_with(fake)


if __name__ == "__main__":
    unittest.main()
