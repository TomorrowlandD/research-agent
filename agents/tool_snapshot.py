"""Persist a complete tool outcome before model-specific preview or truncation."""
from dataclasses import asdict
import json
from pathlib import Path

from .paths import RESEARCH_DIRNAME
from .tool_result import ToolExecutionResult, ToolFailure


def write_tool_snapshot(trace_id: str, span_id: str, name: str, args: dict,
                        result: ToolExecutionResult) -> None:
    directory = Path.home() / RESEARCH_DIRNAME / "snapshots" / trace_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{span_id}.json"
    record = {"tool_name": name, "normalized_args": args, "result": asdict(result)}
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class SnapshotMissing(ToolFailure):
    """The selected recording contains no result for this tool call."""


def replay_tool_snapshot(path: Path, name: str, args: dict) -> ToolExecutionResult:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SnapshotMissing(f"Snapshot missing: {path}") from exc
    if row["tool_name"] != name or row["normalized_args"] != args:
        raise SnapshotMissing(f"Snapshot missing for {name} with the supplied arguments in {path.name}.")
    result = row["result"]
    if not (isinstance(result["content"], str) and isinstance(result["is_error"], bool)
            and isinstance(result["meta"], dict)):
        raise ValueError(f"Invalid tool result in snapshot {path.name}")
    return ToolExecutionResult(**result)
