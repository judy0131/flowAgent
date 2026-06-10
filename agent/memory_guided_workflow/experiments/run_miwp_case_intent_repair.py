from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent.memory_guided_workflow.experiments.export_intent_coverage import (
        build_available_intents,
        detect_intents_with_llm,
        judge_coverage_with_llm,
        load_tool_desc,
        normalize_coverage_payload,
        normalize_detected_intents,
    )
    from agent.memory_guided_workflow.incremental_planning import IncrementalPlanner
    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.models import TaskStep, UserRequest
    from agent.memory_guided_workflow.planning_memory import PlanningMemory
    from agent.memory_guided_workflow.run_miwp_case import (
        _load_or_build_tool_knowledge,
        _load_or_build_transition_graph,
        _verify_and_repair_workflow,
        _workflow_dag_with_taskbench_tools,
        build_taskbench_prediction,
        save_taskbench_prediction,
    )
    from agent.memory_guided_workflow.task_understanding import TaskUnderstanding
    from agent.memory_guided_workflow.workflow_coverage_verification import WorkflowCoverageVerifier
    from agent.memory_guided_workflow.utils import extract_json_object
except ImportError:
    import sys

    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.experiments.export_intent_coverage import (
        build_available_intents,
        detect_intents_with_llm,
        judge_coverage_with_llm,
        load_tool_desc,
        normalize_coverage_payload,
        normalize_detected_intents,
    )
    from agent.memory_guided_workflow.incremental_planning import IncrementalPlanner
    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.models import TaskStep, UserRequest
    from agent.memory_guided_workflow.planning_memory import PlanningMemory
    from agent.memory_guided_workflow.run_miwp_case import (
        _load_or_build_tool_knowledge,
        _load_or_build_transition_graph,
        _verify_and_repair_workflow,
        _workflow_dag_with_taskbench_tools,
        build_taskbench_prediction,
        save_taskbench_prediction,
    )
    from agent.memory_guided_workflow.task_understanding import TaskUnderstanding
    from agent.memory_guided_workflow.workflow_coverage_verification import WorkflowCoverageVerifier
    from agent.memory_guided_workflow.utils import extract_json_object


DEFAULT_OUTPUT_DATE = datetime.now().strftime("%Y%m%d")


class ProfileTimer:
    def __init__(self, enabled: bool = False):
        self.enabled = bool(enabled)
        self.records: List[Dict[str, Any]] = []

    def section(self, name: str) -> "_ProfileSection":
        return _ProfileSection(self, name)

    def add(self, name: str, seconds: float) -> None:
        if not self.enabled:
            return
        self.records.append({"name": name, "seconds": float(seconds)})

    def print_summary(self, prefix: str = "profile") -> None:
        if not self.enabled:
            return
        total = sum(float(record["seconds"]) for record in self.records)
        print(f"{prefix}: total={total:.3f}s")
        for record in self.records:
            print(f"{prefix}: {record['name']}={record['seconds']:.3f}s")


class _ProfileSection:
    def __init__(self, profiler: ProfileTimer, name: str):
        self.profiler = profiler
        self.name = name
        self.started_at = 0.0

    def __enter__(self) -> "_ProfileSection":
        if self.profiler.enabled:
            self.started_at = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.profiler.enabled:
            self.profiler.add(self.name, time.perf_counter() - self.started_at)


