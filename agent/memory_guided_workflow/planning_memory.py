from __future__ import annotations

import json
from dataclasses import is_dataclass
from typing import Any, Dict, List, Mapping, Optional

try:
    from .models import (
        PlanningMemoryState,
        TaskStep,
        WorkflowDAG,
        WorkflowEdge,
        WorkflowNode,
        from_dict_list,
        to_plain_dict,
    )
except ImportError:
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.models import (
        PlanningMemoryState,
        TaskStep,
        WorkflowDAG,
        WorkflowEdge,
        WorkflowNode,
        from_dict_list,
        to_plain_dict,
    )


class PlanningMemory:
    """Workflow planning state store for MIWP.

    PlanningMemory only stores and updates workflow planning state. It does not
    retrieve tools, score transitions, call LLMs, plan, verify, repair, or bind
    artifacts.
    """

    def __init__(self, tasks: List[TaskStep]):
        self.tasks = [_normalize_task(task, index) for index, task in enumerate(tasks)]
        self._state = PlanningMemoryState(
            completed_task_ids=[],
            remaining_task_ids=[task.task_id for task in self.tasks],
            selected_tool_ids=[],
            selected_action_history=[],
            workflow_dag=WorkflowDAG(),
            current_step=0,
            metadata={"warnings": []},
        )

    @property
    def state(self) -> PlanningMemoryState:
        """Return the mutable memory state."""
        return self._state

    @property
    def completed_task_ids(self) -> List[str]:
        return self._state.completed_task_ids

    @property
    def remaining_task_ids(self) -> List[str]:
        return self._state.remaining_task_ids

    @property
    def selected_tool_ids(self) -> List[str]:
        return self._state.selected_tool_ids

    @property
    def selected_action_history(self) -> List[Dict[str, Any]]:
        return self._state.selected_action_history

    @property
    def workflow_dag(self) -> WorkflowDAG:
        return self._state.workflow_dag

    @property
    def workflow_so_far(self) -> WorkflowDAG:
        return self._state.workflow_dag

    @property
    def current_step(self) -> int:
        return self._state.current_step

    @property
    def metadata(self) -> Dict[str, Any]:
        return self._state.metadata

    def get_remaining_tasks(self) -> List[TaskStep]:
        """Return unfinished TaskStep objects."""
        task_map = self._task_map()
        return [
            task_map[task_id]
            for task_id in self.remaining_task_ids
            if task_id in task_map
        ]

    def get_completed_tasks(self) -> List[TaskStep]:
        """Return completed TaskStep objects."""
        task_map = self._task_map()
        return [
            task_map[task_id]
            for task_id in self.completed_task_ids
            if task_id in task_map
        ]

    def get_selected_tools(self) -> List[str]:
        """Return selected tool ids in first-use order."""
        return list(self.selected_tool_ids)

    def get_last_selected_tool(self) -> Optional[str]:
        """Return the most recently selected tool id."""
        if not self.selected_tool_ids:
            return None
        return self.selected_tool_ids[-1]

    def get_workflow_dag(self) -> WorkflowDAG:
        """Return the current workflow DAG."""
        return self.workflow_dag

    def get_workflow_nodes(self) -> List[WorkflowNode]:
        """Return workflow nodes."""
        return list(self.workflow_dag.nodes)

    def get_workflow_edges(self) -> List[WorkflowEdge]:
        """Return workflow edges."""
        return list(self.workflow_dag.edges)

    def get_node(self, node_id: str) -> Optional[WorkflowNode]:
        """Return a workflow node by node_id."""
        if hasattr(self.workflow_dag, "get_node"):
            return self.workflow_dag.get_node(node_id)
        for node in self.workflow_dag.nodes:
            if node.node_id == node_id:
                return node
        return None

    def get_node_id_by_tool(self, tool_id: str) -> Optional[str]:
        """Return the newest node_id that used the given tool_id."""
        for node in reversed(self.workflow_dag.nodes):
            if node.tool_id == tool_id:
                return node.node_id
        return None

    def add_selected_action(self, task_id: str, action: Any) -> None:
        """Record a selected action and update selected_tool_ids."""
        if not self._task_exists(task_id):
            self._record_unknown_task_warning(task_id, "add_selected_action")

        raw_action = _action_to_plain_dict(action)
        tool_id = str(raw_action.get("tool_id", "") or "").strip()
        tool_name = str(raw_action.get("tool_name", "") or raw_action.get("name", "") or "").strip()

        record = {
            "step": self.current_step,
            "task_id": task_id,
            "tool_id": tool_id,
            "tool_name": tool_name,
            "retrieval_score": raw_action.get("retrieval_score"),
            "reason": raw_action.get("reason", ""),
            "raw_action": raw_action,
        }
        self.selected_action_history.append(record)

        if tool_id and tool_id not in self.selected_tool_ids:
            self.selected_tool_ids.append(tool_id)

    def add_workflow_node(self, node: WorkflowNode) -> None:
        """Add a workflow node. Duplicate node_id is ignored."""
        if self.get_node(node.node_id) is not None:
            return
        if hasattr(self.workflow_dag, "add_node"):
            self.workflow_dag.add_node(node)
        else:
            self.workflow_dag.nodes.append(node)

    def add_workflow_edge(self, edge: WorkflowEdge) -> None:
        """Add a workflow edge. Duplicate source/target/type is ignored."""
        edge_key = (edge.source_node_id, edge.target_node_id, edge.edge_type)
        existing_keys = {
            (item.source_node_id, item.target_node_id, item.edge_type)
            for item in self.workflow_dag.edges
        }
        if edge_key in existing_keys:
            return
        if hasattr(self.workflow_dag, "add_edge"):
            self.workflow_dag.add_edge(edge)
        else:
            self.workflow_dag.edges.append(edge)

    def remove_workflow_node(self, node_id: str) -> None:
        """Remove a workflow node and its incident edges."""
        self.workflow_dag.nodes = [
            node for node in self.workflow_dag.nodes
            if node.node_id != node_id
        ]
        self.workflow_dag.edges = [
            edge for edge in self.workflow_dag.edges
            if edge.source_node_id != node_id and edge.target_node_id != node_id
        ]

    def remove_workflow_edge(self, source_node_id: str, target_node_id: str) -> None:
        """Remove workflow edges between source_node_id and target_node_id."""
        self.workflow_dag.edges = [
            edge for edge in self.workflow_dag.edges
            if not (
                edge.source_node_id == source_node_id
                and edge.target_node_id == target_node_id
            )
        ]

    def mark_task_completed(self, task_id: str) -> None:
        """Move task_id from remaining to completed."""
        if not self._task_exists(task_id):
            self._record_unknown_task_warning(task_id, "mark_task_completed")
            return
        if task_id in self.remaining_task_ids:
            self.remaining_task_ids.remove(task_id)
        if task_id not in self.completed_task_ids:
            self.completed_task_ids.append(task_id)

        task = self._task_map().get(task_id)
        if task is not None:
            task.status = "completed"

    def mark_task_remaining(self, task_id: str) -> None:
        """Move task_id from completed back to remaining."""
        if not self._task_exists(task_id):
            self._record_unknown_task_warning(task_id, "mark_task_remaining")
            return
        if task_id in self.completed_task_ids:
            self.completed_task_ids.remove(task_id)
        if task_id not in self.remaining_task_ids:
            self.remaining_task_ids.append(task_id)

        task = self._task_map().get(task_id)
        if task is not None:
            task.status = "remaining"

    def apply_selected_action(
        self,
        task_id: str,
        action: Any,
        node: WorkflowNode,
        edges: Optional[List[WorkflowEdge]] = None,
    ) -> None:
        """Apply one planner decision to the memory state."""
        self.add_selected_action(task_id, action)
        self.add_workflow_node(node)
        for edge in edges or []:
            self.add_workflow_edge(edge)
        self.mark_task_completed(task_id)
        self._state.current_step += 1

    def workflow_summary(self) -> Dict[str, Any]:
        """Return a compact summary of the current workflow."""
        return {
            "node_count": len(self.workflow_dag.nodes),
            "edge_count": len(self.workflow_dag.edges),
            "tool_ids": [node.tool_id for node in self.workflow_dag.nodes],
            "task_ids": [node.task_id for node in self.workflow_dag.nodes],
        }

    def snapshot(self) -> Dict[str, Any]:
        """Return a lightweight state snapshot."""
        return {
            "current_step": self.current_step,
            "completed_task_ids": list(self.completed_task_ids),
            "remaining_task_ids": list(self.remaining_task_ids),
            "selected_tool_ids": list(self.selected_tool_ids),
            "workflow_summary": self.workflow_summary(),
            "metadata": to_plain_dict(self.metadata),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the complete memory state."""
        return {
            "tasks": [task.to_dict() for task in self.tasks],
            "state": self.state.to_dict(),
            "completed_task_ids": list(self.completed_task_ids),
            "remaining_task_ids": list(self.remaining_task_ids),
            "selected_tool_ids": list(self.selected_tool_ids),
            "selected_action_history": to_plain_dict(self.selected_action_history),
            "workflow_dag": self.workflow_dag.to_dict(),
            "current_step": self.current_step,
            "metadata": to_plain_dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanningMemory":
        """Restore memory from a serialized dictionary."""
        tasks = from_dict_list(TaskStep, data.get("tasks", []))
        memory = cls(tasks=tasks)

        state_payload = data.get("state")
        if isinstance(state_payload, Mapping):
            memory._state = PlanningMemoryState.from_dict(dict(state_payload))
        else:
            workflow_payload = data.get("workflow_dag", {})
            memory._state = PlanningMemoryState(
                completed_task_ids=[str(item) for item in data.get("completed_task_ids", [])],
                remaining_task_ids=[str(item) for item in data.get("remaining_task_ids", [])],
                selected_tool_ids=[str(item) for item in data.get("selected_tool_ids", [])],
                selected_action_history=list(data.get("selected_action_history", []) or []),
                workflow_dag=WorkflowDAG.from_dict(workflow_payload if isinstance(workflow_payload, dict) else {}),
                current_step=int(data.get("current_step", 0) or 0),
                metadata=dict(data.get("metadata", {}) or {}),
            )

        memory.metadata.setdefault("warnings", [])
        return memory

    def _task_map(self) -> Dict[str, TaskStep]:
        return {task.task_id: task for task in self.tasks}

    def _task_exists(self, task_id: str) -> bool:
        return task_id in self._task_map()

    def _record_unknown_task_warning(self, task_id: str, operation: str) -> None:
        self._record_warning(
            "unknown_task_id",
            f"Unknown task_id '{task_id}' during {operation}.",
            {"task_id": task_id, "operation": operation},
        )

    def _record_warning(
        self,
        code: str,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        warning = {"code": code, "message": message}
        if extra:
            warning.update(extra)
        self.metadata.setdefault("warnings", []).append(warning)


def _normalize_task(task: Any, index: int) -> TaskStep:
    if isinstance(task, TaskStep):
        return task
    if isinstance(task, Mapping):
        payload = dict(task)
        payload.setdefault("task_id", payload.get("step_id") or payload.get("id") or f"t{index + 1}")
        payload.setdefault("description", payload.get("text") or "")
        return TaskStep.from_dict(payload)

    return TaskStep(
        task_id=str(getattr(task, "task_id", getattr(task, "step_id", getattr(task, "id", f"t{index + 1}")))),
        description=str(getattr(task, "description", getattr(task, "text", task))),
        priority=float(getattr(task, "priority", index + 1) or index + 1),
        referenced_literals=list(getattr(task, "referenced_literals", []) or []),
        metadata=dict(getattr(task, "metadata", {}) or {}),
    )


def _action_to_plain_dict(action: Any) -> Dict[str, Any]:
    if isinstance(action, Mapping):
        return dict(action)
    if hasattr(action, "to_dict"):
        payload = action.to_dict()
        return dict(payload) if isinstance(payload, Mapping) else {"value": payload}
    if is_dataclass(action):
        payload = to_plain_dict(action)
        return dict(payload) if isinstance(payload, Mapping) else {"value": payload}
    return {
        "value": action,
        "tool_id": str(getattr(action, "tool_id", "") or ""),
        "tool_name": str(getattr(action, "tool_name", "") or ""),
        "retrieval_score": getattr(action, "retrieval_score", None),
        "reason": str(getattr(action, "reason", "") or ""),
    }


def _main() -> None:
    tasks = [
        TaskStep(task_id="t1", description="Download an image."),
        TaskStep(task_id="t2", description="Extract the text."),
    ]
    memory = PlanningMemory(tasks=tasks)

    node1 = WorkflowNode(
        node_id="n_t1",
        task_id="t1",
        task_description="Download an image.",
        tool_id="Image Downloader",
        tool_name="Image Downloader",
    )
    action1 = {
        "tool_id": "Image Downloader",
        "tool_name": "Image Downloader",
        "retrieval_score": 0.9,
        "reason": "download image first",
    }
    memory.apply_selected_action(
        task_id="t1",
        action=action1,
        node=node1,
    )

    node2 = WorkflowNode(
        node_id="n_t2",
        task_id="t2",
        task_description="Extract the text.",
        tool_id="Image-to-Text",
        tool_name="Image-to-Text",
    )
    edge = WorkflowEdge(
        source_node_id="n_t1",
        target_node_id="n_t2",
        edge_type="tool_transition",
    )
    action2 = {
        "tool_id": "Image-to-Text",
        "tool_name": "Image-to-Text",
        "retrieval_score": 0.92,
        "reason": "extract text after image download",
    }
    memory.apply_selected_action(
        task_id="t2",
        action=action2,
        node=node2,
        edges=[edge],
    )

    print(f"completed_task_ids = {memory.completed_task_ids}")
    print(f"remaining_task_ids = {memory.remaining_task_ids}")
    print(f"selected_tool_ids = {memory.selected_tool_ids}")
    print(f"last_selected_tool = {memory.get_last_selected_tool()}")
    print(f"node_id_by_tool = {memory.get_node_id_by_tool('Image-to-Text')}")
    print("workflow_dag:")
    print(json.dumps(memory.get_workflow_dag().to_dict(), ensure_ascii=False, indent=2))
    print("snapshot:")
    print(json.dumps(memory.snapshot(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
