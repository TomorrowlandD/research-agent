import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent


def make_agent(protocol):
    return Agent(custom_system_prompt="system", api_key="test", is_sub_agent=True,
                 api_base="http://example.test/v1" if protocol == "openai" else None)


def reply(protocol, calls=(), tokens=100):
    if protocol == "openai":
        message = {"role": "assistant", "content": "done"}
        if calls:
            message["tool_calls"] = [
                {"type": "function", "id": call_id,
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for call_id, name, args in calls
            ]
        return {"choices": [{"message": message}], "usage": {"prompt_tokens": tokens}}
    blocks = [SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)
              for call_id, name, args in calls]
    return SimpleNamespace(content=blocks or [SimpleNamespace(type="text", text="done")],
                           stop_reason="tool_use" if calls else "end_turn",
                           usage=SimpleNamespace(input_tokens=tokens, output_tokens=2))


class ToolBatchFoldingTests(unittest.TestCase):
    def test_tool_batch_after_success_or_failed_summary(self):
        calls = [("fold", "compact_context", {}), ("read", "read_file", {"file_path": "README.md"})]
        for protocol in ("openai", "anthropic"):
            for summary in ('{"episode_memory":{"current_progress":"saved"}}', "", RuntimeError("offline")):
                with self.subTest(protocol=protocol, summary=summary):
                    agent = make_agent(protocol)
                    history = getattr(agent, f"_{protocol}_messages")
                    history.extend([{"role": "user", "content": "first"},
                                    {"role": "assistant", "content": "ok"}])
                    requests = []

                    async def respond():
                        requests.append(deepcopy(getattr(agent, f"_{protocol}_messages")))
                        return reply(protocol, calls if len(requests) == 1 else ())

                    side = AsyncMock(side_effect=summary) if isinstance(summary, Exception) else AsyncMock(return_value=summary)
                    with (patch.object(agent, f"_call_{protocol}_stream", side_effect=respond),
                          patch.object(agent, "_build_side_query", return_value=side),
                          patch.object(agent, "_write_tool_trace"),
                          patch("agents.agent.save_folded_session_memory"),
                          patch("agents.agent.execute_tool", new_callable=AsyncMock, return_value="contents") as read):
                        asyncio.run(getattr(agent, f"_chat_{protocol}")("continue"))

                    self.assertEqual(len(requests), 2)
                    if isinstance(summary, str) and summary:
                        read.assert_not_awaited()
                        self.assertEqual(agent._fold_count, 1)
                        rebuilt = requests[1]
                        self.assertIn("saved", rebuilt[int(protocol == "openai")]["content"])
                        self.assertIn({"role": "assistant", "content": "ok"}, rebuilt)
                        for msg in rebuilt:
                            self.assertNotEqual(msg["role"], "tool")
                            self.assertFalse(msg.get("tool_calls"))
                            if isinstance(msg.get("content"), list):
                                self.assertFalse(any(b["type"] in ("tool_use", "tool_result") for b in msg["content"]))
                    else:
                        read.assert_awaited_once()
                        self.assertEqual(agent._fold_count, 0)
                        self.assertEqual(requests[1][:len(requests[0])], requests[0])
                        if protocol == "openai":
                            results = [(m["tool_call_id"], m["content"]) for m in requests[1] if m["role"] == "tool"]
                        else:
                            results = [(b["tool_use_id"], b["content"]) for b in requests[1][-1]["content"]]
                        self.assertEqual([key for key, _ in results], ["fold", "read"])
                        self.assertIn("failed", results[0][1])
                        self.assertEqual(results[1][1], "contents")


class PreflightBudgetTests(unittest.TestCase):
    def test_parallel_results_fold_before_next_request(self):
        calls = [(str(i), "read_file", {"file_path": "README.md"}) for i in range(4)]
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = make_agent(protocol)
                agent.effective_window = 100_000
                folds_at_request = []

                async def respond():
                    folds_at_request.append(agent._fold_count)
                    return reply(protocol, calls, tokens=45_000) if len(folds_at_request) == 1 else reply(protocol)

                with (patch.object(agent, f"_call_{protocol}_stream", side_effect=respond),
                      patch.object(agent, "_write_tool_trace"), patch("agents.agent.save_folded_session_memory"),
                      patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock, return_value={}),
                      patch("agents.agent.execute_tool", new_callable=AsyncMock, return_value="x" * (25 * 1024)),
                      patch("agents.agent.print_tool_result")):
                    chat = getattr(agent, f"_chat_{protocol}")
                    asyncio.run(chat("read four files"))
                    asyncio.run(chat("continue"))
                self.assertEqual(folds_at_request, [0, 1, 1])

    def test_text_only_preflight_preserves_unanswered_prompt(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = make_agent(protocol)
                agent.effective_window = 100_000
                question = "latest question? " + "0123456789" * 2000
                requests = []

                async def respond():
                    requests.append(deepcopy(getattr(agent, f"_{protocol}_messages")))
                    return reply(protocol, tokens=60_000 if len(requests) == 1 else 100)

                with (patch.object(agent, f"_call_{protocol}_stream", side_effect=respond),
                      patch("agents.agent.save_folded_session_memory"),
                      patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock,
                                   return_value={}) as summarize):
                    chat = getattr(agent, f"_chat_{protocol}")
                    for prompt in ("start", question, "continue"):
                        asyncio.run(chat(prompt))
                summarize.assert_awaited_once()
                self.assertNotIn(question, summarize.await_args.args[0])
                self.assertTrue(requests[1][-1]["content"].endswith(question))
                self.assertEqual(agent._fold_count, 1)

    def test_compression_invalidates_usage_only_when_history_changes(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = make_agent(protocol)
                messages = getattr(agent, f"_{protocol}_messages")
                for i in range(12):
                    if protocol == "openai":
                        messages.append({"role": "tool", "tool_call_id": str(i), "content": "old"})
                    else:
                        messages.append({"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": str(i), "content": "old"}]})
                agent._record_context_usage(1000)
                agent._run_compression_pipeline()
                self.assertEqual(agent.last_accounted_msg_index, len(messages))
                agent.last_api_call_time = 1
                agent._run_compression_pipeline()
                self.assertEqual((agent.last_input_token_count, agent.last_accounted_msg_index), (0, 0))
                agent._record_context_usage(100)
                agent.clear_history()
                self.assertEqual((agent.last_input_token_count, agent.last_accounted_msg_index), (0, 0))
