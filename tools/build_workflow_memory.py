import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence, Union


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_INPUT_PATH = REPO_ROOT / "taskbench" / "data_multimedia" / "data.json"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "taskbench" / "data_multimedia" / "workflow_memory_fold0_excluded.json"
DEFAULT_SOURCE_NAME = "taskbench"
DEFAULT_MAX_MOTIF_SIZE = 4
DEFAULT_NUM_FOLDS = 5
DEFAULT_FOLD_INDEX = 0
DEFAULT_FOLD_MODE = "exclude"
DEFAULT_TRUSTED_MIN_SUPPORT = 3

from agent.pipeline_orchestrator.workflow_memory import (
    WorkflowMemoryIndex,
    load_case_id_file,
    select_taskbench_records,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build workflow memory from TaskBench-style records.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT_PATH), help="Path to TaskBench JSONL/JSON data file.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Where to write workflow memory JSON.")
    parser.add_argument("--source-name", default=DEFAULT_SOURCE_NAME, help="Source label stored in the memory file.")
    parser.add_argument("--max-motif-size", type=int, default=DEFAULT_MAX_MOTIF_SIZE, help="Maximum workflow path motif size.")
    parser.add_argument("--include-ids-file", help="Optional newline-delimited case-id allowlist.")
    parser.add_argument("--exclude-ids-file", help="Optional newline-delimited case-id denylist.")
    parser.add_argument("--num-folds", type=int, default=DEFAULT_NUM_FOLDS, help="Optional deterministic fold count for leakage-controlled builds.")
    parser.add_argument("--fold-index", type=int, default=DEFAULT_FOLD_INDEX, help="Which deterministic fold to include or exclude.")
    parser.add_argument(
        "--trusted-min-support",
        type=int,
        default=DEFAULT_TRUSTED_MIN_SUPPORT,
        help="Minimum motif support kept in trusted prior tables.",
    )
    parser.add_argument(
        "--fold-mode",
        choices=("exclude", "include"),
        default=DEFAULT_FOLD_MODE,
        help="Whether the selected fold is excluded from memory (default) or included.",
    )
    return parser


def build_workflow_memory(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    *,
    source_name: str = "taskbench",
    max_motif_size: int = 4,
    include_ids_file: Optional[Union[str, Path]] = None,
    exclude_ids_file: Optional[Union[str, Path]] = None,
    num_folds: Optional[int] = None,
    fold_index: Optional[int] = None,
    fold_mode: str = "exclude",
    trusted_min_support: int = DEFAULT_TRUSTED_MIN_SUPPORT,
) -> WorkflowMemoryIndex:
    input_path = Path(input_path)
    output_path = Path(output_path)
    records = WorkflowMemoryIndex.load_taskbench_records(input_path)

    include_ids = load_case_id_file(Path(include_ids_file)) if include_ids_file else None
    exclude_ids = load_case_id_file(Path(exclude_ids_file)) if exclude_ids_file else None
    selected_records = select_taskbench_records(
        records,
        include_ids=include_ids,
        exclude_ids=exclude_ids,
        num_folds=num_folds,
        fold_index=fold_index,
        fold_mode=fold_mode,
    )
    if not selected_records:
        raise ValueError("No records selected for workflow memory build.")

    memory = WorkflowMemoryIndex.build_from_taskbench_records(
        selected_records,
        source_name=source_name,
        max_motif_size=max(2, int(max_motif_size)),
        trusted_min_support=max(1, int(trusted_min_support)),
    )
    memory.to_json(output_path)

    selection_bits = [f"selected={len(selected_records)}/{len(records)}"]
    if include_ids is not None:
        selection_bits.append(f"include_ids={len(include_ids)}")
    if exclude_ids is not None:
        selection_bits.append(f"exclude_ids={len(exclude_ids)}")
    if num_folds is not None:
        selection_bits.append(f"fold={fold_index}/{num_folds} mode={fold_mode}")
    print(
        f"Built workflow memory: motifs={len(memory.motifs)}, "
        f"transitions={len(memory.transition_counts)}, "
        f"trusted_motifs={len(memory.motif_prior)}, "
        f"trusted_transitions={len(memory.transition_prior)}, "
        f"trusted_starts={len(memory.start_prior)}; {'; '.join(selection_bits)}"
    )
    return memory


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    build_workflow_memory(
        input_path=args.input,
        output_path=args.output,
        source_name=args.source_name,
        max_motif_size=args.max_motif_size,
        include_ids_file=args.include_ids_file,
        exclude_ids_file=args.exclude_ids_file,
        num_folds=args.num_folds,
        fold_index=args.fold_index,
        fold_mode=args.fold_mode,
        trusted_min_support=args.trusted_min_support,
    )


if __name__ == "__main__":
    main()
