from contextlib import chdir
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.main import main
from agents.session import get_latest_session_id, load_session, save_session


class SessionResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.project = root / "project"
        self.other = root / "other"
        self.project.mkdir()
        self.other.mkdir()
        self.enterContext(chdir(self.project))
        self.enterContext(patch("agents.session.SESSION_DIR", root / "sessions"))
        self.enterContext(patch("agents.main._load_env_file"))

    def agent(self):
        return Agent(custom_system_prompt="test", api_base="http://example.test", api_key="test")

    def run_cli(self, args, *, protocol="openai"):
        with (patch("sys.argv", ["researchagent", "--resume", *args]),
              patch("agents.main._resolve_api_config", return_value=("http://example.test", "test", protocol == "openai")),
              patch("agents.agent.build_system_prompt", return_value="test"),
              patch("agents.main.run_repl", new_callable=AsyncMock) as repl):
            main()
            return repl.await_args.args[0]

    def test_restore_and_save_same_id_and_creation_time(self):
        original = self.agent()
        original._openai_messages.append({"role": "user", "content": "remember me"})
        original._folded_session_memories.append({"trigger": "tool"})
        original._confirmed_paths.add("/old")
        original._auto_save()
        data = load_session(original.session_id)
        self.assertEqual(data["metadata"]["schema_version"], 2)
        data.update(approved_paths=["/old"], web_cache={"stale": True})
        save_session(original.session_id, data)
        resumed = self.run_cli([])
        self.assertEqual(resumed.session_id, original.session_id)
        self.assertEqual(resumed._openai_messages[-1]["content"], "remember me")
        self.assertEqual(resumed._folded_session_memories, original._folded_session_memories)
        self.assertFalse(resumed._confirmed_paths)
        self.assertFalse(resumed._web_cache.entries)
        resumed._auto_save()
        meta = load_session(original.session_id)["metadata"]
        self.assertEqual(meta["created_at"], data["metadata"]["created_at"])
        self.assertGreaterEqual(meta["updated_at"], data["metadata"]["updated_at"])
        self.assertEqual(self.run_cli([original.session_id]).session_id, resumed.session_id)

    def test_default_selection_and_explicit_scope_errors(self):
        first, second = self.agent(), self.agent()
        first._auto_save()
        second._auto_save()
        first_data = load_session(first.session_id)
        first_data["metadata"]["updated_at"] = "2099-01-01T00:00:00Z"
        save_session(first.session_id, first_data)
        self.assertEqual(get_latest_session_id("openai"), first.session_id)
        self.assertIsNone(get_latest_session_id("anthropic"))
        with patch("agents.main.print_error") as error, self.assertRaises(SystemExit):
            self.run_cli([first.session_id], protocol="anthropic")
        self.assertIn("protocol", error.call_args.args[0])
        with chdir(self.other):
            self.assertNotIn(self.run_cli([]).session_id, (first.session_id, second.session_id))
            with patch("agents.main.print_error") as error, self.assertRaises(SystemExit):
                self.run_cli([first.session_id])
            self.assertIn("project", error.call_args.args[0])

    def test_legacy_requires_id_then_upgrades_on_save(self):
        legacy = {"metadata": {"id": "old", "cwd": str(self.project), "startTime": "2025-01-01T00:00:00Z"},
                  "openaiMessages": [{"role": "system", "content": "system"}, {"role": "user", "content": "old text"}],
                  "anthropicMessages": None, "foldedSessionMemories": []}
        save_session("old", legacy)
        self.assertIsNone(get_latest_session_id("openai"))
        with patch("agents.main.print_warning") as warn:
            resumed = self.run_cli(["old"])
        self.assertIn("Legacy", warn.call_args.args[0])
        self.assertEqual(resumed.session_id, "old")
        self.assertEqual(resumed._openai_messages[-1]["content"], "old text")
        resumed._auto_save()
        self.assertEqual(load_session("old")["metadata"]["created_at"], legacy["metadata"]["startTime"])
        self.assertEqual(get_latest_session_id("openai"), "old")

    def invoke_installed(self, args, protocol="openai"):
        from agents.main import cli_main

        with (patch("sys.argv", ["researchagent", *args]), patch("agents.main._load_cli_env"),
              patch("agents.main._resolve_api_config", return_value=("http://example.test", "key", protocol == "openai")),
              patch("agents.agent.build_system_prompt", return_value="system"),
              patch("agents.main.run_one_shot", new_callable=AsyncMock) as once,
              patch("agents.main.run_repl", new_callable=AsyncMock) as repl):
            cli_main()
            return once, repl

    def test_project_selection_and_latest_for_both_protocols(self):
        from agents.memory import _project_hash
        from agents.session import project_sessions

        for directory in (self.project, self.other):
            with chdir(directory):
                for protocol in ("openai", "anthropic"):
                    for n in (1, 2):
                        sid = f"{directory.name}-{protocol}-{n}"
                        key = "openaiMessages" if protocol == "openai" else "anthropicMessages"
                        content = "old task" if protocol == "openai" else [{"type": "text", "text": "old task"}]
                        save_session(sid, {"metadata": {"session_id": sid, "project_hash": _project_hash(),
                                     "protocol": protocol, "updated_at": f"2026-11-0{n}T01:00:00Z"},
                                     key: [{"role": "user", "content": content}]})
        for directory in (self.project, self.other):
            with chdir(directory):
                for protocol in ("openai", "anthropic"):
                    with self.subTest(directory=directory, protocol=protocol):
                        prefix = f"{directory.name}-{protocol}-"
                        self.assertEqual([(sid, title) for _, sid, title in project_sessions(protocol)],
                                         [(prefix + "2", "old task"), (prefix + "1", "old task")])
                        once, _ = self.invoke_installed(["-c", "-p", "continue"], protocol)
                        self.assertEqual(once.await_args.args[0].session_id, prefix + "2")
                        with patch("builtins.input", side_effect=["invalid", "0", "3", "2"]):
                            _, repl = self.invoke_installed(["-r"], protocol)
                        self.assertEqual(repl.await_args.args[0].session_id, prefix + "1")

    def test_empty_cancel_and_conflicts(self):
        with self.assertRaises(SystemExit) as error:
            self.invoke_installed(["-c"])
        self.assertEqual(error.exception.code, 1)
        with patch("builtins.input", side_effect=AssertionError("unexpected input")):
            _, repl = self.invoke_installed(["-r"])
            repl.assert_not_awaited()
        self.agent()._auto_save()
        for answer in ("q", "", KeyboardInterrupt(), EOFError()):
            with self.subTest(answer=answer), patch("builtins.input", side_effect=[answer]):
                once, repl = self.invoke_installed(["-r"])
                once.assert_not_awaited()
                repl.assert_not_awaited()
        for args in (["-c", "-r"], ["-r", "--resume", "id"], ["--resume", "-c"], ["-r", "-p", "task"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                self.invoke_installed(args)
            self.assertEqual(error.exception.code, 2)

    def test_explicit_missing_or_invalid_id_aborts(self):
        for session_id in ("missing", "../outside"):
            with self.subTest(session_id=session_id), patch("agents.main.print_error"), self.assertRaises(SystemExit):
                self.run_cli([session_id])
