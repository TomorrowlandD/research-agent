import asyncio
from contextlib import chdir
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

from agents.agent import Agent
from agents.main import main
from agents.tools import check_permission
from tests.test_context_p0 import reply


class CandidateResearchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(chdir(self.root))
        self.enterContext(patch("pathlib.Path.home", return_value=self.root))
        self.enterContext(patch("agents.session.SESSION_DIR", self.root / "sessions"))
        self.enterContext(patch("agents.main._load_env_file"))
        self.enterContext(patch("agents.agent.build_system_prompt", return_value="test"))
        self.enterContext(patch("agents.agent.start_spinner"))
        self.enterContext(patch("agents.agent.stop_spinner"))
        self.enterContext(patch("agents.agent.print_tool_result"))
        self.enterContext(patch.dict("os.environ", {"RESEARCH_SEARCH_PROVIDER": "tavily", "RESEARCH_TOOL_MODE": "live"}))
        self.key = "arxiv:2401.12345"

    def agent(self, protocol="openai", **options):
        return Agent(api_base="http://example.test" if protocol == "openai" else None,
                     api_key="test", enable_memory_prefetch=False, enable_online_evolution=False,
                     enable_mcp=False, **options)

    def resume_cli(self, protocol, session_id, repl):
        config = ("http://example.test", "test", protocol == "openai")
        with (patch("sys.argv", ["researchagent", "--resume", session_id]),
              patch("agents.main._resolve_api_config", return_value=config),
              patch("agents.main.run_repl", side_effect=repl) as run_repl):
            main()
        run_repl.assert_awaited_once()

    def test_both_protocols_fold_resume_rewrite_query_and_reconsider(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = self.agent(protocol)
                path = self.root / ".research" / "artifacts" / agent.session_id / "candidates.jsonl"
                one = {"url": "https://arxiv.org/abs/2401.12345v1", "title": "Example", "content": "snippet"}
                two = {**one, "url": "https://arxiv.org/pdf/2401.12345"}
                update = {"canonical_id": self.key, "status": "rejected", "reason": "无开源代码"}
                search = ("s1", "web_search", {"query": "initial"})
                decide = ("d1", "update_candidate_status", update)
                search_again = ("s2", "web_search", {"query": "rewritten"})
                responses = [reply(protocol, [search]), reply(protocol, [decide]), reply(protocol)]
                with patch("agents.web_tools.SearchProvider.search", new_callable=AsyncMock, side_effect=[[one], [two]]) as upstream:
                    with patch.object(agent, f"_call_{protocol}_stream", side_effect=responses):
                        asyncio.run(agent.chat("找可复现论文"))
                    self.assertEqual(json.loads(path.read_text())["status"], "rejected")
                    cached = asyncio.run(agent._execute_tool_call("web_search", {"query": "initial"}))
                    self.assertTrue(cached.meta["cache_hit"])
                    self.assertIn("无开源代码", ElementTree.fromstring(cached.content).text)
                    with patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock, return_value={}):
                        asyncio.run(agent.compact())
                    self.assertEqual(agent._fold_count, 1)

                    async def resume(resumed):
                        self.assertEqual(resumed.session_id, agent.session_id)
                        resumed.enable_memory_prefetch = resumed.enable_mcp = resumed.enable_online_evolution = False
                        with patch.object(resumed, f"_call_{protocol}_stream",
                                          side_effect=[reply(protocol, [search_again]), reply(protocol)]):
                            await resumed.chat("换个查询再找")
                        messages = getattr(resumed, f"_{protocol}_messages")
                        text = (messages[-2]["content"] if protocol == "openai"
                                else messages[-2]["content"][0]["content"])
                        visible = ElementTree.fromstring(text).text
                        for expected in (two["url"], self.key, "历史已记录", "状态: rejected", "原因: 无开源代码"):
                            self.assertIn(expected, visible)
                        result = await resumed._execute_tool_call("list_candidates", {"status": "rejected"})
                        self.assertEqual([row["canonical_id"] for row in json.loads(result.content)], [self.key])
                        changed = await resumed._execute_tool_call("update_candidate_status",
                                                                  {**update, "status": "selected", "reason": "条件已变更"})
                        self.assertTrue(changed.ok, changed.content)

                    self.resume_cli(protocol, agent.session_id, resume)
                    self.assertEqual(upstream.await_count, 2)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([(r["canonical_id"], r["status"], r["reason"]) for r in rows],
                                 [(self.key, "selected", "条件已变更")])

    def test_tool_batch_fold_resume_and_deduplicate(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                self.check_research_after_tool_fold(protocol)

    def check_research_after_tool_fold(self, protocol):
        agent = self.agent(protocol)
        messages = f"_{protocol}_messages"
        stream = f"_call_{protocol}_stream"
        ledger = agent._candidate_ledger.path
        log = self.root / "sessions" / f"{agent.session_id}.events.jsonl"
        reason = "无开源代码"
        decision = {"canonical_id": self.key, "status": "rejected", "reason": reason}
        first_hit = {"url": "https://arxiv.org/abs/2401.12345v1", "title": "Example", "content": "snippet"}
        second_hit = {**first_hit, "url": "https://arxiv.org/pdf/2401.12345"}
        fold_batch = [("list-before", "list_candidates", {}), ("fold-now", "compact_context", {}),
                      ("list-after", "list_candidates", {})]
        batches = iter([[("search", "web_search", {"query": "reproducible study"})],
                        [("reject", "update_candidate_status", decision)], fold_batch, []])
        requests = []

        async def respond():
            requests.append(deepcopy(getattr(agent, messages)))
            return reply(protocol, next(batches))

        async def resume(resumed):
            self.assertEqual(resumed.session_id, agent.session_id)
            # The system prompt is regenerated on restore; compare the conversation itself.
            start = int(protocol == "openai")
            self.assertEqual(getattr(resumed, messages)[start:], getattr(agent, messages)[start:])
            resumed.enable_memory_prefetch = resumed.enable_mcp = resumed.enable_online_evolution = False
            again = ("rewritten", "web_search", {"query": "study with public implementation"})
            with patch.object(resumed, stream, side_effect=[reply(protocol, [again]), reply(protocol)]):
                await resumed.chat("换个查询复查")
            receipt = getattr(resumed, messages)[-2]
            if protocol == "openai":
                self.assertEqual(receipt["tool_call_id"], "rewritten")
                body = receipt["content"]
            else:
                result, = receipt["content"]
                self.assertEqual(result["tool_use_id"], "rewritten")
                body = result["content"]
            visible = ElementTree.fromstring(body).text
            for expected in (second_hit["url"], self.key, "状态: rejected", f"原因: {reason}"):
                self.assertIn(expected, visible)
            await resumed._event_log.flush()

        with patch("agents.web_tools.SearchProvider.search", new_callable=AsyncMock,
                   side_effect=[[first_hit], [second_hit]]) as upstream:
            with (patch.object(agent, stream, side_effect=respond),
                  patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock,
                               return_value={"episode_memory": {"current_progress": "筛选完成"}})):
                asyncio.run(agent.chat("找可复现论文"))
            self.assertEqual(len(requests), 4)
            before, folded = requests[-2:]
            self.assertEqual(folded[-4:], before[-4:])  # Both completed call/receipt pairs, verbatim.
            if protocol == "openai":
                calls = [c["id"] for m in folded for c in m.get("tool_calls", [])]
                receipts = [m["tool_call_id"] for m in folded if m["role"] == "tool"]
            else:
                blocks = [b for m in folded if isinstance(m["content"], list) for b in m["content"]]
                calls = [b["id"] for b in blocks if b["type"] == "tool_use"]
                receipts = [b["tool_use_id"] for b in blocks if b["type"] == "tool_result"]
                self.assertTrue(all(a["role"] != b["role"] for a, b in zip(folded, folded[1:])))
            self.assertEqual(calls, ["search", "reject"])
            self.assertEqual(receipts, calls)

            asyncio.run(agent._event_log.flush())
            prefix = log.read_bytes()
            events = [json.loads(line) for line in prefix.splitlines()]
            fold, = [e["payload"] for e in events if e["event_type"] == "context_fold"]
            self.assertEqual(fold["trigger"], "tool")
            results = [e["payload"] for e in events if e["event_type"] == "tool_result"]
            self.assertEqual([r["tool_call_id"] for r in results], ["search", "reject", "list-before", "fold-now"])
            self.assertTrue(all(not r["is_error"] for r in results))
            self.assertIn(reason, results[2]["content"])
            self.resume_cli(protocol, agent.session_id, resume)
            self.assertEqual(upstream.await_count, 2)

        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual([(r["canonical_id"], r["status"], r["reason"]) for r in rows],
                         [(self.key, "rejected", reason)])
        data = log.read_bytes()
        self.assertTrue(data.startswith(prefix))
        appended = [json.loads(line) for line in data[len(prefix):].splitlines()]
        self.assertEqual([e["event_type"] for e in appended],
                         ["session_resumed", "user_message", "assistant_message", "tool_result", "assistant_message"])
        self.assertEqual(appended[3]["payload"]["tool_call_id"], "rewritten")
        all_events = events + appended
        self.assertTrue(all(e["session_id"] == agent.session_id for e in all_events))
        self.assertEqual([e["seq"] for e in all_events], list(range(1, len(all_events) + 1)))

    def test_large_reason_keeps_hits_visible_and_full_reason_queryable(self):
        agent = self.agent()
        hits = [{"title": f"Study {i}", "url": f"https://example.org/{i}", "content": "x" * 300}
                for i in range(10)]
        args = {"query": "same", "max_results": 10}
        with patch("agents.web_tools.SearchProvider.search", new_callable=AsyncMock, return_value=hits):
            first = asyncio.run(agent._execute_tool_call("web_search", args))
            reason = "<missing&code>" * 100
            asyncio.run(agent._execute_tool_call("update_candidate_status", {
                "canonical_id": "url:https://example.org/0", "status": "rejected", "reason": reason}))
            cached = asyncio.run(agent._execute_tool_call("web_search", args))
        first_text = ElementTree.fromstring(first.content).text
        cached_text = ElementTree.fromstring(cached.content).text
        self.assertIn("状态: rejected", cached_text)
        self.assertIn("<missing&code>", cached_text)
        self.assertLessEqual(len(cached.content), 4000)
        shown = [hit["url"] for hit in hits if hit["url"] in first_text.splitlines()]
        self.assertTrue(shown)
        self.assertEqual(shown, [hit["url"] for hit in hits if hit["url"] in cached_text.splitlines()])
        listed = asyncio.run(agent._execute_tool_call("list_candidates", {}))
        rows = json.loads(listed.content)
        self.assertEqual([r["url"] for r in rows], shown)
        self.assertEqual(rows[0]["reason"], reason)

    def test_ledger_write_failure_returns_sources_and_retries_on_cache_hit(self):
        agent = self.agent()
        row = {"url": "https://arxiv.org/pdf/2401.12345", "title": "Example", "content": "snippet"}
        args = {"query": "same"}
        with patch("agents.web_tools.SearchProvider.search", new_callable=AsyncMock, return_value=[row]) as upstream:
            with patch.object(agent._candidate_ledger, "_save", side_effect=PermissionError("read-only")), self.assertLogs():
                first = asyncio.run(agent._execute_tool_call("web_search", args))
            self.assertTrue(first.ok)
            self.assertIn(row["url"], first.content)
            self.assertIn("read-only", first.meta["ledger_warning"])
            self.assertFalse(agent._candidate_ledger.path.exists())
            second = asyncio.run(agent._execute_tool_call("web_search", args))
            self.assertTrue(second.meta["cache_hit"])
            self.assertIn("首次记录", second.content)
            self.assertTrue(agent._candidate_ledger.path.exists())
            upstream.assert_awaited_once()

    def test_shared_provider_cache_cannot_leak_a_session_decision(self):
        first = self.agent()
        second = self.agent(web_cache=first._web_cache)
        row = {"title": "A", "url": "https://arxiv.org/abs/2401.12345", "content": "snippet"}
        with patch("agents.web_tools.SearchProvider.search", new_callable=AsyncMock, return_value=[row]) as upstream:
            for agent in (first, second):
                result = asyncio.run(agent._execute_tool_call("web_search", {"query": "same"}))
                self.assertIn("首次记录", result.content)
                self.assertNotIn("reject this", result.content)
                if agent is first:
                    result = asyncio.run(first._execute_tool_call("update_candidate_status", {
                        "canonical_id": self.key, "status": "rejected", "reason": "reject this"}))
                    self.assertTrue(result.ok)
            upstream.assert_awaited_once()
        self.assertEqual(check_permission("update_candidate_status", {}, "plan")["action"], "deny")
        self.assertEqual(check_permission("list_candidates", {}, "plan")["action"], "allow")
