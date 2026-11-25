import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.agent import Agent


class ToolSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("pathlib.Path.home", return_value=self.home))
        self.snapshots = self.home / ".research" / "snapshots"

    def agent(self, mode=None):
        with patch.dict(os.environ):
            if mode is None:
                os.environ.pop("RESEARCH_TOOL_MODE", None)
            else:
                os.environ["RESEARCH_TOOL_MODE"] = mode
            return Agent(api_base="https://example.invalid", api_key="unused",
                         custom_system_prompt="test", enable_mcp=False)

    async def test_record_fixed_page_and_cache_hit(self):
        body = ("<article><p>" + "sample observation " * 2100 + "END-483</p></article>").encode()
        requests = []

        async def handle(reader, writer):
            try:
                requests.append((await reader.readuntil(b"\r\n\r\n")).splitlines()[0])
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                             + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        agent = self.agent("record")
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/sample"
            with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                first = await agent._execute_tool_call("web_fetch", {"url": url + "#first"})
                second = await agent._execute_tool_call("web_fetch", {"url": url + "#second"})
        self.assertEqual(requests, [b"GET /sample HTTP/1.1"])
        self.assertTrue(first.ok, first.content[:300])
        self.assertIn("END-483", first.content)
        self.assertGreater(len(first.content.encode()), 30 * 1024)
        self.assertTrue(second.meta["cache_hit"])
        files = list((self.snapshots / agent.trace_id).glob("*.json"))
        self.assertEqual(len(files), 2)
        rows = [json.loads(path.read_text()) for path in files]
        self.assertTrue(all(row["tool_name"] == "web_fetch" and row["normalized_args"] == {"url": url}
                            for row in rows))
        self.assertCountEqual([row["result"] for row in rows], [asdict(first), asdict(second)])

    async def test_default_live_writes_no_snapshot_and_record_preserves_error(self):
        args = {"file_path": str(self.home / "missing.txt")}
        live = await self.agent()._execute_tool_call("read_file", args)
        self.assertTrue(live.is_error)
        self.assertFalse(self.snapshots.exists())
        agent = self.agent("record")
        failure = await agent._execute_tool_call("read_file", args)
        self.assertTrue(failure.is_error)
        path, = (self.snapshots / agent.trace_id).glob("*.json")
        self.assertEqual(json.loads(path.read_text())["result"], asdict(failure))
