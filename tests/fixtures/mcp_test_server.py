"""Offline MCP v2 fixture. Run with the same interpreter as the client tests.

The two parent-* modes deliberately retain inherited pipes. Their supervising
test must keep PID handles and clean up on failure; all ordinary exits run finally.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool


def tool(name, properties=None, required=()):
    return Tool(name=name, description=f"Fixture {name}", input_schema={
        "type": "object", "properties": properties or {}, "required": list(required),
        "additionalProperties": False,
    })


TOOLS = [
    tool("echo", {"text": {"type": "string"}}, ["text"]),
    tool("delayed_echo", {"text": {"type": "string"}, "delay_ms": {"type": "integer", "minimum": 0}}, ["text", "delay_ms"]),
    tool("tool_error", {"message": {"type": "string"}, "empty": {"type": "boolean"}}),
    tool("content_case", {"case": {"type": "string", "enum": ["multi", "empty", "nontext", "mixed"]}}, ["case"]),
    tool("get_pids"),
    tool("read_test_env"),
    tool("stderr_burst", {"bytes_count": {"type": "integer", "minimum": 0, "maximum": 1048576}}),
    tool("exit_server", {"delay_ms": {"type": "integer", "minimum": 0}}),
]


def text(value):
    return CallToolResult(content=[TextContent(type="text", text=value)])


class ObservedStream:
    """Observe public transport messages; optionally discard cancellation only.

    The SDK still performs all JSON-RPC parsing, dispatch and response creation.
    Events are written after send completes, so tests can prove a late response.
    """
    def __init__(self, stream, options, direction):
        self.stream, self.options, self.direction = stream, options, direction

    def record(self, message, discarded=False):
        if self.options.events_file and hasattr(message, "message"):
            wire = message.message.model_dump(by_alias=True, exclude_none=True)
            with Path(self.options.events_file).open("a", encoding="utf-8") as out:
                out.write(json.dumps({"direction": self.direction, "discarded": discarded, "wire": wire}) + "\n")

    async def receive(self):
        while True:
            message = await self.stream.receive()
            discarded = (self.options.mode == "ignore-cancel" and
                         getattr(getattr(message, "message", None), "method", None) == "notifications/cancelled")
            self.record(message, discarded)
            if not discarded:
                return message

    async def send(self, message):
        await self.stream.send(message)
        self.record(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        import anyio
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)

    async def aclose(self):
        await self.stream.aclose()


async def serve(options):
    child = None
    exit_timer = None
    if options.mode.startswith("parent-"):
        # Spawn before stdio_server diverts the server's fd 0/1, so the child
        # really owns copies of the wire pipes. It never reads or writes them.
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(120)"],
            stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        )
    pids = {"parent_pid": os.getpid(), "child_pid": child.pid if child else None}
    try:
        if options.pid_file:
            Path(options.pid_file).write_text(json.dumps(pids), encoding="utf-8")

        async def list_tools(ctx, params):
            if options.mode == "list-error":
                raise MCPError(-32603, "fixture list failure")
            if options.mode == "list-delay":
                await asyncio.sleep(options.delay_ms / 1000)
            return ListToolsResult(tools=TOOLS)

        async def call_tool(ctx, params):
            nonlocal exit_timer
            args = params.arguments or {}
            name = params.name
            if name in ("echo", "delayed_echo"):
                if name == "delayed_echo":
                    await asyncio.sleep(args["delay_ms"] / 1000)
                return text(args["text"])
            if name == "tool_error":
                return CallToolResult(is_error=True, content=[] if args.get("empty") else [
                    TextContent(type="text", text=args.get("message", "fixture error"))])
            if name == "content_case":
                first, second = TextContent(type="text", text="first"), TextContent(type="text", text="second")
                image = ImageContent(type="image", mime_type="image/png", data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII=")
                return CallToolResult(content={"multi": [first, second], "empty": [], "nontext": [image], "mixed": [first, image, second]}[args["case"]])
            if name == "get_pids":
                return text(json.dumps(pids))
            if name == "read_test_env":
                return text(json.dumps({key: os.environ.get(key) for key in (
                    "RESEARCH_MCP_TEST_INHERITED", "RESEARCH_MCP_TEST_OVERRIDE")}))
            if name == "stderr_burst":
                sys.stderr.write("x" * min(args.get("bytes_count", 262144), 1048576))
                sys.stderr.flush()
                return text("ok")
            if name == "exit_server":
                if exit_timer:
                    exit_timer.cancel()
                # Deliberate crash: skips finally to exercise transport cleanup.
                exit_timer = asyncio.get_running_loop().call_later(
                    max(args.get("delay_ms", 50), 10) / 1000, os._exit, 0)
                return text("exiting")
            raise MCPError(-32602, f"Unknown fixture tool: {name}")

        server = Server("research-mcp-fixture", version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)

        async def initialize_control(ctx, call_next):
            if ctx.method == "initialize":
                if options.mode == "init-error":
                    raise MCPError(-32603, "fixture initialization failure")
                if options.mode == "init-delay":
                    await asyncio.sleep(options.delay_ms / 1000)
            return await call_next(ctx)

        server.middleware.append(initialize_control)
        async with stdio_server() as (read_stream, write_stream):
            if options.events_file or options.mode == "ignore-cancel":
                read_stream = ObservedStream(read_stream, options, "received")
                write_stream = ObservedStream(write_stream, options, "sent")
            await server.run(read_stream, write_stream, server.create_initialization_options())
            if options.mode == "parent-stubborn":
                # EOF is insufficient: the SDK must terminate the process tree.
                await asyncio.Event().wait()
    finally:
        if exit_timer:
            exit_timer.cancel()
        if child is not None:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal", choices=[
        "normal", "init-error", "init-delay", "list-error", "list-delay",
        "parent-stubborn", "parent-exits-child-holds-pipes", "ignore-cancel",
    ])
    parser.add_argument("--delay-ms", type=int, default=200)
    parser.add_argument("--pid-file")
    parser.add_argument("--events-file")
    asyncio.run(serve(parser.parse_args()))
