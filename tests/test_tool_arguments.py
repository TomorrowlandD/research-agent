from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import anthropic

from agents.tools import tool_definitions, validate_tool_arguments
import test_deepseek_config as config_tests
from test_deepseek_thinking import FakeStream, answer, call, chunk, httpx


class ArgumentValidationTests(unittest.TestCase):
    def test_existing_schemas(self):
        cases = [
            ("read_file", '{"file_path":"README.md"}', None),
            ("read_file", '{"file_path":', "invalid JSON"),
            ("read_file", "", "invalid JSON"),
            ("read_file", "[]", "JSON object"),
            ("read_file", "null", "JSON object"),
            ("read_file", '"{}"', "JSON object"),
            ("read_file", "42", "JSON object"),
            ("read_file", "{}", "required property"),
            ("read_file", '{"file_path":42}', "string"),
            ("run_shell", '{"command":"echo ok","timeout":true}', "number"),
            ("skill_create", '{"name":"n","description":"d","instructions":"i","user_invocable":"false"}', "boolean"),
            ("skill_evolve", '{"skill_name":"s","lesson":"l","target":"wrong"}', "not one of"),
            ("enter_plan_mode", "{}", None),
        ]
        for name, raw, error in cases:
            with self.subTest(name=name, raw=raw):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        validate_tool_arguments(name, raw, tool_definitions, encoded=True)
                else:
                    self.assertEqual(validate_tool_arguments(name, raw, tool_definitions, encoded=True), json.loads(raw))

    def test_nested_mcp_schema_and_uncoerced_values(self):
        definitions = [{"name": "mcp__demo__query", "input_schema": {
            "type": "object", "required": ["rows"], "properties": {
                "rows": {"type": "array", "items": {"type": "object", "required": ["count"],
                          "properties": {"count": {"type": "integer"}}}},
            },
        }}]
        valid = {"rows": [{"count": 2}]}
        self.assertIs(validate_tool_arguments("mcp__demo__query", valid, definitions), valid)
        with self.assertRaisesRegex(ValueError, "rows.0.count"):
            validate_tool_arguments("mcp__demo__query", {"rows": [{"count": "2"}]}, definitions)


class ToolArgumentLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("start_spinner", "stop_spinner", "print_info", "print_cost", "print_tool_call",
                     "print_tool_result", "print_divider", "print_assistant_text"):
            self.stack.enter_context(patch("agents.agent." + name))
        self.permission = self.stack.enter_context(patch("agents.agent.check_permission", return_value={"action": "allow"}))

    def agent(self, provider="openai"):
        options = {"enable_folding": False, "is_sub_agent": True}
        if provider == "openai":
            options.update(model="gpt-4o", api_base="https://example.invalid/v1")
        elif provider == "anthropic":
            options.update(model="claude-sonnet-4-6", api_base=None)
        else:
            options["thinking"] = True
        return config_tests.DeepSeekConfigTests()._agent(**options)

    async def run_openai(self, agent, calls):
        create = agent._openai_client.chat.completions.create
        create.side_effect = [FakeStream(chunk(reasoning="thought", calls=calls, finish="tool_calls")), answer()]
        await agent._chat_openai("test")
        # Verify the next model request actually contains the matching results.
        return [m for m in create.call_args_list[1].kwargs["messages"] if m["role"] == "tool"]

    async def test_openai_and_deepseek_recover_all_argument_errors(self):
        for provider in ("openai", "deepseek"):
            for raw in ('{"file_path":', "[]", "null", "{}", '{"file_path":42}', '"text"'):
                with self.subTest(provider=provider, raw=raw):
                    self.permission.reset_mock()
                    agent = self.agent(provider)
                    with patch.object(agent, "_execute_tool_call", new_callable=AsyncMock) as execute:
                        results = await self.run_openai(agent, [call(args=raw)])
                    execute.assert_not_awaited()
                    self.permission.assert_not_called()
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0]["tool_call_id"], "read_1")
                    self.assertIn("Invalid arguments for read_file", results[0]["content"])

    async def test_mixed_batch_preserves_concurrent_and_serial_execution(self):
        for provider in ("openai", "deepseek"):
            with self.subTest(provider=provider):
                agent = self.agent(provider)
                started = set()
                both_started = asyncio.Event()

                async def execute(name, inp):
                    if name == "read_file":
                        started.add(inp["file_path"])
                        if len(started) == 2:
                            both_started.set()
                        await asyncio.wait_for(both_started.wait(), timeout=2)
                    return "ok"

                specs = [("read_file", "null"), ("read_file", '{"file_path":"a"}'),
                         ("read_file", '{"file_path":"b"}'), ("run_shell", '{"command":12}'),
                         ("enter_plan_mode", "{}"), ("run_shell", '{"command":"echo ok"}')]
                with patch.object(agent, "_execute_tool_call", side_effect=execute) as dispatch:
                    results = await self.run_openai(agent, [
                        call(args=raw, name=name, ident=f"id_{i}", index=i) for i, (name, raw) in enumerate(specs)
                    ])
                self.assertEqual(dispatch.await_count, 4)
                self.assertEqual([m["tool_call_id"] for m in results], [f"id_{i}" for i in range(6)])
                self.assertTrue(all("Invalid arguments" in results[i]["content"] for i in (0, 3)))
                self.assertEqual([results[i]["content"] for i in (1, 2, 4, 5)], ["ok"] * 4)

    async def test_broken_openai_envelopes_fail_before_any_execution(self):
        valid = {"id": "good", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path":"a"}'}}
        broken = [
            {"type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "bad", "type": "function", "function": {"arguments": "{}"}},
            {"id": "bad", "type": "function", "function": {"name": "read_file"}},
            {"id": "bad", "type": "function", "function": None},
            {"id": "bad", "type": "unexpected"}, valid, None,
        ]
        for provider in ("openai", "deepseek"):
            for bad in broken:
                with self.subTest(provider=provider, bad=bad):
                    agent = self.agent(provider)
                    response = {"choices": [{"finish_reason": "tool_calls", "message": {
                        "role": "assistant", "reasoning_content": "thought", "tool_calls": [valid, bad],
                    }}]}
                    with patch.object(agent, "_call_openai_stream", AsyncMock(return_value=response)), \
                         patch.object(agent, "_execute_tool_call", new_callable=AsyncMock) as execute:
                        with self.assertRaises(ValueError):
                            await agent._chat_openai("test")
                    execute.assert_not_awaited()
                    self.assertFalse(any(m["role"] == "assistant" for m in agent._openai_messages))

    async def test_anthropic_errors_and_empty_object_reach_next_request(self):
        agent = self.agent("anthropic")
        inputs = [[], None, {}, {"file_path": 42}, {"file_path": "a"}, {}]
        blocks = [NS(type="tool_use", id=f"id_{i}", name="read_file" if i < 5 else "enter_plan_mode", input=inp)
                  for i, inp in enumerate(inputs)]
        responses = [NS(content=blocks, stop_reason="tool_use", usage=NS(input_tokens=0, output_tokens=0)),
                     NS(content=[], stop_reason="end_turn", usage=NS(input_tokens=0, output_tokens=0))]
        with patch.object(agent, "_call_anthropic_stream", AsyncMock(side_effect=responses)), \
             patch.object(agent, "_execute_tool_call", AsyncMock(return_value="ok")) as execute:
            await agent._chat_anthropic("test")
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(self.permission.call_count, 2)
        results = agent._anthropic_messages[2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], [f"id_{i}" for i in range(6)])
        self.assertTrue(all(r["is_error"] and "Invalid arguments" in r["content"] for r in results[:4]))
        self.assertEqual([r["content"] for r in results[4:]], ["ok", "ok"])
        self.assertTrue(all(isinstance(b["input"], dict) for b in agent._anthropic_messages[1]["content"]))

    async def test_mcp_invalid_input_never_reaches_transport(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                self.permission.reset_mock()
                agent = self.agent(provider)
                agent._mcp_manager._tools = [{"serverName": "demo", "name": "query", "inputSchema": {
                    "type": "object", "required": ["query"], "properties": {"query": {"type": "string"}},
                }}]
                agent.tools += agent._mcp_manager.get_tool_definitions()
                connection = NS(call_tool=AsyncMock(return_value="mcp ok"))
                agent._mcp_manager._connections["demo"] = connection
                name = "mcp__demo__query"
                if provider == "openai":
                    results = await self.run_openai(agent, [
                        call(name=name, args='{"query":12}', ident="bad"),
                        call(name=name, args='{"query":"ok"}', ident="good", index=1),
                    ])
                else:
                    blocks = [NS(type="tool_use", name=name, id="bad", input={"query": 12}),
                              NS(type="tool_use", name=name, id="good", input={"query": "ok"})]
                    with patch.object(agent, "_call_anthropic_stream", AsyncMock(side_effect=[
                        NS(content=blocks, stop_reason="tool_use", usage=NS(input_tokens=0, output_tokens=0)),
                        NS(content=[], stop_reason="end_turn", usage=NS(input_tokens=0, output_tokens=0)),
                    ])):
                        await agent._chat_anthropic("test")
                    results = agent._anthropic_messages[2]["content"]
                connection.call_tool.assert_awaited_once_with("query", {"query": "ok"})
                self.permission.assert_called_once()
                self.assertIn("Invalid arguments", results[0]["content"])
                self.assertEqual(results[1]["content"], "mcp ok")

    async def test_anthropic_broken_ids_fail_before_any_execution(self):
        for bad_id in (None, "", "good", 7):
            with self.subTest(bad_id=bad_id):
                agent = self.agent("anthropic")
                blocks = [NS(type="tool_use", id="good", name="read_file", input={"file_path": "a"}),
                          NS(type="tool_use", id=bad_id, name="read_file", input={"file_path": "b"})]
                with patch.object(agent, "_call_anthropic_stream", AsyncMock(return_value=NS(
                    content=blocks, stop_reason="tool_use", usage=NS(input_tokens=0, output_tokens=0),
                ))), patch.object(agent, "_execute_tool_call", new_callable=AsyncMock) as execute:
                    with self.assertRaises(ValueError):
                        await agent._chat_anthropic("test")
                execute.assert_not_awaited()
                self.assertFalse(any(m["role"] == "assistant" for m in agent._anthropic_messages))

    async def test_anthropic_real_sdk_sse_preserves_bad_json_until_validation(self):
        requests = []
        raw_inputs = ['{"file_path":}', '{"file_path":"partial"', '[]', 'null', '{}',
                      '{"file_path":42}', '{"file_path":"README.md"}', '{}']

        def handler(request):
            requests.append(json.loads(request.content))
            events = [{"type": "message_start", "message": {
                "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6",
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            }}]
            if len(requests) == 1:
                for i, raw in enumerate(raw_inputs):
                    events.append({"type": "content_block_start", "index": i, "content_block": {
                        "type": "tool_use", "id": f"id_{i}", "name": "read_file" if i < 7 else "enter_plan_mode", "input": {},
                    }})
                    # Exercise split JSON, not just pre-decoded fake response blocks.
                    for fragment in (raw[:3], raw[3:]):
                        events.append({"type": "content_block_delta", "index": i,
                                       "delta": {"type": "input_json_delta", "partial_json": fragment}})
                    events.append({"type": "content_block_stop", "index": i})
            else:
                events.extend([
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
                    {"type": "content_block_stop", "index": 0},
                ])
            events.extend([
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if len(requests) == 1 else "end_turn",
                                                      "stop_sequence": None}, "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ])
            sse = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

        agent = self.agent("anthropic")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = anthropic.AsyncAnthropic(api_key="test-placeholder", http_client=http_client)
            agent._anthropic_client = client
            with patch.object(agent, "_execute_tool_call", AsyncMock(return_value="ok")) as execute:
                await agent._chat_anthropic("test")
            await client.close()
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(self.permission.call_count, 2)
        self.assertEqual(len(requests), 2)
        history = requests[1]["messages"]
        results = history[-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], [f"id_{i}" for i in range(8)])
        self.assertTrue(all(r["is_error"] for r in results[:6]))
        self.assertTrue(all("invalid JSON" in results[i]["content"] for i in (0, 1)))
        self.assertEqual([r["content"] for r in results[6:]], ["ok", "ok"])
        self.assertTrue(all(isinstance(b["input"], dict) for b in history[-2]["content"]))
        self.assertEqual(history[-2]["content"][0]["input"], {"_invalid_tool_arguments": raw_inputs[0]})
        self.assertEqual(agent._anthropic_messages[-1]["content"], [{"type": "text", "text": "done"}])
        self.assertEqual((agent.total_input_tokens, agent.total_output_tokens), (20, 6))


if __name__ == "__main__":
    unittest.main()
