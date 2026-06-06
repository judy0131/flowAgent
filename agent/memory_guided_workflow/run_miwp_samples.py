from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

try:
    from .llm_client import OpenAICompatibleLLMClient
    from .run_miwp_case import (
        _load_or_build_tool_knowledge,
        _load_or_build_transition_graph,
        build_taskbench_prediction,
        run_case,
        save_taskbench_prediction,
    )
except ImportError:
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.run_miwp_case import (
        _load_or_build_tool_knowledge,
        _load_or_build_transition_graph,
        build_taskbench_prediction,
        run_case,
        save_taskbench_prediction,
    )


def load_samples(path: str) -> List[Dict[str, Any]]:
    """Load JSONL samples with id, type, and user_request."""
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
    output_path = Path(args.output)
    if output_path.exists() and not args.resume:
        output_path.unlink()
    trace_output_path = Path(args.trace_output) if args.trace_output else None
    if trace_output_path and trace_output_path.exists() and not args.resume:
        trace_output_path.unlink()

    completed_ids = _load_completed_ids(output_path) if args.resume else set()

    print(f"sample_file={args.sample_file}")
    print(f"sample_count={len(samples)}")
    print(f"output={args.output}")
    if trace_output_path:
        print(f"trace_output={trace_output_path}")
    if args.resume:
        print(f"resume_completed={len(completed_ids)}")

    llm_client = OpenAICompatibleLLMClient(
        llm_config_path=args.llm_config,
        llm_profile=args.llm_profile,
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

    for index, sample in enumerate(samples, start=1):
        case_id = sample["id"]
        if case_id in completed_ids:
            print(f"[{index}/{len(samples)}] skip id={case_id} type={sample['type']}")
            continue

        print(f"[{index}/{len(samples)}] run id={case_id} type={sample['type']}")
        result = run_case(
            request=sample["user_request"],
            llm_config=args.llm_config,
            llm_profile=args.llm_profile,
            tool_desc=args.tool_desc,
            tool_index=args.tool_index,
            graph_desc=args.graph_desc,
            data=args.data,
            transition_graph=args.transition_graph,
            top_k=args.top_k,
            max_repair_rounds=args.max_repair_rounds,
            llm_client=llm_client,
            tool_knowledge=tool_knowledge,
            transition_graph_obj=transition_graph_obj,
        )
        prediction = build_taskbench_prediction(
            case_id=case_id,
            user_request=sample["user_request"],
            result=result,
        )
        save_taskbench_prediction(
            path=args.output,
            prediction=prediction,
            append=True,
        )
        if trace_output_path:
            save_trace_record(
                path=str(trace_output_path),
                record={
                    "id": case_id,
                    "type": sample["type"],
                    "user_request": sample["user_request"],
                    "tasks": result.get("tasks", []),
                    "workflow_dag": result.get("workflow_dag", {}),
                    "coverage_verification": result.get("coverage_verification", {}),
                    "repair_history": result.get("repair_history", []),
                    "planning_trace": result.get("planning_trace", []),
                },
                append=True,
            )

        if args.show_trace:
            print(json.dumps(result["planning_trace"], ensure_ascii=False, indent=2))

    print("done")


def save_trace_record(path: str, record: Dict[str, Any], append: bool = True) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with output_path.open(mode, encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_completed_ids(path: Path) -> set[str]:
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


def _default_path(*parts: str) -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root.joinpath(*parts))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run MIWP on a JSONL sample file.")
    parser.add_argument(
        "--sample-file",
        default=_default_path("agent", "memory_guided_workflow", "samples", "miwp_sample_10_each.jsonl"),
        help="JSONL file with id, type, and user_request.",
    )
    parser.add_argument(
        "--output",
        default=_default_path("agent", "memory_guided_workflow", "outputs", "miwp_sample_predictions.jsonl"),
        help="TaskBench prediction JSONL output path.",
    )
    parser.add_argument(
        "--trace-output",
        default=None,
        help="Optional JSONL path for full per-case MIWP traces.",
    )
    parser.add_argument(
        "--llm-config",
        default="configs/qwen.json",
        help="LLM config used by TaskUnderstanding and IncrementalPlanner.",
    )
    parser.add_argument("--llm-profile", default=None, help="Optional LLM profile.")
    parser.add_argument(
        "--tool-desc",
        default=_default_path("taskbench", "data_multimedia", "tool_desc.json"),
        help="Path to tool_desc.json.",
    )
    parser.add_argument(
        "--tool-index",
        default=_default_path("taskbench", "data_multimedia", "tool_knowledge_index.json"),
        help="Path to prebuilt ToolKnowledge index.",
    )
    parser.add_argument(
        "--graph-desc",
        default=_default_path("taskbench", "data_multimedia", "graph_desc.json"),
        help="Path to graph_desc.json.",
    )
    parser.add_argument(
        "--data",
        default=_default_path("taskbench", "data_multimedia", "data.json"),
        help="Path to data.json.",
    )
    parser.add_argument(
        "--transition-graph",
        default=_default_path("taskbench", "data_multimedia", "tool_transition_graph.json"),
        help="Path to prebuilt ToolTransitionGraph JSON.",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Candidate tools per task.")
    parser.add_argument("--max-repair-rounds", type=int, default=1, help="Maximum coverage repair rounds.")
    parser.add_argument("--resume", action="store_true", help="Skip ids already present in output.")
    parser.add_argument("--show-trace", action="store_true", help="Print planning trace for each case.")
    args = parser.parse_args()

    run_samples(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
