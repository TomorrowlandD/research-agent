"""Run the two fixed pre-P1 web research tasks with the configured model.

From the repository root: PYTHONPATH=. python -s -B tests/fixtures/web_baseline.py
The BASELINE lines contain only the reproducible task metrics and answer.
"""
import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from unittest.mock import patch

from dotenv import dotenv_values

from agents.agent import Agent
from agents.main import _resolve_api_config


CASES = [
    ("static", "catalog.html", "读取该页，报告 North Lake 观测记录的样本编号。", "NL-4827"),
    ("interactive", "results.html", "打开该页，点击 Show result，报告显示的样本编号。", "SW-6319"),
]


STATIC_PAGE = "<h1>Observation catalog</h1><p>North Lake: NL-4827</p>"


def case_prompt(case, port):
    _, path, question, _ = case
    return f"{question} 使用网页工具核实，只回答编号。http://127.0.0.1:{port}/{path}"


def write_interactive_page(pages):
    (pages / "results.html").write_text(
        "<h1>Observation results</h1><button onclick=\"fetch('sample.txt').then(r => r.text())"
        ".then(t => document.querySelector('output').textContent=t)\">Show result</button><output></output>"
    )
    (pages / "sample.txt").write_text("SW-6319")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def approve(message):
    return message in {
        "MCP tool call: mcp__playwright__browser_" + action
        for action in ("navigate", "click", "snapshot", "wait_for", "close")
    }


async def run_case(case, port, model, api_options):
    label, _, _, expected = case
    agent = Agent(
        model=model, **api_options, confirm_fn=approve, max_turns=8, max_output_tokens=1500,
        enable_memory_prefetch=False, enable_online_evolution=False, enable_folding=False,
    )
    prompt = case_prompt(case, port)
    start = time.monotonic()
    try:
        result = await agent.run_once(prompt)
        elapsed = round(time.monotonic() - start, 3)
        trace = Path.home() / ".research" / "traces" / f"{agent.trace_id}.jsonl"
        rows = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        # run_once also streams intermediate planning text. Score the final assistant message.
        history = agent._openai_messages if agent.use_openai else agent._anthropic_messages
        answer = agent._message_text(history[-1]).strip() if history and history[-1]["role"] == "assistant" else ""
        web_evidence = any(r["ok"] and (r["source_type"] == "mcp" or r["tool_name"] == "web_fetch")
                           for r in rows)
        report = {
            "case": label, "model": model, "trace_id": agent.trace_id,
            "seconds": elapsed, "tool_calls": len(rows), "tools": [r["tool_name"] for r in rows],
            "main_tokens": result["tokens"], "side_tokens": result["side_tokens"],
            "answer": answer, "expected": expected,
            "passed": answer == expected and web_evidence, "spans": rows,
        }
        print("BASELINE " + json.dumps(report, ensure_ascii=False), flush=True)
        return report
    finally:
        await agent.drain_background_skill_tasks()
        await agent._mcp_manager.disconnect_all()


async def main(output):
    repo = Path.cwd()
    home = Path.home()
    # Resolve the CLI model configuration before starting the external MCP processes.
    values = {k: v for k, v in dotenv_values(repo / ".env").items() if v and k not in os.environ}
    with patch.dict(os.environ, values):
        base, key, openai_protocol = _resolve_api_config(None)
        model = os.environ.get("MODEL", "deepseek-chat")
    if not key:
        raise SystemExit("Configure a model API key before running the live baseline.")
    options = {"api_key": key, "api_base" if openai_protocol else "anthropic_base_url": base}

    with tempfile.TemporaryDirectory(prefix="research-web-baseline-") as temp:
        root = Path(temp)
        work = root / "workspace"
        work.mkdir()
        shutil.copyfile(repo / ".mcp.json", work / ".mcp.json")
        pages = root / "pages"
        pages.mkdir()
        (pages / "catalog.html").write_text(STATIC_PAGE)
        write_interactive_page(pages)
        handler = partial(QuietHandler, directory=str(pages))
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = {"HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
                   "npm_config_cache": os.environ.get("npm_config_cache", str(home / ".npm")),
                   "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                                                               str(home / ".cache/ms-playwright"))}
            try:
                os.chdir(work)
                with patch.dict(os.environ, env):
                    results = [await run_case(case, server.server_port, model, options) for case in CASES]
            finally:
                os.chdir(repo)
                server.shutdown()
                thread.join()
    if output:
        output.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return all(row["passed"] for row in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save task metrics and tool spans as JSON")
    raise SystemExit(0 if asyncio.run(main(parser.parse_args().output)) else 1)
