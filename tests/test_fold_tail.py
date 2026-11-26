from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from agents.session import load_session
from agents.session_memory import append_anthropic_message, select_fold_tail
from tests.test_context_p0 import make_agent, reply


def pair(user, answer):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": answer}]


def batch(protocol, ids=("a", "b"), result_ids=("b", "a")):
    if protocol == "openai":
        return [{"role": "assistant", "content": None, "reasoning_content": "read both files",
                 "tool_calls": [{"type": "function", "id": key,
                                 "function": {"name": "read_file", "arguments": "{}"}} for key in ids]},
                *[{"role": "tool", "tool_call_id": key, "content": "file.py:37 " + key} for key in result_ids]]
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": key, "name": "read_file", "input": {}}
                                               for key in ids]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": key, "content": "file.py:37 " + key}
                                         for key in result_ids]}]


class TailSelectionTests(unittest.TestCase):
    def test_batch_is_indivisible_and_budget_stops_at_first_unit_that_does_not_fit(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                tools = batch(protocol)
                history = pair("older", "reply") + tools
                def select(messages, budget):
                    return select_fold_tail(messages, openai=protocol == "openai", budget=budget, estimate=lambda m: 1)
                self.assertEqual(select(history, len(tools)), tools)
                self.assertEqual(select(history, len(tools) - 1), [])
                opener = {"role": "user", "content": "read both paths"}
                self.assertEqual(select([opener] + tools, len(tools)), tools)
                self.assertEqual(select([opener] + tools, len(tools) + 1), [opener] + tools)
                self.assertEqual(select(history + pair("recent", "reply"), len(tools) + 1), pair("recent", "reply"))

    def test_adjacent_anthropic_user_text_keeps_exact_separator_and_original_messages(self):
        first = {"role": "user", "content": "summary"}
        second = {"role": "user", "content": "path:37"}
        view = [first]
        append_anthropic_message(view, second)
        self.assertEqual(view, [{"role": "user", "content": "summary\n\npath:37"}])
        self.assertEqual(first["content"], "summary")
        self.assertEqual(second["content"], "path:37")

    def test_malformed_tool_batches_are_never_retained(self):
        for protocol in ("openai", "anthropic"):
            for ids, results in [(("a", "b"), ("a",)), (("a", "b"), ("a", "a")),
                                 (("a",), ("a", "extra")), (("a", "a"), ("a", "a"))]:
                with self.subTest(protocol=protocol, ids=ids, results=results):
                    history = pair("q", "a") + batch(protocol, ids, results)
                    tail = select_fold_tail(history, openai=protocol == "openai", budget=100, estimate=lambda m: 1)
                    self.assertEqual(tail, pair("q", "a"))


class FoldViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.sessions = root / "sessions"
        self.enterContext(patch("agents.session.SESSION_DIR", self.sessions))
        self.enterContext(patch("agents.agent.save_folded_session_memory"))
        self.enterContext(patch("agents.agent.Agent._generate_folded_session_memory", new_callable=AsyncMock,
                                return_value={"episode_memory": {"current_progress": "summary"}}))

    async def test_tool_trigger_excludes_current_batch_even_with_an_earlier_receipt(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = make_agent(protocol)
                attr = f"_{protocol}_messages"
                history = getattr(agent, attr)
                previous = batch(protocol)
                history.extend(pair("exact user constraint", "ack") + [{"role": "user", "content": "read both paths"}] + previous)
                calls = [("already-read", "read_file", {"file_path": "x"}),
                         ("compact-now", "compact_context", {}), ("skipped", "read_file", {"file_path": "y"})]
                requests = []
                async def respond():
                    requests.append(deepcopy(getattr(agent, attr)))
                    return reply(protocol, calls if len(requests) == 1 else ())
                with (patch.object(agent, f"_call_{protocol}_stream", side_effect=respond),
                      patch.object(agent, "_write_tool_trace"),
                      patch("agents.agent.execute_tool", new_callable=AsyncMock, return_value="current result") as read):
                    await getattr(agent, f"_chat_{protocol}")("continue")
                read.assert_awaited_once()
                view = requests[1]
                self.assertIn("exact user constraint", json.dumps(view))
                for message in previous:
                    self.assertIn(message, view)
                for key in ("already-read", "compact-now", "skipped"):
                    self.assertNotIn(key, json.dumps(view))
                if protocol == "anthropic":
                    self.assertTrue(all(a["role"] != b["role"] for a, b in zip(view, view[1:])))
                await agent._event_log.flush()

    async def test_threshold_keeps_pending_question_and_complete_batch_through_restore(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = make_agent(protocol)
                attr = f"_{protocol}_messages"
                previous = batch(protocol)
                history = getattr(agent, attr)
                history.extend(pair("q", "a") + [{"role": "user", "content": "read both paths"}] + deepcopy(previous))
                agent._record_context_usage(agent.effective_window)
                history.append({"role": "user", "content": "keep file.py:37 exactly"})
                await agent._ensure_context_budget()
                view = getattr(agent, attr)
                if protocol == "openai":
                    self.assertEqual(view[-4:-1], previous)
                    self.assertEqual(view[-1]["content"], "keep file.py:37 exactly")
                else:
                    self.assertEqual(view[-2], previous[0])
                    self.assertEqual(view[-1]["content"][:-1], previous[1]["content"])
                    self.assertEqual(view[-1]["content"][-1], {"type": "text", "text": "keep file.py:37 exactly"})
                    self.assertTrue(all(a["role"] != b["role"] for a, b in zip(view, view[1:])))
                agent._auto_save()
                await agent._event_log.flush()
                log = self.sessions / f"{agent.session_id}.events.jsonl"
                payload = json.loads(log.read_text().splitlines()[-1])["payload"]
                self.assertEqual(payload["retained_tail_messages"], 3 + len(previous))
                self.assertEqual(payload["pending_user_messages"], 1)
                resumed = make_agent(protocol)
                resumed.restore_session(load_session(agent.session_id))
                self.assertEqual(getattr(resumed, attr), view)
                await resumed._event_log.flush()

    async def test_oversized_latest_unit_falls_back_under_both_budget_limits(self):
        for protocol in ("openai", "anthropic"):
            for window, size in [(1000, 600), (200000, 13000)]:
                with self.subTest(protocol=protocol, window=window):
                    agent = make_agent(protocol)
                    agent.effective_window = window
                    attr = f"_{protocol}_messages"
                    history = getattr(agent, attr)
                    history.extend(pair("old", "reply") + pair("recent", "9" * size))
                    await agent.compact()
                    view = getattr(agent, attr)
                    self.assertEqual([m["role"] for m in view], ["system", "user"] if protocol == "openai" else ["user"])
                    self.assertIn("<session-folded-memory>", view[-1]["content"])

    async def test_refolding_does_not_retain_a_previous_summary_or_bridge_as_dialogue(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = make_agent(protocol)
                attr = f"_{protocol}_messages"
                getattr(agent, attr).extend(pair("u1", "a1") + pair("u2", "a2"))
                await agent.compact()
                once = deepcopy(getattr(agent, attr))
                await agent.compact()
                self.assertEqual(getattr(agent, attr), once)