def run_case_with_intent_repair(
    request: str,
    planner_llm_config: str,
    planner_llm_profile: Optional[str],
    checker_llm_config: str,
    checker_llm_profile: Optional[str],
    tool_desc: str,
    tool_index: Optional[str],
    graph_desc: str,
    data: Optional[str],
    transition_graph: Optional[str],
    top_k: int,
    max_workflow_repair_rounds: int,
    planner_client: Optional[OpenAICompatibleLLMClient] = None,
    checker_client: Optional[OpenAICompatibleLLMClient] = None,
    tool_knowledge: Any = None,
    transition_graph_obj: Any = None,
    profiler: Optional[ProfileTimer] = None,
    warmup_embedding_model: bool = False,
) -> Dict[str, Any]:
    """Run MIWP with GPT intent coverage repair after Qwen task decomposition."""
    profiler = profiler or ProfileTimer(enabled=False)
    planner_client = planner_client or OpenAICompatibleLLMClient(
        llm_config_path=planner_llm_config,
        llm_profile=planner_llm_profile,
    )
    checker_client = checker_client or OpenAICompatibleLLMClient(
        llm_config_path=checker_llm_config,
        llm_profile=checker_llm_profile,
    )

    with profiler.section("load_intent_tools"):
        tools = load_tool_desc(tool_desc)
        available_intents = build_available_intents(tools)
        valid_intents = {
            str(item.get("intent", "")).strip().lower()
            for item in available_intents
            if str(item.get("intent", "")).strip()
        }

    with profiler.section("task_understanding"):
        understanding = TaskUnderstanding(llm_client=planner_client).parse(
            UserRequest(text=request)
        )
    fallback_reason = understanding.raw_llm_output.get("fallback_reason")
    if fallback_reason:
        raise RuntimeError(f"TaskUnderstanding failed: {fallback_reason}")

    initial_tasks = list(understanding.steps)
    initial_check = run_intent_coverage_check(
        checker_client=checker_client,
        user_request=request,
        task_steps=[task.description for task in initial_tasks],
        tools=tools,
        available_intents=available_intents,
        valid_intents=valid_intents,
        profiler=profiler,
        profile_prefix="initial_intent",
    )

    final_tasks = initial_tasks
    task_repair: Dict[str, Any] = {
        "applied": False,
        "raw_qwen_output": "",
        "repaired_tasks": [],
    }
    repaired_check: Optional[Dict[str, Any]] = None

    if bool(initial_check["normalized"].get("is_missing_intent", False)):
        with profiler.section("task_step_repair"):
            repair_payload, raw_repair = repair_task_steps_with_qwen(
                planner_client=planner_client,
                user_request=request,
                initial_tasks=initial_tasks,
                intent_check=initial_check,
            )
            repaired_tasks = normalize_repaired_tasks(
                user_request=request,
                payload=repair_payload,
                fallback_tasks=initial_tasks,
            )
        if repaired_tasks:
            final_tasks = repaired_tasks
            task_repair = {
                "applied": True,
                "raw_qwen_output": raw_repair,
                "repaired_tasks": [task.to_dict() for task in repaired_tasks],
            }
            repaired_check = run_intent_coverage_check(
                checker_client=checker_client,
                user_request=request,
                task_steps=[task.description for task in final_tasks],
                tools=tools,
                available_intents=available_intents,
                valid_intents=valid_intents,
                profiler=profiler,
                profile_prefix="repaired_intent",
            )
        else:
            task_repair = {
                "applied": False,
                "raw_qwen_output": raw_repair,
                "repaired_tasks": [],
                "error": "qwen_repair_returned_no_valid_tasks",
            }

    memory = PlanningMemory(tasks=final_tasks)
    if tool_knowledge is None:
        with profiler.section("load_tool_knowledge"):
            tool_knowledge = _load_or_build_tool_knowledge(
                tool_desc=tool_desc,
                tool_index=tool_index,
            )
    if warmup_embedding_model and hasattr(tool_knowledge, "warmup_embedding_model"):
        with profiler.section("warmup_embedding_model"):
            tool_knowledge.warmup_embedding_model()
    if transition_graph_obj is None:
        with profiler.section("load_transition_graph"):
            transition_graph_obj = _load_or_build_transition_graph(
                tool_desc=tool_desc,
                graph_desc=graph_desc,
                data=data,
                transition_graph=transition_graph,
            )

    planner = IncrementalPlanner(
        tool_knowledge=tool_knowledge,
        tool_transition_graph=transition_graph_obj,
        llm_client=planner_client,
        top_k=top_k,
    )
    with profiler.section("incremental_planning"):
        planner.plan(final_tasks, memory)

    verifier = WorkflowCoverageVerifier(llm_client=planner_client)
    with profiler.section("workflow_repair_verification"):
        repair_history = _verify_and_repair_workflow(
            user_request=request,
            tasks=final_tasks,
            memory=memory,
            planner=planner,
            verifier=verifier,
            max_repair_rounds=max_workflow_repair_rounds,
        )
    with profiler.section("export_taskbench_dag"):
        workflow_dag = _workflow_dag_with_taskbench_tools(
            memory.get_workflow_dag().to_dict()
        )
        workflow_dag = normalize_dag_argument_counts(workflow_dag)
    coverage_verification = repair_history[-1] if repair_history else {}

    return {
        "request": request,
        "initial_tasks": [task.to_dict() for task in initial_tasks],
        "tasks": [task.to_dict() for task in final_tasks],
        "intent_coverage": {
            "initial": initial_check,
            "task_repair": task_repair,
            "after_task_repair": repaired_check,
        },
        "memory_snapshot": memory.snapshot(),
        "workflow_dag": workflow_dag,
        "planning_trace": planner.debug_history,
        "coverage_verification": coverage_verification,
        "repair_history": repair_history,
    }


