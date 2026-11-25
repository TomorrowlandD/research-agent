"""Local browser session for the ResearchAgent runtime. Run with python -m agents.web."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from aiohttp import WSMsgType, web

from .agent import Agent
from .main import _load_env_file, _resolve_api_config


ASSETS = Path(__file__).with_name("web_ui")


class BrowserSession:
    def __init__(self, socket, make_agent):
        self.socket = socket
        self.pending = {}
        self.turn = None
        self.agent = make_agent(confirm_fn=self.confirm, output_fn=self.output)
        self.agent.set_plan_approval_fn(self.review_plan)

    async def output(self, text):
        await self.socket.send_json({"type": "output", "text": text})

    async def ask(self, label, message):
        request_id = uuid4().hex
        decision = asyncio.get_running_loop().create_future()
        self.pending[request_id] = decision
        try:
            await self.socket.send_json({"type": "approval", "id": request_id, "label": label, "message": message})
            return await decision
        finally:
            self.pending.pop(request_id, None)

    async def confirm(self, message):
        return await self.ask("工具审批", message)

    async def review_plan(self, content):
        approved = await self.ask("计划审批", content)
        return {"choice": "manual-execute" if approved else "keep-planning"}

    async def run_turn(self, prompt):
        await self.socket.send_json({"type": "started"})
        try:
            await self.agent.chat(prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception("Web session turn failed")
            if not self.socket.closed:
                await self.socket.send_json({"type": "failed", "message": f"任务中断（{type(exc).__name__}），请检查服务端日志。"})
        else:
            if not self.socket.closed:
                await self.socket.send_json({"type": "complete"})

    async def receive(self, data):
        kind = data.get("type") if isinstance(data, dict) else None
        if kind == "prompt":
            prompt = data.get("text")
            if self.turn is not None and not self.turn.done():
                error = "当前任务尚未结束。"
            elif not isinstance(prompt, str) or not prompt.strip():
                error = "请输入任务。"
            else:
                self.turn = asyncio.create_task(self.run_turn(prompt.strip()))
                return
        elif kind == "decision":
            key = data.get("id")
            future = self.pending.get(key) if isinstance(key, str) else None
            if future is None or future.done() or type(data.get("allow")) is not bool:
                error = "审批无效或已处理。"
            else:
                future.set_result(data["allow"])
                return
        else:
            error = "无效的会话消息。"
        await self.socket.send_json({"type": "error", "message": error})

    async def close(self):
        if self.turn is not None:
            self.turn.cancel()
            await asyncio.gather(self.turn, return_exceptions=True)
        try:
            await self.agent.drain_background_skill_tasks()
        finally:
            try:
                await self.agent._mcp_manager.disconnect_all()
            finally:
                client = self.agent._openai_client or self.agent._anthropic_client
                await client.close()


@web.middleware
async def local_browser(request, handler):
    # Require a loopback Host even when a remote DNS name resolves to this listener.
    if request.url.host not in {"127.0.0.1", "localhost"}:
        raise web.HTTPForbidden()
    if request.path == "/session" and request.headers.get("Origin") != f"http://{request.host}":
        raise web.HTTPForbidden()
    return await handler(request)


def create_app(make_agent):
    app = web.Application(middlewares=[local_browser])
    sockets = set()

    async def page(request):
        name = request.match_info.get("asset", "index.html")
        return web.FileResponse(ASSETS / name, headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        })

    async def connect(request):
        socket = web.WebSocketResponse(max_msg_size=128 * 1024)
        session = BrowserSession(socket, make_agent)
        try:
            await socket.prepare(request)
            sockets.add(socket)
            await socket.send_json({"type": "ready"})
            async for frame in socket:
                if frame.type != WSMsgType.TEXT:
                    break
                try:
                    data = json.loads(frame.data)
                except ValueError:
                    data = None
                await session.receive(data)
        finally:
            sockets.discard(socket)
            await session.close()
        return socket

    async def shutdown(_app):
        await asyncio.gather(*(ws.close(code=1001) for ws in list(sockets)))

    app.router.add_get("/", page)
    app.router.add_get("/{asset:app\\.js|style\\.css}", page)
    app.router.add_get("/session", connect)
    app.on_shutdown.append(shutdown)
    return app


def main():
    parser = argparse.ArgumentParser(description="ResearchAgent 本地 Web 会话")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    _load_env_file()
    base, key, openai = _resolve_api_config(None)
    if not key:
        parser.error("请先在环境变量或 .env 中配置模型 API 密钥。")
    options = {"api_key": key, "api_base" if openai else "anthropic_base_url": base,
               "model": os.environ.get("MODEL") or "deepseek-chat"}
    web.run_app(create_app(lambda **callbacks: Agent(**options, **callbacks)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
