"""Exercise the Web Trace panel using Chromium, real delegation/cache/HTTP and fixed model turns."""
import argparse
import asyncio
from contextlib import chdir
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from aiohttp import web

from agents.agent import Agent
from agents.web import create_app


async def check(evidence):
    driver = Path(__file__).with_suffix(".cjs").resolve()
    requests = 0

    async def page(request):
        nonlocal requests
        requests += 1
        return web.Response(text="<article>Trace observation: TRACE-CHECK</article>", content_type="text/html")

    async def model(agent):
        history = agent._openai_messages
        last = history[-1]
        count = sum(m["role"] == "tool" for m in history)
        name = None
        args = {}
        text = None
        if last["role"] == "user" and last["content"] == "空任务":
            text = "EMPTY"
        elif agent.is_sub_agent and count < 2:
            name = "web_fetch"
            args = {"url": url + "/sample#" + str(count)}
        elif agent.is_sub_agent:
            assert "TRACE-CHECK" in last["content"]
            text = "TRACE-CHECK"
        elif count == 0:
            name = "agent"
            args = {"type": "explore", "description": "verify cached observation", "prompt": "fetch the sample twice"}
        elif count == 1:
            assert "TRACE-CHECK" in last["content"]
            name = "read_file"
            args = {"file_path": "missing-sample.txt"}
        else:
            text = "TRACE-CHECK"
        message = {"role": "assistant", "content": text}
        if name is None:
            await agent._emit_text(text)
        else:
            message["tool_calls"] = [{"type": "function", "id": f"call-{len(history)}",
                                      "function": {"name": name, "arguments": json.dumps(args)}}]
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}

    def make_agent(**callbacks):
        return Agent(api_base="http://model.invalid", api_key="offline", custom_system_prompt="test",
                     enable_mcp=False, enable_folding=False, enable_memory_prefetch=False,
                     enable_online_evolution=False, **callbacks)

    with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
        env = {"HOME": tmp, "USERPROFILE": tmp, "RESEARCH_TOOL_MODE": "live",
               "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        with patch.dict(os.environ, env), patch.object(Agent, "_call_openai_stream", model):
            app = create_app(make_agent)
            app.router.add_get("/sample", page)
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", 0).start()
                url = f"http://127.0.0.1:{runner.addresses[0][1]}"
                process = await asyncio.create_subprocess_exec("node", str(driver), url, tmp)
                if await process.wait() or requests != 1:
                    raise RuntimeError(f"Trace acceptance failed; upstream requests={requests}")
                trace, = (Path(tmp) / ".research" / "traces").glob("*.jsonl")
                evidence.write_bytes(trace.read_bytes())
            finally:
                await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(check(parser.parse_args().output.resolve()))
