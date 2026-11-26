
#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
import re

from .memory import _project_hash

from .paths import RESEARCH_DIRNAME, workspace_dir

from typing import Any
import json

SESSION_DIR = Path.home() / RESEARCH_DIRNAME / "sessions"



def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    d = workspace_dir() / RESEARCH_DIRNAME / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", session_id):
        raise ValueError("Invalid session ID. Use a snapshot ID, without a path or extension.")


def save_session(session_id: str, data: dict[str, Any]) -> None:
    validate_session_id(session_id)
    _ensure_dir()
    stream = NamedTemporaryFile(dir=SESSION_DIR, delete=False, mode="w", encoding="utf-8")
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(data, stream, indent=2, default=str)
        temporary.replace(SESSION_DIR / f"{session_id}.json")
    finally:
        temporary.unlink(missing_ok=True)


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    (d / f"{session_id}.folded-memory.latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_session(session_id: str) -> dict[str, Any] | None:
    validate_session_id(session_id)
    path = SESSION_DIR / f"{session_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
        return None
    meta = data["metadata"]
    if any(meta[key] != session_id for key in ("session_id", "id") if key in meta):
        return None
    meta.setdefault("session_id", session_id)
    return data


def list_sessions() -> list[dict[str, Any]]:
    results = []
    for path in SESSION_DIR.glob("*.json"):
        try:
            session = load_session(path.stem)
        except ValueError:
            continue
        if session is not None:
            results.append(session["metadata"])
    return results


def get_latest_session_id(protocol: str) -> str | None:
    project = _project_hash()
    matching = []
    for meta in list_sessions():
        if meta.get("project_hash") != project or meta.get("protocol") != protocol:
            continue
        try:
            updated = datetime.fromisoformat(meta["updated_at"])
        except (KeyError, TypeError, ValueError):
            continue
        matching.append((updated.astimezone(timezone.utc), meta["session_id"]))
    return max(matching)[1] if matching else None
