"""Local browser session for the ResearchAgent runtime. Run with python -m agents.web."""
import argparse
import asyncio
import json
import logging
import math
import os
from pathlib import Path

from aiohttp import WSMsgType, web

from .session_manager import SessionManager
from .agent import Agent
from .paths import RESEARCH_DIRNAME
from .main import _load_env_file, _resolve_api_config


DEFAULT_IDLE_TIMEOUT = 900
IDLE_CLOSE_CODE = 4001
SESSION_MANAGER = web.AppKey("session_manager", SessionManager)
ASSETS = Path(__file__).with_name("web_ui")


def read_turn_trace(trace_id):
    """Load only the completed turn selected by the server-side Agent."""
    rows = []
    seen = set()
    partial = False
    path = Path.home() / RESEARCH_DIRNAME / "traces" / f"{trace_id}.jsonl"
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    partial = True
                    continue
                if not isinstance(row, dict) or row.get("trace_id") != trace_id:
                    partial = True
                    continue
                labels = ("span_id", "tool_name", "agent_role", "timestamp", "source_type")
                latency = row.get("latency_ms")
                if (not all(isinstance(row.get(k), str) and row[k] for k in labels)
                        or row["span_id"] in seen or type(latency) not in (int, float)
                        or not math.isfinite(latency) or latency < 0
                        or type(row.get("cache_hit")) is not bool or type(row.get("ok")) is not bool):
                    partial = True
                    continue
                seen.add(row["span_id"])
                rows.append(row)
    except FileNotFoundError:
        pass  # The runtime creates a trace file on the first executed tool call.
    except (OSError, UnicodeError):
        partial = True
        logging.getLogger(__name__).warning("Cannot read trace %s", trace_id)
    return {"id": trace_id, "spans": rows, "partial": partial}


class SessionConnection:
    """One bounded outgoing queue keeps a slow socket out of the Agent loop."""
    def __init__(self, socket, events, idle_timeout):
        self.socket = socket
        self.events = events
        self.idle_timeout = idle_timeout
        self.timer = None
        self.queue = asyncio.Queue(maxsize=512)
        self.closing = False

    def waiting(self, waiting):
        if self.timer is not None and not self.timer.expired():
            deadline = asyncio.get_running_loop().time() + self.idle_timeout if waiting else None
            self.timer.reschedule(deadline)

    def offer(self, frame):
        if self.closing:
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.closing = True
            asyncio.create_task(self.socket.close(code=1013, message=b"Reconnect to recover events"))

    async def send(self):
        try:
            while True:
                await self.socket.send_json(await self.queue.get())
        except Exception:
            logging.getLogger(__name__).debug("WebSocket delivery failed", exc_info=True)
            await self.socket.close()

    def receive(self, session, data):
        kind = data.get("type") if isinstance(data, dict) else None
        if kind == "prompt":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                code = session.start(text.strip())
                if code == "started":
                    return
                message = "当前任务尚未结束。" if code == "busy" else "服务正在关闭。"
            else:
                code, message = "invalid_prompt", "请输入任务。"
        elif kind == "decision":
            code = session.decide(data.get("id"), data.get("allow"))
            if code == "accepted":
                if self.events:
                    self.offer({"type": "decision_ack", "code": code, "id": data["id"]})
                return
            message = "审批无效或已处理。"
        elif kind == "stop":
            self.offer({"type": "stop_ack", "code": session.stop()})
            return
        else:
            code, message = "invalid_message", "无效的会话消息。"
        self.offer({"type": "error", "code": code, "message": message})


@web.middleware
async def local_browser(request, handler):
    # Require a loopback Host even when a remote DNS name resolves to this listener.
    if request.url.host not in {"127.0.0.1", "localhost"}:
        raise web.HTTPForbidden()
    if request.path == "/session" and request.headers.get("Origin") != f"http://{request.host}":
        raise web.HTTPForbidden()
    return await handler(request)


def create_app(make_agent, *, idle_timeout=DEFAULT_IDLE_TIMEOUT, approval_timeout=DEFAULT_IDLE_TIMEOUT):
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError("idle_timeout must be a positive finite duration")
    if not math.isfinite(approval_timeout) or approval_timeout <= 0:
        raise ValueError("approval_timeout must be a positive finite duration")
    manager = SessionManager(make_agent, read_turn_trace, idle_timeout, approval_timeout)
    app = web.Application(middlewares=[local_browser])
    app[SESSION_MANAGER] = manager
    sockets = set()

    async def page(request):
        name = request.match_info.get("asset", "index.html")
        return web.FileResponse(ASSETS / name, headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        })

    async def connect(request):
        socket = web.WebSocketResponse(max_msg_size=128 * 1024, heartbeat=30)
        if not socket.can_prepare(request).ok:
            raise web.HTTPBadRequest(text="WebSocket upgrade required.")
        key = request.query.get("session_id")
        client = SessionConnection(socket, key is not None or request.query.get("events") == "1", idle_timeout)
        try:
            after = int(request.query.get("after", "0"))
            session = await manager.attach(client, key, request.query.get("workspace_dir"), after)
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except RuntimeError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc

        sender = None
        try:
            await socket.prepare(request)
            sockets.add(socket)
            sender = asyncio.create_task(client.send())
            async with asyncio.timeout(None) as timer:
                client.timer = timer
                client.waiting(session.status != "running")
                async for frame in socket:
                    if frame.type != WSMsgType.TEXT:
                        break
                    try:
                        data = json.loads(frame.data)
                    except ValueError:
                        data = None
                    client.receive(session, data)
        except TimeoutError:
            if client.timer is None or not client.timer.expired():
                raise
            await socket.close(code=IDLE_CLOSE_CODE, message=b"Session idle timeout")
        finally:
            client.timer = None
            client.closing = True
            sockets.discard(socket)
            session.detach(client)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
        return socket

    async def shutdown(_app):
        manager.closing = True
        await asyncio.gather(manager.shutdown(), *(ws.close(code=1001) for ws in list(sockets)))

    app.router.add_get("/", page)
    app.router.add_get("/{asset:app\\.js|trace\\.js|style\\.css}", page)
    app.router.add_get("/session", connect)
    app.on_shutdown.append(shutdown)
    return app


def main():
    parser = argparse.ArgumentParser(description="ResearchAgent 本地 Web 会话")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT, metavar="SECONDS",
                        help="等待输入的连接和无连接空闲会话的超时秒数（默认 900）")
    parser.add_argument("--approval-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT, metavar="SECONDS",
                        help="每项审批可等待的最长秒数（默认 900）")
    args = parser.parse_args()
    if not math.isfinite(args.idle_timeout) or args.idle_timeout <= 0:
        parser.error("--idle-timeout must be a positive finite duration")
    if not math.isfinite(args.approval_timeout) or args.approval_timeout <= 0:
        parser.error("--approval-timeout must be a positive finite duration")
    _load_env_file()
    base, key, openai = _resolve_api_config(None)
    if not key:
        parser.error("请先在环境变量或 .env 中配置模型 API 密钥。")
    options = {"api_key": key, "api_base" if openai else "anthropic_base_url": base,
               "model": os.environ.get("MODEL") or "deepseek-chat"}
    app = create_app(lambda **callbacks: Agent(**options, **callbacks), idle_timeout=args.idle_timeout,
                     approval_timeout=args.approval_timeout)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
