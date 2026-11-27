import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, Mock, call, patch

from agents.main import run_repl
from agents.skills import SkillDefinition


class ReplCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = Mock()
        self.agent.chat = AsyncMock()
        self.agent.extract_now = AsyncMock(return_value={"ok": True})
        self.agent.drain_background_skill_tasks = AsyncMock()
        self.agent._mcp_manager.disconnect_all = AsyncMock()
        self.lookup = self.enterContext(patch("agents.main.get_skill_by_name", return_value=None))
        self.execute = self.enterContext(patch("agents.main.execute_skill"))

    async def session(self, *lines):
        with patch("builtins.input", side_effect=[*lines, "exit"]), redirect_stdout(io.StringIO()) as out:
            await run_repl(self.agent)
        return out.getvalue()

    async def test_unknown_command_returns_to_prompt_for_cost(self):
        self.agent.show_cost.side_effect = lambda: print("cost shown")
        output = await self.session("/不存在", "/cost")
        self.assertLess(output.index("未知命令 `/不存在`"), output.index("cost shown"))
        self.agent.chat.assert_not_called()
        self.lookup.assert_called_once_with("不存在")

    async def test_prefix_typo_does_not_run_extraction(self):
        output = await self.session("/extract_now_typo hint")
        self.assertIn("未知命令 `/extract_now_typo`", output)
        self.agent.extract_now.assert_not_called()
        self.agent.chat.assert_not_called()

    async def test_invalid_builtin_arguments_report_usage(self):
        commands = ("/skill-feedback", "/skill-evolve", "/skill-create", "/clear")
        output = await self.session(*commands[:3], "/clear extra")
        for command in commands:
            self.assertIn(f"Usage: {command}", output)
        self.agent.clear_history.assert_not_called()
        self.lookup.assert_not_called()
        self.agent.chat.assert_not_called()

    async def test_empty_input_and_bare_slash(self):
        output = await self.session("  ", "/", " /\t ")
        self.assertEqual(output.count("REPL commands:"), 2)
        self.assertIn("/extract_now [hint]", output)
        self.lookup.assert_not_called()
        self.agent.chat.assert_not_called()

    async def test_paths_and_slash_text_go_to_chat(self):
        messages = ("/tmp/test.log 帮我分析报错", "/ 请帮我查一下相关论文", "普通消息")
        await self.session(*messages)
        self.assertEqual(self.agent.chat.await_args_list, [call(msg) for msg in messages])
        self.lookup.assert_not_called()

    async def test_optional_argument_can_be_a_path(self):
        await self.session("/extract_now", "  /extract_now\t/tmp/test.log  ")
        self.assertEqual(self.agent.extract_now.await_args_list, [call(""), call("/tmp/test.log")])
        self.agent.chat.assert_not_called()

    async def test_internal_skill_returns_unknown(self):
        self.lookup.return_value = SkillDefinition("internal", "", user_invocable=False)
        output = await self.session("/internal")
        self.assertIn("未知命令 `/internal`", output)
        self.execute.assert_not_called()
        self.agent.chat.assert_not_called()

    async def test_user_invocable_skills_receive_arguments(self):
        self.lookup.side_effect = [SkillDefinition("inline", ""), SkillDefinition("worker", "", context="fork")]
        self.execute.return_value = {"prompt": "expanded inline skill"}
        await self.session("/inline\t/tmp/test.log", "/worker  two words")
        self.execute.assert_called_once_with("inline", "/tmp/test.log")
        self.assertEqual(self.agent.chat.await_args_list, [
            call("expanded inline skill"),
            call('Use the skill tool to invoke "worker" with args: two words'),
        ])
