"""Tool outcomes shared by local tools, MCP and the model adapters."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    is_error: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.is_error

    @classmethod
    def failure(cls, content: str, error_type: str) -> "ToolExecutionResult":
        return cls(content, is_error=True, meta={"error_type": error_type})


class ToolFailure(Exception):
    """Expected tool failure with a message suitable for the model."""
