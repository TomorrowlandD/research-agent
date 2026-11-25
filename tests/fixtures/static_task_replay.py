"""Record or replay the static P0/P1 task through Agent.run_once()."""
import argparse
import asyncio
from contextlib import chdir
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
from unittest.mock import patch

import httpx
from dotenv import dotenv_values

from agents.agent import Agent
from agents.deepseek import is_official_v4_pro
from agents.main import _resolve_api_config
from agents.tools import tool_definitions
from web_baseline import CASES, STATIC_PAGE, case_prompt


CASE = CASES[0]
SYSTEM = "Use web_fetch to verify the user's observation and answer with only the sample ID."


def live_config():
    values = {k: v for k, v in dotenv_values(".env").items() if v and k not in os.environ}
    with patch.dict(os.environ, values):
        base, key, openai = _resolve_api_config(None)
        model = os.environ.get("MODEL", "deepseek-chat")
    if not key:
        raise ValueError("A configured model API key is required to record this task")
    profile = {"model": model, "openai": openai, "deepseek": is_official_v4_pro(model, base)}
    return profile, {"api_key": key, "api_base" if openai else "anthropic_base_url": base}


async def run_static(bundle, options, home, *, replay):
    profile = bundle["profile"]
    agent = Agent(model=profile["model"], **options, custom_system_prompt=SYSTEM,
                  custom_tools=[t for t in tool_definitions if t["name"] == "web_fetch"],
                  max_turns=2, max_output_tokens=1500, thinking=False, enable_mcp=False,
                  enable_folding=False, enable_memory_prefetch=False, enable_online_evolution=False)
    sdk = agent._openai_client if profile["openai"] else agent._anthropic_client
    send = sdk._client.send
    sequence = []
    execute_tool = agent._execute_tool_call

    async def tool_call(name, args):
        sequence.append(name)
        return await execute_tool(name, args)

    roles = []

    async def model_turn(request, **kwargs):
        payload = json.loads(request.content)
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        index = len(roles)
        sequence.append("model")
        roles.append(payload["messages"][-1]["role"])
        if index >= 2:
            raise ValueError("Static task called the model more than twice")
        if replay:
            if index >= len(bundle["turns"]):
                raise ValueError(f"Missing recorded model response for turn {index + 1}")
            turn = bundle["turns"][index]
            if digest != turn["request_sha256"]:
                raise ValueError(f"Model input differs from recording at turn {index + 1}")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=turn["response_sse"], request=request)
        response = await send(request, **kwargs)
        response.raise_for_status()
        await response.aread()
        bundle["turns"].append({"request_sha256": digest, "response_sse": response.text})
        return response

    try:
        with (patch.object(sdk._client, "send", model_turn),
              patch.object(agent, "_execute_tool_call", tool_call)):
            await agent.run_once(case_prompt(CASE, bundle["port"]))
        messages = agent._openai_messages if profile["openai"] else agent._anthropic_messages
        answer = agent._message_text(messages[-1]).strip()
        root = home / ".research"
        trace = root / "traces" / f"{agent.trace_id}.jsonl"
        spans = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        expected_roles = ["user", "tool" if profile["openai"] else "user"]
        if (len(bundle["turns"]) != 2 or sequence != ["model", "web_fetch", "model"]
                or roles != expected_roles or messages[-1]["role"] != "assistant"
                or answer != CASE[3] or len(spans) != 1 or spans[0]["tool_name"] != "web_fetch"
                or not spans[0]["ok"]):
            raise ValueError(f"Static task diverged: roles={roles}, tools={[s['tool_name'] for s in spans]}, answer={answer!r}")
        if not replay:
            snapshot = root / "snapshots" / agent.trace_id / (spans[0]["span_id"] + ".json")
            bundle["snapshot"] = json.loads(snapshot.read_text())
        return {"answer": answer, "sequence": sequence}
    finally:
        await agent.drain_background_skill_tasks()
        await sdk.close()


def record(path):
    profile, options = live_config()
    requests = []

    class Catalog(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path != "/" + CASE[1]:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(STATIC_PAGE.encode())

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Catalog) as server:
        home = Path(tmp)
        bundle = {"version": 1, "profile": profile, "port": server.server_port, "turns": []}
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with chdir(home), patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp,
                        "RESEARCH_TOOL_MODE": "record", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                report = asyncio.run(run_static(bundle, options, home, replay=False))
        finally:
            server.shutdown()
            thread.join()
    if requests != ["/" + CASE[1]]:
        raise ValueError(f"Unexpected page requests: {requests}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
        try:
            json.dump(bundle, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.close()
            os.replace(out.name, path)
        finally:
            Path(out.name).unlink(missing_ok=True)
    return {**report, "model_responses_recorded": len(bundle["turns"]), "page_requests": len(requests)}


def replay(path):
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if bundle["version"] != 1:
        raise ValueError("Unsupported static recording format")
    profile = bundle["profile"]
    base = "https://api.deepseek.com" if profile["deepseek"] else "https://replay.invalid"
    options = {"api_key": "offline", "api_base" if profile["openai"] else "anthropic_base_url": base}
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        snapshot = home / "tool.json"
        snapshot.write_text(json.dumps(bundle["snapshot"]), encoding="utf-8")
        with (chdir(home), patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp,
                  "RESEARCH_TOOL_MODE": "replay", "RESEARCH_TOOL_SNAPSHOT": str(snapshot)}),
              patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                           side_effect=AssertionError("Outbound HTTP during static replay")) as upstream):
            report = asyncio.run(run_static(bundle, options, home, replay=True))
            upstream.assert_not_called()
    return {**report, "model_responses_replayed": len(bundle["turns"]), "upstream_http_requests": upstream.call_count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["record", "replay"])
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    report = (record if args.mode == "record" else replay)(args.file.resolve())
    print(json.dumps(report, ensure_ascii=False))
