"""Single-process ownership of Web sessions, independent of their connections."""
import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from uuid import uuid4
from weakref import WeakValueDictionary

from . import session as storage
from .event_log import read_events
from .memory import _project_hash
from .paths import use_workspace

log = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    decision: asyncio.Future
    timeout: asyncio.TimerHandle


class ManagedSession:
    def __init__(self, manager, session_id, workspace):
        self.manager = manager
        self.id = session_id
        self.workspace = workspace
        self.agent = manager.make_agent(session_id=session_id, confirm_fn=self.confirm, output_fn=self.output)
        self.agent.set_plan_approval_fn(self.review_plan)
        self.clients = set()
        self.turn = None
        self.trace_id = None
        self.worker = None
        self.stop_reason = None
        self.snapshot_dirty = False
        self.pending = {}
        self.decisions = {}
        self.events = []
        self.journal_status = "idle"
        self.journal_pending = {}
        self.status = "idle"
        self.expiry = None
        self.last_activity = asyncio.get_running_loop().time()

    def apply_event(self, row):
        self.events.append(row)
        if row["event_type"] != "session_state":
            return
        event = row["payload"]
        self.journal_status = event["status"]
        kind = event["type"]
        if kind == "approval":
            self.journal_pending[event["id"]] = event
        elif kind in {"decided", "expired"}:
            self.journal_pending.pop(event["id"], None)
            self.decisions[event["id"]] = kind

    async def committed(self, row):
        rows = [row]
        if row["seq"] != len(self.events) + 1:
            # A separate journal writer may have appended between our two rows.
            history = await asyncio.to_thread(read_events, self.id)
            rows = history[len(self.events):row["seq"]]
        for committed in rows:
            self.apply_event(committed)
            for client in tuple(self.clients):
                if client.events:
                    client.offer({"type": "event", "event": committed})

    def broadcast(self, event, *, legacy_only=False):
        for client in tuple(self.clients):
            if not legacy_only or not client.events:
                client.offer(event)

    def transition(self, kind, status, **fields):
        self.status = status
        self.last_activity = asyncio.get_running_loop().time()
        event = {"type": kind, "status": status, **fields}
        self.agent.record_session_event(event, trace_id=self.trace_id)
        return event

    def refresh_timers(self):
        for client in self.clients:
            client.waiting(self.status != "running")

    def attach(self, client, after):
        # No suspension between taking this prefix and subscribing to its suffix.
        cursor = len(self.events)
        if after < 0 or after > cursor:
            raise ValueError("Invalid event cursor.")
        ready = {"type": "ready", "session_id": self.id, "workspace_dir": str(self.workspace),
                 "status": self.journal_status, "cursor": cursor}
        if client.events:
            ready.update(events=self.events[after:], pending_approvals=list(self.journal_pending.values()))
        client.offer(ready)
        self.clients.add(client)
        self.last_activity = asyncio.get_running_loop().time()
        if self.expiry is not None:
            self.expiry.cancel()
            self.expiry = None
        client.waiting(self.status != "running")

    def detach(self, client):
        self.clients.discard(client)
        self.schedule_expiry()

    def schedule_expiry(self):
        self.last_activity = asyncio.get_running_loop().time()
        if self.clients or self.turn is not None or self.manager.closing:
            return
        if self.expiry is not None:
            self.expiry.cancel()
        self.expiry = asyncio.get_running_loop().call_later(
            self.manager.idle_timeout, self.manager.expire, self, self.last_activity,
        )

    async def output(self, text):
        self.broadcast({"type": "output", "text": text})

    async def ask(self, label, message):
        if self.stop_reason is not None:
            raise asyncio.CancelledError
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        event = self.transition("approval", "waiting_for_approval", id=request_id, label=label, message=message)
        timeout = asyncio.get_running_loop().call_later(self.manager.approval_timeout, self.stop, "approval_timeout")
        self.pending[request_id] = PendingApproval(future, timeout)
        self.refresh_timers()
        self.broadcast(event, legacy_only=True)
        try:
            return await future
        finally:
            timeout.cancel()
            if self.pending.pop(request_id, None) is not None:
                self.expire_approval(request_id)

    async def confirm(self, message):
        return await self.ask("工具审批", message)

    async def review_plan(self, content):
        approved = await self.ask("计划审批", content)
        return {"choice": "manual-execute" if approved else "keep-planning"}

    def expire_approval(self, key):
        self.decisions[key] = "expired"
        self.transition("expired", "waiting_for_approval" if self.pending else "running", id=key)

    def decide(self, key, allow):
        if not isinstance(key, str) or type(allow) is not bool:
            return "invalid_decision"
        pending = self.pending.pop(key, None)
        if pending is None:
            return self.decisions.get(key, "unknown_approval")
        pending.timeout.cancel()
        if pending.decision.done():
            self.expire_approval(key)
            return "expired"
        self.decisions[key] = "decided"
        pending.decision.set_result(allow)
        self.transition("decided", "waiting_for_approval" if self.pending else "running", id=key, allow=allow)
        self.refresh_timers()
        return "accepted"

    def start(self, prompt):
        if self.manager.closing:
            return "closing"
        if self.turn is not None:
            return "busy"
        self.stop_reason = None
        self.snapshot_dirty = True
        self.trace_id = uuid4().hex
        with use_workspace(self.workspace):
            event = self.transition("started", "running")
            self.turn = asyncio.create_task(self.run_turn(prompt))
        self.refresh_timers()
        self.broadcast(event, legacy_only=True)
        return "started"

    def stop(self, reason="stopped"):
        if self.turn is None or self.status in {"complete", "failed", "cancelled"}:
            return "idle"
        if self.stop_reason is None:
            self.stop_reason = reason
            for key in list(self.pending):
                pending = self.pending.pop(key)
                pending.timeout.cancel()
                self.expire_approval(key)
                pending.decision.cancel()
            if self.worker is not None:
                self.worker.cancel()
        return "stopping"

    async def run_turn(self, prompt):
        fields = {}
        try:
            if self.stop_reason is None:
                self.worker = asyncio.create_task(self.agent.chat(prompt, trace_id=self.trace_id))
                await self.worker
                self.snapshot_dirty = False  # Main Agent saves before chat returns.
            kind = "cancelled" if self.stop_reason or self.agent._aborted else "complete"
        except asyncio.CancelledError:
            kind = "cancelled"
        except Exception as exc:
            log.exception("Web session turn failed")
            kind = "failed"
            fields["message"] = f"任务中断（{type(exc).__name__}），请检查服务端日志。"
        finally:
            self.worker = None

        if self.agent.trace_id == self.trace_id:
            try:
                fields["trace"] = await asyncio.to_thread(self.manager.read_trace, self.trace_id)
            except Exception:
                log.exception("Cannot read trace %s", self.trace_id)
        if self.stop_reason:
            kind = "cancelled"
            fields["reason"] = self.stop_reason
        if kind != "complete":
            try:
                await asyncio.to_thread(self.agent.save_snapshot)
                self.snapshot_dirty = False
            except Exception:
                log.exception("Cannot save interrupted session %s", self.id)
                kind = "failed"
                fields["message"] = "会话快照保存失败，请检查服务端日志。"
        try:
            event = self.transition(kind, kind, **fields)
            await self.agent.flush_events()
        except Exception:
            log.exception("Cannot finalize Web session %s", self.id)
            self.status = "failed"
            event = {"type": "failed", "message": "会话保存失败，请检查服务端日志。"}
            self.broadcast(event)
        else:
            if kind == "cancelled":
                # The existing browser unlocks its composer on failed/complete.
                event = {**event, "type": "failed", "message": "任务已取消。"}
            self.broadcast(event, legacy_only=True)
        finally:
            self.turn = None
            self.refresh_timers()
            self.schedule_expiry()

    async def release(self):
        with use_workspace(self.workspace):
            try:
                await self.agent.drain_background_skill_tasks()
                await self.agent.flush_events()
            finally:
                try:
                    await self.agent._mcp_manager.disconnect_all()
                finally:
                    client = self.agent._openai_client or self.agent._anthropic_client
                    await client.close()


