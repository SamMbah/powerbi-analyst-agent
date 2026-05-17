"""Agent state for one Power BI analysis session."""

from dataclasses import dataclass, field


@dataclass
class AgentState:
    # The user's natural language question
    question: str = ""

    # Selected workspace and dataset (set during discovery phase)
    workspace_id: str = ""
    workspace_name: str = ""
    dataset_id: str = ""
    dataset_name: str = ""

    # Schema injected at the start so the agent knows what's available
    schema_summary: str = ""

    # Full history of (thought, dax, result, fig_path) steps
    steps: list[dict] = field(default_factory=list)

    # Completion state
    is_complete: bool = False
    final_answer: str = ""

    # Safety cap on iterations
    max_iterations: int = 15
