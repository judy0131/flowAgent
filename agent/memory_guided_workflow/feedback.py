from typing import Dict, List, Tuple

from .models import ExecutionFeedback, LearningUpdate, WorkflowDAG


class ExecutionFeedbackModule:
    """Collect execution observations and expose learning-update hooks."""

    def __init__(self) -> None:
        self._feedback: List[ExecutionFeedback] = []

    def record(self, feedback: ExecutionFeedback) -> None:
        self._feedback.append(feedback)

    def all_feedback(self) -> List[ExecutionFeedback]:
        return list(self._feedback)

    def build_learning_update(self, workflow: WorkflowDAG) -> LearningUpdate:
        trajectory = tuple(node.tool_id for node in workflow.nodes)
        transition_priors: Dict[Tuple[str, ...], float] = {}
        for source, target in zip(trajectory, trajectory[1:]):
            transition_priors[(source, target)] = 1.0

        return LearningUpdate(
            tool_trajectories=[trajectory] if trajectory else [],
            transition_priors=transition_priors,
            experience_replay=[
                {
                    "node_id": item.node_id,
                    "status": item.status,
                    "cost": item.cost,
                    "quality": item.quality,
                    "metrics": dict(item.metrics),
                }
                for item in self._feedback
            ],
        )
