import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.pipeline_orchestrator.workflow_memory import (
    WorkflowMemoryIndex,
    load_case_id_file,
    select_taskbench_records,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build workflow memory from TaskBench-style records.")
    parser.add_argument("--input", required=True, help="Path to TaskBench JSONL/JSON data file.")
    parser.add_argument("--output", required=True, help="Where to write workflow memory JSON.")
    parser.add_argument("--source-name", default="taskbench", help="Source label stored in the memory file.")
    parser.add_argument("--max-motif-size", type=int, default=4, help="Maximum workflow path motif size.")
    parser.add_argument("--include-ids-file", help="Optional newline-delimited case-id allowlist.")
    parser.add_argument("--exclude-ids-file", help="Optional newline-delimited case-id denylist.")
    parser.add_argument("--num-folds", type=int, help="Optional deterministic fold count for leakage-controlled builds.")
    parser.add_argument("--fold-index", type=int, help="Which deterministic fold to include or exclude.")
    parser.add_argument(
        "--fold-mode",
        choices=("exclude", "include"),
        default="exclude",
        help="Whether the selected fold is excluded from memory (default) or included.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    records = WorkflowMemoryIndex.load_taskbench_records(input_path)

    include_ids = load_case_id_file(Path(args.include_ids_file)) if args.include_ids_file else None
    exclude_ids = load_case_id_file(Path(args.exclude_ids_file)) if args.exclude_ids_file else None
    selected_records = select_taskbench_records(
        records,
        include_ids=include_ids,
        exclude_ids=exclude_ids,
        num_folds=args.num_folds,
        fold_index=args.fold_index,
        fold_mode=args.fold_mode,
    )
    if not selected_records:
        raise ValueError("No records selected for workflow memory build.")

    memory = WorkflowMemoryIndex.build_from_taskbench_records(
        selected_records,
        source_name=args.source_name,
        max_motif_size=max(2, int(args.max_motif_size)),
    )
    memory.to_json(Path(args.output))

    selection_bits = [f"selected={len(selected_records)}/{len(records)}"]
    if include_ids is not None:
        selection_bits.append(f"include_ids={len(include_ids)}")
    if exclude_ids is not None:
        selection_bits.append(f"exclude_ids={len(exclude_ids)}")
    if args.num_folds is not None:
        selection_bits.append(f"fold={args.fold_index}/{args.num_folds} mode={args.fold_mode}")
    print(
        f"Built workflow memory: motifs={len(memory.motifs)}, "
        f"transitions={len(memory.transition_counts)}; {'; '.join(selection_bits)}"
    )


if __name__ == "__main__":
    main()
