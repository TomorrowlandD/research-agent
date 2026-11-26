"""Append-only session events, independent of the replaceable context snapshot."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import errno
import json
import logging
import os
from pathlib import Path
import time

from . import session


# Submission order is event order, even when tools complete concurrently.
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-events")


def _lock_for_append(stream):
    # The OS releases the lock when the descriptor closes, including on exit.
    # Bound contention so an unavailable log cannot stall a model turn.
    deadline = time.monotonic() + 0.1
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                raise
            time.sleep(0.002)


def _last_seq(stream, end: int) -> int:
    if end == 0:
        return 0
    stream.seek(end - 1)
    if stream.read(1) != b"\n":
        raise ValueError("incomplete final event line")
    # Search backwards by blocks; reopening a long session only reads the tail.
    pos = end - 1
    while pos:
        start = max(0, pos - 8192)
        stream.seek(start)
        boundary = stream.read(pos - start).rfind(b"\n")
        if boundary >= 0:
            pos = start + boundary + 1
            break
        pos = start
    stream.seek(pos)
    seq = json.loads(stream.readline())["seq"]
    if type(seq) is not int or seq < 1:
        raise ValueError("invalid final event sequence")
    return seq


class SessionEventLog:
    def __init__(self, session_id: str, protocol: str):
        session.validate_session_id(session_id)
        self.session_id = session_id
        self.protocol = protocol
        self._end = 0
        self._seq = 0
        self._pending = None

    def record(self, trace_id: str, event_type: str, payload: dict) -> None:
        """Freeze the event now. Keep filesystem I/O off the active model loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._pending is not None:
                self._pending.result()
            self.append(trace_id, event_type, payload)
            return
        self._pending = _writer.submit(self._append_at, session.SESSION_DIR, trace_id, event_type,
                                       deepcopy(payload), datetime.now(timezone.utc).isoformat())

    async def flush(self) -> None:
        if self._pending is not None:
            await asyncio.shield(asyncio.wrap_future(self._pending))

    def append(self, trace_id: str, event_type: str, payload: dict) -> None:
        self._append_at(session.SESSION_DIR, trace_id, event_type, payload, datetime.now(timezone.utc).isoformat())

    def _append_at(self, directory: Path, trace_id: str, event_type: str, payload: dict, timestamp: str) -> None:
        path = directory / f"{self.session_id}.events.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_CREAT | os.O_APPEND | os.O_RDWR | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(path, flags, 0o600), "a+b", buffering=0) as stream:
                _lock_for_append(stream)
                end = stream.seek(0, os.SEEK_END)
                seq = (self._seq if end == self._end else _last_seq(stream, end)) + 1
                event = {"seq": seq, "timestamp": timestamp,
                         "session_id": self.session_id, "trace_id": trace_id, "protocol": self.protocol,
                         "event_type": event_type, "payload": payload}
                line = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8", "backslashreplace")
                if stream.write(line) != len(line):
                    raise OSError("incomplete event write")
                self._end, self._seq = end + len(line), seq
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            logging.warning("Cannot append %s to session event log %s: %s", event_type, path, exc)

    def tool_result(self, trace_id: str, call_id: str, name: str, content: str,
                    *, is_error: bool, artifact_path: Path | None = None) -> None:
        payload = {"tool_call_id": call_id, "tool_name": name, "is_error": is_error}
        if artifact_path is None:
            payload["content"] = content
        else:
            payload.update(content_preview=content, artifact_path=str(artifact_path))
        self.record(trace_id, "tool_result", payload)