def run_intent_coverage_check(
    checker_client: OpenAICompatibleLLMClient,
    user_request: str,
    task_steps: List[str],
    tools: List[Dict[str, str]],
    available_intents: List[Dict[str, Any]],
    valid_intents: set[str],
    profiler: Optional[ProfileTimer] = None,
    profile_prefix: str = "intent",
) -> Dict[str, Any]:
    profiler = profiler or ProfileTimer(enabled=False)
    with profiler.section(f"{profile_prefix}.detect_intents"):
        detector_payload, raw_detector = detect_intents_with_llm(
            client=checker_client,
            user_request=user_request,
            available_tools=tools,
        )
    detected_intents = normalize_detected_intents(
        detector_payload,
        valid_intents=valid_intents,
    )
    with profiler.section(f"{profile_prefix}.judge_coverage"):
        coverage_payload, raw_coverage = judge_coverage_with_llm(
            client=checker_client,
            user_request=user_request,
            task_steps=task_steps,
            available_intents=available_intents,
            detected_intents=detected_intents,
        )
    normalized = normalize_coverage_payload(
        coverage_payload,
        detected_intents=detected_intents,
        available_intents=available_intents,
    )
    return {
        "task_steps": list(task_steps),
        "detected_intents": detected_intents,
        "normalized": normalized,
        "raw_detector_output": raw_detector,
        "raw_coverage_output": raw_coverage,
    }


def repair_task_steps_with_qwen(
    planner_client: OpenAICompatibleLLMClient,
    user_request: str,
    initial_tasks: List[TaskStep],
    intent_check: Dict[str, Any],
) -> tuple[Dict[str, Any], str]:
    prompt = build_task_step_repair_prompt(
        user_request=user_request,
        initial_tasks=initial_tasks,
        intent_check=intent_check,
    )
    raw_text = planner_client.chat(
        messages=[
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": prompt},
        ]
    )
    return extract_json_object(raw_text), raw_text


def build_task_step_repair_prompt(
    user_request: str,
    initial_tasks: List[TaskStep],
    intent_check: Dict[str, Any],
) -> str:
    tasks_json = json.dumps(
        [task.to_dict() for task in initial_tasks],
        ensure_ascii=False,
        indent=2,
    )
    missing_json = json.dumps(
        intent_check["normalized"].get("missing_intents", []),
        ensure_ascii=False,
        indent=2,
    )
    covered_json = json.dumps(
        intent_check["normalized"].get("covered_intents", []),
        ensure_ascii=False,
        indent=2,
    )
    detected_json = json.dumps(
        intent_check.get("detected_intents", []),
        ensure_ascii=False,
        indent=2,
    )
    return f"""
You are repairing task decomposition for workflow planning.

The initial task decomposition was produced by Qwen.
An intent coverage checker found missing user intents.

Your job:
Return a revised complete task decomposition that covers the user request and
the missing intents.

Rules:
- Return task_steps only; do not generate tools, tool names, workflow nodes, or DAG edges.
- One task_step maps to at most one primary intent.
- You may split an over-broad task_step into multiple atomic task_steps.
- You may append missing task_steps when the existing decomposition is otherwise usable.
- Preserve user-provided literals such as URLs, file names, quoted text, and explicit parameter values.
- Do not introduce actions that are not required by the user request or missing_intents.
- Use concise executable task descriptions.
- Return JSON only.

Output JSON schema:
{{
  "tasks": [
    {{
      "task_id": "t1",
      "description": "...",
      "referenced_literals": []
    }}
  ]
}}

user_request:
{user_request}

initial_tasks:
{tasks_json}

detected_intents:
{detected_json}

covered_intents:
{covered_json}

missing_intents:
{missing_json}
""".strip()


