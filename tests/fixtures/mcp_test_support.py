"""Offline test resources, bounded observations and owned-process evidence."""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile

from agents.mcp_client import McpConnection

SERVER = Path(__file__).with_name("mcp_test_server.py")


def process_parents():
    """Read-only Windows snapshot; only descendants of held PIDs are retained."""
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("exe", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry, parents = Entry(), {}
    entry.size = ctypes.sizeof(entry)
    try:
        found = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while found:
            parents[entry.pid] = entry.parent
            found = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return parents


async def eventually(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


class OwnedProcesses:
    """Retain handles before exit; fallback termination never implies a pass."""
    def __init__(self, pidfile):
        self.pidfile = pidfile
        self.handles = {}

    def capture(self):
        if os.name != "nt":
            return
        import win32api
        for role, pid in json.loads(self.pidfile.read_text()).items():
            if pid and pid not in self.handles:
                self.handles[pid] = (role, win32api.OpenProcess(0x100000 | 0x1000 | 1, False, pid))
        parents = process_parents()
        added = True
        while added:
            added = False
            for pid, parent in parents.items():
                if parent in self.handles and pid not in self.handles:
                    try:
                        handle = win32api.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
                    except OSError:
                        continue  # Already exited between the snapshot and OpenProcess.
                    self.handles[pid] = ("descendant", handle)
                    added = True

    def states(self):
        import win32process
        return [{"pid": pid, "role": role, "exit_code": win32process.GetExitCodeProcess(handle),
                 "exited": win32process.GetExitCodeProcess(handle) != 259}
                for pid, (role, handle) in self.handles.items()]

    def dispose(self):
        if os.name != "nt":
            return
        import win32api
        import win32event
        import win32process
        for role, handle in self.handles.values():
            if win32process.GetExitCodeProcess(handle) == 259:
                win32api.TerminateProcess(handle, 99)
                win32event.WaitForSingleObject(handle, 5000)
                print("MCP_FALLBACK " + json.dumps({"role": role,
                      "exit_code": win32process.GetExitCodeProcess(handle)}), flush=True)
            handle.Close()
        self.handles.clear()


class Fixture:
    def __init__(self, directory, mode, env):
        self.pidfile = Path(directory) / "pids.json"
        self.eventsfile = Path(directory) / "events.jsonl"
        self.config = {"command": sys.executable, "args": ["-B", str(SERVER), "--mode", mode,
                       "--delay-ms", "30000", "--pid-file", str(self.pidfile),
                       "--events-file", str(self.eventsfile)], "env": env or {}}
        self.conn = McpConnection(mode, **self.config)
        self.owned = OwnedProcesses(self.pidfile)

    def events(self):
        if not self.eventsfile.exists():
            return []
        lines = self.eventsfile.read_text(encoding="utf-8").splitlines(keepends=True)
        return [json.loads(line) for line in lines if line.endswith("\n")]

    def request(self, method, tool=None):
        return next((row["wire"] for row in self.events() if row["direction"] == "received"
                     and row["wire"].get("method") == method
                     and (tool is None or row["wire"].get("params", {}).get("name") == tool)), None)

    async def capture(self):
        await eventually(self.pidfile.exists)
        self.owned.capture()

    async def assert_exited(self, testcase):
        testcase.assertTrue(self.conn._owner_task is None or self.conn._owner_task.done())
        testcase.assertIsNone(self.conn._session)
        if os.name == "nt":
            initial = self.owned.states()
            try:
                await eventually(lambda: all(row["exited"] for row in self.owned.states()), timeout=5)
            except TimeoutError:
                pass  # Print and assert the final states before fallback cleanup.
            states = self.owned.states()
            testcase.assertTrue(states)
            print("MCP_RESOURCE " + json.dumps({"test": testcase.id(), "processes": states,
                                               "owner_done": True, "states_at_close": initial}), flush=True)
            testcase.assertTrue(all(row["exited"] for row in states), states)


@asynccontextmanager
async def fixture(testcase, mode="normal", env=None, start=True):
    with tempfile.TemporaryDirectory(prefix="mcp-test-") as directory:
        f = Fixture(directory, mode, env)
        try:
            if start:
                await f.conn.connect()
                await f.capture()
                await f.conn.initialize()
                f.owned.capture()
            yield f
        finally:
            try:
                if f.pidfile.exists():
                    f.owned.capture()
                await f.conn.close()
                if f.owned.handles:
                    await f.assert_exited(testcase)
            finally:
                f.owned.dispose()


class AsyncAuditMixin:
    async def asyncSetUp(self):
        self.loop_errors = []
        self.initial_tasks = asyncio.all_tasks()
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        await asyncio.sleep(.05)
        pending = [task for task in asyncio.all_tasks() if task not in self.initial_tasks
                   and task is not asyncio.current_task() and not task.done()]
        self.assertEqual(pending, [], "Test leaked background tasks")
        self.assertEqual(self.loop_errors, [], "Unhandled loop/task/transport error")
