"""WSL/Linux acceptance fixture: real Web + MCP + Chromium, deterministic model turns."""
import argparse
import asyncio
from contextlib import chdir
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from aiohttp import web

from agents.agent import Agent
from agents.web import create_app


async def exercise(output):
    repo = Path(__file__).resolve().parents[2]
    started = set()
    session_ids = {}
    release = asyncio.Event()

    async def model(agent):
        history = agent._openai_messages
        label = next(m["content"] for m in history if m["role"] == "user")
        last = history[-1]
        if not agent.is_sub_agent:
            session_ids[label] = agent.session_id
        name = None
        args = {}
        text = None
        if last["role"] == "user":
            if agent.is_sub_agent:
                name = "mcp__playwright__browser_navigate"
                args = {"url": url + "/sample"}
            else:
                name = "agent"
                args = {"type": "explore", "description": "session process check", "prompt": label}
        elif agent.is_sub_agent and len([m for m in history if m["role"] == "tool"]) == 1:
            if "/sample" not in last["content"]:
                raise AssertionError("Navigation did not return the fixture page")
            started.add(label)
            if label == "survivor":
                await release.wait()
            name = "mcp__playwright__browser_snapshot"
        else:
            if "SESSION-PROBE" not in last["content"]:
                raise AssertionError("Missing live browser evidence")
            text = "SESSION-PROBE"
            await agent._emit_text(text)
        message = {"role": "assistant", "content": text}
        if name:
            message["tool_calls"] = [{"type": "function", "id": f"call-{len(history)}",
                                      "function": {"name": name, "arguments": json.dumps(args)}}]
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}

    async def sample(request):
        return web.Response(text="<h1>SESSION-PROBE</h1>", content_type="text/html")

    async def probe(request):
        return web.json_response(sorted(started))

    async def ids(request):
        return web.json_response(session_ids)

    async def resume(request):
        release.set()
        return web.Response()

    def make_agent(**options):
        return Agent(api_base="http://model.invalid", api_key="offline", custom_system_prompt="test",
                     enable_folding=False, enable_memory_prefetch=False, enable_online_evolution=False, **options)

    home = Path.home()
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(repo / ".mcp.json", Path(tmp) / ".mcp.json")
        env = {"HOME": tmp, "USERPROFILE": tmp, "npm_config_cache": str(home / ".npm"),
               "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(home / ".cache/ms-playwright"))}
        with (chdir(tmp), patch.dict(os.environ, env), patch("agents.session.SESSION_DIR", Path(tmp) / "sessions"),
              patch.object(Agent, "_call_openai_stream", model)):
            app = create_app(make_agent, idle_timeout=4, approval_timeout=180)
            app.router.add_get("/sample", sample)
            app.router.add_get("/probe", probe)
            app.router.add_get("/ids", ids)
            app.router.add_post("/release", resume)
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", 0).start()
                url = f"http://127.0.0.1:{runner.addresses[0][1]}"
                cmd = ["node", str(repo / "tests/fixtures/web_session_lifecycle.cjs"), url, str(os.getpid()), str(output)]
                process = await asyncio.create_subprocess_exec(*cmd)
                if await process.wait():
                    raise RuntimeError("Multi-tab browser acceptance failed")
            finally:
                await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(exercise(parser.parse_args().output.resolve()))
