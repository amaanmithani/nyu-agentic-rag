"""The contract every tool returns. Tools never raise into the agent loop."""

from typing import Any, Literal

from pydantic import BaseModel, Field

# ok      evidence found
# empty   valid call, nothing matched
# weak    matches exist but the best one is a poor fit -> agent should revise the query
# error   bad arguments / unknown doc / page out of range / tool crashed (note says why)
# timeout tool exceeded TOOL_TIMEOUT_S on every attempt
Status = Literal["ok", "empty", "weak", "error", "timeout"]


class Citation(BaseModel):
    doc_id: str
    page: int

    def key(self) -> str:
        return f"{self.doc_id}:{self.page}"


class ToolResult(BaseModel):
    status: Status
    data: Any = None
    note: str = ""  # human/LLM-readable explanation, especially for non-ok statuses
    citations: list[Citation] = Field(default_factory=list)  # pages this result exposed
    terminal: bool = False  # True for ask_clarification: the run stops after this tool
    # Filled in by the executor:
    attempts: int = 1
    ms: int = 0

    def summary(self) -> str:
        """One line for the UI trace and trace_events table."""
        cites = ", ".join(c.key() for c in self.citations[:6])
        more = f" (+{len(self.citations) - 6})" if len(self.citations) > 6 else ""
        return f"{self.status}: {self.note}" + (f" [{cites}{more}]" if cites else "")
