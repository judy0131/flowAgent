from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_guided_workflow.experiments.run_miwp_case_intent_repair import (
    ProfileTimer,
    append_jsonl,
    default_path,
    load_completed_ids,
    load_samples,
    run_case_with_intent_repair,
)
from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.run_miwp_case import (
    _load_or_build_tool_knowledge,
    _load_or_build_transition_graph,
    build_taskbench_prediction,
    save_taskbench_prediction,
)


DEFAULT_OUTPUT_DATE = datetime.now().strftime("%Y%m%d")


class SynchronizedToolKnowledge:
    """Serialize embedding retrieval while sharing the warmed tool index."""

    def __init__(self, tool_knowledge: Any):
        self._tool_knowledge = tool_knowledge
        self._retrieve_lock = threading.Lock()

    def retrieve_tools(self, *args: Any, **kwargs: Any) -> Any:
        with self._retrieve_lock:
            return self._tool_knowledge.retrieve_tools(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool_knowledge, name)


def run_one_sample(
    sample: Dict[str, Any],
    args: argparse.Namespace,
    tool_knowledge: Any,
    transition_graph_obj: Any,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    case_id = str(sample["id"]).strip()
    profiler = ProfileTimer(enabled=bool(args.profile))
    planner_client = OpenAICompatibleLLMClient(
        llm_config_path=args.planner_llm_config,
        llm_profile=args.planner_llm_profile,
    )
    checker_client = OpenAICompatibleLLMClient(
        llm_config_path=args.checker_llm_config,
        llm_profile=args.checker_llm_profile,
    )
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
        profiler=profiler,
    )
    prediction = build_taskbench_prediction(
        case_id=case_id,
        user_request=sample["user_request"],
        result=result,
    )
    trace = {
        "id": case_id,
        "type": sample.get("type", ""),
        "user_request": sample["user_request"],
        **result,
    }
    return {
        "id": case_id,
        "type": sample.get("type", ""),
        "prediction": prediction,
        "trace": trace,
        "profile_records": list(profiler.records),
        "elapsed_seconds": time.perf_counter() - started_at,
    }


def run_samples(args: argparse.Namespace) -> None:
    loaded_samples = load_samples(args.sample_file)
    selected_samples = select_samples(loaded_samples, args)

    output_path = Path(args.output)
    trace_output_path = Path(args.trace_output) if args.trace_output else None
    error_output_path = Path(args.error_output) if args.error_output else None
    completed_ids = load_completed_ids(output_path) if args.resume else set()
    pending_samples = [
        sample
        for sample in selected_samples
        if str(sample["id"]).strip() not in completed_ids
    ]

    print(f"sample_file={args.sample_file}")
    print(f"loaded_sample_count={len(loaded_samples)}")
    print(f"selected_sample_count={len(selected_samples)}")
    print(f"pending_sample_count={len(pending_samples)}")
    print(f"workers={args.workers}")
    print(f"planner_llm_config={args.planner_llm_config}")
    print(f"checker_llm_config={args.checker_llm_config}")
    print(f"output={output_path}")
    if trace_output_path:
        print(f"trace_output={trace_output_path}")
    if error_output_path:
        print(f"error_output={error_output_path}")
    if args.dry_run:
        print("dry_run=1")
        print("done")
        return

    if output_path.exists() and not args.resume:
        output_path.unlink()
    if trace_output_path and trace_output_path.exists() and not args.resume:
        trace_output_path.unlink()
    if error_output_path and error_output_path.exists() and not args.resume:
        error_output_path.unlink()

    if not pending_samples:
        print("done")
        return

    tool_knowledge = _load_or_build_tool_knowledge(
        tool_desc=args.tool_desc,
        tool_index=args.tool_index,
    )
    if not args.no_warmup and hasattr(tool_knowledge, "warmup_embedding_model"):
        print("warming_up_embedding_model=1")
        tool_knowledge.warmup_embedding_model()
    synchronized_tool_knowledge = SynchronizedToolKnowledge(tool_knowledge)

    transition_graph_obj = _load_or_build_transition_graph(
        tool_desc=args.tool_desc,
        graph_desc=args.graph_desc,
        data=args.data,
        transition_graph=args.transition_graph,
    )

    started_at = time.perf_counter()
    completed_count = 0
    failed_count = 0
    max_workers = max(int(args.workers or 1), 1)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_sample = {
            executor.submit(
                run_one_sample,
                sample,
                args,
                synchronized_tool_knowledge,
                transition_graph_obj,
            ): sample
            for sample in pending_samples
        }
        for future in as_completed(future_to_sample):
            sample = future_to_sample[future]
            case_id = str(sample["id"]).strip()
            try:
                record = future.result()
            except Exception as exc:
                failed_count += 1
                completed_count += 1
                print(
                    f"[{completed_count}/{len(pending_samples)}] "
                    f"failed id={case_id} type={sample.get('type', '')} error={exc}"
                )
                if error_output_path:
                    append_jsonl(
                        error_output_path,
                        {
                            "id": case_id,
                            "type": sample.get("type", ""),
                            "user_request": sample.get("user_request", ""),
                            "error": str(exc),
                        },
                    )
                if args.fail_fast:
                    raise
                continue

            save_taskbench_prediction(
                path=str(output_path),
                prediction=record["prediction"],
                append=True,
            )
            if trace_output_path:
                append_jsonl(trace_output_path, record["trace"])
            completed_count += 1
            print(
                f"[{completed_count}/{len(pending_samples)}] "
                f"done id={record['id']} type={record['type']} "
                f"elapsed={record['elapsed_seconds']:.3f}s"
            )
            if args.profile:
                print_profile_records(
                    prefix=f"profile id={record['id']}",
                    records=record["profile_records"],
                )

    elapsed = time.perf_counter() - started_at
    print(
        f"done completed={completed_count - failed_count} "
        f"failed={failed_count} elapsed={elapsed:.3f}s"
    )


