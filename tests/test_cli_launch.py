import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents.main import _load_cli_env, _resolve_api_config, cli_main, main, run_repl
from agents.paths import use_workspace
from agents.prompt import load_claude_md
from agents.tools import _read_file, _run_shell, _write_file


class LaunchTests(unittest.TestCase):
    def test_configuration_layers_across_aliases(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            home = Path(tmp)
            project = home / "project"
            project.mkdir()
            (home / ".researchagent").mkdir()
            (home / ".researchagent/.env").write_text("APIKEY=global-key\nAPI=https://global.test\nMODEL=global\n")
            (project / ".env").write_text("OPENAI_API_KEY=project-key\nMODEL=project\n")
            os.environ["OPENAI_BASE_URL"] = "https://process.test"
            with use_workspace(project), patch("pathlib.Path.home", return_value=home):
                _load_cli_env()
            self.assertEqual(_resolve_api_config(None), ("https://process.test", "project-key", True))
            self.assertEqual(os.environ["MODEL"], "project")

    def test_workspace_rules_files_and_shell(self):
        with tempfile.TemporaryDirectory() as tmp, use_workspace(Path(tmp)):
            Path(tmp, "CLAUDE.md").write_text("project-marker")
            self.assertIn("project-marker", load_claude_md())
            with patch("agents.tools._auto_update_memory_index"):
                _write_file({"file_path": "probe", "content": "marker"})
            self.assertIn("marker", _read_file({"file_path": "probe"}))
            self.assertEqual(Path(tmp, "probe").read_text(), "marker")
            self.assertEqual(Path(_run_shell({"command": "cd" if os.name == "nt" else "pwd"}).strip()), Path(tmp))

    def test_entry_points(self):
        for entry, argv, interactive in (
            (cli_main, [], True), (cli_main, ["task"], True),
            (cli_main, ["-p", "task"], False), (main, ["task"], False),
        ):
            with (self.subTest(argv=argv, entry=entry), patch("sys.argv", ["cli", *argv]),
                  patch("agents.main._load_cli_env"), patch("agents.main._load_env_file"),
                  patch("agents.main._resolve_api_config", return_value=("http://example.test", "key", True)),
                  patch("agents.main.Agent") as factory, patch("agents.main.run_repl", new_callable=AsyncMock) as repl,
                  patch("agents.main.run_one_shot", new_callable=AsyncMock) as once):
                entry()
                runner = repl if interactive else once
                runner.assert_awaited_once_with(factory.return_value, *(["task"] if argv else []))
                (once if interactive else repl).assert_not_awaited()
        with patch("sys.argv", ["cli", "-p"]), self.assertRaises(SystemExit) as error:
            cli_main()
        self.assertEqual(error.exception.code, 2)

    def test_interactive_first_task_can_approve_then_follow_up(self):
        agent = Mock()
        agent.drain_background_skill_tasks = AsyncMock()
        agent._mcp_manager.disconnect_all = AsyncMock()
        turns = []

        async def chat(task):
            if not turns:
                confirm = agent.set_confirm_fn.call_args.args[0]
                self.assertTrue(await confirm("write probe"))
            turns.append(task)

        agent.chat = chat
        with patch("builtins.input", side_effect=["y", "follow-up", "exit"]):
            asyncio.run(run_repl(agent, "first task"))
        self.assertEqual(turns, ["first task", "follow-up"])
        agent._mcp_manager.disconnect_all.assert_awaited_once()
