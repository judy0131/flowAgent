from typing import Dict, Optional, Sequence

from .feedback import ExecutionFeedbackModule
from .incremental_planning import IncrementalWorkflowPlanningModule
from .models import (
    AgentRunResult,
    ExecutionFeedback,
    PlanningConfig,
    ToolSpec,
    UserRequest,
    to_plain_dict,
)
from .planning_memory import PlanningMemoryModule
from .task_understanding import TaskUnderstanding
from .tool_knowledge import ToolKnowledgeModule
from .verification import WorkflowVerificationModule


class MemoryGuidedWorkflowAgent:
    """Top-level scaffold for the five-module workflow planning agent."""

    def __init__(
        self,
        tools: Optional[Sequence[ToolSpec]] = None,
        config: Optional[PlanningConfig] = None,
        task_understanding: Optional[TaskUnderstanding] = None,
        tool_knowledge: Optional[ToolKnowledgeModule] = None,
        planning_memory: Optional[PlanningMemoryModule] = None,
        incremental_planning: Optional[IncrementalWorkflowPlanningModule] = None,
        verification: Optional[WorkflowVerificationModule] = None,
        feedback: Optional[ExecutionFeedbackModule] = None,
    ):
        self.config = config or PlanningConfig()
        self.task_understanding = task_understanding or TaskUnderstanding()
        self.tool_knowledge = tool_knowledge or ToolKnowledgeModule(tools)
        self.planning_memory = planning_memory or PlanningMemoryModule()
        self.incremental_planning = incremental_planning or IncrementalWorkflowPlanningModule(
            tool_knowledge=self.tool_knowledge,
            memory_module=self.planning_memory,
            config=self.config,
        )
        self.verification = verification or WorkflowVerificationModule(
            tool_knowledge=self.tool_knowledge,
            config=self.config,
        )
        self.feedback = feedback or ExecutionFeedbackModule()

    @classmethod
    def from_skill_registry(
        cls,
        registry: object,
        config: Optional[PlanningConfig] = None,
    ) -> "MemoryGuidedWorkflowAgent":
        return cls(
            config=config,
            tool_knowledge=ToolKnowledgeModule.from_skill_registry(registry),
        )

    def run(self, user_request: str) -> AgentRunResult:
        understanding = self.task_understanding.parse(UserRequest(text=user_request))
        memory = self.task_understanding.initialize_memory(understanding)
        selected_candidate, candidate_pool = self.incremental_planning.plan(memory)
        memory = selected_candidate.memory
        report = self.verification.verify(memory)
        if not report.passed:
            report = self.verification.refine(memory, report)

        execution_feedback: list[ExecutionFeedback] = []
        summary = self._build_summary(memory, report)
        return AgentRunResult(
            understanding=understanding,
            memory=memory,
            candidate_pool=candidate_pool,
            selected_workflow=selected_candidate.workflow,
            verification=report,
            execution_feedback=execution_feedback,
            summary=summary,
        )

    async def arun(self, user_request: str) -> AgentRunResult:
        understanding = self.task_understanding.parse(UserRequest(text=user_request))
        memory = self.task_understanding.initialize_memory(understanding)
        selected_candidate, candidate_pool = self.incremental_planning.plan(memory)
        memory = selected_candidate.memory
        report = self.verification.verify(memory)
        if not report.passed:
            report = self.verification.refine(memory, report)

        execution_feedback: list[ExecutionFeedback] = []
        summary = self._build_summary(memory, report)
        return AgentRunResult(
            understanding=understanding,
            memory=memory,
            candidate_pool=candidate_pool,
            selected_workflow=selected_candidate.workflow,
            verification=report,
            execution_feedback=execution_feedback,
            summary=summary,
        )

    def run_dict(self, user_request: str) -> Dict[str, object]:
        return to_plain_dict(self.run(user_request))

    @staticmethod
    def _build_summary(memory, report) -> str:
        return (
            f"steps={len(memory.task.steps)}, "
            f"covered={len(memory.covered_goal_ids)}, "
            f"remaining={len(memory.remaining_goal_ids)}, "
            f"nodes={len(memory.workflow.nodes)}, "
            f"verification_passed={report.passed}"
        )