class SessionManager:
    def __init__(self, make_agent, read_trace, idle_timeout, approval_timeout):
        self.make_agent = make_agent
        self.read_trace = read_trace
        self.default_workspace = Path.cwd().resolve()
        self.idle_timeout = idle_timeout
        self.approval_timeout = approval_timeout
        self.sessions = {}
        self.locks = WeakValueDictionary()
        self.cleanup_tasks = set()
        self.opening = set()
        self.closing = False

    def lock(self, key):
        lock = self.locks.get(key)
        if lock is None:
            self.locks[key] = lock = asyncio.Lock()
        return lock

    def workspace(self, requested, snapshot):
        saved = snapshot["metadata"].get("cwd") if snapshot is not None else None
        if saved is not None and (not isinstance(saved, str) or not Path(saved).is_absolute()):
            raise ValueError("Invalid stored workspace.")
        if snapshot is not None and saved is None and (
            not requested or not snapshot["metadata"].get("project_hash")
        ):
            raise ValueError("Legacy session requires a verifiable workspace.")
        raw = saved if requested is None else requested
        if raw == "":
            raise ValueError("Empty workspace.")
        path = (self.default_workspace / (raw or ".")).resolve()
        if not path.is_dir() or (saved is not None and Path(saved).resolve() != path):
            raise ValueError("Workspace does not match or is unavailable.")
        if snapshot is not None:
            with use_workspace(path):
                expected = snapshot["metadata"].get("project_hash")
                if expected and expected != _project_hash():
                    raise ValueError("Session workspace hash does not match.")
        return path

    async def attach(self, client, requested_id, directory, after=0):
        key = uuid4().hex if requested_id is None else requested_id
        storage.validate_session_id(key)
        if after < 0 or (requested_id is None and after != 0):
            raise ValueError("Invalid event cursor.")
        async with self.lock(key):
            if self.closing:
                raise RuntimeError("Service is closing.")
            session = self.sessions.get(key)
            if session is None:
                opened = asyncio.get_running_loop().create_future()
                self.opening.add(opened)
                try:
                    session = await self.open_session(key, requested_id, directory, client, after)
                finally:
                    opened.set_result(None)
                    self.opening.discard(opened)
            else:
                if directory is not None and self.workspace(directory, None) != session.workspace:
                    raise ValueError("Session belongs to another workspace.")
                await session.agent.flush_events()
                if self.closing:
                    raise RuntimeError("Service is closing.")
                session.attach(client, after)
            return session

    async def open_session(self, key, requested_id, directory, client, after):
        snapshot = None
        if requested_id is not None:
            snapshot = await asyncio.to_thread(storage.load_session, key)
            if snapshot is None:
                raise LookupError("Unknown session.")
        workspace = self.workspace(directory, snapshot)
        with use_workspace(workspace):
            session = ManagedSession(self, key, workspace)
            try:
                await self.initialize(session, snapshot)
                if self.closing:
                    raise RuntimeError("Service is closing.")
                session.attach(client, after)
            except BaseException:
                await session.release()
                raise
        self.sessions[key] = session
        return session

    async def initialize(self, session, snapshot):
        agent = session.agent
        if snapshot is not None:
            agent.restore_session(snapshot)
            await agent.flush_events()
            for row in await asyncio.to_thread(read_events, session.id):
                session.apply_event(row)
        agent.set_event_fn(session.committed)
        if snapshot is not None and not any(row["event_type"] in {"user_message", "history_snapshot"}
                                            for row in session.events):
            messages = snapshot.get("openaiMessages") or snapshot.get("anthropicMessages") or []
            messages = [m for m in messages if m.get("role") != "system"]
            if messages:
                agent.record_session_event({"messages": messages}, event_type="history_snapshot")
        if session.journal_status in {"running", "waiting_for_approval"}:
            session.trace_id = next(row["trace_id"] for row in reversed(session.events)
                                    if row["event_type"] == "session_state")
            for key in list(session.journal_pending):
                session.expire_approval(key)
            session.transition("failed", "failed", reason="service_interrupted")
        else:
            session.status = session.journal_status
        # Bind even a newly created, empty session before handing out its ID.
        if snapshot is None:
            await asyncio.to_thread(agent.save_snapshot)
        elif not snapshot["metadata"].get("cwd"):
            snapshot["metadata"]["cwd"] = str(session.workspace)
            await asyncio.to_thread(storage.save_session, session.id, snapshot)
        await agent.flush_events()

    def expire(self, session, last_activity):
        self.track_cleanup(self._release(session, idle_since=last_activity))

    def track_cleanup(self, coro):
        task = asyncio.create_task(coro)
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_finished)

    def cleanup_finished(self, task):
        self.cleanup_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Session cleanup failed: %s", task.exception())

    async def _release(self, session, *, idle_since=None):
        async with self.lock(session.id):
            if self.sessions.get(session.id) is not session:
                return
            if idle_since is not None and (
                session.clients or session.turn is not None or session.last_activity != idle_since
            ):
                return
            if session.expiry is not None:
                session.expiry.cancel()
            if session.turn is not None:
                session.stop("service_shutdown")
                await session.turn
            if idle_since is not None:
                try:
                    with use_workspace(session.workspace):
                        await session.agent.flush_events()
                        if session.snapshot_dirty:
                            await asyncio.to_thread(session.agent.save_snapshot)
                            session.snapshot_dirty = False
                except Exception:
                    log.exception("Keeping session %s after a persistence failure", session.id)
                    session.schedule_expiry()
                    return
            try:
                await session.release()
            finally:
                self.sessions.pop(session.id, None)

    async def shutdown(self, timeout=30):
        self.closing = True
        for session in list(self.sessions.values()):
            session.stop("service_shutdown")
            self.track_cleanup(self._release(session))
        work = self.cleanup_tasks | self.opening
        if work:
            _, pending = await asyncio.wait(work, timeout=timeout)
            if pending:
                # Keep ownership of late SDK teardown until the event loop exits.
                log.error("Web session shutdown deadline exceeded (%s tasks)", len(pending))
