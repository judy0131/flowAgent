from typing import Dict, List, Optional, Set

from .models import (
    IssueSeverity,
    PlanningConfig,
    PlanningMemoryState,
    RepairAction,
    VerificationIssue,
    VerificationReport,
    WorkflowDAG,
)
from .tool_knowledge import ToolKnowledgeModule


class WorkflowVerificationModule:
    """Run multi-view workflow checks and expose a repair hook."""

    def __init__(
        self,
        tool_knowledge: ToolKnowledgeModule,
        config: Optional[PlanningConfig] = None,
    ):
        self.tool_knowledge = tool_knowledge
        self.config = config or PlanningConfig()

    def verify(self, memory: PlanningMemoryState) -> VerificationReport:
        issues: List[VerificationIssue] = []
        issues.extend(self._check_goal_coverage(memory))
        issues.extend(self._check_tool_binding(memory.workflow))
        issues.extend(self._check_dependency_consistency(memory.workflow))
        issues.extend(self._check_tool_compatibility(memory.workflow))
        issues.extend(self._check_dag(memory.workflow))
        issues.extend(self._check_fork_merge(memory.workflow))
        issues.extend(self._check_redundant_steps(memory.workflow))

        passed = not any(issue.severity == IssueSeverity.ERROR for issue in issues)
        score = 1.0 if passed else 0.0
        repair_actions = self._build_repair_actions(issues)
        return VerificationReport(
            passed=passed,
            issues=issues,
            repair_actions=repair_actions,
            score=score,
        )

    def refine(self, memory: PlanningMemoryState, report: VerificationReport) -> VerificationReport:
        return VerificationReport(
            passed=report.passed,
            issues=list(report.issues),
            repair_actions=list(report.repair_actions),
            repaired_workflow=memory.workflow,
            score=report.score,
            metadata={"repair_strategy": "repair_actions_placeholder"},
        )

    def _check_goal_coverage(self, memory: PlanningMemoryState) -> List[VerificationIssue]:
        issues: List[VerificationIssue] = []
        for goal_id in memory.remaining_goal_ids:
            issues.append(
                VerificationIssue(
                    code="missing_goal_coverage",
                    message=f"Goal is not covered: {goal_id}",
                    severity=IssueSeverity.ERROR,
                    metadata={"goal_id": goal_id},
                )
            )
        return issues

    def _check_tool_binding(self, workflow: WorkflowDAG) -> List[VerificationIssue]:
        issues: List[VerificationIssue] = []
        for node in workflow.nodes:
            if node.tool_name == self.config.placeholder_tool_name:
                issues.append(
                    VerificationIssue(
                        code="placeholder_tool",
                        message="Workflow node still needs a concrete tool binding.",
                        severity=IssueSeverity.WARNING,
                        node_id=node.id,
                    )
                )
                continue
            if self.tool_knowledge.get_tool(node.tool_name) is None:
                issues.append(
                    VerificationIssue(
                        code="unknown_tool",
                        message=f"Unknown tool: {node.tool_name}",
                        severity=IssueSeverity.ERROR,
                        node_id=node.id,
                    )
                )
        return issues

    def _check_tool_compatibility(self, workflow: WorkflowDAG) -> List[VerificationIssue]:
        issues: List[VerificationIssue] = []
        nodes_by_id = {node.id: node for node in workflow.nodes}
        for edge in workflow.edges:
            source = nodes_by_id.get(edge.source)
            target = nodes_by_id.get(edge.target)
            if source is None or target is None:
                continue
            compatibility = self.tool_knowledge.type_compatibility(source.tool_id, target.tool_id)
            if compatibility < 0:
                issues.append(
                    VerificationIssue(
                        code="tool_type_incompatible",
                        message=f"Tool output/input types do not match: {source.tool_id} -> {target.tool_id}",
                        severity=IssueSeverity.ERROR,
                        node_id=target.id,
                        metadata={"source_node_id": source.id, "target_node_id": target.id},
                    )
                )
        return issues

    @staticmethod
    def _check_dependency_consistency(workflow: WorkflowDAG) -> List[VerificationIssue]:
        issues: List[VerificationIssue] = []
        node_ids = set(workflow.node_ids())
        for edge in workflow.edges:
            if edge.source not in node_ids:
                issues.append(
                    VerificationIssue(
                        code="missing_edge_source",
                        message=f"Edge source is missing: {edge.source}",
                        severity=IssueSeverity.ERROR,
                    )
                )
            if edge.target not in node_ids:
                issues.append(
                    VerificationIssue(
                        code="missing_edge_target",
                        message=f"Edge target is missing: {edge.target}",
                        severity=IssueSeverity.ERROR,
                    )
                )
        return issues

    @staticmethod
    def _check_dag(workflow: WorkflowDAG) -> List[VerificationIssue]:
        adjacency: Dict[str, List[str]] = {node.id: [] for node in workflow.nodes}
        for edge in workflow.edges:
            adjacency.setdefault(edge.source, []).append(edge.target)

        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visiting:
                return False
            if node_id in visited:
                return True
            visiting.add(node_id)
            for target in adjacency.get(node_id, []):
                if not visit(target):
                    return False
            visiting.remove(node_id)
            visited.add(node_id)
            return True

        for node_id in list(adjacency):
            if not visit(node_id):
                return [
                    VerificationIssue(
                        code="workflow_cycle",
                        message="Workflow graph contains a cycle.",
                        severity=IssueSeverity.ERROR,
                    )
                ]
        return []

    @staticmethod
    def _check_fork_merge(workflow: WorkflowDAG) -> List[VerificationIssue]:
        issues: List[VerificationIssue] = []
        in_degree: Dict[str, int] = {node.id: 0 for node in workflow.nodes}
        out_degree: Dict[str, int] = {node.id: 0 for node in workflow.nodes}
        for edge in workflow.edges:
            out_degree[edge.source] = out_degree.get(edge.source, 0) + 1
            in_degree[edge.target] = in_degree.get(edge.target, 0) + 1

        for node in workflow.nodes:
            if len(workflow.nodes) > 1 and in_degree.get(node.id, 0) == 0 and out_degree.get(node.id, 0) == 0:
                issues.append(
                    VerificationIssue(
                        code="isolated_node",
                        message=f"Workflow node is isolated: {node.id}",
                        severity=IssueSeverity.WARNING,
                        node_id=node.id,
                    )
                )
        return issues

    @staticmethod
    def _check_redundant_steps(workflow: WorkflowDAG) -> List[VerificationIssue]:
        issues: List[VerificationIssue] = []
        seen = set()
        for node in workflow.nodes:
            key = (node.tool_id, tuple(sorted(node.goal_ids)), tuple(sorted(node.input_artifact_ids)))
            if key in seen:
                issues.append(
                    VerificationIssue(
                        code="redundant_step",
                        message=f"Potential redundant step: {node.id}",
                        severity=IssueSeverity.WARNING,
                        node_id=node.id,
                    )
                )
            seen.add(key)
        return issues

    @staticmethod
    def _build_repair_actions(issues: List[VerificationIssue]) -> List[RepairAction]:
        actions: List[RepairAction] = []
        for issue in issues:
            if issue.code == "missing_goal_coverage":
                actions.append(
                    RepairAction(
                        action_type="add_missing_goal_step",
                        reason=issue.message,
                        target_goal_id=str(issue.metadata.get("goal_id", "")) or None,
                    )
                )
            elif issue.code in {"unknown_tool", "placeholder_tool"}:
                actions.append(
                    RepairAction(
                        action_type="rebind_tool",
                        reason=issue.message,
                        target_node_id=issue.node_id,
                    )
                )
            elif issue.code in {"tool_type_incompatible", "missing_edge_source", "missing_edge_target"}:
                actions.append(
                    RepairAction(
                        action_type="repair_dependency",
                        reason=issue.message,
                        target_node_id=issue.node_id,
                    )
                )
            elif issue.code == "workflow_cycle":
                actions.append(
                    RepairAction(
                        action_type="remove_cycle_edge",
                        reason=issue.message,
                    )
                )
            elif issue.code == "redundant_step":
                actions.append(
                    RepairAction(
                        action_type="remove_redundant_step",
                        reason=issue.message,
                        target_node_id=issue.node_id,
                    )
                )
        return actions
