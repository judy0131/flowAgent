from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .incremental_planning import IncrementalPlanner
    from .llm_client import OpenAICompatibleLLMClient
    from .models import TaskStep, UserRequest
    from .planning_memory import PlanningMemory
    from .task_understanding import TaskUnderstanding
    from .tool_knowledge import ToolKnowledge
    from .tool_transition_graph import ToolTransitionGraph
    from .workflow_coverage_verification import (
        WorkflowCoverageVerifier,
        compute_connected_components,
        _is_executable_repair_description,
    )
except ImportError:
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.incremental_planning import IncrementalPlanner
    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.models import TaskStep, UserRequest
    from agent.memory_guided_workflow.planning_memory import PlanningMemory
    from agent.memory_guided_workflow.task_understanding import TaskUnderstanding
    from agent.memory_guided_workflow.tool_knowledge import ToolKnowledge
    from agent.memory_guided_workflow.tool_transition_graph import ToolTransitionGraph
    from agent.memory_guided_workflow.workflow_coverage_verification import (
        WorkflowCoverageVerifier,
        compute_connected_components,
        _is_executable_repair_description,
    )


def run_case(
    request: str,
    llm_config: str,
    llm_profile: Optional[str],
    tool_desc: str,
    tool_index: Optional[str],
    graph_desc: str,
    data: Optional[str],
    transition_graph: Optional[str],
    top_k: int,
    max_repair_rounds: int = 1,
    llm_client: Optional[OpenAICompatibleLLMClient] = None,
    tool_knowledge: Optional[ToolKnowledge] = None,
    transition_graph_obj: Optional[ToolTransitionGraph] = None,
) -> Dict[str, Any]:
    """Run the MIWP pipeline through incremental planning."""
    if llm_client is None:
        llm_client = OpenAICompatibleLLMClient(
            llm_config_path=llm_config,
            llm_profile=llm_profile,
        )

    understanding = TaskUnderstanding(llm_client=llm_client).parse(
        UserRequest(text=request)
    )
    fallback_reason = understanding.raw_llm_output.get("fallback_reason")
    if fallback_reason:
        raise RuntimeError(f"TaskUnderstanding failed: {fallback_reason}")

    tasks = list(understanding.steps)
    memory = PlanningMemory(tasks=tasks)
    if tool_knowledge is None:
        tool_knowledge = _load_or_build_tool_knowledge(
            tool_desc=tool_desc,
            tool_index=tool_index,
        )
    if transition_graph_obj is None:
        transition_graph_obj = _load_or_build_transition_graph(
            tool_desc=tool_desc,
            graph_desc=graph_desc,
            data=data,
            transition_graph=transition_graph,
        )

    planner = IncrementalPlanner(
        tool_knowledge=tool_knowledge,
        tool_transition_graph=transition_graph_obj,
        llm_client=llm_client,
        top_k=top_k,
    )
    final_dag = planner.plan(tasks, memory)
    verifier = WorkflowCoverageVerifier(llm_client=llm_client)
    repair_history = _verify_and_repair_workflow(
        user_request=request,
        tasks=tasks,
        memory=memory,
        planner=planner,
        verifier=verifier,
        max_repair_rounds=max_repair_rounds,
    )
    final_dag = memory.get_workflow_dag()
    workflow_dag = _workflow_dag_with_taskbench_tools(final_dag.to_dict())
    coverage_verification = repair_history[-1] if repair_history else {}

    return {
        "request": request,
        "tasks": [task.to_dict() for task in tasks],
        "memory_snapshot": memory.snapshot(),
        "workflow_dag": workflow_dag,
        "planning_trace": planner.debug_history,
        "coverage_verification": coverage_verification,
        "repair_history": repair_history,
    }


