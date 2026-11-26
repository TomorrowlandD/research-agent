import asyncio
from contextlib import chdir
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

                    with (patch("sys.argv", ["researchagent", "--resume", agent.session_id]),
                          patch("agents.main._resolve_api_config", return_value=("http://example.test", "test", protocol == "openai")),
                          patch("agents.main.run_repl", side_effect=resume)):
                        main()
                    self.assertEqual(upstream.await_count, 2)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([(r["canonical_id"], r["status"], r["reason"]) for r in rows],
                                 [(self.key, "selected", "条件已变更")])

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