def select_samples(samples: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    selected = list(samples)
    case_id_filter = {
        item.strip()
        for item in str(args.case_id_filter or "").split(",")
        if item.strip()
    }
    if case_id_filter:
        selected = [
            sample
            for sample in selected
            if str(sample.get("id", "")).strip() in case_id_filter
        ]
    if args.skip_single:
        selected = [
            sample
            for sample in selected
            if str(sample.get("type", "")).strip().lower() != "single"
        ]
    if args.limit:
        selected = selected[: int(args.limit)]
    return selected


def print_profile_records(prefix: str, records: List[Dict[str, Any]]) -> None:
    total = sum(float(record.get("seconds", 0.0) or 0.0) for record in records)
    print(f"{prefix}: total={total:.3f}s")
    for record in records:
        print(f"{prefix}: {record.get('name')}={float(record.get('seconds', 0.0) or 0.0):.3f}s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run MIWP intent-repair samples with a thread pool."
    )
    parser.add_argument(
        "--sample-file",
        default=default_path(
            "agent",
            "memory_guided_workflow",
            "samples",
            "miwp_sample_10_each.jsonl",
        ),
        help="JSONL sample file with id, type, and user_request.",
    )
    parser.add_argument(
        "--output",
        default=default_path(
            "agent",
            "memory_guided_workflow",
            "outputs",
            f"miwp_samples_intent_repair_predictions_{DEFAULT_OUTPUT_DATE}.jsonl",
        ),
        help="TaskBench prediction JSONL output.",
    )
    parser.add_argument(
        "--trace-output",
        default=default_path(
            "agent",
            "memory_guided_workflow",
            "outputs",
            f"miwp_samples_intent_repair_traces_{DEFAULT_OUTPUT_DATE}.jsonl",
        ),
        help="Full experiment trace JSONL output.",
    )
    parser.add_argument(
        "--error-output",
        default=default_path(
            "agent",
            "memory_guided_workflow",
            "outputs",
            f"miwp_samples_intent_repair_errors_{DEFAULT_OUTPUT_DATE}.jsonl",
        ),
        help="Failed case JSONL output.",
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
    parser.add_argument("--workers", type=int, default=4, help="Concurrent case workers.")
    parser.add_argument("--skip-single", action="store_true", help="Skip samples with type=single.")
    parser.add_argument("--limit", type=int, default=0, help="Run at most this many selected samples.")
    parser.add_argument(
        "--case-id-filter",
        default="",
        help="Comma-separated sample ids to run.",
    )
    parser.add_argument("--resume", action="store_true", help="Skip ids already present in --output.")
    parser.add_argument("--profile", action="store_true", help="Print per-case stage elapsed time.")
    parser.add_argument("--no-warmup", action="store_true", help="Skip embedding model warmup.")
    parser.add_argument("--fail-fast", action="store_true", help="Raise on the first failed case.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected counts without running cases.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_samples(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