def _verify_and_repair_workflow(
    user_request: str,
    tasks: List[TaskStep],
    memory: PlanningMemory,
    planner: IncrementalPlanner,
    verifier: WorkflowCoverageVerifier,
    max_repair_rounds: int,
) -> List[Dict[str, Any]]:
    repair_history: List[Dict[str, Any]] = []
    workflow = memory.get_workflow_dag().to_dict()
    component_info = compute_connected_components(workflow)

    if int(max_repair_rounds or 0) <= 0:
        verification = _build_skipped_repair_report(
            component_info=component_info,
            reason="repair_disabled",
        )
        repair_history.append(verification)
        planner.debug_history.append({"coverage_verification": verification})
        return repair_history

    if int(component_info.get("component_count", 0) or 0) <= 1:
        verification = _build_skipped_repair_report(
            component_info=component_info,
            reason="single_connected_component",
        )
        repair_history.append(verification)
        planner.debug_history.append({"coverage_verification": verification})
        return repair_history

    verification = verifier.verify(
        user_request=user_request,
        workflow=workflow,
        component_info=component_info,
    )
    verification["repair_round"] = 0
    verification.setdefault("metadata", {})["repair_trigger"] = "independent_components"
    repair_history.append(verification)
    planner.debug_history.append({"coverage_verification": verification})

    repair_limit = min(max(int(max_repair_rounds or 0), 0), 1)
    for repair_round in range(1, repair_limit + 1):
        if bool(verification.get("is_fully_covered", False)):
            break

        repair_tasks = _build_repair_tasks(
            raw_repair_tasks=verification.get("repair_tasks", []),
            existing_tasks=tasks,
        )
        if not repair_tasks:
            break

        for repair_task in repair_tasks:
            _append_repair_task(tasks, memory, repair_task)
            planner.plan_next(repair_task, memory)
            if planner.debug_history:
                planner.debug_history[-1]["repair_round"] = repair_round
                planner.debug_history[-1]["repair_task_added"] = repair_task.to_dict()

        verification = verifier.verify(
            user_request=user_request,
            workflow=memory.get_workflow_dag().to_dict(),
        )
        verification["repair_round"] = repair_round
        repair_history.append(verification)
        planner.debug_history.append({"coverage_verification": verification})

    return repair_history


def _build_skipped_repair_report(
    component_info: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    return {
        "component_count": int(component_info.get("component_count", 0) or 0),
        "components": component_info.get("components", []),
        "is_fully_covered": True,
        "missing_requirements": [],
        "repair_tasks": [],
        "repair_round": 0,
        "metadata": {"repair_skipped": reason},
    }


def _build_repair_tasks(
    raw_repair_tasks: Any,
    existing_tasks: List[TaskStep],
) -> List[TaskStep]:
    repair_tasks: List[TaskStep] = []
    for item in raw_repair_tasks if isinstance(raw_repair_tasks, list) else []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "") or "").strip()
        if not description or not _is_executable_repair_description(description):
            continue
        if _is_duplicate_repair_description(description, existing_tasks, repair_tasks):
            continue
        repair_tasks.append(
            TaskStep(
                task_id=_next_task_id(existing_tasks, repair_tasks),
                description=description,
                referenced_literals=[
                    str(value).strip()
                    for value in item.get("referenced_literals", []) or []
                    if str(value).strip()
                ],
                metadata={"repair_task": True},
            )
        )
    return repair_tasks


def _is_duplicate_repair_description(
    description: str,
    existing_tasks: List[TaskStep],
    pending_repair_tasks: List[TaskStep],
) -> bool:
    normalized = _normalize_task_description(description)
    if not normalized:
        return True
    existing_descriptions = [
        _normalize_task_description(task.description)
        for task in list(existing_tasks) + list(pending_repair_tasks)
    ]
    return normalized in existing_descriptions


def _normalize_task_description(description: str) -> str:
    text = str(description or "").strip().lower()
    return " ".join(text.rstrip(".").split())


