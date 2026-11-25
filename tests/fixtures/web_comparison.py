"""Collect serial, order-balanced live measurements of the two historical web paths."""
import argparse
import asyncio
from contextlib import chdir
from datetime import datetime, timezone
from functools import partial
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from threading import Thread
import time
from unittest.mock import patch

from dotenv import dotenv_values

from web_baseline import Agent, CASES, QuietHandler, STATIC_PAGE, approve, case_prompt, write_interactive_page
from agents.main import _resolve_api_config

ROOT = Path(__file__).resolve().parents[2]
REVISIONS = {"P0": "8702527", "P1": "3a999e5"}


async def measure(arm, case, port):
    config = json.loads((Path(os.environ["PYTHONPATH"]) / ".mcp.json").read_text())
    if arm == "P0":
        # Use the pinned Linux launch commands while retaining the old full main-agent catalog.
        for server in config["mcpServers"].values():
            server.pop("roles", None)
            server.pop("allowed_tools", None)
    env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v and k not in os.environ}
    with patch.dict(os.environ, env):
        base, key, openai = _resolve_api_config(None)
        model = os.environ.get("MODEL", "deepseek-chat")
    if not key:
        raise ValueError("Model API key required")
    options = {"api_key": key, "api_base" if openai else "anthropic_base_url": base}
    calls = []
    catalogs = {}
    execute = Agent._execute_tool_call

    async def observe(agent, name, args):
        role = "child" if agent.is_sub_agent else "main"
        catalogs[role] = len([t for t in agent.tools if t["name"].startswith("mcp__playwright__")])
        event = {"role": role, "tool": name, "ok": False}
        calls.append(event)
        result = await execute(agent, name, args)
        event["ok"] = result.ok if hasattr(result, "ok") else not str(result).startswith("Error:")
        return result

    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory() as tmp, chdir(tmp), patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp,
            "RESEARCH_TOOL_MODE": "live", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
        Path(".mcp.json").write_text(json.dumps(config))
        agent = Agent(model=model, **options, confirm_fn=approve, max_turns=8, max_output_tokens=1500,
                      enable_memory_prefetch=False, enable_folding=False, enable_online_evolution=False)
        try:
            prompt = case_prompt(case, port)
            with patch.object(Agent, "_execute_tool_call", observe):
                start = time.monotonic()
                result = await agent.run_once(prompt)
                seconds = round(time.monotonic() - start, 3)
            history = agent._openai_messages if agent.use_openai else agent._anthropic_messages
            answer = agent._message_text(history[-1]).strip() if history[-1]["role"] == "assistant" else ""
            evidence = any(c["ok"] and (c["tool"] == "web_fetch" or c["tool"].startswith("mcp__playwright__"))
                           for c in calls)
            return {"case": case[0], "started_utc": started, "model": model, "answer": answer,
                    "expected": case[3], "passed": answer == case[3] and evidence, "seconds": seconds,
                    "tool_calls": len(calls), "calls": calls, "browser_catalog_sizes": catalogs,
                    "main_tokens": result["tokens"], "side_tokens": result["side_tokens"]}
        finally:
            await agent.drain_background_skill_tasks()
            await agent._mcp_manager.disconnect_all()


def collect(path, rounds):
    if rounds < 2:
        raise ValueError("At least two paired rounds required")
    with tempfile.TemporaryDirectory() as tmp, path.open("x", encoding="utf-8") as out:
        root = Path(tmp)
        revs = {}
        for arm, ref in REVISIONS.items():
            revs[arm] = subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()
            archive = subprocess.check_output(["git", "archive", ref, "agents"], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(root / arm, filter="data")
            launch = subprocess.check_output(["git", "show", REVISIONS["P1"] + ":.mcp.json"], cwd=ROOT)
            (root / arm / ".mcp.json").write_bytes(launch)

        pages = root / "pages"
        pages.mkdir()
        (pages / CASES[0][1]).write_text(STATIC_PAGE)
        write_interactive_page(pages)
        with ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(pages))) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for repeat in range(1, rounds + 1):
                    for case_index, case in enumerate(CASES):
                        order = ["P0", "P1"] if (repeat + case_index) % 2 else ["P1", "P0"]
                        for arm in order:
                            env = {**os.environ, "PYTHONPATH": str(root / arm),
                                   "npm_config_cache": str(Path.home() / ".npm"),
                                   "PLAYWRIGHT_BROWSERS_PATH": str(Path.home() / ".cache/ms-playwright")}
                            cmd = [sys.executable, "-s", "-B", __file__, "sample", arm, str(case_index), str(server.server_port)]
                            proc = subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True, timeout=180)
                            if proc.returncode:
                                raise RuntimeError(f"Round {repeat} {arm}/{case[0]} failed: {proc.stderr[-1500:]}")
                            row = json.loads(proc.stdout.splitlines()[-1])
                            row.update(arm=arm, revision=revs[arm], repeat=repeat)
                            out.write(json.dumps(row, ensure_ascii=False) + "\n")
                            out.flush()
                            print(repeat, arm, case[0], row["passed"], row["seconds"], flush=True)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("collect")
    live.add_argument("output", type=Path)
    live.add_argument("--rounds", type=int, default=10)
    worker = commands.add_parser("sample")
    worker.add_argument("arm", choices=REVISIONS)
    worker.add_argument("case", type=int, choices=range(len(CASES)))
    worker.add_argument("port", type=int)
    args = parser.parse_args()
    if args.command == "collect":
        collect(args.output, args.rounds)
    else:
        print(json.dumps(asyncio.run(measure(args.arm, CASES[args.case], args.port)), ensure_ascii=False))
