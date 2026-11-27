import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from agents.agent import Agent
from agents.event_log import SessionEventLog, read_events
from agents.paths import use_workspace
from agents.session import load_session, save_session
from agents.web import SESSION_MANAGER, SessionConnection, create_app
from test_deepseek_thinking import FakeStream, answer, call, chunk
from test_web_session import anthropic_stream


async def until(test):
    async with asyncio.timeout(5):
        while not test():
            await asyncio.sleep(0.01)


class ManagedWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("agents.session.SESSION_DIR", self.root / "sessions"))
        self.enterContext(patch("pathlib.Path.home", return_value=self.root / "home"))
        self.enterContext(patch.dict(os.environ, {"RESEARCH_TOOL_MODE": "live"}))
        self.projects = [self.root / name for name in ("one", "two")]
        for project in self.projects:
            project.mkdir()
        self.protocol = "openai"
        self.agents = []
        self.gate = asyncio.Event()
        self.model_waiting = asyncio.Event()
        self.client = await self.server()

    async def server(self, **settings):
        client = TestClient(TestServer(create_app(self.agent, **settings)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def agent(self, **callbacks):
        openai = self.protocol == "openai"
        agent = Agent(api_base="https://example.invalid" if openai else None, api_key="test",
                      custom_system_prompt="test", enable_mcp=False, enable_folding=False,
                      enable_memory_prefetch=False, enable_online_evolution=False, **callbacks)
        self.agents.append(agent)

        async def model(**request):
            last = request["messages"][-1]
            if last["role"] == "user" and isinstance(last["content"], str):
                if last["content"] == "hold":
                    self.model_waiting.set()
                    await self.gate.wait()
                args = json.dumps({"file_path": "probe.txt", "content": last["content"]})
                if openai:
                    return FakeStream(chunk(calls=[call(name="write_file", args=args)], finish="tool_calls"))
                return anthropic_stream(NS(type="tool_use", name="write_file", id="write", input={}),
                                        NS(partial_json=args), "tool_use")
            return answer("saved") if openai else anthropic_stream(NS(type="text", text=""), NS(text="saved"), "end_turn")

        endpoint = agent._openai_client.chat.completions if openai else agent._anthropic_client.messages
        self.enterContext(patch.object(endpoint, "create", model))
        return agent

    async def connect(self, *, client=None, **params):
        client = client or self.client
        socket = await client.ws_connect("/session", params={"events": "1", **params},
                                         origin=str(client.make_url("/")).rstrip("/"))
        ready = await socket.receive_json(timeout=5)
        self.assertEqual(ready["type"], "ready")
        return socket, ready

    async def frame(self, ws, predicate):
        async with asyncio.timeout(5):
            while True:
                frame = await ws.receive_json()
                if predicate(frame):
                    return frame

    async def state(self, ws, kind):
        frame = await self.frame(ws, lambda e: e["type"] == "event" and
                                 e["event"]["event_type"] == "session_state" and
                                 e["event"]["payload"]["type"] == kind)
        return frame["event"]

    async def test_disconnect_with_approval_and_competing_decisions(self):
        for protocol, project in zip(("openai", "anthropic"), self.projects):
            with self.subTest(protocol=protocol):
                self.protocol = protocol
                ws, ready = await self.connect(workspace_dir=str(project))
                key = ready["session_id"]
                await ws.send_json({"type": "prompt", "text": protocol})
                approval = await self.state(ws, "approval")
                request = approval["payload"]["id"]
                await ws.close()
                (left, resumed), (right, peer) = await asyncio.gather(*[
                    self.connect(session_id=key, after=str(approval["seq"])) for _ in range(2)])
                for snapshot in (resumed, peer):
                    self.assertEqual(snapshot["status"], "waiting_for_approval")
                    self.assertEqual(snapshot["events"], [])
                    self.assertEqual([p["id"] for p in snapshot["pending_approvals"]], [request])
                decision = {"type": "decision", "id": request, "allow": True}
                await asyncio.gather(left.send_json(decision), right.send_json(decision))
                replies = await asyncio.gather(*[self.frame(s, lambda f: f["type"] in {"decision_ack", "error"})
                                                for s in (left, right)])
                self.assertCountEqual([r["code"] for r in replies], ["accepted", "decided"])
                await self.state(left, "complete")
                self.assertEqual((project / "probe.txt").read_text(), protocol)
                rows = read_events(key)
                self.assertEqual(sum(e["event_type"] == "tool_result" for e in rows), 1)
                await left.close()
                await right.close()

    async def test_resume_prefix_and_live_suffix_cover_offline_turn_exactly_once(self):
        project = self.projects[0]
        (project / ".research").mkdir()
        (project / ".research/settings.json").write_text(json.dumps({"permissions": {"allow": ["write_file"]}}))
        ws, ready = await self.connect(workspace_dir=str(project))
        key = ready["session_id"]
        await ws.send_json({"type": "prompt", "text": "hold"})
        await asyncio.wait_for(self.model_waiting.wait(), 5)
        await ws.close()
        session = self.client.app[SESSION_MANAGER].sessions[key]
        await until(lambda: not session.clients)
        # Hold the snapshot on the socket while new journal rows are committed.
        entered, send_ready = asyncio.Event(), asyncio.Event()
        original = SessionConnection.send

        async def delayed(connection):
            entered.set()
            await send_ready.wait()
            await original(connection)

        with patch.object(SessionConnection, "send", delayed):
            reconnect = asyncio.create_task(self.connect(session_id=key, after=str(ready["cursor"])))
            await asyncio.wait_for(entered.wait(), 5)
            self.gate.set()
            await until(lambda: session.turn is None)
            send_ready.set()
            ws, snapshot = await reconnect
        self.assertEqual(snapshot["status"], "running")
        rows = list(snapshot["events"])
        while rows[-1]["payload"].get("type") != "complete":
            frame = await ws.receive_json(timeout=5)
            if frame["type"] == "event":
                rows.append(frame["event"])
        recorded = read_events(key)[ready["cursor"]:]
        self.assertEqual(rows, recorded)
        self.assertEqual([r["seq"] for r in rows], list(range(ready["cursor"] + 1, len(read_events(key)) + 1)))
        self.assertEqual((project / "probe.txt").read_text(), "hold")
        self.assertEqual(sum(r["event_type"] == "tool_result" for r in rows), 1)
        await ws.close()

    async def test_detached_task_completes_and_restores_after_restart(self):
        ws, ready = await self.connect(workspace_dir=str(self.projects[0]))
        key = ready["session_id"]
        await ws.send_json({"type": "prompt", "text": "hold"})
        await asyncio.wait_for(self.model_waiting.wait(), 5)
        manager = self.client.app[SESSION_MANAGER]
        session = manager.sessions[key]
        session.agent.permission_mode = "acceptEdits"
        await ws.close()
        await until(lambda: not session.clients)
        self.gate.set()
        await until(lambda: session.turn is None)
        await self.client.close()
        fresh = await self.server()
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.connect(client=fresh, session_id=key, workspace_dir=str(self.projects[1]))
        self.assertEqual(error.exception.status, 400)
        ws, snapshot = await self.connect(client=fresh, session_id=key)
        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["workspace_dir"], str(self.projects[0]))
        self.assertEqual(sum(e["event_type"] == "tool_result" for e in snapshot["events"]), 1)
        self.assertIn("saved", json.dumps(snapshot["events"]))
        self.assertGreater(len(self.agents[-1]._openai_messages), 1)
        self.assertFalse((self.projects[1] / "probe.txt").exists())
        await ws.close()

    async def test_concurrent_restore_busy_stop_and_idle_eviction(self):
        client = await self.server(idle_timeout=0.3)
        manager = client.app[SESSION_MANAGER]
        ws, ready = await self.connect(client=client, workspace_dir=str(self.projects[0]))
        key = ready["session_id"]
        original = self.agents[-1]
        await ws.close()
        await until(lambda: key not in manager.sessions)
        self.assertTrue(original._openai_client.is_closed())
        (a, _), (b, _) = await asyncio.gather(*[self.connect(client=client, session_id=key) for _ in range(2)])
        self.assertEqual(len(self.agents), 2)
        await asyncio.gather(*[s.send_json({"type": "prompt", "text": "hold"}) for s in (a, b)])
        readers = [asyncio.create_task(self.frame(s, lambda f: f.get("code") == "busy")) for s in (a, b)]
        done, pending = await asyncio.wait(readers, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        for reader in pending:
            reader.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.assertEqual(len(done), 1)
        self.assertEqual(done.pop().result()["type"], "error")
        session = manager.sessions[key]
        await self.model_waiting.wait()
        await a.close()
        await b.close()
        await until(lambda: not session.clients)
        await manager._release(session, idle_since=session.last_activity)
        self.assertIs(manager.sessions[key], session)
        self.gate.set()
        await until(lambda: bool(session.pending))
        await manager._release(session, idle_since=session.last_activity)
        self.assertIs(manager.sessions[key], session)
        ws, resumed = await self.connect(client=client, session_id=key)
        request = resumed["pending_approvals"][0]["id"]
        await ws.send_json({"type": "stop"})
        await self.state(ws, "cancelled")
        await ws.send_json({"type": "decision", "id": request, "allow": True})
        self.assertEqual((await self.frame(ws, lambda f: f["type"] == "error"))["code"], "expired")
        self.assertFalse((self.projects[0] / "probe.txt").exists())
        await ws.close()
        await until(lambda: key not in manager.sessions)
        self.assertTrue(self.agents[-1]._openai_client.is_closed())

    async def test_approval_deadline_and_shutdown_invalidate_receipts(self):
        for action in ("deadline", "shutdown"):
            with self.subTest(action=action):
                client = await self.server(approval_timeout=0.25 if action == "deadline" else 60)
                ws, ready = await self.connect(client=client, workspace_dir=str(self.projects[0]))
                await ws.send_json({"type": "prompt", "text": "write"})
                row = await self.state(ws, "approval")
                if action == "deadline":
                    terminal = await self.state(ws, "cancelled")
                    self.assertEqual(terminal["payload"]["reason"], "approval_timeout")
                await client.close()
                ws, resume = await self.connect(session_id=ready["session_id"])
                self.assertEqual(resume["status"], "cancelled")
                self.assertFalse(resume["pending_approvals"])
                await ws.send_json({"type": "decision", "id": row["payload"]["id"], "allow": True})
                self.assertEqual((await self.frame(ws, lambda f: f["type"] == "error"))["code"], "expired")
                self.assertFalse((self.projects[0] / "probe.txt").exists())
                await ws.close()

    async def test_two_live_projects_apply_their_own_rules_and_paths(self):
        clients = []
        for project, rule in zip(self.projects, ("allow", "deny")):
            settings = project / ".research/settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"permissions": {rule: ["write_file"]}}))
            clients.append(await self.connect(workspace_dir=str(project)))
        await asyncio.gather(*(ws.send_json({"type": "prompt", "text": "hold"}) for ws, _ in clients))
        await asyncio.gather(*(self.state(ws, "started") for ws, _ in clients))
        self.gate.set()
        await asyncio.gather(*(self.state(ws, "complete") for ws, _ in clients))
        for index, (ws, ready) in enumerate(clients):
            rows = read_events(ready["session_id"])
            receipt, = [r for r in rows if r["event_type"] == "tool_result"]
            self.assertEqual(receipt["payload"]["is_error"], index == 1)
            self.assertEqual(len({r["trace_id"] for r in rows}), 1)
            await ws.close()
        self.assertEqual((self.projects[0] / "probe.txt").read_text(), "hold")
        self.assertFalse((self.projects[1] / "probe.txt").exists())

    async def test_failed_sender_leaves_other_client_and_approval_alive(self):
        project = self.projects[0]
        bad, first = await self.connect(workspace_dir=str(project))
        key = first["session_id"]
        session = self.client.app[SESSION_MANAGER].sessions[key]
        broken, = session.clients
        good, _ = await self.connect(session_id=key)
        with self.assertRaises(WSServerHandshakeError) as mismatch:
            await self.connect(session_id=key, workspace_dir=str(self.projects[1]))
        self.assertEqual(mismatch.exception.status, 400)
        with patch.object(broken.socket, "send_json", side_effect=ConnectionResetError):
            await good.send_json({"type": "prompt", "text": "survived"})
            pending = await self.state(good, "approval")
            await until(lambda: broken not in session.clients)
        await good.send_json({"type": "decision", "id": pending["payload"]["id"], "allow": True})
        await self.state(good, "complete")
        self.assertEqual((project / "probe.txt").read_text(), "survived")
        await bad.close()
        await good.close()

    async def test_interrupted_journal_restores_failed_status_without_replay(self):
        with use_workspace(self.projects[0]):
            original = self.agent()
            original.save_snapshot()
        key = original.session_id
        log = SessionEventLog(key, "openai")
        log.append("old", "session_state", {"type": "started", "status": "running"})
        log.append("old", "session_state", {"type": "approval", "status": "waiting_for_approval",
                                           "id": "old-request", "label": "approval", "message": "write"})
        await original._openai_client.close()
        ws, ready = await self.connect(session_id=key)
        self.assertEqual(ready["status"], "failed")
        self.assertEqual(ready["events"][-1]["payload"]["reason"], "service_interrupted")
        self.assertFalse(ready["pending_approvals"])
        await ws.send_json({"type": "decision", "id": "old-request", "allow": True})
        self.assertEqual((await ws.receive_json(timeout=5))["code"], "expired")
        self.assertFalse((self.projects[0] / "probe.txt").exists())
        await ws.close()

    async def test_shutdown_deadline_keeps_ownership_of_late_cleanup(self):
        socket, ready = await self.connect(workspace_dir=str(self.projects[0]))
        manager = self.client.app[SESSION_MANAGER]
        agent = self.agents[-1]
        gate = asyncio.Event()
        with patch.object(agent, "drain_background_skill_tasks", side_effect=gate.wait):
            try:
                with self.assertLogs("agents.session_manager", level="ERROR"):
                    await asyncio.wait_for(manager.shutdown(timeout=0.02), 1)
                self.assertIn(ready["session_id"], manager.sessions)
                self.assertEqual(manager.sessions[ready["session_id"]].start("late"), "closing")
            finally:
                gate.set()
                await until(lambda: not manager.cleanup_tasks)
        self.assertTrue(agent._openai_client.is_closed())
        await socket.close()

    async def test_legacy_snapshot_requires_provable_workspace(self):
        with use_workspace(self.projects[0]):
            original = self.agent()
            original.save_snapshot()
        key = original.session_id
        snapshot = load_session(key)
        del snapshot["metadata"]["cwd"]
        snapshot["openaiMessages"].append({"role": "user", "content": "legacy message"})
        save_session(key, snapshot)
        for query in ({}, {"workspace_dir": str(self.projects[1])}):
            with self.assertRaises(WSServerHandshakeError) as error:
                await self.connect(session_id=key, **query)
            self.assertEqual(error.exception.status, 400)
        ws, ready = await self.connect(session_id=key, workspace_dir=str(self.projects[0]))
        baseline = next(r for r in ready["events"] if r["event_type"] == "history_snapshot")
        self.assertEqual(baseline["payload"]["messages"], [{"role": "user", "content": "legacy message"}])
        self.assertEqual(load_session(key)["metadata"]["cwd"], str(self.projects[0]))
        await ws.close()
        await original._openai_client.close()
