"""Capture a local tool process and clean up its POSIX process group."""
import asyncio
import os
import signal
from pathlib import Path
from tempfile import TemporaryFile


async def _finish(awaitable):
    """Keep owned resources alive through a second cancellation during cleanup."""
    task = asyncio.ensure_future(awaitable)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


async def _reap(launch):
    # Cancellation may arrive after fork but before asyncio returns the handle.
    try:
        process = await launch
    except OSError:
        return
    try:
        if os.name == "posix":
            # The leader may already have exited while a descendant is still alive.
            os.killpg(process.pid, signal.SIGTERM)
            deadline = asyncio.get_running_loop().time() + 0.5
            while asyncio.get_running_loop().time() < deadline:
                os.killpg(process.pid, 0)
                await asyncio.sleep(0.025)
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    finally:
        await process.wait()


async def run_tool_process(command: str | list[str], *, cwd: Path, timeout: float) -> tuple[int, str, str]:
    # A grandchild can inherit stdout/stderr and outlive its shell. Files let us
    # observe the leader's exit independently of EOF on its descendants' handles.
    with TemporaryFile(mode="w+") as stdout, TemporaryFile(mode="w+") as stderr:
        options = dict(cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=os.name == "posix")
        if isinstance(command, str):
            launch = asyncio.create_task(asyncio.create_subprocess_shell(command, **options))
        else:
            launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, **options))
        try:
            process = await asyncio.shield(launch)
            await asyncio.wait_for(process.wait(), timeout)
        finally:
            await _finish(_reap(launch))

        def read_output():
            stdout.seek(0)
            stderr.seek(0)
            return process.returncode, stdout.read(), stderr.read()

        return await _finish(asyncio.to_thread(read_output))