def normalize_repaired_tasks(
    user_request: str,
    payload: Dict[str, Any],
    fallback_tasks: List[TaskStep],
) -> List[TaskStep]:
    raw_tasks = payload.get("tasks")
    if raw_tasks is None:
        raw_tasks = payload.get("task_steps")
    tasks: List[TaskStep] = []
    seen_descriptions = set()
    for index, item in enumerate(raw_tasks if isinstance(raw_tasks, list) else []):
        if isinstance(item, dict):
            description = str(item.get("description", "") or item.get("text", "")).strip()
            referenced_literals = clean_string_list(item.get("referenced_literals", []))
        else:
            description = str(item).strip()
            referenced_literals = []
        if not description:
            continue
        key = " ".join(description.lower().rstrip(".").split())
        if key in seen_descriptions:
            continue
        seen_descriptions.add(key)
        tasks.append(
            TaskStep(
                task_id=f"t{len(tasks) + 1}",
                description=description,
                priority=float(len(tasks) + 1),
                referenced_literals=referenced_literals,
                metadata={"intent_coverage_repair": True},
            )
        )

    if not tasks:
        return []

    if not any(task.referenced_literals for task in tasks):
        request_literals = extract_request_literals(user_request)
        if len(request_literals) == 1:
            tasks[0].referenced_literals = list(request_literals)

    if is_same_task_list(tasks, fallback_tasks):
        return []

    return tasks


