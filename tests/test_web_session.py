"""Exercise the local WebSocket entry point with the real permission and tool loops."""
import asyncio
from contextlib import chdir
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from agents.agent import Agent
from agents.web import IDLE_CLOSE_CODE, create_app, read_turn_trace
from test_deepseek_thinking import FakeStream, answer, call, chunk


class AnthropicStream(FakeStream):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def anthropic_stream(block, delta, reason):
    return AnthropicStream(
        NS(type="message_start", message=NS(usage=NS(input_tokens=1, output_tokens=0))),
        NS(type="content_block_start", index=0, content_block=block),
        NS(type="content_block_delta", index=0, delta=delta),
        NS(type="content_block_stop", index=0),
        NS(type="message_delta", delta=NS(stop_reason=reason), usage=NS(output_tokens=1)),
        NS(type="message_stop"),
    )


class WebSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp)
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        self.enterContext(chdir(self.workspace))
        self.enterContext(patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp, "RESEARCH_TOOL_MODE": "live"}))
        self.protocol = "openai"
        self.results = []
        self.model_gate = None
        self.client = TestClient(TestServer(create_app(self.make_agent)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def make_agent(self, **callbacks):
        openai = self.protocol == "openai"
        agent = Agent(api_base="https://example.invalid" if openai else None, api_key="unused",
                      custom_system_prompt="test", enable_folding=False, enable_online_evolution=False,
                      enable_memory_prefetch=False, enable_mcp=False, **callbacks)

        async def respond(**kwargs):
            if self.model_gate is not None:
                await self.model_gate.wait()
            messages = kwargs["messages"]
            last = messages[-1]
            if last["role"] == "user" and isinstance(last["content"], str):
                args = json.dumps({"file_path": str(self.root / last["content"]), "content": "approved"})
                if openai:
                    return FakeStream(chunk(calls=[call(name="write_file", args=args)], finish="tool_calls"))
                return anthropic_stream(NS(type="tool_use", name="write_file", id="write", input={}),
                                        NS(partial_json=args), "tool_use")
            result = last["content"] if openai else last["content"][0]["content"]
            self.results.append(result)
            text = "已拒绝，继续会话" if "User denied" in result else "已写入，继续会话"
            if openai:
                return answer(text)
            return anthropic_stream(NS(type="text", text=""), NS(text=text), "end_turn")

        create = agent._openai_client.chat.completions if openai else agent._anthropic_client.messages
        self.enterContext(patch.object(create, "create", respond))
        return agent

    async def connect(self, client=None):
        if client is None:
            client = self.client
        socket = await client.ws_connect("/session", origin=str(client.make_url("/")).rstrip("/"))
        self.assertEqual((await socket.receive_json(timeout=5))["type"], "ready")
        return socket

    async def check_approval_paths(self):
        socket = await self.connect()
        trace_ids = set()
        for allow in (True, False):
            name = f"{self.protocol}-{allow}.txt"
            await socket.send_json({"type": "prompt", "text": name})
            self.assertEqual((await socket.receive_json(timeout=5))["type"], "started")
            event = await socket.receive_json(timeout=5)
            self.assertEqual(event["type"], "approval")
            self.assertIn(name, event["message"])
            self.assertFalse((self.root / name).exists())
            await socket.send_json({"type": "decision", "id": event["id"], "allow": allow})
            output = []
            while True:
                event = await socket.receive_json(timeout=5)
                if event["type"] == "complete":
                    trace = event["trace"]
                    self.assertNotIn(trace["id"], trace_ids)
                    trace_ids.add(trace["id"])
                    self.assertFalse(trace["partial"])
                    self.assertEqual([s["tool_name"] for s in trace["spans"]], ["write_file"] if allow else [])
                    break
                self.assertEqual(event["type"], "output", event)
                output.append(event["text"])
            self.assertEqual("".join(output).strip(), "已写入，继续会话" if allow else "已拒绝，继续会话")
            self.assertEqual((self.root / name).exists(), allow)
            self.assertFalse((self.workspace / name).exists())
        self.assertIn("Successfully wrote", self.results[0])
        self.assertIn("User denied", self.results[1])
        await socket.close()

    async def test_openai_decisions_continue_same_session(self):
        await self.check_approval_paths()

    async def test_anthropic_decisions_continue_same_session(self):
        self.protocol = "anthropic"
        await self.check_approval_paths()

    def test_damaged_trace_keeps_valid_rows_and_excludes_foreign_or_duplicate_spans(self):
        directory = self.root / ".research" / "traces"
        directory.mkdir(parents=True)
        row = {"trace_id": "own", "span_id": "one", "parent_span_id": None, "tool_name": "web_fetch",
               "agent_role": "main", "timestamp": "2026-11-26T00:00:00Z", "source_type": "builtin",
               "latency_ms": 1.25, "ok": True, "cache_hit": False, "output_chars": 5, "error_type": None}
        rows = [json.dumps(row), "{broken", json.dumps({**row, "trace_id": "other"}),
                json.dumps(row), json.dumps({**row, "span_id": "two", "latency_ms": None})]
        (directory / "own.jsonl").write_text("\n".join(rows), encoding="utf-8")
        self.assertEqual(read_turn_trace("own"), {"id": "own", "partial": True, "spans": [row]})

    async def test_failed_turn_keeps_its_completed_tool_span(self):
        with patch("agents.agent.Agent._auto_save", side_effect=RuntimeError("fixture failure")):
            socket = await self.connect()
            await socket.send_json({"type": "prompt", "text": "before-failure.txt"})
            self.assertEqual((await socket.receive_json(timeout=5))["type"], "started")
            approval = await socket.receive_json(timeout=5)
            await socket.send_json({"type": "decision", "id": approval["id"], "allow": True})
            event = await socket.receive_json(timeout=5)
            while event["type"] == "output":
                event = await socket.receive_json(timeout=5)
            self.assertEqual(event["type"], "failed")
            self.assertEqual([s["tool_name"] for s in event["trace"]["spans"]], ["write_file"])
            await socket.close()

    async def test_pending_approval_is_bound_to_session_and_turn(self):
        first, second = await self.connect(), await self.connect()
        await first.send_json({"type": "prompt", "text": "pending.txt"})
        await first.receive_json(timeout=5)
        approval = await first.receive_json(timeout=5)
        decision = {"type": "decision", "id": approval["id"], "allow": True}
        await second.send_json(decision)
        self.assertEqual((await second.receive_json(timeout=5))["type"], "error")
        await first.send_json({**decision, "allow": "false"})
        self.assertEqual((await first.receive_json(timeout=5))["type"], "error")
        await first.send_json({"type": "prompt", "text": "overlap.txt"})
        self.assertEqual((await first.receive_json(timeout=5))["type"], "error")
        await first.close()
        await second.close()
        self.assertFalse((self.root / "pending.txt").exists())
        self.assertFalse((self.root / "overlap.txt").exists())

    async def short_lived_client(self):
        client = TestClient(TestServer(create_app(self.make_agent, idle_timeout=0.25)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def expect_idle_close(self, socket):
        frame = await socket.receive(timeout=4)
        self.assertEqual((frame.type, frame.data), (WSMsgType.CLOSE, IDLE_CLOSE_CODE))
        await socket.close()

    async def test_connected_session_without_work_expires(self):
        socket = await self.connect(await self.short_lived_client())
        await self.expect_idle_close(socket)

    async def test_waiting_for_approval_expires_without_executing(self):
        socket = await self.connect(await self.short_lived_client())
        await socket.send_json({"type": "prompt", "text": "expired.txt"})
        self.assertEqual((await socket.receive_json(timeout=4))["type"], "started")
        self.assertEqual((await socket.receive_json(timeout=4))["type"], "approval")
        await self.expect_idle_close(socket)
        self.assertFalse((self.root / "expired.txt").exists())

    async def test_active_model_survives_other_timeout_and_then_expires_after_completion(self):
        self.model_gate = asyncio.Event()
        client = await self.short_lived_client()
        active = await self.connect(client)
        await active.send_json({"type": "prompt", "text": "active.txt"})
        self.assertEqual((await active.receive_json(timeout=4))["type"], "started")
        await self.expect_idle_close(await self.connect(client))
        self.model_gate.set()
        approval = await active.receive_json(timeout=4)
        await active.send_json({"type": "decision", "id": approval["id"], "allow": True})
        while True:
            event = await active.receive_json(timeout=4)
            if event["type"] == "complete":
                break
            self.assertEqual(event["type"], "output", event)
        self.assertEqual((self.root / "active.txt").read_text(), "approved")
        await self.expect_idle_close(active)

    def test_idle_timeout_must_be_positive_and_finite(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                create_app(self.make_agent, idle_timeout=value)

    async def test_foreign_origin_and_host_are_rejected(self):
        with self.assertRaises(WSServerHandshakeError) as raised:
            await self.client.ws_connect("/session", origin="http://example.invalid")
        self.assertEqual(raised.exception.status, 403)
        response = await self.client.get("/", headers={"Host": "example.invalid"})
        self.assertEqual(response.status, 403)
