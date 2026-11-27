import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agents.agent import Agent
from agents.paths import use_workspace
from agents.prompt import get_git_context
from agents.skill_evolution import resolve_skill_file
from agents.skills import create_skill, get_skill_by_name, record_feedback, reset_skill_cache
from agents.subagent import get_sub_agent_config, reset_agent_cache
from agents.tools import check_permission, execute_tool, reset_permission_cache
from fixtures.mcp_test_support import SERVER


class WorkspaceIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("pathlib.Path.home", return_value=root / "home"))
        self.projects = [root / "alpha", root / "beta"]
        for project in self.projects:
            project.mkdir()
            self.addCleanup(self.reset_caches, project)

    @staticmethod
    def reset_caches(project):
        with use_workspace(project):
            reset_permission_cache()
            reset_skill_cache()
            reset_agent_cache()

    @staticmethod
    def configure(project, rule, label):
        config = project / ".research"
        skills = config / "skills" / "guide"
        skills.mkdir(parents=True, exist_ok=True)
        agents = config / "agents"
        agents.mkdir(exist_ok=True)
        (config / "settings.json").write_text(json.dumps({"permissions": {rule: ["run_shell"]}}))
        (skills / "SKILL.md").write_text(f"---\nname: guide\ndescription: {label}\n---\n{label}\n")
        (agents / "reviewer.md").write_text(f"---\nname: reviewer\n---\n{label}\n")

    def check_config(self, rule, label):
        self.assertEqual(check_permission("run_shell", {"command": "pwd"})["action"], rule)
        self.assertEqual(get_skill_by_name("guide").description, label)
        self.assertEqual(get_sub_agent_config("reviewer")["system_prompt"].strip(), label)

    async def test_concurrent_project_context(self):
        barrier = asyncio.Barrier(2)

        async def run(project, rule):
            self.configure(project, rule, project.name)
            subprocess.run(["git", "init", "-q", "-b", project.name], cwd=project, check=True)
            with use_workspace(project):
                await barrier.wait()
                self.check_config(rule, project.name)
                context = await asyncio.to_thread(get_git_context)
                self.assertIn(f"?? {project.name}.txt", context)
                await execute_tool("write_file", {"file_path": "result.txt", "content": project.name})
                await barrier.wait()
                self.assertIn(project.name, await execute_tool("read_file", {"file_path": "result.txt"}))
                self.assertEqual((project / "result.txt").read_text(), project.name)

        for project in self.projects:
            (project / f"{project.name}.txt").touch()
        async with asyncio.timeout(10):
            await asyncio.gather(*(run(p, rule) for p, rule in zip(self.projects, ("allow", "deny"))))

    def test_cache_refresh_is_local_and_uses_normalized_path(self):
        for p in self.projects:
            self.configure(p, "deny", "original")
            with use_workspace(p):
                self.check_config("deny", "original")
            self.configure(p, "allow", "updated")

        first, second = self.projects
        self.reset_caches(second / ".." / first.name)
        with use_workspace(first):
            self.check_config("allow", "updated")
        with use_workspace(second):
            self.check_config("deny", "original")

    def test_project_skill_files_and_feedback(self):
        for project in self.projects:
            with use_workspace(project):
                result = create_skill("shared-name", project.name, "Apply the guide.")
                self.assertTrue(result["ok"], result)
                self.assertEqual(get_skill_by_name("shared-name").description, project.name)
                self.assertEqual(resolve_skill_file("shared-name", target="project"),
                                 project / ".research/skills/shared-name/SKILL.md")
                record_feedback("shared-name", "good", project.name)
                rows = (project / ".research/skill-evolution/usage.jsonl").read_text().splitlines()
                notes = [row["note"] for row in map(json.loads, rows) if row["event"] == "feedback"]
                self.assertEqual(notes, [project.name])

    async def test_deferred_tool_activation_belongs_to_one_agent(self):
        with use_workspace(self.projects[0]):
            for api_base in (None, "https://example.invalid/v1"):
                with self.subTest(api_base=api_base):
                    options = dict(api_key="test", api_base=api_base, enable_mcp=False)
                    first, second = Agent(**options), Agent(**options)
                    result = await first._execute_tool_call("tool_search", {"query": "enter_plan_mode"})
                    self.assertTrue(result.ok, result.content)
                    self.assertEqual([tool["name"] for tool in json.loads(result.content)], ["enter_plan_mode"])
                    for agent, active in ((first, True), (second, False)):
                        names = [tool["name"] for tool in agent._active_tool_definitions()]
                        self.assertEqual("enter_plan_mode" in names, active)
                        agent._refresh_runtime_system_prompt()
                        hint = next(line for line in agent._system_prompt.splitlines()
                                    if "available via tool_search" in line)
                        self.assertEqual("enter_plan_mode" in hint, not active)

    async def test_same_named_mcp_servers_run_in_separate_projects(self):
        barrier = asyncio.Barrier(2)

        async def run(project):
            shutil.copyfile(SERVER, project / "server.py")
            (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"demo": {
                "command": sys.executable, "args": ["-B", "server.py", "--pid-file", "pid.json"],
            }}}))
            with use_workspace(project):
                agent = Agent(api_key="test", custom_system_prompt="test")
                manager = agent._mcp_manager
                try:
                    await manager.load_and_connect()
                    await barrier.wait()
                    reply = await manager.call_tool("mcp__demo__get_pids", {})
                    expected = json.loads((project / "pid.json").read_text())
                    self.assertEqual(json.loads(reply.content), expected)
                    return expected["parent_pid"]
                finally:
                    await manager.disconnect_all()

        async with asyncio.timeout(30):
            pids = await asyncio.gather(*(run(project) for project in self.projects))
        self.assertNotEqual(*pids)