def normalize_dag_argument_counts(dag: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(dag) if isinstance(dag, dict) else {}
    nodes = [
        dict(node)
        for node in normalized.get("nodes", [])
        if isinstance(node, dict)
    ]
    for node in nodes:
        metadata = dict(node.get("metadata", {}) or {})
        input_types = [
            str(item).strip()
            for item in metadata.get("input_types", [])
            if str(item).strip()
        ]
        required_count = len(input_types)
        if required_count <= 0:
            node["metadata"] = metadata
            continue

        tool = dict(node.get("tool", {}) or {})
        arguments = tool.get("arguments", metadata.get("arguments", []))
        if not isinstance(arguments, list):
            arguments = []
        if len(arguments) > required_count:
            arguments = select_required_arguments(arguments, required_count)

        metadata["arguments"] = list(arguments)
        completion = metadata.get("argument_completion", {})
        if isinstance(completion, dict):
            completion = dict(completion)
            completion["final_count"] = len(arguments)
            if len(arguments) <= required_count:
                completion.pop("warning", None)
            metadata["argument_completion"] = completion
        node["metadata"] = metadata
        tool["arguments"] = list(arguments)
        node["tool"] = tool

    normalized["nodes"] = nodes
    return normalized


def select_required_arguments(arguments: List[Any], required_count: int) -> List[str]:
    cleaned = clean_string_list(arguments)
    refs = [item for item in cleaned if is_node_reference(item)]
    literals = [item for item in cleaned if not is_node_reference(item)]
    return (refs + literals)[:required_count]


def is_node_reference(value: Any) -> bool:
    return bool(re.fullmatch(r"<node-\d+>", str(value or "").strip()))


def clean_string_list(value: Any) -> List[str]:
    values = value if isinstance(value, list) else [value]
    result: List[str] = []
    seen = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def extract_request_literals(text: str) -> List[str]:
    raw_text = str(text or "")
    literals: List[str] = []
    for pattern in (r"'([^'\n]{2,})'", r'"([^"\n]{2,})"'):
        literals.extend(match.group(1).strip() for match in re.finditer(pattern, raw_text))
    literals.extend(re.findall(r"https?://[^\s,;]+", raw_text))
    literals.extend(
        re.findall(
            r"\b[\w.-]+\.(?:mp4|mov|avi|mkv|wav|mp3|flac|jpg|jpeg|png|gif|txt|pdf|csv|json|doc|docx)\b",
            raw_text,
            flags=re.IGNORECASE,
        )
    )
    return clean_string_list(literals)


def is_same_task_list(left: List[TaskStep], right: List[TaskStep]) -> bool:
    left_desc = [normalize_description(task.description) for task in left]
    right_desc = [normalize_description(task.description) for task in right]
    return left_desc == right_desc


def normalize_description(description: str) -> str:
    return " ".join(str(description or "").strip().lower().rstrip(".").split())


def load_samples(path: str) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            sample = json.loads(line)
            case_id = str(sample.get("id", "")).strip()
            user_request = str(sample.get("user_request", "")).strip()
            if not case_id or not user_request:
                raise ValueError(f"invalid sample at line {line_number}: expected id and user_request")
            samples.append(
                {
                    "id": case_id,
                    "type": str(sample.get("type", "")).strip(),
                    "user_request": user_request,
                }
            )
    return samples


def run_samples(args: argparse.Namespace) -> None:
    samples = load_samples(args.sample_file)
    case_id_filter = {
        item.strip()
        for item in str(args.case_id_filter or "").split(",")
        if item.strip()
    }
    if case_id_filter:
        samples = [
            sample
            for sample in samples
            if str(sample.get("id", "")).strip() in case_id_filter
        ]
    if args.skip_single:
        samples = [
            sample
            for sample in samples
            if str(sample.get("type", "")).strip().lower() != "single"
        ]
    if args.limit:
        samples = samples[: int(args.limit)]

    output_path = Path(args.output)
    trace_output_path = Path(args.trace_output) if args.trace_output else None
    if output_path.exists() and not args.resume:
        output_path.unlink()
    if trace_output_path and trace_output_path.exists() and not args.resume:
        trace_output_path.unlink()

    completed_ids = load_completed_ids(output_path) if args.resume else set()

    planner_client = OpenAICompatibleLLMClient(
        llm_config_path=args.planner_llm_config,
        llm_profile=args.planner_llm_profile,
    )
    checker_client = OpenAICompatibleLLMClient(
        llm_config_path=args.checker_llm_config,
        llm_profile=args.checker_llm_profile,
    )
    tool_knowledge = _load_or_build_tool_knowledge(
        tool_desc=args.tool_desc,
        tool_index=args.tool_index,
    )
    if hasattr(tool_knowledge, "warmup_embedding_model"):
        print("warming_up_embedding_model=1")
        tool_knowledge.warmup_embedding_model()
    transition_graph_obj = _load_or_build_transition_graph(
        tool_desc=args.tool_desc,
        graph_desc=args.graph_desc,
        data=args.data,
        transition_graph=args.transition_graph,
    )

    print(f"sample_file={args.sample_file}")
    print(f"sample_count={len(samples)}")
    print(f"planner_llm_config={args.planner_llm_config}")
    print(f"checker_llm_config={args.checker_llm_config}")
    print(f"output={output_path}")
    if trace_output_path:
        print(f"trace_output={trace_output_path}")

    for index, sample in enumerate(samples, start=1):
        case_id = sample["id"]
        if case_id in completed_ids:
            print(f"[{index}/{len(samples)}] skip id={case_id} type={sample['type']}")
            continue

        print(f"[{index}/{len(samples)}] run id={case_id} type={sample['type']}")
        case_profiler = ProfileTimer(enabled=bool(args.profile))
        result = run_case_with_intent_repair(
            request=sample["user_request"],
            planner_llm_config=args.planner_llm_config,
            planner_llm_profile=args.planner_llm_profile,
            checker_llm_config=args.checker_llm_config,
            checker_llm_profile=args.checker_llm_profile,
            tool_desc=args.tool_desc,
            tool_index=args.tool_index,
            graph_desc=args.graph_desc,
            data=args.data,
            transition_graph=args.transition_graph,
            top_k=args.top_k,
            max_workflow_repair_rounds=args.max_workflow_repair_rounds,
            planner_client=planner_client,
            checker_client=checker_client,
            tool_knowledge=tool_knowledge,
            transition_graph_obj=transition_graph_obj,
            profiler=case_profiler,
        )
        case_profiler.print_summary(prefix=f"profile id={case_id}")
        prediction = build_taskbench_prediction(
            case_id=case_id,
            user_request=sample["user_request"],
            result=result,
        )
        save_taskbench_prediction(
            path=str(output_path),
            prediction=prediction,
            append=True,
        )
        if trace_output_path:
            append_jsonl(
                trace_output_path,
                {
                    "id": case_id,
                    "type": sample["type"],
                    "user_request": sample["user_request"],
                    **result,
                },
            )

    print("done")


def load_completed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed = set()
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            case_id = str(row.get("id", "")).strip()
            if case_id:
                completed.add(case_id)
    return completed


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_case_summary(result: Dict[str, Any]) -> None:
    initial = result["intent_coverage"]["initial"]["normalized"]
    after = result["intent_coverage"].get("after_task_repair")
    print("Initial tasks:")
    for task in result.get("initial_tasks", []):
        print(f"  [{task['task_id']}] {task['description']}")
    print(
        "\nInitial intent coverage: "
        f"is_missing_intent={initial.get('is_missing_intent')}"
    )
    if initial.get("missing_intents"):
        print("  missing_intents=" + json.dumps(initial["missing_intents"], ensure_ascii=False))
    if result["intent_coverage"]["task_repair"].get("applied"):
        print("\nRepaired tasks:")
        for task in result.get("tasks", []):
            print(f"  [{task['task_id']}] {task['description']}")
    if after:
        normalized = after["normalized"]
        print(
            "\nAfter repair intent coverage: "
            f"is_missing_intent={normalized.get('is_missing_intent')}"
        )
        if normalized.get("missing_intents"):
            print("  missing_intents=" + json.dumps(normalized["missing_intents"], ensure_ascii=False))

    dag = result.get("workflow_dag", {})
    print(f"\nFinal Workflow DAG: nodes={len(dag.get('nodes', []))} edges={len(dag.get('edges', []))}")


def default_path(*parts: str) -> str:
    repo_root = Path(__file__).resolve().parents[3]
    return str(repo_root.joinpath(*parts))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run MIWP with Qwen task planning and GPT intent coverage repair "
            "immediately after task decomposition."
        )
    )
    parser.add_argument("--request", default=None, help="Single natural-language request.")
    parser.add_argument("--case-id", default="manual", help="Case id for single-request output.")
    parser.add_argument("--sample-file", default=None, help="Optional JSONL sample file.")
    parser.add_argument(
        "--output",
        default=default_path(
            "agent",
            "memory_guided_workflow",
            "outputs",
            f"miwp_intent_repair_predictions_{DEFAULT_OUTPUT_DATE}.jsonl",
        ),
        help="TaskBench prediction JSONL output.",
    )
    parser.add_argument(
        "--trace-output",
        default=default_path(
            "agent",
            "memory_guided_workflow",
            "outputs",
            f"miwp_intent_repair_traces_{DEFAULT_OUTPUT_DATE}.jsonl",
        ),
        help="Full experiment trace JSONL output.",
    )
    parser.add_argument("--planner-llm-config", default="configs/qwen.json")
    parser.add_argument("--planner-llm-profile", default=None)
    parser.add_argument("--checker-llm-config", default="configs/openai.json")
    parser.add_argument("--checker-llm-profile", default=None)
    parser.add_argument(
        "--tool-desc",
        default=default_path("taskbench", "data_multimedia", "tool_desc.json"),
    )
    parser.add_argument(
        "--tool-index",
        default=default_path("taskbench", "data_multimedia", "tool_knowledge_index.json"),
    )
    parser.add_argument(
        "--graph-desc",
        default=default_path("taskbench", "data_multimedia", "graph_desc.json"),
    )
    parser.add_argument(
        "--data",
        default=default_path("taskbench", "data_multimedia", "data.json"),
    )
    parser.add_argument(
        "--transition-graph",
        default=default_path("taskbench", "data_multimedia", "tool_transition_graph.json"),
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--max-workflow-repair-rounds",
        type=int,
        default=0,
        help="Existing post-DAG workflow repair rounds. Default 0 isolates intent repair.",
    )
    parser.add_argument("--skip-single", action="store_true", help="Skip samples with type=single.")
    parser.add_argument("--limit", type=int, default=0, help="Run at most this many samples.")
    parser.add_argument(
        "--case-id-filter",
        default="",
        help="Comma-separated sample ids to run when --sample-file is used.",
    )
    parser.add_argument("--resume", action="store_true", help="Skip ids already present in --output.")
    parser.add_argument("--profile", action="store_true", help="Print per-stage elapsed time.")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.sample_file:
        run_samples(args)
        return 0

    request = args.request or "Download an image, extract the text from it, and translate the text into French."
    case_id = str(args.case_id).strip() or "manual"
    profiler = ProfileTimer(enabled=bool(args.profile))
    result = run_case_with_intent_repair(
        request=request,
        planner_llm_config=args.planner_llm_config,
        planner_llm_profile=args.planner_llm_profile,
        checker_llm_config=args.checker_llm_config,
        checker_llm_profile=args.checker_llm_profile,
        tool_desc=args.tool_desc,
        tool_index=args.tool_index,
        graph_desc=args.graph_desc,
        data=args.data,
        transition_graph=args.transition_graph,
        top_k=args.top_k,
        max_workflow_repair_rounds=args.max_workflow_repair_rounds,
        profiler=profiler,
        warmup_embedding_model=True,
    )
    profiler.print_summary(prefix=f"profile id={case_id}")
    print_case_summary(result)

    prediction = build_taskbench_prediction(
        case_id=case_id,
        user_request=request,
        result=result,
    )
    save_taskbench_prediction(
        path=args.output,
        prediction=prediction,
        append=True,
    )
    if args.trace_output:
        append_jsonl(
            Path(args.trace_output),
            {
                "id": case_id,
                "type": "manual",
                "user_request": request,
                **result,
            },
        )
    print(f"\nsaved_prediction={args.output}")
    if args.trace_output:
        print(f"saved_trace={args.trace_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
