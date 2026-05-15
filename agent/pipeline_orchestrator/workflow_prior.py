from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .retrieval import (
    WorkflowMemoryRetriever,
    format_workflow_memory_prompt_block,
    score_workflow_with_retrieval_context,
)
from .workflow_memory import WorkflowMemoryIndex


class TaskBenchPriorIndex:
    """Unified TaskBench-derived workflow prior index."""

    def __init__(self, memory_index: WorkflowMemoryIndex):
        self.memory_index = memory_index

    @classmethod
    def from_json(cls, path: Path) -> "TaskBenchPriorIndex":
        return cls(WorkflowMemoryIndex.from_json(path))

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TaskBenchPriorIndex":
        return cls(WorkflowMemoryIndex.from_dict(payload))

    @classmethod
    def build_from_taskbench_records(
        cls,
        records: Iterable[Dict[str, Any]],
        *,
        source_name: str = "taskbench",
        max_motif_size: int = 4,
    ) -> "TaskBenchPriorIndex":
        return cls(
            WorkflowMemoryIndex.build_from_taskbench_records(
                records,
                source_name=source_name,
                max_motif_size=max_motif_size,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.memory_index.to_dict()

    def to_json(self, path: Path) -> None:
        self.memory_index.to_json(path)


class TaskBenchPrior:
    """Query-conditioned access and scoring over a TaskBench prior index."""

    def __init__(self, prior_index: TaskBenchPriorIndex):
        self.index = prior_index
        self.retriever = WorkflowMemoryRetriever(prior_index.memory_index)

    def retrieve(
        self,
        query: str,
        *,
        detected_actions: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        return self.retriever.retrieve(query, detected_actions=detected_actions)

    def recommend_start_skills(
        self,
        query: str,
        *,
        detected_actions: Optional[Iterable[str]] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        return self.retriever.recommend_start_tools(
            query,
            detected_actions=detected_actions,
            top_k=top_k,
        )

    def recommend_next_skills(
        self,
        query: str,
        current_skill: Optional[str],
        *,
        detected_actions: Optional[Iterable[str]] = None,
        visited_skills: Optional[Set[str]] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        return self.retriever.recommend_next_tools(
            query,
            current_skill,
            detected_actions=detected_actions,
            visited_tools={str(skill).strip() for skill in (visited_skills or set()) if str(skill).strip()},
            top_k=top_k,
        )

    def score_compiled_workflow(
        self,
        compiled_nodes: Sequence[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> Dict[str, float]:
        return score_workflow_with_retrieval_context(compiled_nodes, context)


def format_taskbench_prior_prompt_block(context: Dict[str, Any]) -> str:
    return format_workflow_memory_prompt_block(context)


def score_workflow_with_taskbench_prior_context(
    compiled_nodes: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> Dict[str, float]:
    return score_workflow_with_retrieval_context(compiled_nodes, context)