def _append_repair_task(
    tasks: List[TaskStep],
    memory: PlanningMemory,
    repair_task: TaskStep,
) -> None:
    tasks.append(repair_task)
    memory.tasks.append(repair_task)
    if repair_task.task_id not in memory.remaining_task_ids:
        memory.remaining_task_ids.append(repair_task.task_id)


def _next_task_id(
    existing_tasks: List[TaskStep],
    pending_repair_tasks: List[TaskStep],
) -> str:
    existing_ids = {task.task_id for task in existing_tasks}
    existing_ids.update(task.task_id for task in pending_repair_tasks)
    index = len(existing_ids) + 1
    while f"t{index}" in existing_ids:
        index += 1
    return f"t{index}"


def build_taskbench_prediction(
    case_id: str,
    user_request: str,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert an MIWP run result to TaskBench prediction JSONL schema."""
    dag = result.get("workflow_dag", {})
    dag = _workflow_dag_with_taskbench_tools(dag) if isinstance(dag, dict) else {}
    nodes = dag.get("nodes", []) if isinstance(dag, dict) else []
    edges = dag.get("edges", []) if isinstance(dag, dict) else []
    node_by_id = {
        str(node.get("node_id", "")): node
        for node in nodes
        if isinstance(node, dict)
    }

    task_steps = [
        str(node.get("task_description", "") or node.get("tool_name", ""))
        for node in nodes
        if isinstance(node, dict)
    ]
    node_index_by_id = {
        str(node.get("node_id", "")): index
        for index, node in enumerate(nodes)
        if isinstance(node, dict)
    }
    incoming_refs_by_node_id: Dict[str, List[str]] = {
        str(node.get("node_id", "")): []
        for node in nodes
        if isinstance(node, dict)
    }
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source_node_id = str(edge.get("source_node_id", ""))
        target_node_id = str(edge.get("target_node_id", ""))
        if source_node_id not in node_index_by_id or target_node_id not in incoming_refs_by_node_id:
            continue
        reference = f"<node-{node_index_by_id[source_node_id]}>"
        if reference not in incoming_refs_by_node_id[target_node_id]:
            incoming_refs_by_node_id[target_node_id].append(reference)

    task_nodes = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        tool = node.get("tool", {})
        arguments = tool.get("arguments", []) if isinstance(tool, dict) else []
        task_name = tool.get("task", "") if isinstance(tool, dict) else ""
        task_nodes.append(
            {
                "task": str(task_name or node.get("tool_name", "")),
                "arguments": list(arguments) if isinstance(arguments, list) else [],
            }
        )
    task_links = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source_node = node_by_id.get(str(edge.get("source_node_id", "")))
        target_node = node_by_id.get(str(edge.get("target_node_id", "")))
        if not source_node or not target_node:
            continue
        task_links.append(
            {
                "source": str(source_node.get("tool_name", "")),
                "target": str(target_node.get("tool_name", "")),
            }
        )

    return {
        "id": str(case_id),
        "user_request": user_request,
        "result": {
            "task_steps": task_steps,
            "task_nodes": task_nodes,
            "task_links": task_links,
        },
    }


def _workflow_dag_with_taskbench_tools(dag: Dict[str, Any]) -> Dict[str, Any]:
    """Attach TaskBench-style tool objects to workflow nodes.

    Each workflow node keeps the MIWP fields, and also receives:

    {"tool": {"task": tool_name, "arguments": [...]}}

    Arguments are read from node.metadata.arguments and completed with incoming
    predecessor references from DAG edges when needed.
    """
    if not isinstance(dag, dict):
        return {}

    normalized = dict(dag)
    nodes = [
        dict(node)
        for node in normalized.get("nodes", [])
        if isinstance(node, dict)
    ]
    edges = [
        dict(edge)
        for edge in normalized.get("edges", [])
        if isinstance(edge, dict)
    ]
    node_index_by_id = {
        str(node.get("node_id", "")): index
        for index, node in enumerate(nodes)
    }
    incoming_refs_by_node_id: Dict[str, List[str]] = {
        str(node.get("node_id", "")): []
        for node in nodes
    }

    for edge in edges:
        source_node_id = str(edge.get("source_node_id", ""))
        target_node_id = str(edge.get("target_node_id", ""))
        if source_node_id not in node_index_by_id or target_node_id not in incoming_refs_by_node_id:
            continue
        reference = f"<node-{node_index_by_id[source_node_id]}>"
        if reference not in incoming_refs_by_node_id[target_node_id]:
            incoming_refs_by_node_id[target_node_id].append(reference)

    for node in nodes:
        node_id = str(node.get("node_id", ""))
        metadata = node.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        tool = node.get("tool", {})
        tool_arguments = tool.get("arguments", []) if isinstance(tool, dict) else []
        arguments = metadata.get("arguments", tool_arguments)
        if not isinstance(arguments, list):
            arguments = []
        completed_arguments = _dedupe_preserve_order(
            incoming_refs_by_node_id.get(node_id, []) + list(arguments)
        )
        metadata["arguments"] = completed_arguments
        node["metadata"] = metadata
        node["tool"] = {
            "task": str((tool.get("task") if isinstance(tool, dict) else "") or node.get("tool_name", "")),
            "arguments": completed_arguments,
        }

    normalized["nodes"] = nodes
    normalized["edges"] = edges
    return normalized


def _dedupe_preserve_order(values: List[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def save_taskbench_prediction(
    path: str,
    prediction: Dict[str, Any],
    append: bool = False,
) -> None:
    """Write one TaskBench prediction record as JSONL."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with output_path.open(mode, encoding="utf-8") as file:
        file.write(json.dumps(prediction, ensure_ascii=False) + "\n")


def _load_or_build_tool_knowledge(
    tool_desc: str,
    tool_index: Optional[str],
) -> ToolKnowledge:
    if tool_index:
        path = Path(tool_index)
        if path.exists():
            knowledge = ToolKnowledge(
                tool_desc_path=tool_desc,
                build_index_on_init=False,
            )
            knowledge.load_index(str(path))
            return knowledge

        knowledge = ToolKnowledge(tool_desc_path=tool_desc)
        path.parent.mkdir(parents=True, exist_ok=True)
        knowledge.save_index(str(path))
        return knowledge

    return ToolKnowledge(tool_desc_path=tool_desc)


def _load_or_build_transition_graph(
    tool_desc: str,
    graph_desc: str,
    data: Optional[str],
    transition_graph: Optional[str],
) -> ToolTransitionGraph:
    if transition_graph:
        path = Path(transition_graph)
        if path.exists():
            return ToolTransitionGraph.load(str(path))

    return ToolTransitionGraph(
        tool_desc_path=tool_desc,
        graph_desc_path=graph_desc,
        data_path=data,
    ).build()


def _default_path(*parts: str) -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root.joinpath(*parts))


