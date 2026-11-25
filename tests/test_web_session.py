"""Exercise the local WebSocket entry point with the real permission and tool loops."""
from contextlib import chdir
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
from agents.web import create_app
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
        self.client = TestClient(TestServer(create_app(self.make_agent)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def make_agent(self, **callbacks):
        openai = self.protocol == "openai"
        agent = Agent(api_base="https://example.invalid" if openai else None, api_key="unused",
                      custom_system_prompt="test", enable_folding=False, enable_online_evolution=False,
                      enable_memory_prefetch=False, enable_mcp=False, **callbacks)

        async def respond(**kwargs):
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

    async def connect(self):
        socket = await self.client.ws_connect("/session", origin=str(self.client.make_url("/")).rstrip("/"))
        self.assertEqual((await socket.receive_json(timeout=5))["type"], "ready")
        return socket

    async def check_approval_paths(self):
        socket = await self.connect()
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

    async def test_foreign_origin_and_host_are_rejected(self):
        with self.assertRaises(WSServerHandshakeError) as raised:
            await self.client.ws_connect("/session", origin="http://example.invalid")
        self.assertEqual(raised.exception.status, 403)
        response = await self.client.get("/", headers={"Host": "example.invalid"})
        self.assertEqual(response.status, 403)
