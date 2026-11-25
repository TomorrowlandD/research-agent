"""Persist a complete tool outcome before model-specific preview or truncation."""
from dataclasses import asdict
import json
from pathlib import Path

from .paths import RESEARCH_DIRNAME
from .tool_result import ToolExecutionResult


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
