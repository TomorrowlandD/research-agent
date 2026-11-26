"""Directory name shared by user and project storage."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_workspace: ContextVar[Path | None] = ContextVar("workspace", default=None)


def workspace_dir() -> Path:
    return _workspace.get() or Path.cwd()


@contextmanager
def use_workspace(directory: Path):
    token = _workspace.set(directory.resolve())
    try:
        yield
    finally:
        _workspace.reset(token)


RESEARCH_DIRNAME = ".research"
