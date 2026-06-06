from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional, Type, TypeVar, Union, get_args, get_origin


T = TypeVar("T")


def to_plain_dict(value: Any) -> Any:
    """Convert nested dataclasses, lists, and dicts into JSON-friendly values."""
    if is_dataclass(value):
        return {
            item.name: to_plain_dict(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, list):
        return [to_plain_dict(item) for item in value]
    if isinstance(value, tuple):
        return [to_plain_dict(item) for item in value]
    if isinstance(value, dict):
        return {
            key: to_plain_dict(item)
            for key, item in value.items()
        }
    return value


def from_dict_list(cls: Type[T], payload: Any) -> List[T]:
    """Build a list of dataclass objects from a list of dictionaries."""
    if payload is None:
        return []
    items = payload if isinstance(payload, list) else [payload]
    result: List[T] = []
    for item in items:
        if isinstance(item, cls):
            result.append(item)
        elif isinstance(item, dict) and hasattr(cls, "from_dict"):
            result.append(cls.from_dict(item))  # type: ignore[attr-defined]
    return result


class SerializableMixin:
    """Small serialization mixin for MIWP dataclasses."""

    def to_dict(self) -> Dict[str, Any]:
        return to_plain_dict(self)

    @classmethod
    def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise TypeError(f"{cls.__name__}.from_dict expects a dict")

        kwargs: Dict[str, Any] = {}
        aliases = getattr(cls, "_field_aliases", {})
        for item in fields(cls):
            raw_value = _read_field(data, item.name, aliases.get(item.name, []))
            if raw_value is None:
                continue
            kwargs[item.name] = _coerce_field_value(item.type, raw_value)
        return cls(**kwargs)  # type: ignore[arg-type]


def _read_field(data: Dict[str, Any], field_name: str, aliases: List[str]) -> Any:
    if field_name in data:
        return data[field_name]
    for alias in aliases:
        if alias in data:
            return data[alias]
    return None


def _coerce_field_value(field_type: Any, value: Any) -> Any:
    origin = get_origin(field_type)
    args = get_args(field_type)

    if origin in (list, List):
        item_type = args[0] if args else Any
        values = value if isinstance(value, list) else [value]
        return [_coerce_field_value(item_type, item) for item in values]

    if origin in (dict, Dict):
        if not isinstance(value, dict):
            return {}
        value_type = args[1] if len(args) == 2 else Any
        return {
            key: _coerce_field_value(value_type, item)
            for key, item in value.items()
        }

    if origin is Union:
        non_none_args = [arg for arg in args if arg is not type(None)]
        if value is None:
            return None
        if len(non_none_args) == 1:
            return _coerce_field_value(non_none_args[0], value)

    if isinstance(field_type, type) and issubclass(field_type, SerializableMixin):
        if isinstance(value, field_type):
            return value
        if isinstance(value, dict):
            return field_type.from_dict(value)

    return value


# ---------------------------------------------------------------------------
# Task Layer
# ---------------------------------------------------------------------------


@dataclass
class UserRequest(SerializableMixin):
    text: str
    constraints: List[str] = field(default_factory=list)
    preferences: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskStep(SerializableMixin):
    task_id: str
    description: str
    status: str = "remaining"
    priority: float = 1.0
    referenced_literals: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    _field_aliases = {
        "task_id": ["step_id", "goal_id", "id"],
        "description": ["text"],
    }

    @property
    def id(self) -> str:
        return self.task_id

    @property
    def step_id(self) -> str:
        return self.task_id

    @property
    def text(self) -> str:
        return self.description


Goal = TaskStep


@dataclass
class TaskUnderstandingResult(SerializableMixin):
    request: UserRequest
    steps: List[TaskStep] = field(default_factory=list)
    raw_llm_output: Dict[str, Any] = field(default_factory=dict)

    def get_step(self, task_id: str) -> Optional[TaskStep]:
        for step in self.steps:
            if step.task_id == task_id:
                return step
        return None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskUnderstandingResult":
        request_payload = data.get("request", {})
        if isinstance(request_payload, UserRequest):
            request = request_payload
        elif isinstance(request_payload, dict):
            request = UserRequest.from_dict(request_payload)
        else:
            request = UserRequest(text=str(request_payload))

        raw_steps = data.get("steps")
        if raw_steps is None:
            raw_steps = data.get("tasks", [])

        return cls(
            request=request,
            steps=from_dict_list(TaskStep, raw_steps),
            raw_llm_output=dict(data.get("raw_llm_output", {}) or {}),
        )


# ---------------------------------------------------------------------------
# Tool Layer
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec(SerializableMixin):
    tool_id: str
    name: str
    description: str = ""
    input_types: List[str] = field(default_factory=list)
    output_types: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCandidate(SerializableMixin):
    tool_id: str
    name: str
    retrieval_score: float
    intent: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRetrievalResult(SerializableMixin):
    task_id: str
    query: str
    candidates: List[ToolCandidate] = field(default_factory=list)

    _field_aliases = {
        "task_id": ["step_id"],
    }


# ---------------------------------------------------------------------------
# Tool Transition Graph Layer
# ---------------------------------------------------------------------------


@dataclass
class ToolTransitionEdge(SerializableMixin):
    source_tool_id: str
    target_tool_id: str
    edge_type: str = ""
    count: int = 0
    transition_probability: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Workflow Layer
# ---------------------------------------------------------------------------


@dataclass
class WorkflowNode(SerializableMixin):
    node_id: str
    task_id: str
    task_description: str
    tool_id: str
    tool_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    _field_aliases = {
        "node_id": ["id"],
        "task_id": ["step_id", "goal_id"],
        "task_description": ["description"],
    }

    @property
    def id(self) -> str:
        return self.node_id


@dataclass
class WorkflowEdge(SerializableMixin):
    source_node_id: str
    target_node_id: str
    edge_type: str = "tool_transition"
    metadata: Dict[str, Any] = field(default_factory=dict)

    _field_aliases = {
        "source_node_id": ["source"],
        "target_node_id": ["target"],
    }

    @property
    def source(self) -> str:
        return self.source_node_id

    @property
    def target(self) -> str:
        return self.target_node_id


@dataclass
class WorkflowDAG(SerializableMixin):
    dag_id: str = "workflow"
    nodes: List[WorkflowNode] = field(default_factory=list)
    edges: List[WorkflowEdge] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_node(self, node: WorkflowNode) -> None:
        if self.get_node(node.node_id) is None:
            self.nodes.append(node)

    def add_edge(self, edge: WorkflowEdge) -> None:
        edge_key = (edge.source_node_id, edge.target_node_id, edge.edge_type)
        existing_keys = {
            (item.source_node_id, item.target_node_id, item.edge_type)
            for item in self.edges
        }
        if edge_key not in existing_keys:
            self.edges.append(edge)

    def get_node(self, node_id: str) -> Optional[WorkflowNode]:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def node_ids(self) -> List[str]:
        return [node.node_id for node in self.nodes]

    def get_successors(self, node_id: str) -> List[WorkflowNode]:
        target_ids = [
            edge.target_node_id
            for edge in self.edges
            if edge.source_node_id == node_id
        ]
        return [node for node in self.nodes if node.node_id in target_ids]

    def get_predecessors(self, node_id: str) -> List[WorkflowNode]:
        source_ids = [
            edge.source_node_id
            for edge in self.edges
            if edge.target_node_id == node_id
        ]
        return [node for node in self.nodes if node.node_id in source_ids]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowDAG":
        if isinstance(data, cls):
            return data
        return cls(
            dag_id=str(data.get("dag_id", "workflow")),
            nodes=from_dict_list(WorkflowNode, data.get("nodes", [])),
            edges=from_dict_list(WorkflowEdge, data.get("edges", [])),
            metadata=dict(data.get("metadata", {}) or {}),
        )


# ---------------------------------------------------------------------------
# Planning Layer
# ---------------------------------------------------------------------------


@dataclass
class PredecessorCandidate(SerializableMixin):
    node_id: str
    tool_id: str
    tool_name: str
    task_id: str
    task_description: str
    transition_probability: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanningCandidate(SerializableMixin):
    task_id: str
    task_description: str
    tool_id: str
    tool_name: str
    retrieval_score: float
    predecessor_candidates: List[PredecessorCandidate] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlannerDecision(SerializableMixin):
    task_id: str
    selected_tool_id: str
    predecessor_node_ids: List[str] = field(default_factory=list)
    reason: str = ""
    raw_llm_output: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Memory Layer
# ---------------------------------------------------------------------------


@dataclass
class Artifact(SerializableMixin):
    artifact_id: str
    name: str
    artifact_type: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.artifact_id


ArtifactSpec = Artifact


@dataclass
class PlanningMemoryState(SerializableMixin):
    completed_task_ids: List[str] = field(default_factory=list)
    remaining_task_ids: List[str] = field(default_factory=list)
    selected_tool_ids: List[str] = field(default_factory=list)
    selected_action_history: List[Dict[str, Any]] = field(default_factory=list)
    workflow_dag: WorkflowDAG = field(default_factory=WorkflowDAG)
    current_step: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    _field_aliases = {
        "completed_task_ids": ["covered_goal_ids"],
        "remaining_task_ids": ["remaining_goal_ids"],
        "selected_tool_ids": ["used_tool_ids"],
        "workflow_dag": ["workflow_so_far", "workflow"],
    }

    @property
    def workflow_so_far(self) -> WorkflowDAG:
        return self.workflow_dag

    @property
    def used_tool_ids(self) -> List[str]:
        return self.selected_tool_ids

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanningMemoryState":
        if isinstance(data, cls):
            return data

        workflow_payload = (
            data.get("workflow_dag")
            or data.get("workflow_so_far")
            or data.get("workflow")
            or {}
        )

        return cls(
            completed_task_ids=list(
                data.get("completed_task_ids")
                or data.get("covered_goal_ids")
                or []
            ),
            remaining_task_ids=list(
                data.get("remaining_task_ids")
                or data.get("remaining_goal_ids")
                or []
            ),
            selected_tool_ids=list(
                data.get("selected_tool_ids")
                or data.get("used_tool_ids")
                or []
            ),
            selected_action_history=list(data.get("selected_action_history") or []),
            workflow_dag=WorkflowDAG.from_dict(workflow_payload)
            if isinstance(workflow_payload, dict)
            else workflow_payload,
            current_step=int(data.get("current_step", 0) or 0),
            metadata=dict(data.get("metadata", {}) or {}),
        )


# ---------------------------------------------------------------------------
# Verification Layer
# ---------------------------------------------------------------------------


@dataclass
class VerificationIssue(SerializableMixin):
    code: str
    message: str
    severity: str = "warning"
    node_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationReport(SerializableMixin):
    passed: bool
    issues: List[VerificationIssue] = field(default_factory=list)
    score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VerificationReport":
        if isinstance(data, cls):
            return data
        return cls(
            passed=bool(data.get("passed", False)),
            issues=from_dict_list(VerificationIssue, data.get("issues", [])),
            score=float(data.get("score", 0.0) or 0.0),
            metadata=dict(data.get("metadata", {}) or {}),
        )
