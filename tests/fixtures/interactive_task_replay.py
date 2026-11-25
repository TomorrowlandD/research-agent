"""Record and replay the delegated interactive P0/P1 case with real Agent loops."""
import argparse
import asyncio
from contextlib import chdir, ExitStack
from dataclasses import asdict
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Thread
from unittest.mock import patch

import httpx

from agents.agent import Agent
from agents.mcp_client import McpManager
from agents.tool_result import ToolExecutionResult
from agents.tools import tool_definitions
from static_task_replay import live_config, model_exchange
from web_baseline import CASES, QuietHandler, approve, case_prompt, write_interactive_page

CASE = CASES[1]
PREFIX = "mcp__playwright__browser_"
ACTIONS = ["navigate", "snapshot", "click", "snapshot"]
SYSTEM = ("Delegate this interactive task to explore. Ask it to navigate, call browser_snapshot "
          "with empty arguments, click Show result, and call browser_snapshot again with empty "
          "arguments, even if the click output contains the number. Return only the sample ID.")


async def run(bundle, options, home, offline):
    profile = bundle["profile"]
    main = Agent(model=profile["model"], **options, custom_system_prompt=SYSTEM,
                 custom_tools=[t for t in tool_definitions if t["name"] == "agent"],
                 confirm_fn=approve, max_turns=8, max_output_tokens=1500, thinking=False,
                 enable_folding=False, enable_memory_prefetch=False, enable_online_evolution=False)
    turns = bundle["turns"]
    calls = bundle["browser_calls"]
    real_run = Agent.run_once
    real_mcp = McpManager.call_tool
    real_defs = McpManager.get_tool_definitions
    turn_count = 0
    call_count = 0

    async def run_with_model(agent, prompt):
        sdk = agent._openai_client if profile["openai"] else agent._anthropic_client
        send = sdk._client.send

        async def exchange(request, **kwargs):
            nonlocal turn_count
            index = turn_count
            turn_count += 1
            if offline and (index >= len(turns) or turns[index]["role"] != agent.agent_role):
                raise ValueError(f"Unexpected model turn {index + 1} for {agent.agent_role}")
            response = await model_exchange(turns, index, offline, send, request, kwargs)
            if not offline:
                turns[index]["role"] = agent.agent_role
            return response

        try:
            with patch.object(sdk._client, "send", exchange):
                return await real_run(agent, prompt)
        finally:
            await sdk.close()

    def definitions(manager, role):
        if offline:
            return bundle["definitions"][role]
        defs = real_defs(manager, role)
        bundle["definitions"][role] = defs
        return defs

    async def browser(manager, name, args):
        nonlocal call_count
        index = call_count
        call_count += 1
        if index >= len(ACTIONS) or name != PREFIX + ACTIONS[index]:
            raise ValueError(f"Unexpected browser call {index + 1}: {name}")
        if offline:
            row = calls[index]
            if (row["name"], row["args"]) != (name, args):
                raise ValueError(f"Browser call differs at position {index + 1}")
            return ToolExecutionResult(**row["result"])
        result = await real_mcp(manager, name, args)
        calls.append({"name": name, "args": args, "result": asdict(result)})
        return result

    with ExitStack() as stack:
        stack.enter_context(patch.object(Agent, "run_once", run_with_model))
        stack.enter_context(patch.object(McpManager, "get_tool_definitions", definitions))
        stack.enter_context(patch.object(McpManager, "call_tool", browser))
        if offline:
            stack.enter_context(patch.object(McpManager, "load_and_connect", return_value=None))
            original_popen = subprocess.Popen
            unexpected_processes = []

            def guard_process(args, *positional, **kwargs):
                # SDK platform detection can invoke uname; no MCP/browser process is allowed.
                if args == ["uname", "-p"] and not kwargs.get("shell"):
                    return original_popen(args, *positional, **kwargs)
                unexpected_processes.append(str(args))
                raise AssertionError("Unexpected process during replay")

            stack.enter_context(patch.object(subprocess, "Popen", guard_process))
            http = stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                                    side_effect=AssertionError("HTTP during replay")))
        try:
            await main.run_once(case_prompt(CASE, bundle["port"]))
        finally:
            await main.drain_background_skill_tasks()
            await main._mcp_manager.disconnect_all()

    history = main._openai_messages if profile["openai"] else main._anthropic_messages
    answer = main._message_text(history[-1]).strip()
    trace = home / ".research" / "traces" / (main.trace_id + ".jsonl")
    spans = [json.loads(line) for line in trace.read_text().splitlines()]
    parents = [s for s in spans if s["agent_role"] == "main" and s["tool_name"] == "agent"]
    if len(parents) != 1 or parents[0]["parent_span_id"] is not None or len(spans) != 5:
        raise ValueError("Expected one main delegation and four child spans")
    children = [s for s in spans if s["agent_role"] == "explore" and s["parent_span_id"] == parents[0]["span_id"]]
    actions = [s["tool_name"].removeprefix(PREFIX) for s in children]
    if (actions != ACTIONS or not all(s["ok"] for s in spans) or answer != CASE[3]
            or history[-1]["role"] != "assistant" or turn_count != len(turns)
            or call_count != len(calls) or len(calls) != 4):
        raise ValueError(f"Interactive run diverged: {actions}, {answer!r}, {turn_count} model turns")
    before, after = calls[1], calls[3]
    if (before["args"] != after["args"] or "Show result" not in before["result"]["content"]
            or answer in before["result"]["content"] or answer not in after["result"]["content"]):
        raise ValueError("Expected distinct pre-click and post-click results for identical snapshot calls")
    report = {"answer": answer, "main_delegations": 1, "explore_actions": actions, "model_turns": turn_count}
    if offline:
        http.assert_not_called()
        if unexpected_processes:
            raise ValueError(f"Unexpected process starts: {unexpected_processes}")
        report.update(upstream_http_requests=http.call_count, browser_starts=len(unexpected_processes))
    return report


def execute(mode, path):
    offline = mode == "replay"
    if offline:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if bundle["version"] != 1:
            raise ValueError("Unsupported interactive recording format")
        profile = bundle["profile"]
        base = "https://api.deepseek.com" if profile["deepseek"] else "https://replay.invalid"
        options = {"api_key": "offline", "api_base" if profile["openai"] else "anthropic_base_url": base}
    else:
        profile, options = live_config()
        bundle = {"version": 1, "profile": profile, "turns": [], "browser_calls": [], "definitions": {}}
    original_home = Path.home()
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        root = Path(tmp)
        if not offline:
            shutil.copyfile(".mcp.json", root / ".mcp.json")
            pages = root / "pages"
            pages.mkdir()
            write_interactive_page(pages)
            server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(pages))))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            bundle["port"] = server.server_port
        env = {"HOME": tmp, "USERPROFILE": tmp, "RESEARCH_TOOL_MODE": "live",
               "npm_config_cache": str(original_home / ".npm"),
               "PLAYWRIGHT_BROWSERS_PATH": str(original_home / ".cache/ms-playwright")}
        with chdir(root), patch.dict(os.environ, env):
            report = asyncio.run(run(bundle, options, root, offline))
    if not offline:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as out:
            try:
                json.dump(bundle, out, indent=2, ensure_ascii=False)
                out.write("\n")
                out.close()
                os.replace(out.name, path)
            finally:
                Path(out.name).unlink(missing_ok=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["record", "replay"])
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    print(json.dumps(execute(args.mode, args.file.resolve()), ensure_ascii=False))