def _print_summary(result: Dict[str, Any], show_trace: bool) -> None:
    print("TaskUnderstanding:")
    for task in result["tasks"]:
        print(f"  [{task['task_id']}] {task['description']}")

    print("\nFinal Workflow DAG:")
    dag = result["workflow_dag"]
    print(f"  nodes={len(dag.get('nodes', []))}")
    print(f"  edges={len(dag.get('edges', []))}")

    print("\nNodes:")
    for node in dag.get("nodes", []):
        tool = node.get("tool", {}) if isinstance(node, dict) else {}
        arguments = tool.get("arguments", []) if isinstance(tool, dict) else []
        print(
            f"  {node['node_id']}: "
            f"task={node['task_id']} "
            f"tool={node['tool_name']} "
            f"arguments={arguments}"
        )

    print("\nEdges:")
    if not dag.get("edges"):
        print("  <none>")
    for edge in dag.get("edges", []):
        probability = edge.get("metadata", {}).get("transition_probability", 0.0)
        print(
            f"  {edge['source_node_id']} -> {edge['target_node_id']} "
            f"type={edge['edge_type']} "
            f"p={probability}"
        )

    print("\nMemory Snapshot:")
    print(json.dumps(result["memory_snapshot"], ensure_ascii=False, indent=2))

    coverage = result.get("coverage_verification", {})
    if coverage:
        print("\nCoverage Verification:")
        print(
            f"  component_count={coverage.get('component_count')} "
            f"is_fully_covered={coverage.get('is_fully_covered')}"
        )
        missing = coverage.get("missing_requirements", [])
        repair_tasks = coverage.get("repair_tasks", [])
        if missing:
            print(f"  missing_requirements={missing}")
        if repair_tasks:
            print(f"  repair_tasks={repair_tasks}")

    if show_trace:
        print("\nPlanning Trace:")
        print(json.dumps(result["planning_trace"], ensure_ascii=False, indent=2))
        print("\nRepair History:")
        print(json.dumps(result.get("repair_history", []), ensure_ascii=False, indent=2))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run a real MIWP case end to end.")
    parser.add_argument(
        "--request",
        default="Download an image, extract the text from it, and translate the text into French.",
        help="Natural-language user request.",
    )
    parser.add_argument(
        "--case-id",
        default="manual",
        help="Case id written to TaskBench prediction output.",
    )
    parser.add_argument(
        "--llm-config",
        default="configs/qwen.json",
        help="LLM config used by TaskUnderstanding and IncrementalPlanner.",
    )
    parser.add_argument(
        "--llm-profile",
        default=None,
        help="Optional profile when the LLM config defines profiles.",
    )
    parser.add_argument(
        "--tool-desc",
        default=_default_path("taskbench", "data_multimedia", "tool_desc.json"),
        help="Path to tool_desc.json.",
    )
    parser.add_argument(
        "--tool-index",
        default=_default_path("taskbench", "data_multimedia", "tool_knowledge_index.json"),
        help="Optional saved ToolKnowledge embedding index. If missing, it is built and saved.",
    )
    parser.add_argument(
        "--graph-desc",
        default=_default_path("taskbench", "data_multimedia", "graph_desc.json"),
        help="Path to graph_desc.json.",
    )
    parser.add_argument(
        "--data",
        default=_default_path("taskbench", "data_multimedia", "data.json"),
        help="Path to data.json for transition counts.",
    )
    parser.add_argument(
        "--transition-graph",
        default=_default_path("taskbench", "data_multimedia", "tool_transition_graph.json"),
        help="Optional saved transition graph JSON. If missing, graph is rebuilt.",
    )
    parser.add_argument("--top-k", type=int, default=3, help="Candidate tools per task.")
    parser.add_argument("--max-repair-rounds", type=int, default=1, help="Maximum coverage repair rounds.")
    parser.add_argument("--show-trace", action="store_true", help="Print full planning contexts and decisions.")
    parser.add_argument("--output", default=None, help="Optional path to save TaskBench prediction JSONL.")
    parser.add_argument("--append-output", action="store_true", help="Append one JSONL record to --output.")
    args = parser.parse_args()

    result = run_case(
        request=args.request,
        llm_config=args.llm_config,
        llm_profile=args.llm_profile,
        tool_desc=args.tool_desc,
        tool_index=args.tool_index,
        graph_desc=args.graph_desc,
        data=args.data,
        transition_graph=args.transition_graph,
        top_k=args.top_k,
        max_repair_rounds=args.max_repair_rounds,
    )

    _print_summary(result, show_trace=args.show_trace)

    if args.output:
        prediction = build_taskbench_prediction(
            case_id=args.case_id,
            user_request=args.request,
            result=result,
        )
        save_taskbench_prediction(
            path=args.output,
            prediction=prediction,
            append=args.append_output,
        )
        print(f"\nsaved_prediction={args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
